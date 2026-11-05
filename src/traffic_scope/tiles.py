"""中国地图网页端路况瓦片采集与分类——支持百度 / 高德双数据源。

数据源（均为地图网页端路况图层**实际请求**的栅格 PNG，无需 AK、无 Cookie）：
- **百度** `https://traffic.map.baidu.com/traffic/TrafficTileService?level={lv}&x={x}&y={y}&time={us}&smallflow=1&scaler=1`
  （map.baidu.com 渲染同源，经 2026-10-19 浏览器抓包确证）；
- **高德** `https://tm.amap.com/trafficengine/mapabc/traffictile?v=1.0&x={x}&y={y}&z={z}`
  （www.amap.com 渲染同源，与 traffic_tile_crawler.py 参考实现一致）。

两源关键差异（均已内置抽象）：
- **瓦片网格**：百度 = 官方 BD09MC（LL2MC 分带多项式）；高德 = 标准 Web
  墨卡托 XYZ（GCJ-02 坐标）。二者 y 轴方向相反（百度 ty 向北递增，高德
  标准 XYZ y 向南递增），拼接与像素→WGS84 反算各自独立实现。
- **调色板**：百度 畅通(79,210,125)/缓行(255,208,69)/拥堵(232,14,14)/
  严重拥堵(181,0,0)；高德 畅通(51,177,0)/缓行(255,204,0)/拥堵(222,0,0)/
  严重拥堵(150,20,20)。四级语义一致，仅 RGB 不同。
- **level 范围**：百度 12–18，高德 10–18。

合规：两接口均为地图厂商未公开文档接口，仅限自用研究，禁止批量抓取与
再分发（详见 docs/compliance.md）。内置限速与重试。
"""

from __future__ import annotations

import io
import math
import time

import numpy as np
from PIL import Image
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .coords import (bd09_to_wgs84, gcj02_to_wgs84, wgs84_to_bd09,
                     wgs84_to_gcj02)

# --------------------------------------------------------------------------
# 数据源元信息
# --------------------------------------------------------------------------

# 百度（默认）调色板：状态码 → (RGB, 中文名)。中文名两源一致，RGB 不同。
STATUS = {
    0: ("", "无数据"),
    1: ((79, 210, 125), "畅通"),
    2: ((255, 208, 69), "缓行"),
    3: ((232, 14, 14), "拥堵"),
    4: ((181, 0, 0), "严重拥堵"),
}

# 数据源配置：url / referer / level 范围 / 四级参考色（最近邻分类用）
PROVIDERS = {
    "baidu": {
        "label": "百度",
        "url": "https://traffic.map.baidu.com/traffic/TrafficTileService",
        "referer": "https://map.baidu.com/",
        "min_level": 12,
        "max_level": 18,
        "palette": {i: STATUS[i][0] for i in (1, 2, 3, 4)},
    },
    "amap": {
        "label": "高德",
        "url": "https://tm.amap.com/trafficengine/mapabc/traffictile",
        "referer": "https://www.amap.com/",
        "min_level": 10,
        "max_level": 18,
        "palette": {
            1: (51, 177, 0),
            2: (255, 204, 0),
            3: (222, 0, 0),
            4: (150, 20, 20),
        },
    },
}

# 向后兼容别名
TILE_URL = PROVIDERS["baidu"]["url"]
MIN_LEVEL = 12
MAX_LEVEL = 18
DEFAULT_LEVEL = 15

# 最近邻分类参数（与 traffic_tile_crawler.py 参考实现一致，经实测验证）：
# 每像素取与四级参考色平方欧氏距离最近者，距离超过阈值判「无数据」——
# 用于排除道路描边（近白）与透明底抗锯齿过渡像素。
DIST_THRESHOLD = 3000
# 半透明像素 alpha 低于此值不参与分类（排除抗锯齿边缘噪声）
MIN_ALPHA = 128

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _headers(provider: str) -> dict[str, str]:
    return {"User-Agent": _UA, "Referer": PROVIDERS[provider]["referer"]}


def status_name(code: int) -> str:
    """状态码 → 中文名（畅通/缓行/拥堵/严重拥堵，两源一致）。"""
    return STATUS[code][1]


# --------------------------------------------------------------------------
# 百度官方 BD09LL ↔ BD09MC 分带多项式（业界公开通用系数，与网页端投影一致）
# --------------------------------------------------------------------------

_MCBAND = (12890594.86, 8362377.87, 5591021.0, 3481989.83, 1678043.12, 0.0)
_MC2LL = (
    (1.410526172116255e-8, 8.98305509648872e-6, -1.9939833816331, 200.9824383106796,
     -187.2403703815547, 91.6087516669843, -23.38765649603339, 2.57121317296198,
     -0.03801003308653, 17337981.2),
    (-7.435856389565537e-9, 8.983055097726239e-6, -0.78625201886289, 96.32687599759846,
     -1.85204757529826, -59.36935905485877, 47.40033549296737, -16.50741931063887,
     2.28786674699375, 10260144.86),
    (-3.030883460898826e-8, 8.98305509983578e-6, 0.30071316287616, 59.74293618442277,
     7.357984074871, -25.38371002664745, 13.45380521110908, -3.29883767235584,
     0.32710905363475, 6856817.37),
    (-1.981981304930552e-8, 8.983055099779535e-6, 0.03278182852591, 40.31678527705744,
     0.65659298677277, -4.44255534477492, 0.85341911805263, 0.12923347998204,
     -0.04625736007561, 4482777.06),
    (3.09191371068437e-9, 8.983055096812155e-6, 0.00006995724062, 23.10934304144901,
     -0.00023663490511, -0.6321817810242, -0.00663494467273, 0.03430082397953,
     -0.00466043876332, 2555164.4),
    (2.890871144776878e-9, 8.983055095805407e-6, -3.068298e-8, 7.47137025468032,
     -0.00000353937994, -0.02145144861037, -0.00001234426596, 0.00010322952773,
     -0.00000323890364, 826088.5),
)
_LLBAND = (75.0, 60.0, 45.0, 30.0, 15.0, 0.0)
_LL2MC = (
    (-0.0015702102444, 111320.7020616939, 1704480524535203.0, -10338987376042340.0,
     26112667856603880.0, -35149669176653700.0, 26595700718403920.0,
     -10725012454188240.0, 1800819912950474.0, 82.5),
    (0.0008277824516172526, 111320.7020463578, 647795574.6671607, -4082003173.641316,
     10774905663.51142, -15171875531.51559, 12053065338.62167, -5124939663.577472,
     913311935.9512032, 67.5),
    (0.00337398766765, 111320.7020202162, 4481351.045890365, -23393751.19931662,
     79682215.47186455, -115964993.2797253, 97236711.15602145, -43661946.33752821,
     8477230.501135234, 52.5),
    (0.00220636496208, 111320.7020209128, 51751.86112841131, 3796837.749470245,
     992013.7397791013, -1221952.21711287, 1340652.697009075, -620943.6990984312,
     144416.9293806241, 37.5),
    (-0.0003441963504368392, 111320.7020576856, 278.2353980772752, 2485758.690035394,
     6070.750963243378, 54821.18345352118, 9540.606633304236, -2710.55326746645,
     1405.483844121726, 22.5),
    (-0.0003218135878613132, 111320.7020701615, 0.00369383431289, 823725.6402795718,
     0.46104986909093, 2351.343141331292, 1.58060784298199, 8.77738589078284,
     0.37238884252424, 7.45),
)


def _poly(x: float, y: float, cf: tuple[float, ...]) -> tuple[float, float]:
    """通用多项式求值（百度官方 convertor）。"""
    t = cf[0] + cf[1] * abs(x)
    cc = abs(y) / cf[9]
    f = (cf[2] + cf[3] * cc + cf[4] * cc**2 + cf[5] * cc**3
         + cf[6] * cc**4 + cf[7] * cc**5 + cf[8] * cc**6)
    return math.copysign(t, x), math.copysign(f, y)


def bd09ll_to_mc(lon: float, lat: float) -> tuple[float, float]:
    """BD09LL → 百度官方墨卡托 BD09MC（LL2MC 分带多项式）。"""
    lat = max(min(lat, 74.0), -74.0)
    cf = next((c for band, c in zip(_LLBAND, _LL2MC) if lat >= band), _LL2MC[-1])
    return _poly(lon, lat, cf)


def mc_to_bd09ll(x: float, y: float) -> tuple[float, float]:
    """百度官方墨卡托 BD09MC → BD09LL（MC2LL 分带多项式）。"""
    ay = abs(y)
    cf = next(c for band, c in zip(_MCBAND, _MC2LL) if ay >= band)
    return _poly(x, y, cf)


# --------------------------------------------------------------------------
# 高德 Web 墨卡托 XYZ 辅助
# --------------------------------------------------------------------------

_WEBMERC_R = 6378137.0


def _amap_world_px(lon: float, lat: float, level: int) -> tuple[float, float]:
    """WGS84 → 高德全局像素坐标（GCJ-02 + 标准 Web 墨卡托 XYZ）。"""
    glon, glat = wgs84_to_gcj02(lon, lat)
    n = 2 ** level
    gx = (glon + 180.0) / 360.0 * 256 * n
    gy = (1.0 - math.asinh(math.tan(math.radians(glat))) / math.pi) / 2.0 * 256 * n
    return gx, gy


def _amap_px_to_wgs84(gx: float, gy: float, level: int) -> tuple[float, float]:
    """高德全局像素坐标 → WGS84（GCJ-02 → WGS84）。"""
    n = 256 * 2 ** level
    lon_gcj = gx / n * 360.0 - 180.0
    lat_gcj = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * gy / n))))
    return gcj02_to_wgs84(lon_gcj, lat_gcj)


def resolution(level: int, provider: str = "baidu") -> float:
    """米/像素（近似，供 simplify/最小长度容差）。百度 = 2^(18−level)；
    高德用中心纬度的 Web 墨卡托分辨率。"""
    if provider == "amap":
        # 赤道处分辨率，随纬度乘 cos(lat)；此处用赤道值做保守近似
        return 156543.03392804097 / (2 ** level)
    return float(2 ** (18 - level))


# --------------------------------------------------------------------------
# 瓦片编号（provider 感知）
# --------------------------------------------------------------------------

def wgs84_to_tile(lon: float, lat: float, level: int,
                  provider: str = "baidu") -> tuple[int, int]:
    """WGS84 点 → 瓦片号。百度走官方 BD09MC；高德走标准 Web 墨卡托 XYZ。"""
    if provider == "amap":
        gx, gy = _amap_world_px(lon, lat, level)
        return math.floor(gx / 256), math.floor(gy / 256)
    mx, my = bd09ll_to_mc(*wgs84_to_bd09(lon, lat))
    t = 256 * resolution(level)
    return math.floor(mx / t), math.floor(my / t)


def tiles_for_bbox(bounds: tuple[float, float, float, float],
                   level: int, provider: str = "baidu") -> list[tuple[int, int]]:
    """WGS84 (minx, miny, maxx, maxy) → 覆盖瓦片号集合。

    投影沿经纬度单调，四角各自转换取极值即可覆盖全范围。
    """
    minx, miny, maxx, maxy = bounds
    tx0, ty0 = wgs84_to_tile(minx, miny, level, provider)
    tx1, ty1 = wgs84_to_tile(maxx, maxy, level, provider)
    return [(tx, ty) for tx in range(min(tx0, tx1), max(tx0, tx1) + 1)
            for ty in range(min(ty0, ty1), max(ty0, ty1) + 1)]


def tile_origin_mc(tx: int, ty: int, level: int) -> tuple[float, float]:
    t = 256 * resolution(level)
    return tx * t, ty * t


def tile_polygon(tx: int, ty: int, level: int, provider: str = "baidu") -> BaseGeometry:
    """瓦片覆盖范围面（WGS84）。质检图层与导出用。"""
    if provider == "amap":
        n = 256 * 2 ** level
        gx0, gy0 = tx * 256, ty * 256
        corners = [_amap_px_to_wgs84(gx0, gy0, level),
                   _amap_px_to_wgs84(gx0 + 256, gy0, level),
                   _amap_px_to_wgs84(gx0 + 256, gy0 + 256, level),
                   _amap_px_to_wgs84(gx0, gy0 + 256, level)]
        return Polygon(corners)

    ox, oy = tile_origin_mc(tx, ty, level)
    t = 256 * resolution(level)
    corners = [mc_to_bd09ll(ox, oy + t), mc_to_bd09ll(ox + t, oy + t),
               mc_to_bd09ll(ox + t, oy), mc_to_bd09ll(ox, oy)]
    return Polygon([bd09_to_wgs84(*c) for c in corners])


def tile_key(level: int, tx: int, ty: int) -> str:
    """瓦片逻辑 id（进度/存档用），形如 "15_6605_1778"。"""
    return f"{level}_{tx}_{ty}"


def tile_key_parts(tid: str) -> tuple[int, int, int]:
    lv, tx, ty = tid.split("_")
    return int(lv), int(tx), int(ty)


# ---------------------------------------------------------------------- 采集

def fetch_tile_png(tx: int, ty: int, level: int, provider: str = "baidu",
                   timeout: float = 15.0, max_retries: int = 3) -> bytes:
    """下载一个路况瓦片，返回 PNG 原始字节（指数退避）。"""
    import requests

    meta = PROVIDERS[provider]
    headers = _headers(provider)
    if provider == "amap":
        url = f"{meta['url']}?v=1.0&x={tx}&y={ty}&z={level}"
        kwargs: dict = {"url": url, "headers": headers, "timeout": timeout}
    else:
        us = int(time.time() * 1e6)
        params = {"level": level, "x": tx, "y": ty, "time": us,
                  "smallflow": 1, "scaler": 1}
        kwargs = {"url": meta["url"], "params": params, "headers": headers,
                  "timeout": timeout}

    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(**kwargs)
            r.raise_for_status()
            if r.content[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError(f"非 PNG 响应（{len(r.content)} 字节）")
            return r.content
        except Exception as e:
            last = e
            if attempt < max_retries:
                time.sleep(min(2 ** (attempt + 1), 10))
    raise RuntimeError(f"{provider} 瓦片 {level}/{tx}/{ty} 下载失败: {last}")


# ---------------------------------------------------------------------- 分类

def classify_tile(png_bytes: bytes, provider: str = "baidu") -> np.ndarray:
    """PNG 瓦片 → 路况等级矩阵（uint8，256×256）。

    返回矩阵取值：0=无数据/透明，1=畅通，2=缓行，3=拥堵，4=严重拥堵。
    分级采用「最近邻参考色 + 距离阈值」：每像素取与四级主干色平方欧氏距离
    最近者，距离超过 DIST_THRESHOLD 判无数据（排除道路描边近白与透明底抗
    锯齿过渡），alpha 低于 MIN_ALPHA 的半透明像素直接判无数据。
    参考色按 provider 取（百度/高德调色板不同）。
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.asarray(img, dtype=np.int32)
    a = arr[..., 3]
    valid = a > MIN_ALPHA
    out = np.zeros(arr.shape[:2], dtype=np.uint8)
    if not valid.any():
        return out

    rgb = arr[..., :3]
    palette = PROVIDERS[provider]["palette"]
    refs = np.array([palette[i] for i in (1, 2, 3, 4)], dtype=np.int32)  # (4,3)
    # 向量化最近邻：dist[i,j,k] = 像素 (i,j) 到第 k 个参考色的平方欧氏距离
    diff = rgb[:, :, None, :] - refs[None, None, :, :]     # (H, W, 4, 3)
    dist = (diff ** 2).sum(axis=-1)                        # (H, W, 4)
    best = dist.argmin(axis=-1)                            # (H, W)
    best_dist = dist.min(axis=-1)
    hit = valid & (best_dist < DIST_THRESHOLD)
    out[hit] = best[hit] + 1
    return out


# ---------------------------------------------------------------------- 镶嵌

class Mosaic:
    """分级矩阵的镶嵌体：分级数组 + 像素→WGS84 的精确映射。

    属性：
    - status: np.uint8 数组（H×W），0 无数据；
    - level、provider（baidu/amap）、tx0、ty_top（顶部/最北瓦片号）；
    - pixel_to_wgs84(px, py) / wgs84_to_pixel(lon, lat): 像素↔WGS84 精确换算。
    """

    def __init__(self, status: np.ndarray, level: int, tx0: int, ty_top: int,
                 provider: str = "baidu"):
        self.status = status
        self.level = level
        self.provider = provider
        self.tx0 = tx0
        self.ty_top = ty_top                 # 顶部（最北）瓦片号
        self.res = float(2 ** (18 - level)) if provider == "baidu" \
            else 156543.03392804097 / (2 ** level)
        if provider == "amap":
            self.ox = self.oy = None         # 高德走全局像素坐标
        else:
            t = 256 * self.res
            self.ox = tx0 * t                # 左上像素对应的 MC 原点 x
            self.oy = (ty_top + 1) * t       # 顶部（最北）瓦片的上边 MC y

    @property
    def shape(self) -> tuple[int, int]:
        return self.status.shape

    def pixel_to_wgs84(self, px: float, py: float) -> tuple[float, float]:
        """像素坐标（列, 行）→ WGS84。行 0 为北边，y 向南递减。"""
        if self.provider == "amap":
            n = 256 * 2 ** self.level
            gx = self.tx0 * 256 + px
            gy = self.ty_top * 256 + py
            lon_gcj = gx / n * 360.0 - 180.0
            lat_gcj = math.degrees(
                math.atan(math.sinh(math.pi * (1.0 - 2.0 * gy / n))))
            return gcj02_to_wgs84(lon_gcj, lat_gcj)
        mx = self.ox + px * self.res
        my = self.oy - py * self.res
        return bd09_to_wgs84(*mc_to_bd09ll(mx, my))

    def wgs84_to_pixel(self, lon: float, lat: float) -> tuple[float, float]:
        """WGS84 → 像素坐标（列, 行）。pixel_to_wgs84 的逆变换。"""
        if self.provider == "amap":
            n = 256 * 2 ** self.level
            glon, glat = wgs84_to_gcj02(lon, lat)
            gx = (glon + 180.0) / 360.0 * n
            gy = (1.0 - math.asinh(math.tan(math.radians(glat))) / math.pi) / 2.0 * n
            return gx - self.tx0 * 256, gy - self.ty_top * 256
        mx, my = bd09ll_to_mc(*wgs84_to_bd09(lon, lat))
        return (mx - self.ox) / self.res, (self.oy - my) / self.res


def assemble_mosaic(level: int, classified: dict[tuple[int, int], np.ndarray],
                    provider: str = "baidu"):
    """把各瓦片分级矩阵拼成一张大矩阵，返回 Mosaic（含精确像素→WGS84 映射）。

    注意 y 轴方向：百度瓦片 ty 向北递增（ty 大的放顶部/行 0）；高德标准
    XYZ 的 y 向南递增（ty 小的放顶部/行 0）。
    """
    if not classified:
        raise ValueError("没有可拼接的瓦片")
    tx0 = min(tx for tx, _ in classified)
    tx1 = max(tx for tx, _ in classified)
    ty_min = min(ty for _, ty in classified)
    ty_max = max(ty for _, ty in classified)
    w = (tx1 - tx0 + 1) * 256
    h = (ty_max - ty_min + 1) * 256
    status = np.zeros((h, w), dtype=np.uint8)
    for (tx, ty), arr in classified.items():
        x0 = (tx - tx0) * 256
        if provider == "amap":
            y0 = (ty - ty_min) * 256        # y 小在顶部（标准 XYZ）
            ty_top = ty_min
        else:
            y0 = (ty_max - ty) * 256        # ty 大在顶部（百度）
            ty_top = ty_max
        status[y0:y0 + 256, x0:x0 + 256] = arr
    return Mosaic(status, level, tx0, ty_top, provider)


def assemble_raw_mosaic(level: int, raw_pngs: dict[tuple[int, int], bytes],
                        provider: str = "baidu"):
    """原始瓦片 PNG → RGBA 大图（保留原色与透明底），返回 (rgba, Mosaic)。

    与 assemble_mosaic 相同瓦片范围与 y 轴方向规则（百度 ty 向北递增、
    高德标准 XYZ y 向南递增），但不做分类重着色：每张瓦片 convert("RGBA")
    后原样贴入，背景保持透明。Mosaic 定位对象用同尺寸占位矩阵构造，
    供 GeoTIFF/PNG 世界文件定位与范围裁剪复用。
    """
    if not raw_pngs:
        raise ValueError("没有可拼接的原始瓦片")
    tx0 = min(tx for tx, _ in raw_pngs)
    tx1 = max(tx for tx, _ in raw_pngs)
    ty_min = min(ty for _, ty in raw_pngs)
    ty_max = max(ty for _, ty in raw_pngs)
    w = (tx1 - tx0 + 1) * 256
    h = (ty_max - ty_min + 1) * 256
    ty_top = ty_min if provider == "amap" else ty_max
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    for (tx, ty), data in raw_pngs.items():
        x0 = (tx - tx0) * 256
        y0 = (ty - ty_min) * 256 if provider == "amap" else (ty_max - ty) * 256
        tile = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"),
                          dtype=np.uint8)
        rgba[y0:y0 + 256, x0:x0 + 256] = tile
    placeholder = np.zeros((h, w), dtype=np.uint8)   # 仅作定位载体
    return rgba, Mosaic(placeholder, level, tx0, ty_top, provider)


def _inside_mask(mosaic, polygon: BaseGeometry) -> np.ndarray:
    """查询范围多边形 → 像素中心是否在内的布尔掩膜（H×W）。

    边界逐顶点投影到像素空间（投影平滑，城市尺度直线段足够），
    用 matplotlib.path 判断像素中心是否在范围内；逐行计算控制内存。

    MultiPolygon 逐岛单独建 Path 再取并集，避免把多个岛/洞的顶点
    串联进同一 Path 造成自交、奇偶规则误判（曾导致市区核心被整片误删）。
    有洞的 Polygon 用 exterior 减去 interiors 的差集。
    """
    from matplotlib.path import Path

    h, w = mosaic.status.shape
    geoms = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)

    def _to_path(coords):
        return Path([mosaic.wgs84_to_pixel(x, y) for x, y in coords])

    islands = []  # (exterior Path, [hole Paths...])
    for geom in geoms:
        ext = list(geom.exterior.coords)
        if len(ext) < 3:
            continue
        holes = [_to_path(list(ring.coords))
                 for ring in geom.interiors if len(ring.coords) >= 3]
        islands.append((_to_path(ext), holes))

    if not islands:
        return np.ones((h, w), dtype=bool)

    inside = np.zeros((h, w), dtype=bool)
    for y in range(h):
        row_x = np.arange(w, dtype=np.float64) + 0.5
        row_y = np.full(w, y + 0.5, dtype=np.float64)
        pts = np.column_stack([row_x, row_y])
        row_inside = np.zeros(w, dtype=bool)
        for ex_path, hole_paths in islands:
            island = ex_path.contains_points(pts)
            for hp in hole_paths:
                island &= ~hp.contains_points(pts)
            row_inside |= island
        inside[y] = row_inside
    return inside


def clip_to_polygon(mosaic, polygon: BaseGeometry) -> None:
    """原地把镶嵌矩阵裁剪到查询范围：范围外像素置 0。"""
    inside = _inside_mask(mosaic, polygon)
    mosaic.status[~inside] = 0


def clip_rgba_to_polygon(rgba: np.ndarray, mosaic, polygon: BaseGeometry) -> None:
    """原地把 RGBA 大图裁剪到查询范围：范围外 alpha 置 0（变透明）。"""
    inside = _inside_mask(mosaic, polygon)
    rgba[~inside, 3] = 0


def fit_affine(mosaic):
    """像素→WGS84 的最小二乘仿射（供 GeoTIFF/PNG 世界文件定位）。

    在镶嵌体上取 5×5 锚点做精确投影，拟合 6 参数仿射。城市尺度（≤数十 km）
    残差远小于 1 m。返回 rasterio Affine 与 (west, south, east, north) 边界。

    用纯 Python 正规方程求解（避免调用 BLAS，规避某些环境下 lstsq 崩溃）。
    """
    from rasterio.transform import Affine

    h, w = mosaic.status.shape
    cols = np.linspace(0, w, 5)
    rows = np.linspace(0, h, 5)
    xs, ys, lons, lats = [], [], [], []
    for py in rows:
        for px in cols:
            lon, lat = mosaic.pixel_to_wgs84(px, py)
            xs.append(float(px))
            ys.append(float(py))
            lons.append(lon)
            lats.append(lat)

    def _solve(xs, ys, vals):
        # 正规方程：A = [[Sxx,Sxy,Sx],[Sxy,Syy,Sy],[Sx,Sy,N]] · [a,b,c] = [Sxv,Syv,Sv]
        n = len(xs)
        Sx = sum(xs)
        Sy = sum(ys)
        Sxx = sum(x * x for x in xs)
        Syy = sum(y * y for y in ys)
        Sxy = sum(x * y for x, y in zip(xs, ys))
        Sxv = sum(x * v for x, v in zip(xs, vals))
        Syv = sum(y * v for y, v in zip(ys, vals))
        Sv = sum(vals)
        det = (Sxx * (Syy * n - Sy * Sy)
               - Sxy * (Sxy * n - Sy * Sx)
               + Sx * (Sxy * Sy - Syy * Sx))
        if abs(det) < 1e-12:
            return 0.0, 0.0, 0.0
        a = (Sxv * (Syy * n - Sy * Sy)
             - Sxy * (Syv * n - Sy * Sv)
             + Sx * (Syv * Sy - Syy * Sv)) / det
        b = (Sxx * (Syv * n - Sy * Sv)
             - Sxv * (Sxy * n - Sy * Sx)
             + Sx * (Sxy * Sv - Syv * Sx)) / det
        c = (Sxx * (Syy * Sv - Syv * Sy)
             - Sxy * (Sxy * Sv - Syv * Sx)
             + Sxv * (Sxy * Sy - Syy * Sx)) / det
        return a, b, c

    a, b, c = _solve(xs, ys, lons)
    d, e, f = _solve(xs, ys, lats)
    transform = Affine(a, b, c, d, e, f)
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    lls = [transform * (px, py) for px, py in corners]
    west = min(p[0] for p in lls)
    east = max(p[0] for p in lls)
    south = min(p[1] for p in lls)
    north = max(p[1] for p in lls)
    return transform, (west, south, east, north)


def status_to_rgba(status: np.ndarray, provider: str = "baidu") -> np.ndarray:
    """分级矩阵 → RGBA uint8（无数据透明，供 PNG/可视化）。"""
    h, w = status.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    palette = PROVIDERS[provider]["palette"]
    for code in (1, 2, 3, 4):
        r, g, b = palette[code]
        m = status == code
        rgba[m, 0] = r
        rgba[m, 1] = g
        rgba[m, 2] = b
        rgba[m, 3] = 255
    return rgba
