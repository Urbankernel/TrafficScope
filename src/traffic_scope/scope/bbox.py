"""bbox 矩形框范围。

职责：[xmin, ymin, xmax, ymax]（crs 指定坐标系）→ ScopeArea 面（WGS84）。
支持 wgs84 / gcj02（高德、腾讯坐标拾取器）/ bd09（百度坐标拾取器），
非 WGS84 输入自动转换，方便直接粘贴拾取网页得到的坐标。
"""

from __future__ import annotations

from shapely.geometry import box

from ..coords import to_wgs84
from .base import ScopeArea, normalize_polygon


class BboxError(ValueError):
    pass


def bbox_area(bounds: list[float] | tuple[float, ...], name: str = "",
              crs: str = "wgs84") -> ScopeArea:
    """bbox → 矩形面。crs 指定输入坐标系，非 wgs84 时自动转 WGS84 进入下游。"""
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        raise BboxError(f"bbox 须为 [xmin, ymin, xmax, ymax] 四元组，当前: {bounds!r}")
    try:
        xmin, ymin, xmax, ymax = (float(v) for v in bounds)
    except (TypeError, ValueError) as e:
        raise BboxError(f"bbox 元素必须为数值: {bounds!r}") from e
    if not (xmin < xmax and ymin < ymax):
        raise BboxError(f"bbox 需满足 xmin<xmax 且 ymin<ymax，当前: {bounds!r}")
    if not (-180 <= xmin <= 180 and -180 <= xmax <= 180
            and -90 <= ymin <= 90 and -90 <= ymax <= 90):
        raise BboxError(f"bbox 超出经纬度合法范围: {bounds!r}")

    meta = {"bbox": [xmin, ymin, xmax, ymax], "crs": crs}
    if crs != "wgs84":
        try:
            lon1, lat1 = to_wgs84(xmin, ymin, crs)
            lon2, lat2 = to_wgs84(xmax, ymax, crs)
        except ValueError as e:
            raise BboxError(str(e)) from e
        # 转换后重取外包（局部偏移虽小，仍保证 xmin<xmax / ymin<ymax）
        wgs = [min(lon1, lon2), min(lat1, lat2), max(lon1, lon2), max(lat1, lat2)]
        meta["bbox_wgs84"] = wgs
        xmin, ymin, xmax, ymax = wgs

    geom = normalize_polygon(geom=box(xmin, ymin, xmax, ymax))
    return ScopeArea(
        polygon=geom,
        source="bbox",
        name=name or f"bbox_{xmin:.4f}_{ymin:.4f}_{xmax:.4f}_{ymax:.4f}",
        meta=meta,
    )
