"""矢量文件范围。

职责：读取用户提供的 Shapefile / GeoJSON / KML / GeoPackage，
统一转为 WGS84 后合并为单个面。
"""

from __future__ import annotations

import os

from .base import ScopeArea, normalize_polygon

SUPPORTED_EXTS = (".shp", ".geojson", ".json", ".kml", ".gpkg")


def load_vector_area(path: str, layer: str | int | None = None) -> ScopeArea:
    """从矢量文件加载查询范围。

    - 多要素自动合并（unary_union）；
    - 无 CRS 的文件按 WGS84 处理并告警（脏数据兜底）；
    - KML 需要 fiona 的 KML 驱动支持。
    """
    import geopandas as gpd  # 延迟导入

    if not os.path.exists(path):
        raise FileNotFoundError(f"范围文件不存在: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的范围文件格式 {ext!r}，可选 {SUPPORTED_EXTS}")

    read_kwargs = {}
    if layer is not None:
        read_kwargs["layer"] = layer
    gdf = gpd.read_file(path, **read_kwargs)
    if gdf.empty:
        raise ValueError(f"范围文件为空: {path}")

    if gdf.crs is None:
        import warnings
        warnings.warn(f"{path} 缺少坐标系定义，按 WGS84 处理——"
                      f"若实际为 GCJ-02/BD-09 请先用 coords 模块清洗", stacklevel=2)
        gdf = gdf.set_crs(4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    geom = gdf.geometry.union_all()
    return ScopeArea(
        polygon=normalize_polygon(geom=geom),
        source="file",
        name=os.path.splitext(os.path.basename(path))[0],
        meta={"path": os.path.abspath(path), "n_features": len(gdf)},
    )
