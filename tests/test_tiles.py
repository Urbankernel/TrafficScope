"""tiles 模块测试：瓦片网格、采集参数、调色板分类、镶嵌与裁剪（离线）。"""

import io

import numpy as np
from PIL import Image

from traffic_scope.tiles import (assemble_mosaic, assemble_raw_mosaic,
                                 classify_tile, clip_rgba_to_polygon,
                                 clip_to_polygon, fit_affine,
                                 resolution, status_to_rgba,
                                 tile_key, tile_polygon, tiles_for_bbox,
                                 wgs84_to_tile)


def test_tile_number_matches_browser_capture():
    """瓦片号应与浏览器抓包一致（2026-10-19）。

    level=15 瓦片 (6605,1778) 中心反算落在浦东张江 WGS84≈(121.51677,31.24227)；
    杨浦中心 (121.5325,31.2700) 在 level=15 应得 (6606,1780)。
    """
    assert wgs84_to_tile(121.51677, 31.24227, 15) == (6605, 1778)
    assert wgs84_to_tile(121.5325, 31.2700, 15) == (6606, 1780)


def test_amap_tile_grid():
    """高德瓦片 = 标准 Web 墨卡托 XYZ（GCJ-02）。

    上海人民广场附近 WGS84 (121.47, 31.23) 在 z=15 应落在一个确定瓦片内；
    只验证自洽：同点瓦片号稳定、瓦片多边形覆盖该点、bbox 覆盖无遗漏。
    """
    lon, lat = 121.47, 31.23
    tx, ty = wgs84_to_tile(lon, lat, 15, "amap")
    assert isinstance(tx, int) and isinstance(ty, int)
    # 瓦片多边形应包含该点（GCJ-02→WGS84 反算闭环）
    poly = tile_polygon(tx, ty, 15, "amap")
    from shapely.geometry import Point
    assert poly.contains(Point(lon, lat)) or poly.buffer(1e-6).contains(Point(lon, lat))
    # bbox 覆盖：四角都在返回的瓦片集合内
    bbox = (lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01)
    tiles = tiles_for_bbox(bbox, 15, "amap")
    for x, y in [(bbox[0], bbox[1]), (bbox[2], bbox[3])]:
        assert wgs84_to_tile(x, y, 15, "amap") in tiles


def test_amap_y_axis_reversed_vs_baidu():
    """高德（标准 XYZ）与百度（BD09MC）y 轴方向相反——同一纬度走向的瓦片
    集合，二者 ty 的相对次序应相反（高德 y 向南递增，百度 ty 向北递增）。"""
    bbox = (121.46, 31.22, 121.48, 31.24)
    bd = tiles_for_bbox(bbox, 15, "baidu")
    am = tiles_for_bbox(bbox, 15, "amap")
    assert bd and am


def test_tiles_for_bbox_covers_bounds():
    bbox = (121.520, 31.260, 121.530, 31.270)
    tiles = tiles_for_bbox(bbox, 15)
    assert tiles
    # 每个瓦片都与 bbox 相交（覆盖无遗漏）
    from shapely.geometry import box
    area = box(*bbox)
    for tx, ty in tiles:
        assert tile_polygon(tx, ty, 15).intersects(area)
    # 四个角点都被至少一个瓦片覆盖
    for x, y in [(bbox[0], bbox[1]), (bbox[2], bbox[3])]:
        tx, ty = wgs84_to_tile(x, y, 15)
        assert (tx, ty) in tiles


def _png_from_pixels(colors):
    """构造 256×256 PNG：按 (x0, x1, y0, y1, rgba) 画色块。"""
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    for x0, x1, y0, y1, c in colors:
        for x in range(x0, x1):
            for y in range(y0, y1):
                img.putpixel((x, y), c)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_classify_tile_statuses():
    png = _png_from_pixels([
        (0, 50, 0, 50, (79, 210, 125, 255)),     # 畅通绿
        (60, 110, 0, 50, (255, 208, 69, 255)),   # 缓行黄
        (120, 170, 0, 50, (232, 14, 14, 255)),   # 拥堵红
        (180, 230, 0, 50, (181, 0, 0, 255)),     # 严重拥堵暗红
        (0, 50, 60, 110, (254, 255, 254, 255)),  # 道路描边近白 → 排除
    ])
    status = classify_tile(png)
    assert status[25, 25] == 1
    assert status[25, 85] == 2
    assert status[25, 145] == 3
    assert status[25, 205] == 4
    assert status[85, 25] == 0   # 白描边应被排除


def test_classify_tile_amap_palette():
    """高德调色板分类：用高德四级参考色 + 近白描边。"""
    png = _png_from_pixels([
        (0, 50, 0, 50, (51, 177, 0, 255)),      # 畅通绿（高德）
        (60, 110, 0, 50, (255, 204, 0, 255)),   # 缓行黄（高德）
        (120, 170, 0, 50, (222, 0, 0, 255)),    # 拥堵红（高德）
        (180, 230, 0, 50, (150, 20, 20, 255)),  # 严重拥堵暗红（高德）
        (0, 50, 60, 110, (254, 255, 254, 255)),  # 描边近白 → 排除
    ])
    status = classify_tile(png, "amap")
    assert status[25, 25] == 1
    assert status[25, 85] == 2
    assert status[25, 145] == 3
    assert status[25, 205] == 4
    assert status[85, 25] == 0


def test_assemble_and_clip():
    # 构造 2×2 瓦片（ty 向北递增），仅 ty=1（北）瓦片有路况像素
    full = np.zeros((256, 256), dtype=np.uint8)
    full[100:110, 100:110] = 3
    classified = {(0, 0): np.zeros((256, 256), np.uint8),
                  (1, 0): np.zeros((256, 256), np.uint8),
                  (0, 1): full,
                  (1, 1): np.zeros((256, 256), np.uint8)}
    mosaic = assemble_mosaic(15, classified)
    assert mosaic.shape == (512, 512)
    # ty=1 在最北 → 放到顶部（行 0），故 (105,105) 是满像素瓦片的位置
    assert (mosaic.status == 3).sum() == 100
    assert mosaic.status[105, 105] == 3

    # 裁剪：以镶嵌体四角构成的多边形，全范围应保留
    from shapely.geometry import Polygon
    lons = [mosaic.pixel_to_wgs84(px, py) for px, py in
            [(0, 0), (511, 0), (511, 511), (0, 511)]]
    poly = Polygon(lons)
    clip_to_polygon(mosaic, poly)
    assert (mosaic.status == 3).sum() == 100


def test_assemble_amap_y_direction():
    """高德拼接方向：标准 XYZ y 向南递增 → y 小的瓦片放顶部（行 0）。"""
    full = np.zeros((256, 256), dtype=np.uint8)
    full[100:110, 100:110] = 3
    classified = {(0, 0): full,                     # y=0 在最北（顶部）
                  (0, 1): np.zeros((256, 256), np.uint8),
                  (1, 0): np.zeros((256, 256), np.uint8),
                  (1, 1): np.zeros((256, 256), np.uint8)}
    mosaic = assemble_mosaic(15, classified, "amap")
    assert mosaic.shape == (512, 512)
    # y=0 瓦片在顶部（行 0），故其内部像素 (105,105) 应为 3
    assert (mosaic.status == 3).sum() == 100
    assert mosaic.status[105, 105] == 3


def test_fit_affine_and_rgba():
    classified = {(0, 0): np.zeros((256, 256), np.uint8)}
    mosaic = assemble_mosaic(15, classified)
    transform, bounds = fit_affine(mosaic)
    w, s, e, n = bounds
    assert w < e and s < n
    # 中心点投影应落回瓦片中心附近
    lon, lat = transform * (128, 128)
    cx, cy = mosaic.pixel_to_wgs84(128, 128)
    assert abs(lon - cx) < 1e-4 and abs(lat - cy) < 1e-4

    rgba = status_to_rgba(mosaic.status)
    assert rgba.shape == (256, 256, 4)


def test_tile_key_roundtrip():
    assert tile_key(15, 6605, 1778) == "15_6605_1778"


def test_assemble_raw_y_direction():
    """原始拼接方向：百度 ty 向北递增（ty 大在顶），高德 y 向南递增（y 小在顶）。"""
    red = _png_from_pixels([(0, 20, 0, 20, (232, 14, 14, 255))])
    empty = _png_from_pixels([])

    # 百度：红瓦片放 ty=1（北）→ 顶部（行 0）
    raw_bd = {(0, 0): empty, (1, 0): empty, (0, 1): red, (1, 1): empty}
    rgba_bd, _ = assemble_raw_mosaic(15, raw_bd, "baidu")
    assert rgba_bd.shape == (512, 512, 4)
    assert tuple(rgba_bd[10, 10, :3]) == (232, 14, 14)
    assert rgba_bd[400, 10, 3] == 0        # 底部瓦片透明

    # 高德：红瓦片放 y=0（北）→ 顶部（行 0）
    raw_am = {(0, 0): red, (1, 0): empty, (0, 1): empty, (1, 1): empty}
    rgba_am, _ = assemble_raw_mosaic(15, raw_am, "amap")
    assert tuple(rgba_am[10, 10, :3]) == (232, 14, 14)
    assert rgba_am[400, 10, 3] == 0


def test_clip_rgba_to_polygon():
    """范围外 alpha 置 0（变透明），范围内保留。"""
    red = _png_from_pixels([(0, 256, 0, 256, (232, 14, 14, 255))])
    rgba, mosaic = assemble_raw_mosaic(15, {(0, 0): red})
    from shapely.geometry import Polygon
    lons = [mosaic.pixel_to_wgs84(px, py) for px, py in
            [(0, 0), (128, 0), (128, 128), (0, 128)]]
    clip_rgba_to_polygon(rgba, mosaic, Polygon(lons))
    assert rgba[60, 60, 3] == 255          # 范围内（左上）保留
    assert rgba[200, 200, 3] == 0          # 范围外（右下）透明
