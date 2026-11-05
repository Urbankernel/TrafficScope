"""配置加载与校验、快照测试。"""

import os
import tempfile

import pytest
import yaml

from traffic_scope.config import (AUTO_LEVEL, ConfigError, auto_level,
                                   config_snapshot, load_config)


def _write(cfg: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    return path


BASE = {"study_area": {"type": "bbox", "bbox": [121.52, 31.26, 121.54, 31.28]}}


def test_load_minimal_defaults():
    cfg = load_config(_write(BASE))
    assert cfg.collect.provider == "baidu"
    assert cfg.collect.level == AUTO_LEVEL
    assert cfg.collect.qps == 5.0
    assert cfg.collect.clip is True
    assert cfg.output.formats == ["gpkg", "geojson"]
    assert cfg.output.raster is True
    assert cfg.output.html is True


def test_amap_provider_parsed():
    cfg = load_config(_write({**BASE, "traffic": {"provider": "amap"}}))
    assert cfg.collect.provider == "amap"


def test_bad_provider():
    with pytest.raises(ConfigError):
        load_config(_write({**BASE, "traffic": {"provider": "google"}}))


def test_amap_level_range():
    """高德 level 范围 10–18（百度 12–18）。"""
    cfg = load_config(_write({**BASE, "traffic": {"provider": "amap", "level": 11}}))
    assert cfg.collect.level == 11
    # 百度 level 11 非法，高德合法
    with pytest.raises(ConfigError):
        load_config(_write({**BASE, "traffic": {"level": 11}}))


def test_auto_level_thresholds():
    assert auto_level(10) == 17
    assert auto_level(25) == 17
    assert auto_level(26) == 16
    assert auto_level(200) == 16
    assert auto_level(201) == 15


def test_auto_level_amap_shift():
    """高德同面积比百度低 1 级（分辨率约 2 倍），但下限 10 不越界。"""
    assert auto_level(10, "amap") == 16
    assert auto_level(26, "amap") == 15
    assert auto_level(201, "amap") == 14
    # 极小/极低等级场景下不越过 amap 下限 10
    assert auto_level(1e6, "amap") >= 10


def test_auto_level_in_config():
    cfg = load_config(_write({**BASE, "traffic": {"level": "auto"}}))
    assert cfg.collect.level == AUTO_LEVEL


def test_traffic_section_parsed():
    cfg = load_config(_write({**BASE, "traffic": {"level": 16, "qps": 2.5,
                                                  "clip": False}}))
    assert cfg.collect.level == 16
    assert cfg.collect.qps == 2.5
    assert cfg.collect.clip is False


def test_legacy_keys_accepted():
    """历史键 web_traffic / zoom 仍可读取。"""
    cfg = load_config(_write({**BASE, "web_traffic": {"zoom": 13}}))
    assert cfg.collect.level == 13


def test_bad_scope_type():
    with pytest.raises(ConfigError):
        load_config(_write({"study_area": {"type": "draw"}}))


def test_bad_level():
    for bad in (11, 19, 0, "foo"):
        with pytest.raises(ConfigError):
            load_config(_write({**BASE, "traffic": {"level": bad}}))


def test_bad_formats():
    with pytest.raises(ConfigError):
        load_config(_write({**BASE, "output": {"formats": ["csv"]}}))


def test_bad_bbox():
    with pytest.raises(ConfigError):
        load_config(_write({"study_area": {"type": "bbox", "bbox": [1, 2, 3]}}))


def test_bbox_crs_valid():
    cfg = load_config(_write({"study_area": {
        "type": "bbox", "bbox": [121.52, 31.26, 121.54, 31.28], "crs": "gcj02"}}))
    assert cfg.study_area["crs"] == "gcj02"


def test_bad_bbox_crs():
    with pytest.raises(ConfigError):
        load_config(_write({"study_area": {
            "type": "bbox", "bbox": [1, 2, 3, 4], "crs": "foo"}}))


def test_snapshot_structure():
    snap = config_snapshot(load_config(_write(BASE)))
    assert set(snap.keys()) == {"study_area", "traffic", "output", "viz"}
    assert snap["traffic"]["provider"] == "baidu"
    assert snap["traffic"]["level"] == AUTO_LEVEL
    assert "ak" not in yaml.safe_dump(snap).lower()
