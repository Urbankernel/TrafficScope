"""坐标系转换与 bbox 坐标拾取测试。

锁定 GCJ-02 / BD-09 转换的正确性（含历史上 _transform_lon 缺项、
dlon 误用 dlat 变量的回归），以及 bbox 的 crs 输入转换。
"""

import math

from traffic_scope.coords import (bd09_to_wgs84, gcj02_to_wgs84, to_wgs84,
                                  wgs84_to_bd09, wgs84_to_gcj02)
from traffic_scope.scope.bbox import bbox_area

SH = (121.520, 31.260)   # 上海，中国境内（触发加密偏移）


def test_gcj02_shifts_east_of_wgs84():
    """中国境内 GCJ-02 相对 WGS84 经度东偏（上海约 400m，故 0.001–0.02°）。"""
    lon, lat = wgs84_to_gcj02(*SH)
    dlon, dlat = lon - SH[0], lat - SH[1]
    assert 0.001 < dlon < 0.02
    assert -0.02 < dlat < 0.02


def test_gcj02_roundtrip():
    lon, lat = wgs84_to_gcj02(*SH)
    back = gcj02_to_wgs84(lon, lat)
    assert abs(back[0] - SH[0]) < 1e-9
    assert abs(back[1] - SH[1]) < 1e-9


def test_bd09_roundtrip():
    lon, lat = wgs84_to_bd09(*SH)
    back = bd09_to_wgs84(lon, lat)
    assert abs(back[0] - SH[0]) < 1e-6
    assert abs(back[1] - SH[1]) < 1e-6


def test_to_wgs84_dispatch():
    assert to_wgs84(*SH, "wgs84") == SH
    g = to_wgs84(*wgs84_to_gcj02(*SH), "gcj02")
    assert abs(g[0] - SH[0]) < 1e-9
    b = to_wgs84(*wgs84_to_bd09(*SH), "bd09")
    assert abs(b[0] - SH[0]) < 1e-6


def test_bbox_area_crs_conversion():
    """bbox 用 gcj02/bd09 输入时自动转 WGS84，且元数据记录原始输入。"""
    bounds = [121.520, 31.260, 121.545, 31.280]
    a = bbox_area(bounds, crs="gcj02")
    w = bbox_area(bounds, crs="wgs84")
    # GCJ-02 东偏，转 WGS84 后 xmin 应变小（约 400m ≈ 0.004°）
    assert w.polygon.bounds[0] - a.polygon.bounds[0] > 0.001
    assert a.meta["crs"] == "gcj02"
    assert a.meta["bbox"] == bounds
    assert "bbox_wgs84" in a.meta


def test_bbox_area_crs_wgs84_no_shift():
    a = bbox_area([121.520, 31.260, 121.545, 31.280], crs="wgs84")
    assert a.meta["crs"] == "wgs84"
    assert "bbox_wgs84" not in a.meta
    assert a.polygon.bounds == (121.520, 31.260, 121.545, 31.280)
