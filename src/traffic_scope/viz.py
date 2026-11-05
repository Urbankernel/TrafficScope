"""leafmap 交互可视化。

职责：查询范围、路况瓦片网格、路况镶嵌原图（ImageOverlay）、图例与统计
信息面板——全部作为可开关图层进一张地图；默认天地图矢量底图（有 tk 时，
矢量/影像可切换），无 tk 退回
CartoDB Positron（OSM 官方瓦片有 403 封禁策略，不做缺省）；导出自包含 HTML。

leafmap/folium 均为延迟导入，核心流水线不依赖本模块。
图层构建走 folium API（leafmap.Map 是 folium.Map 子类，完全兼容）。
"""

from __future__ import annotations

from shapely.geometry import mapping

from .tiles import PROVIDERS

STATUS_TEXT = {1: "畅通", 2: "缓行", 3: "拥堵", 4: "严重拥堵"}


def web_colors(provider: str) -> dict[int, str]:
    """provider → 状态码 → 网页端十六进制色。"""
    return {code: "#{:02x}{:02x}{:02x}".format(*rgb)
            for code, rgb in PROVIDERS[provider]["palette"].items()}

TIANDITU_TEMPLATES = {
    "vec": ("天地图矢量", "https://t{s}.tianditu.gov.cn/DataServer?T=vec_w&x={x}&y={y}&l={z}&tk={tk}"),
    "cva": ("天地图矢量注记", "https://t{s}.tianditu.gov.cn/DataServer?T=cva_w&x={x}&y={y}&l={z}&tk={tk}"),
    "img": ("天地图影像", "https://t{s}.tianditu.gov.cn/DataServer?T=img_w&x={x}&y={y}&l={z}&tk={tk}"),
    "cia": ("天地图影像注记", "https://t{s}.tianditu.gov.cn/DataServer?T=cia_w&x={x}&y={y}&l={z}&tk={tk}"),
}

_LEGEND_BOX = (
    "position:absolute;bottom:{bottom}px;right:12px;z-index:9999;"
    "background:rgba(255,255,255,0.92);padding:8px 10px;border-radius:6px;"
    "box-shadow:0 1px 4px rgba(0,0,0,0.3);font-size:12px;line-height:1.6;"
    "font-family:'Microsoft YaHei',sans-serif;color:#333;"
)


def _new_map(center_lon: float, center_lat: float, zoom: int):
    try:
        import leafmap.foliumap as leafmap
        return leafmap.Map(center=[center_lat, center_lon], zoom=zoom, tiles=None)
    except Exception:
        import folium
        return folium.Map(location=[center_lat, center_lon], zoom_start=zoom,
                          tiles=None, control_scale=True)


def _carto_tile_url(carto: str = "") -> str:
    """CartoDB Positron 瓦片 URL（无 key 时官方带 "API key required" 水印）。"""
    url = ("https://{s}.basemaps.cartocdn.com/rastertiles/"
           "light_all/{z}/{x}/{y}.png")
    return url + ("?key=" + carto if carto else "")


def _add_basemaps(m, tk: str, tianditu_layer: str = "vec", carto: str = "") -> None:
    import folium

    tianditu_ok = bool(tk)
    folium.TileLayer(
        tiles=_carto_tile_url(carto),
        name="CartoDB Positron",
        attr="© OpenStreetMap contributors © CARTO",
        subdomains="abcd",
        show=not tianditu_ok, overlay=False, control=True,
    ).add_to(m)

    if tianditu_ok:
        for key, show in (("vec", tianditu_layer == "vec"),
                          ("img", tianditu_layer == "img")):
            base_name, base_tpl = TIANDITU_TEMPLATES[key]
            anno_name, anno_tpl = TIANDITU_TEMPLATES["cva" if key == "vec" else "cia"]
            folium.TileLayer(
                tiles=base_tpl.format(tk=tk, s="{s}", x="{x}", y="{y}", z="{z}"),
                name=base_name, attr="天地图", subdomains="01234567",
                show=show, overlay=False, control=True,
            ).add_to(m)
            folium.TileLayer(
                tiles=anno_tpl.format(tk=tk, s="{s}", x="{x}", y="{y}", z="{z}"),
                name=anno_name, attr="天地图", subdomains="01234567",
                show=show, overlay=True, control=True,
            ).add_to(m)


def _legend_html(provider: str) -> str:
    colors = web_colors(provider)
    rows = "".join(
        f'<div style="display:flex;align-items:center;margin:2px 0;">'
        f'<span style="width:18px;height:5px;background:{colors[s]};'
        f'margin-right:6px;border-radius:2px;"></span>'
        f'<span>{STATUS_TEXT[s]}</span></div>'
        for s in (1, 2, 3, 4))
    return (f'<div style="{_LEGEND_BOX.format(bottom=24)}">'
            f'<b>实时路况（网页端同源）</b>{rows}</div>')


def _info_panel_html(summary: dict | None, provider: str) -> str:
    if not summary:
        return ""
    px_by_status = summary.get("pixels_by_status", {})
    stat = " · ".join(f"{k} {v}" for k, v in px_by_status.items())
    label = PROVIDERS.get(provider, PROVIDERS["baidu"])["label"]
    box = (
        "position:absolute;top:12px;left:56px;z-index:9999;max-width:340px;"
        "background:rgba(255,255,255,0.92);padding:8px 10px;border-radius:6px;"
        "box-shadow:0 1px 4px rgba(0,0,0,0.3);font-size:12px;line-height:1.6;"
        "font-family:'Microsoft YaHei',sans-serif;color:#333;")
    return (
        f'<div style="{box}">'
        f'<b>{label}实时路况快照（网页端同源）</b><br>'
        f'采样 {summary.get("sampled_at_min", "")} ~ {summary.get("sampled_at_max", "")}<br>'
        f'路况像素 {summary.get("mosaic_px", 0)} 个'
        f' · 瓦片 {summary.get("tiles_ok", 0)}/{summary.get("tiles_total", 0)}<br>'
        f'<span style="color:#666;">{stat}</span><br>'
        f'<span style="color:#999;font-size:11px;">level={summary.get("level", "")}；'
        f'路况数据权属{label}，仅限自用研究</span></div>')


def build_map(scope, tiles: list[tuple[str, object]],
              mosaic_bounds: tuple[float, float, float, float] | None,
              mosaic_rgba, out_html: str, tk: str = "", carto: str = "",
              summary: dict | None = None, tianditu_layer: str = "vec",
              provider: str = "baidu") -> str:
    """构建并保存交互地图，返回输出路径。

    mosaic_bounds 为 (west, south, east, north)；mosaic_rgba 为 RGBA uint8 数组。
    二者非空时添加"路况原图" ImageOverlay 图层（内嵌 base64，自包含）。
    """
    import folium

    centroid = scope.polygon.centroid
    m = _new_map(centroid.x, centroid.y, 13)
    _add_basemaps(m, tk, tianditu_layer, carto)

    # 路况原图（镶嵌，默认显示）
    if mosaic_rgba is not None and mosaic_bounds is not None:
        from PIL import Image
        import io as _io
        import base64
        buf = _io.BytesIO()
        Image.fromarray(mosaic_rgba, mode="RGBA").save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        west, south, east, north = mosaic_bounds
        folium.raster_layers.ImageOverlay(
            image=f"data:image/png;base64,{b64}",
            bounds=[[south, west], [north, east]],
            name="路况原图（镶嵌）", show=True, opacity=0.9,
        ).add_to(m)

    # 查询范围层
    folium.GeoJson(
        scope.to_geojson(), name="查询范围",
        style_function=lambda _f: {"color": "#e53935", "weight": 2,
                                   "fill": True, "fillOpacity": 0.05},
    ).add_to(m)

    # 瓦片网格层（默认关闭，质检用）
    if tiles:
        fc_tiles = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {"tile_id": k},
                 "geometry": mapping(g)}
                for k, g in tiles],
        }
        folium.GeoJson(
            fc_tiles, name="路况瓦片（质检）", show=False,
            style_function=lambda _f: {"color": "#888888", "weight": 1,
                                       "dashArray": "4 4", "fill": False},
        ).add_to(m)

    m.get_root().html.add_child(folium.Element(_legend_html(provider)))
    panel = _info_panel_html(summary, provider)
    if panel:
        m.get_root().html.add_child(folium.Element(panel))

    folium.LayerControl(collapsed=False).add_to(m)
    m.save(out_html)
    return out_html
