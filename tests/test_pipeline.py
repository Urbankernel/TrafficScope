"""端到端流水线测试：注入 fake fetcher，全程不联网。"""

import io
import json
import os
import tempfile

import pytest

gpd = pytest.importorskip("geopandas")

from PIL import Image

from traffic_scope.cli import run_pipeline
from traffic_scope.config import (CollectCfg, OutputCfg, TrafficConfig,
                                        VizCfg)

BBOX = [121.520, 31.260, 121.530, 31.270]


def _fake_png(red: tuple = (232, 14, 14)) -> bytes:
    """构造一个 256×256 PNG：一条贯穿对角线的红色（拥堵）粗线。"""
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.line([(0, 0), (255, 255)], fill=(*red, 255), width=5)
    d.line([(0, 255), (255, 0)], fill=(*red, 255), width=5)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_cfg(tmp: str) -> TrafficConfig:
    return TrafficConfig(
        study_area={"type": "bbox", "bbox": BBOX},
        collect=CollectCfg(level=15),
        output=OutputCfg(dir=os.path.join(tmp, "runs"), formats=["geojson"],
                         raster=True, keep_raw_png=True, html=False),
        viz=VizCfg(enabled=False),
    )


def test_pipeline_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_cfg(tmp)
        calls = []

        def fake_fetch(key: str) -> bytes:
            calls.append(key)
            return _fake_png()

        run_dir = run_pipeline(cfg, fetch_fn=fake_fetch, log=lambda *_: None)

        # 成果齐备
        out = os.path.join(run_dir, "outputs")
        for f in ("scope_area.geojson", "query_tiles.geojson",
                  "summary.json", "traffic_mosaic.tif", "traffic_mosaic.png",
                  "traffic_mosaic_raw.tif", "traffic_mosaic_raw.png"):
            assert os.path.exists(os.path.join(out, f)), f
        assert os.path.exists(os.path.join(run_dir, "metadata.json"))
        assert os.path.exists(os.path.join(run_dir, "progress.csv"))

        # 已删除矢量化：不再生成路段成果
        for f in ("traffic_segments.geojson", "traffic_segments.csv",
                  "traffic_segments.gpkg", "traffic_segments.shp"):
            assert not os.path.exists(os.path.join(out, f)), f

        # 摘要与元数据（含像素级状态分布，不含 segments）
        with open(os.path.join(out, "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
        assert "segments" not in summary
        assert summary["pixels_by_status"]
        assert summary["tiles_ok"] == summary["tiles_total"]
        assert summary["sampled_at_min"]
        with open(os.path.join(run_dir, "metadata.json"), encoding="utf-8") as f:
            meta = json.load(f)
        assert "segments" not in meta["stats"]
        assert meta["scope"]["source"] == "bbox"

        # raw 存档（PNG）齐备
        raws = [f for f in os.listdir(os.path.join(run_dir, "raw"))
                if f.endswith(".png")]
        assert len(raws) == len(calls)

        # 原始镶嵌 GeoTIFF：RGBA 四波段，第 4 波段为 alpha（0 透明 / 255 不透明）
        rasterio = pytest.importorskip("rasterio")
        with rasterio.open(os.path.join(out, "traffic_mosaic_raw.tif")) as src:
            assert src.count == 4
            alpha = src.read(4)
            assert set(alpha.ravel().tolist()) <= {0, 255}


def test_pipeline_resume_skips_done_tiles():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_cfg(tmp)
        calls = []

        def fake_fetch(key: str) -> bytes:
            calls.append(key)
            return _fake_png()

        run_dir = run_pipeline(cfg, fetch_fn=fake_fetch, log=lambda *_: None)
        n_first = len(calls)
        assert n_first >= 1

        run_pipeline(cfg, fetch_fn=fake_fetch, resume=run_dir,
                     log=lambda *_: None)
        assert len(calls) == n_first   # 续跑零请求


def test_pipeline_failed_tile_partial_result():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_cfg(tmp)

        def flaky_fetch(key: str) -> bytes:
            if key.endswith("_9999_9999"):
                raise RuntimeError("模拟网络错误")
            return _fake_png()

        run_dir = run_pipeline(cfg, fetch_fn=flaky_fetch, log=lambda *_: None)
        # 部分瓦片失败仍产出栅格成果；矢量化已删除，不再生成路段文件
        assert os.path.exists(os.path.join(run_dir, "outputs",
                                           "traffic_mosaic.tif"))
        assert not os.path.exists(os.path.join(run_dir, "outputs",
                                               "traffic_segments.geojson"))


def test_slug_includes_provider():
    """run 目录后缀 = 范围来源方式 + 数据源（如 bbox_baidu / admin_amap）。"""
    from traffic_scope.cli import _slug_for
    from traffic_scope.scope.bbox import bbox_area

    scope = bbox_area(BBOX)
    assert _slug_for(scope, "baidu") == "bbox_baidu"
    assert _slug_for(scope, "amap") == "bbox_amap"
