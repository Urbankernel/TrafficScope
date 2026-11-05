"""行政区划范围。

职责：按 6 位行政区划代码（adcode）从 DataV.GeoAtlas 拉取边界 GeoJSON，
归一化为 WGS84 面。adcode 查询：国家统计局或 DataV 行政区划选择器。
"""

from __future__ import annotations

import json
import urllib.request

import numpy as np
import shapely
from shapely.geometry import shape

from ..coords import gcj02_to_wgs84
from .base import ScopeArea, normalize_polygon

DATAV_URL = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}.json"


class AdminAreaError(RuntimeError):
    pass


def fetch_admin_boundary(adcode: str | int, name: str = "",
                         timeout: float = 30.0) -> ScopeArea:
    """拉取行政区划边界（含下辖子区划的完整边界），统一归一为 WGS84。

    DataV.GeoAtlas 边界为 GCJ-02（火星坐标）——已实测确认：以上城区(330102)为例，
    其质心与 OSM(WGS84) 同名边界质心差 497m，而按 GCJ-02 还原后仅差 49m。
    因此入口处统一 GCJ-02 → WGS84，否则瓦片/成果整体偏东约 500m。
    边界数据版本可能与最新区划调整有出入，高精度场景请改用自然资源部门
    权威边界数据（file 方式导入）。
    """
    adcode = str(adcode).strip()
    if not (adcode.isdigit() and len(adcode) == 6):
        raise AdminAreaError(f"adcode 应为 6 位数字行政区划代码，当前: {adcode!r}")
    if adcode == "100000":
        raise AdminAreaError("全国边界数据量过大且非本工具场景，请用省/市/区级 adcode")

    url = DATAV_URL.format(adcode=adcode)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise AdminAreaError(f"行政区划边界拉取失败（adcode={adcode}）: {e}") from e

    features = payload.get("features") or []
    if not features:
        raise AdminAreaError(f"未获取到边界数据（adcode={adcode}），请检查代码是否有效")

    geoms = [shape(f["geometry"]) for f in features if f.get("geometry")]
    if not geoms:
        raise AdminAreaError(f"边界数据无几何（adcode={adcode}）")

    from shapely.ops import unary_union
    merged = unary_union(geoms)
    # DataV 边界为 GCJ-02 → 入口统一转 WGS84（见 docstring 实测依据）
    _v_g2w = np.vectorize(gcj02_to_wgs84)
    merged = shapely.transform(merged, lambda x, y: _v_g2w(x, y), interleaved=False)
    return ScopeArea(
        polygon=normalize_polygon(geom=merged),
        source="admin",
        name=name or (features[0].get("properties") or {}).get("name", adcode),
        meta={"adcode": adcode},
    )
