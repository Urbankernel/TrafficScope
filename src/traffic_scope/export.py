"""成果导出。

职责：GeoPackage / Shapefile / GeoJSON（矢量）+ GeoTIFF / PNG（镶嵌栅格）+
JSON（摘要）。矢量坐标系一律 WGS84（EPSG:4326），见
docs/compliance.md；镶嵌 GeoTIFF 用像素→WGS84 仿射定位（城市尺度残差 <1m）。
"""

from __future__ import annotations

import json
import os

CRS = "EPSG:4326"
_DRIVERS = {"gpkg": "GPKG", "geojson": "GeoJSON", "shp": "ESRI Shapefile"}


def _to_gdf(rows: list[dict], geometries: list):
    import geopandas as gpd
    return gpd.GeoDataFrame(rows, geometry=geometries, crs=CRS)


def _write(gdf, out_dir: str, name: str, formats: list[str]) -> list[str]:
    written = []
    for fmt in formats:
        path = os.path.join(out_dir, f"{name}.{fmt}")
        gdf.to_file(path, driver=_DRIVERS[fmt])
        written.append(path)
    return written


def export_scope(scope, out_dir: str, formats: list[str]) -> list[str]:
    """查询范围面。"""
    gdf = _to_gdf([{"source": scope.source, "name": scope.name}], [scope.polygon])
    return _write(gdf, out_dir, "scope_area", formats)


def export_tiles(tiles: list[tuple[str, object]], out_dir: str,
                 formats: list[str]) -> list[str]:
    """路况瓦片网格（质检漏抓用）。tiles 为 (key, polygon) 列表。"""
    if not tiles:
        return []
    gdf = _to_gdf([{"tile_id": k} for k, _ in tiles], [g for _, g in tiles])
    return _write(gdf, out_dir, "query_tiles", formats)


def export_mosaic(mosaic, out_dir: str, formats: tuple[str, ...]) -> list[str]:
    """镶嵌分级栅格 → GeoTIFF（单波段 uint8，附调色板）+ PNG（RGBA 预览）。

    返回写出路径列表。transform 为像素→WGS84 仿射。
    """
    from .tiles import PROVIDERS, fit_affine, status_to_rgba

    written = []
    transform, bounds = fit_affine(mosaic)
    if "tif" in formats:
        path = os.path.join(out_dir, "traffic_mosaic.tif")
        _write_geotiff(mosaic.status, transform, path,
                       PROVIDERS[mosaic.provider]["palette"])
        written.append(path)
    if "png" in formats:
        path = os.path.join(out_dir, "traffic_mosaic.png")
        _write_png(status_to_rgba(mosaic.status, mosaic.provider), path)
        written.append(path)
    return written


def export_raw_mosaic(rgba, mosaic, out_dir: str) -> list[str]:
    """原始瓦片拼接 → GeoTIFF（RGBA 四波段）+ PNG（RGBA 预览）。

    保留原始路况颜色与白色道路描边，背景透明。定位复用 mosaic 的仿射。
    """
    from .tiles import fit_affine

    transform, _bounds = fit_affine(mosaic)
    written = []
    path = os.path.join(out_dir, "traffic_mosaic_raw.tif")
    _write_geotiff_rgba(rgba, transform, path)
    written.append(path)
    path = os.path.join(out_dir, "traffic_mosaic_raw.png")
    _write_png(rgba, path)
    written.append(path)
    return written


def _write_geotiff_rgba(rgba, transform, path: str) -> None:
    import numpy as np
    import rasterio

    h, w, _ = rgba.shape
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=4,
        dtype=rasterio.uint8, crs=CRS, transform=transform,
        photometric="RGB",
    ) as dst:
        dst.write(np.ascontiguousarray(rgba).transpose(2, 0, 1))


def _write_geotiff(status, transform, path: str, palette: dict[int, tuple]) -> None:
    import rasterio
    import numpy as np

    h, w = status.shape
    colormap = {
        1: (*palette[1], 255),
        2: (*palette[2], 255),
        3: (*palette[3], 255),
        4: (*palette[4], 255),
    }
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1,
        dtype=rasterio.uint8, crs=CRS, transform=transform, nodata=0,
    ) as dst:
        dst.write(status.astype(np.uint8), 1)
        dst.write_colormap(1, colormap)


def _write_png(rgba, path: str) -> None:
    from PIL import Image
    Image.fromarray(rgba, mode="RGBA").save(path)


def write_summary(summary: dict, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path
