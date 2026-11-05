"""查询范围（Scope）来源抽象与实现。

所有来源（行政区划 / 矢量文件 / bbox）统一归一为一个 WGS84 面进入下游，
主流程不感知来源。
"""

from .admin import AdminAreaError, fetch_admin_boundary
from .base import ScopeArea, close_ring, normalize_polygon
from .bbox import BboxError, bbox_area
from .fileio import load_vector_area

__all__ = [
    "ScopeArea", "close_ring", "normalize_polygon",
    "fetch_admin_boundary", "AdminAreaError",
    "load_vector_area",
    "bbox_area", "BboxError",
]
