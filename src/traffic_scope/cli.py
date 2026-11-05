"""CLI 入口与全流程编排。

数据源：百度（map.baidu.com）或高德（www.amap.com）网页端路况瓦片
（渲染同源，均无需 AK），由 traffic.provider 选择。

流水线：配置 → 查询范围（admin/file/bbox）→ 瓦片切分 → 批量采集
（限速/重试/断点续跑）→ 拼接镶嵌 → 范围裁剪 →
GIS/栅格导出 → 交互地图 → 元数据。

run_pipeline 的 fetch_fn 可注入 fake 实现，使全流程无需网络即可测试。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import yaml

from .config import AUTO_LEVEL, DEFAULT_LEVEL, MAX_LEVEL, MIN_LEVEL, \
    TrafficConfig, auto_level, config_snapshot, load_config
from .metadata import RunContext
from .tiles import (PROVIDERS, assemble_mosaic, assemble_raw_mosaic,
                    classify_tile, clip_rgba_to_polygon, clip_to_polygon,
                    fetch_tile_png, tile_key, tile_polygon, tiles_for_bbox)


# ---------------------------------------------------------------------- 通用

DEFAULT_CONFIG = "configs/config.yaml"
KEYS_FILE = "configs/keys.txt"


def _get_key(name: str) -> str:
    """从 configs/keys.txt 读取指定 key（可选，缺失返回空串）。"""
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == name:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get_tianditu_tk() -> str:
    """从 configs/keys.txt 读取天地图 tk。"""
    return _get_key("tianditu_tk")


def _get_carto_key() -> str:
    """从 configs/keys.txt 读取 CartoDB 底图 key。"""
    return _get_key("carto")


def _load_config(path: str) -> TrafficConfig:
    """加载配置；默认配置缺失时给出复制模版的引导。"""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"配置文件不存在：{path}\n"
            f"请先复制模版：cp configs/example.yaml {path}")
    return load_config(path)


def build_scope(cfg: TrafficConfig):
    """按配置来源构建查询范围（WGS84）。"""
    from .scope.admin import fetch_admin_boundary
    from .scope.bbox import bbox_area
    from .scope.fileio import load_vector_area

    sa = cfg.study_area
    stype = sa["type"]
    if stype == "file":
        return load_vector_area(sa["path"], layer=sa.get("layer"))
    if stype == "admin":
        return fetch_admin_boundary(sa["adcode"], name=sa.get("name", ""))
    if stype == "bbox":
        return bbox_area(sa["bbox"], name=sa.get("name", ""),
                         crs=sa.get("crs", "wgs84"))
    raise ValueError(f"未知 study_area.type: {stype!r}")


def _resolve_level(cfg: TrafficConfig, scope, log=print) -> int:
    """把 level（整数或 'auto'）解析为实际瓦片级别。

    'auto' 时按范围面积自适应（实测 L17 是密度/请求量最优档）：
    ≤25 km²→17、25–200 km²→16、>200 km²→15。
    """
    level = cfg.collect.level
    if level == AUTO_LEVEL:
        area = scope.area_km2()
        provider = cfg.collect.provider
        level = auto_level(area, provider)
        log(f"level=auto：范围面积约 {area:.1f} km²"
            f"（provider={provider}）→ 推荐 level={level}")
    return int(level)


def _slug_for(scope, provider: str = "baidu") -> str:
    """run 目录后缀：范围来源方式 + 数据源，如 bbox_baidu / admin_amap。"""
    return f"{scope.source}_{provider}"


def _progress_bar(total: int, desc: str):
    try:
        from tqdm import tqdm
        bar = tqdm(total=total, desc=desc, unit="瓦片")

        def update(done: int):
            bar.n = done
            bar.refresh()

        def close():
            bar.close()
        return update, close
    except ImportError:
        def update(done: int):
            print(f"  进度: {done}/{total} 瓦片")

        def close():
            pass
        return update, close


# ---------------------------------------------------------------------- 采集

def make_tiles_for_scope(polygon, level: int, provider: str = "baidu") \
        -> list[tuple[str, object]]:
    """范围 → 路况瓦片 [(key, WGS84 多边形)]。"""
    return [(tile_key(level, tx, ty), tile_polygon(tx, ty, level, provider))
            for tx, ty in tiles_for_bbox(polygon.bounds, level, provider)]


def _collect_all(tile_pairs: list, level: int, provider: str, qps: float,
                 max_retries: int, ctx: RunContext, fetch_fn=None,
                 log=print) -> tuple[dict[str, tuple[bytes, str]], list[str]]:
    """采集全部瓦片（顺序 + qps 限速），逐瓦片存档原始 PNG。

    返回 (tid → (png_bytes, sampled_at)，失败瓦片 id 列表)。
    已完成瓦片（progress.csv 中 status=ok）直接读 raw 存档恢复，不重复请求。
    """
    results: dict[str, tuple[bytes, str]] = {}
    failed: list[str] = []
    done = ctx.completed_tiles()
    pending = [k for k, _ in tile_pairs if k not in done]

    for key, _poly in tile_pairs:
        if key in done:
            env = ctx.read_tile_raw(key)
            if env is not None:
                results[key] = env
    if done:
        log(f"  断点续跑：{len(done)} 个瓦片已完成（读存档），剩余 {len(pending)} 个")

    if fetch_fn is None:
        def fetch_fn(key: str) -> bytes:
            _lv, tx, ty = (int(v) for v in key.split("_"))
            return fetch_tile_png(tx, ty, _lv, provider=provider,
                                  max_retries=max_retries)

    interval = 1.0 / qps if qps > 0 else 0.0
    update, close_bar = _progress_bar(len(tile_pairs), "采集路况")
    update(len(results))
    t_last = 0.0
    for key, _poly in tile_pairs:
        if key in results:
            continue
        wait = interval - (time.monotonic() - t_last)
        if wait > 0:
            time.sleep(wait)
        t_last = time.monotonic()
        sampled_at = datetime.now().isoformat(timespec="seconds")
        try:
            data = fetch_fn(key)
            ctx.write_tile_raw(key, sampled_at, data)
            ctx.record_tile(key, "ok", sampled_at)
            results[key] = (data, sampled_at)
        except Exception as e:
            log(f"  瓦片 {key} 采集失败（已记录，可 --resume 重试）: {e}")
            ctx.record_tile(key, "failed", "", str(e)[:200])
            failed.append(key)
        update(len(results) + len(failed))
    close_bar()
    return results, failed


def _classify_all(tile_pairs: list, level: int, provider: str,
                  ctx: RunContext) -> dict:
    """从 raw 存档读取并分类全部瓦片为分级矩阵（run 与 render 共用）。"""
    classified: dict = {}
    for key, _poly in tile_pairs:
        env = ctx.read_tile_raw(key)
        if env is None:
            continue
        data, _sampled = env
        _lv, tx, ty = (int(v) for v in key.split("_"))
        classified[(tx, ty)] = classify_tile(data, provider)
    return classified


def _assemble_raw(tile_pairs: list, level: int, provider: str, ctx: RunContext):
    """从 raw 存档读取原始 PNG 并拼成 RGBA 大图（离线，零请求）。

    返回 (rgba, mosaic_raw)，与 assemble_raw_mosaic 相同；缺失瓦片跳过。
    """
    raw_pngs: dict = {}
    for key, _poly in tile_pairs:
        env = ctx.read_tile_raw(key)
        if env is None:
            continue
        data, _sampled = env
        _lv, tx, ty = (int(v) for v in key.split("_"))
        raw_pngs[(tx, ty)] = data
    return assemble_raw_mosaic(level, raw_pngs, provider)


# ---------------------------------------------------------------------- 主流程

def run_pipeline(cfg: TrafficConfig, fetch_fn=None, resume: str | None = None,
                 log=print) -> str:
    """run 子命令主流程。返回 run 目录路径。"""
    from . import export as exp

    scope = build_scope(cfg)
    provider = cfg.collect.provider
    level = _resolve_level(cfg, scope, log=log)
    cfg.collect.level = level  # 回写实际值，快照可复现（render 需整数 level）
    log(f"查询范围: [{scope.source}] {scope.name}，"
        f"外接对角线 {scope.diagonal_km():.2f} km")

    tile_pairs = make_tiles_for_scope(scope.polygon, level, provider)
    from .tiles import resolution
    res = resolution(level, provider)
    log(f"路况瓦片切分: {len(tile_pairs)} 个（provider={provider}，level={level}，"
        f"约 {res:.0f} m/px）")

    ctx = RunContext(cfg.output.dir, slug=_slug_for(scope, provider), resume=resume)
    log(f"运行目录: {ctx.dir}")

    with open(ctx.config_snapshot, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_snapshot(cfg), f, allow_unicode=True, sort_keys=False)

    # —— 采集 ——
    results, failed = _collect_all(tile_pairs, level, provider, cfg.collect.qps,
                                   cfg.collect.max_retries, ctx,
                                   fetch_fn=fetch_fn, log=log)
    if failed:
        log(f"注意：{len(failed)} 个瓦片失败，成果为部分数据（--resume 可补采）")
    if not results:
        raise RuntimeError("没有任何瓦片采集成功，无法产出成果。")

    # —— 拼接 + 裁剪 ——
    classified = _classify_all(tile_pairs, level, provider, ctx)
    mosaic = assemble_mosaic(level, classified, provider)
    if cfg.collect.clip:
        clip_to_polygon(mosaic, scope.polygon)
    px_nz = int((mosaic.status > 0).sum())
    log(f"拼接镶嵌 {mosaic.shape[1]}×{mosaic.shape[0]} px，"
        f"范围内路况像素 {px_nz} 个")
    if px_nz == 0:
        raise RuntimeError("范围内没有任何路况像素：请检查范围是否位于"
                           f"{PROVIDERS[provider]['label']}地图覆盖区，"
                           "或稍后重试（服务端偶发空瓦片）。")

    # —— 摘要 ——
    # 像素级状态统计
    px_by_status = {}
    for code in (1, 2, 3, 4):
        n = int((mosaic.status == code).sum())
        if n:
            from .tiles import status_name
            px_by_status[status_name(code)] = n
    sampled_ats = [sa for _data, sa in results.values()]
    summary = {
        "provider": provider,
        "source": f"{PROVIDERS[provider]['label']}地图网页端路况瓦片"
                  "（渲染同源，未公开接口，仅限自用研究）",
        "level": level,
        "tiles_total": len(tile_pairs), "tiles_ok": len(results),
        "tiles_failed": failed,
        "mosaic_px": px_nz,
        "pixels_by_status": px_by_status,
        "sampled_at_min": min(sampled_ats, default=""),
        "sampled_at_max": max(sampled_ats, default=""),
    }
    log("像素状态分布: " + " · ".join(f"{k} {v}" for k, v in px_by_status.items()))

    # —— 导出 ——
    out = ctx.outputs_dir
    written: list[str] = []
    written += exp.export_scope(scope, out, cfg.output.formats)
    written += exp.export_tiles(tile_pairs, out, cfg.output.formats)
    if cfg.output.raster:
        written += exp.export_mosaic(mosaic, out, ("tif", "png"))
        # 原始瓦片拼接（保留原色与白描边，背景透明）
        rgba_raw, mosaic_raw = _assemble_raw(tile_pairs, level, provider, ctx)
        if cfg.collect.clip:
            clip_rgba_to_polygon(rgba_raw, mosaic_raw, scope.polygon)
        written += exp.export_raw_mosaic(rgba_raw, mosaic_raw, out)
    written.append(exp.write_summary(summary, os.path.join(out, "summary.json")))
    log("成果导出:")
    for p in written:
        log(f"  {p}")

    # —— 交互地图 ——
    if cfg.viz.enabled and cfg.output.html:
        try:
            from .tiles import fit_affine, status_to_rgba
            from .viz import build_map
            tk = _get_tianditu_tk() if cfg.viz.basemap == "tianditu" else ""
            carto = _get_carto_key()
            _, bounds = fit_affine(mosaic)
            rgba = status_to_rgba(mosaic.status, provider)
            build_map(scope, tile_pairs, bounds, rgba, ctx.map_html,
                      tk=tk, carto=carto, summary=summary, provider=provider)
            log(f"  {ctx.map_html}")
        except Exception as e:
            log(f"交互地图生成失败（不影响 GIS 成果）: {e}")

    # —— 元数据 ——
    ctx.extra["scope"] = {"source": scope.source, "name": scope.name, "meta": scope.meta}
    ctx.write_metadata(params=config_snapshot(cfg), stats=summary)
    log(f"  {ctx.metadata_json}")
    log("完成。")
    return ctx.dir


# ---------------------------------------------------------------------- 子命令

def cmd_estimate(cfg: TrafficConfig, log=print) -> int:
    """干跑估算：不发起请求。"""
    scope = build_scope(cfg)
    provider = cfg.collect.provider
    level = _resolve_level(cfg, scope, log=log)
    tiles = make_tiles_for_scope(scope.polygon, level, provider)
    n = len(tiles)
    est_seconds = n / cfg.collect.qps if cfg.collect.qps > 0 else float("inf")

    log("—— 干跑估算（不发起请求）——")
    log(f"范围: [{scope.source}] {scope.name}，外接对角线 {scope.diagonal_km():.2f} km")
    log(f"路况瓦片数: {n}（provider={provider}，level={level}，无需 AK、无配额限制）")
    log(f"预计耗时: 约 {est_seconds:.0f} 秒（qps={cfg.collect.qps}，保守限速）")
    if scope.diagonal_km() > 50 and level >= 16:
        log("提示：范围大且级别高，瓦片数较多；建议降到 level 15 或先小样验证链路。")
    return 0


def cmd_render(run_dir: str, basemap: str | None = None, log=print) -> int:
    """离线重出图：从 raw 存档恢复，零请求。"""
    from . import export as exp
    from .scope.base import ScopeArea, normalize_polygon
    from .scope.bbox import bbox_area
    from .tiles import fit_affine, status_to_rgba

    ctx = RunContext(resume=run_dir)
    with open(ctx.config_snapshot, "r", encoding="utf-8") as f:
        snap = yaml.safe_load(f) or {}
    sa = snap.get("study_area", {}) or {}
    traffic = snap.get("traffic") or snap.get("web_traffic") or {}
    output = snap.get("output", {}) or {}
    provider = str(traffic.get("provider", "baidu"))
    level = int(traffic.get("level", traffic.get("zoom", DEFAULT_LEVEL)))
    clip = bool(traffic.get("clip", True))

    scope = None
    for fmt in ("geojson", "gpkg", "shp"):
        path = os.path.join(ctx.outputs_dir, f"scope_area.{fmt}")
        if os.path.exists(path):
            import geopandas as gpd
            gdf = gpd.read_file(path)
            scope = ScopeArea(
                polygon=normalize_polygon(geom=gdf.geometry.union_all()),
                source=str(gdf.iloc[0].get("source", "unknown")),
                name=str(gdf.iloc[0].get("name", "")))
            break
    if scope is None and sa.get("type") == "bbox":
        scope = bbox_area(sa["bbox"], name=sa.get("name", ""),
                          crs=sa.get("crs", "wgs84"))
    if scope is None:
        raise RuntimeError("无法恢复查询范围：outputs 下无 scope_area 成果，"
                           "快照也不是 bbox 类型。请用 run 重新采集。")

    tile_pairs = make_tiles_for_scope(scope.polygon, level, provider)
    classified = _classify_all(tile_pairs, level, provider, ctx)
    if not classified:
        raise RuntimeError(f"{run_dir}/raw 下没有瓦片存档，无法重出图。")
    mosaic = assemble_mosaic(level, classified, provider)
    if clip:
        clip_to_polygon(mosaic, scope.polygon)

    sampled_ats = []
    tiles_ok = 0
    for k, _ in tile_pairs:
        env = ctx.read_tile_raw(k)
        if env is not None:
            tiles_ok += 1
            sampled_ats.append(env[1])
    px_by_status = {}
    for code in (1, 2, 3, 4):
        n = int((mosaic.status == code).sum())
        if n:
            from .tiles import status_name
            px_by_status[status_name(code)] = n
    summary = {
        "provider": provider,
        "level": level,
        "tiles_total": len(tile_pairs),
        "tiles_ok": tiles_ok,
        "tiles_failed": [],
        "mosaic_px": int((mosaic.status > 0).sum()),
        "pixels_by_status": px_by_status,
        "sampled_at_min": min(sampled_ats, default=""),
        "sampled_at_max": max(sampled_ats, default=""),
    }

    out = ctx.outputs_dir
    if output.get("raster", True):
        exp.export_mosaic(mosaic, out, ("tif", "png"))
        rgba_raw, mosaic_raw = _assemble_raw(tile_pairs, level, provider, ctx)
        if clip:
            clip_rgba_to_polygon(rgba_raw, mosaic_raw, scope.polygon)
        exp.export_raw_mosaic(rgba_raw, mosaic_raw, out)
    exp.write_summary(summary, os.path.join(out, "summary.json"))

    from .viz import build_map
    bm = basemap or (snap.get("viz", {}) or {}).get("basemap", "tianditu")
    tk = _get_tianditu_tk() if bm == "tianditu" else ""
    carto = _get_carto_key()
    _, bounds = fit_affine(mosaic)
    rgba = status_to_rgba(mosaic.status, provider)
    build_map(scope, tile_pairs, bounds, rgba, ctx.map_html, tk=tk,
              carto=carto, summary=summary, provider=provider)
    log(f"已重出图: {ctx.map_html}")
    return 0


# ---------------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="traffic-scope",
        description="按空间范围（行政区划/矢量文件/bbox）采集中国地图网页端实时路况"
                    "（百度/高德，渲染同源，无需 AK），产出镶嵌图与交互地图")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="正式采集并产出全部成果")
    p_run.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                       help="YAML 配置路径（默认 configs/config.yaml）")
    p_run.add_argument("--resume", metavar="RUN_DIR",
                       help="从指定 run 目录断点续跑（只补未成功瓦片）")

    p_est = sub.add_parser("estimate", help="干跑估算（不发起请求）")
    p_est.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                       help="YAML 配置路径（默认 configs/config.yaml）")

    p_render = sub.add_parser("render", help="从 run 目录存档离线重出图（零请求）")
    p_render.add_argument("run_dir", help="run 目录路径")
    p_render.add_argument("--basemap", choices=["tianditu", "osm"], default=None,
                          help="覆盖快照中的底图设置")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "run":
            cfg = _load_config(args.config)
            run_pipeline(cfg, resume=args.resume)
        elif args.cmd == "estimate":
            cfg = _load_config(args.config)
            cmd_estimate(cfg)
        elif args.cmd == "render":
            cmd_render(args.run_dir, basemap=args.basemap)
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
