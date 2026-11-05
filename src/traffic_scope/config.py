"""YAML 配置加载与校验。

配置即 CLI 的唯一输入；脚本中也可直接构造 TrafficConfig 使用。
数据源为网页端路况瓦片（TrafficTileService，无需 AK）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

SCOPE_TYPES = ("admin", "file", "bbox")
VECTOR_FORMATS = ("gpkg", "geojson", "shp")
RASTER_FORMATS = ("tif", "png")
MIN_LEVEL = 12
MAX_LEVEL = 18
DEFAULT_LEVEL = 15
AUTO_LEVEL = "auto"     # level 的缺省取值：按范围面积自适应推荐
PROVIDERS = ("baidu", "amap")
PROVIDER_LEVEL_RANGE = {"baidu": (12, 18), "amap": (10, 18)}
BBOX_CRS = ("wgs84", "gcj02", "bd09")   # bbox 可选输入坐标系（拾取网页坐标）


class ConfigError(ValueError):
    """配置校验失败。"""


def auto_level(area_km2: float, provider: str = "baidu") -> int:
    """按范围面积自适应推荐瓦片级别。

    实测（2026-10-19，裁剪后同范围骨架里程，百度）：level 越高路网越密——
    L15≈2.3、L16≈6.1、L17≈16.9、L18≈17.4 km/km²；L17→L18 里程饱和（同一张
    网，L18 只提几何分辨率、瓦片数 ×3.4），故 L17 是密度/请求量最优档。
    面积阈值据此设定：小范围给高等级（精细），大范围给低等级（控制请求量）。

    高德瓦片级别分辨率更高（同级别下约是百度的 2 倍，即高德 L(N)≈百度 L(N+1)
    的分辨率），故同面积高德比百度低 1 级，以保持相当的瓦片量与路网密度。
    """
    if area_km2 <= 25:
        level = 17
    elif area_km2 <= 200:
        level = 16
    else:
        level = 15
    if provider == "amap":
        level = max(level - 1, PROVIDER_LEVEL_RANGE["amap"][0])
    return level


@dataclass
class CollectCfg:
    """路况瓦片采集参数。"""
    provider: str = "baidu"          # 数据源 baidu（百度）| amap（高德）
    level: int | str = AUTO_LEVEL   # 瓦片级别，或 "auto" 按面积自适应
    qps: float = 5.0
    max_retries: int = 3
    clip: bool = True                 # 镶嵌图/矢量是否裁剪到范围内


@dataclass
class OutputCfg:
    dir: str = "runs"
    formats: list[str] = field(default_factory=lambda: ["gpkg", "geojson"])
    raster: bool = True               # 是否输出镶嵌 GeoTIFF/PNG
    keep_raw_png: bool = True
    html: bool = True


@dataclass
class VizCfg:
    enabled: bool = True
    basemap: str = "tianditu"          # tianditu | osm


@dataclass
class TrafficConfig:
    study_area: dict = field(default_factory=dict)
    collect: CollectCfg = field(default_factory=CollectCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    viz: VizCfg = field(default_factory=VizCfg)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def validate(cfg: TrafficConfig) -> TrafficConfig:
    sa = cfg.study_area
    _require(sa.get("type") in SCOPE_TYPES,
             f"study_area.type 须为 {SCOPE_TYPES} 之一，当前: {sa.get('type')!r}")
    if sa["type"] == "file":
        _require(bool(sa.get("path")), "study_area.type=file 时必须提供 path")
    if sa["type"] == "admin":
        _require(bool(sa.get("adcode")),
                 "study_area.type=admin 时必须提供 adcode（6 位行政区划代码）")
    if sa["type"] == "bbox":
        _require(isinstance(sa.get("bbox"), (list, tuple)) and len(sa["bbox"]) == 4,
                 "study_area.type=bbox 时 bbox 须为 [xmin, ymin, xmax, ymax]")
        crs = sa.get("crs", "wgs84")
        _require(crs in BBOX_CRS,
                 f"study_area.crs 须为 {BBOX_CRS} 之一，当前: {crs!r}")

    provider = cfg.collect.provider
    _require(provider in PROVIDERS,
             f"traffic.provider 须为 {PROVIDERS} 之一，当前: {provider!r}")
    lo, hi = PROVIDER_LEVEL_RANGE[provider]
    level = cfg.collect.level
    if level != AUTO_LEVEL:
        _require(isinstance(level, int) and not isinstance(level, bool)
                 and lo <= level <= hi,
                 f"traffic.level 须为 {lo}–{hi} 的整数或 '{AUTO_LEVEL}'"
                 f"（provider={provider}），当前: {level!r}")
    _require(cfg.collect.qps > 0, "traffic.qps 必须 > 0")
    _require(cfg.collect.max_retries >= 0, "traffic.max_retries 不能为负")

    bad = [f for f in cfg.output.formats if f not in VECTOR_FORMATS]
    _require(not bad, f"output.formats 含不支持的格式 {bad}，可选 {VECTOR_FORMATS}")
    _require(cfg.viz.basemap in ("tianditu", "osm"),
             f"viz.basemap 须为 tianditu 或 osm，当前: {cfg.viz.basemap!r}")
    return cfg


def load_config(path: str) -> TrafficConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    output_raw = raw.get("output", {}) or {}
    viz_raw = raw.get("viz", {}) or {}
    # traffic 为主键；兼容历史键 web_traffic / zoom
    collect_raw = raw.get("traffic") or raw.get("web_traffic") or {}
    level = collect_raw.get("level", collect_raw.get("zoom", AUTO_LEVEL))
    if level != AUTO_LEVEL:
        try:
            level = int(level)
        except (TypeError, ValueError):
            raise ConfigError(
                f"traffic.level 须为 {MIN_LEVEL}–{MAX_LEVEL} 的整数或 '{AUTO_LEVEL}'，"
                f"当前: {level!r}")

    cfg = TrafficConfig(
        study_area=raw.get("study_area", {}) or {},
        collect=CollectCfg(
            provider=str(collect_raw.get("provider", "baidu")),
            level=level,
            qps=float(collect_raw.get("qps", 5.0)),
            max_retries=int(collect_raw.get("max_retries", 3)),
            clip=bool(collect_raw.get("clip", True)),
        ),
        output=OutputCfg(
            dir=output_raw.get("dir", "runs"),
            formats=list(output_raw.get("formats", ["gpkg", "geojson"])),
            raster=bool(output_raw.get("raster", True)),
            keep_raw_png=bool(output_raw.get("keep_raw_png", True)),
            html=bool(output_raw.get("html", True)),
        ),
        viz=VizCfg(
            enabled=bool(viz_raw.get("enabled", True)),
            basemap=viz_raw.get("basemap", "tianditu"),
        ),
    )
    return validate(cfg)


def config_snapshot(cfg: TrafficConfig) -> dict:
    """配置快照——写入 run 目录 config.yaml，可复现。"""
    return {
        "study_area": cfg.study_area,
        "traffic": {"provider": cfg.collect.provider, "level": cfg.collect.level,
                    "qps": cfg.collect.qps,
                    "max_retries": cfg.collect.max_retries, "clip": cfg.collect.clip},
        "output": {"dir": cfg.output.dir, "formats": cfg.output.formats,
                   "raster": cfg.output.raster, "keep_raw_png": cfg.output.keep_raw_png,
                   "html": cfg.output.html},
        "viz": {"enabled": cfg.viz.enabled, "basemap": cfg.viz.basemap},
    }
