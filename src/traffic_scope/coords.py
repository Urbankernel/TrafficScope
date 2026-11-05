"""坐标系转换。

职责：WGS84 / GCJ-02 / BD-09 互转（本地算法）。
纪律：转换只发生在输入入口与 API 请求链路，成果层一律 WGS84/CGCS2000。
详见 docs/compliance.md。

注：Traffic API 的 coord_type_input 直接支持 wgs84，请求时无需转换；
返回几何（高级权限 traffic_detail.path）仅支持 gcj02/bd09ll，
落盘前必须用本模块转回 WGS84。
"""

from __future__ import annotations

import math

PI = math.pi
X_PI = PI * 3000.0 / 180.0
_A = 6378245.0  # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323  # 偏心率平方

SUPPORTED = ("wgs84", "gcj02", "bd09ll")


def _out_of_china(lon: float, lat: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(lon: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat + 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
    ret += (20.0 * math.sin(6.0 * lon * PI) + 20.0 * math.sin(2.0 * lon * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * PI) + 40.0 * math.sin(lat / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * PI) + 320 * math.sin(lat * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(lon: float, lat: float) -> float:
    ret = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon + 0.1 * lon * lat \
        + 0.1 * math.sqrt(abs(lon))
    ret += (20.0 * math.sin(6.0 * lon * PI) + 20.0 * math.sin(2.0 * lon * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lon * PI) + 40.0 * math.sin(lon / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lon / 12.0 * PI) + 300 * math.sin(lon / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * PI)
    dlon = (dlon * 180.0) / ((_A / sqrtmagic) * math.cos(radlat) * PI)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → WGS84（定点迭代求精，收敛到 < 1e-9 度，上限 10 轮）。"""
    if _out_of_china(lon, lat):
        return lon, lat
    glon, glat = wgs84_to_gcj02(lon, lat)
    wlon, wlat = lon * 2 - glon, lat * 2 - glat  # 差值法粗估
    for _ in range(10):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        nlon, nlat = wlon + (lon - glon), wlat + (lat - glat)
        if abs(nlon - wlon) < 1e-9 and abs(nlat - wlat) < 1e-9:
            wlon, wlat = nlon, nlat
            break
        wlon, wlat = nlon, nlat
    return wlon, wlat


def gcj02_to_bd09(lon: float, lat: float) -> tuple[float, float]:
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * X_PI)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    x, y = lon - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def bd09_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    glon, glat = bd09_to_gcj02(lon, lat)
    return gcj02_to_wgs84(glon, glat)


def wgs84_to_bd09(lon: float, lat: float) -> tuple[float, float]:
    glon, glat = wgs84_to_gcj02(lon, lat)
    return gcj02_to_bd09(glon, glat)


def to_wgs84(lon: float, lat: float, coord_type: str = "wgs84") -> tuple[float, float]:
    """任意支持的坐标系 → WGS84。成果层唯一入口。"""
    coord_type = coord_type.lower()
    if coord_type == "wgs84":
        return lon, lat
    if coord_type == "gcj02":
        return gcj02_to_wgs84(lon, lat)
    if coord_type in ("bd09", "bd09ll"):
        return bd09_to_wgs84(lon, lat)
    raise ValueError(f"不支持的坐标系: {coord_type!r}，可选 {SUPPORTED}")


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """两点球面距离（公里），用于瓦片对角线校验与干跑估算。"""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
