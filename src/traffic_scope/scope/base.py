"""范围来源抽象。

所有来源（行政区划 / 矢量文件 / bbox / 手绘等）统一归一为
一个 WGS84 面进入下游，主流程不感知来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import shapely
from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.geometry.base import BaseGeometry


@dataclass
class ScopeArea:
    polygon: BaseGeometry          # WGS84 Polygon / MultiPolygon
    source: str = "unknown"        # admin | file | bbox
    name: str = ""
    meta: dict = field(default_factory=dict)

    def to_geojson(self) -> dict:
        return {
            "type": "Feature",
            "properties": {"source": self.source, "name": self.name},
            "geometry": mapping(self.polygon),
        }

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.polygon.bounds

    def diagonal_km(self) -> float:
        """外接矩形对角线长度（公里），用于 endpoint 决策与配额预警。"""
        from ..coords import haversine_km
        minx, miny, maxx, maxy = self.polygon.bounds
        return haversine_km(minx, miny, maxx, maxy)

    def area_km2(self) -> float:
        """范围面面积（平方公里，球面近似）。用于 level 面积自适应推荐。"""
        import math
        minx, miny, maxx, maxy = self.polygon.bounds
        lat = math.radians((miny + maxy) / 2)
        # 1 度经度约 111.32 km·cos(lat)，1 度纬度约 110.57 km
        deg2_km2 = (111.32 * math.cos(lat)) * 110.57
        return self.polygon.area * deg2_km2


def close_ring(coords: list[list[float]] | list[tuple[float, float]]) -> list[list[float]]:
    """闭合环（容忍手绘/外部数据未闭合的脏数据）。"""
    ring = [[float(x), float(y)] for x, y in coords]
    if len(ring) < 3:
        raise ValueError(f"多边形顶点数不足（{len(ring)} < 3）")
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def normalize_polygon(coords=None, geom: BaseGeometry | None = None) -> BaseGeometry:
    """坐标序列或几何 → 合法 Polygon/MultiPolygon。

    脏数据对策：未闭合自动闭合；自相交用 make_valid 修复；
    修复结果为 GeometryCollection 时只保留面构件。
    """
    if geom is None:
        if coords is None:
            raise ValueError("coords 与 geom 至少提供一个")
        geom = Polygon(close_ring(coords))

    if not geom.is_valid:
        fixed = shapely.make_valid(geom)
        if fixed.geom_type == "GeometryCollection":
            polys = [g for g in fixed.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            if not polys:
                raise ValueError("多边形修复失败：无有效面构件")
            fixed = polys[0] if len(polys) == 1 else MultiPolygon(
                [p for g in polys for p in (g.geoms if g.geom_type == "MultiPolygon" else [g])])
        geom = fixed

    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"范围必须是面要素，当前为 {geom.geom_type}")
    if geom.area <= 0:
        raise ValueError("范围面面积为 0")
    return geom
