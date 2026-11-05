"""运行元数据与 run 目录。

职责：每次运行独立目录（配置快照 + 原始响应存档 + 成果 + 元数据），杜绝覆盖；
元数据落盘各瓦片采样时刻与数据版本（udt）——实时路况快照的可复现性依赖它。
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime


class RunContext:
    """一次运行的目录与元数据管理。

    目录结构：
        runs/YYYYMMDD_HHMMSS[_slug]/
            config.yaml        配置快照
            progress.csv       瓦片级进度（断点续跑依据）
            raw/               原始瓦片 PNG 存档（离线重放用）
            outputs/           GIS 矢量/CSV/摘要成果
            map.html           交互地图
            metadata.json      运行元数据
    """

    def __init__(self, base_dir: str = "runs", slug: str = "", resume: str | None = None):
        if resume:
            self.dir = resume
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"{stamp}_{slug}" if slug else stamp
            self.dir = os.path.join(base_dir, name)
        os.makedirs(self.outputs_dir, exist_ok=True)
        os.makedirs(self.raw_dir, exist_ok=True)
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.finished_at: str | None = None
        self.extra: dict = {}

    # ------------------------------------------------------------------ 路径

    @property
    def outputs_dir(self) -> str:
        return os.path.join(self.dir, "outputs")

    @property
    def raw_dir(self) -> str:
        return os.path.join(self.dir, "raw")

    @property
    def progress_csv(self) -> str:
        return os.path.join(self.dir, "progress.csv")

    @property
    def map_html(self) -> str:
        return os.path.join(self.dir, "map.html")

    @property
    def metadata_json(self) -> str:
        return os.path.join(self.dir, "metadata.json")

    @property
    def config_snapshot(self) -> str:
        return os.path.join(self.dir, "config.yaml")

    def tile_raw_path(self, tid: str) -> str:
        """tid 形如 "15_6605_1778"（level_x_y，下划线分隔保证文件名安全）。"""
        safe = str(tid).replace("/", "_")
        return os.path.join(self.raw_dir, f"tile_{safe}.png")

    # ------------------------------------------------------------------ 原始响应与进度

    def write_tile_raw(self, tid: str, sampled_at: str, png_bytes: bytes) -> None:
        """瓦片原始 PNG 落盘（旁置采样时刻，供离线重放恢复）。"""
        with open(self.tile_raw_path(tid), "wb") as f:
            f.write(png_bytes)
        with open(self.tile_raw_path(tid) + ".time", "w", encoding="utf-8") as f:
            f.write(sampled_at)

    def read_tile_raw(self, tid: str) -> tuple[bytes, str] | None:
        """读取已存档的瓦片 (png_bytes, sampled_at)；不存在返回 None。"""
        path = self.tile_raw_path(tid)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        sampled = ""
        tp = path + ".time"
        if os.path.exists(tp):
            with open(tp, "r", encoding="utf-8") as f:
                sampled = f.read().strip()
        return data, sampled

    def record_tile(self, tid: str, status: str, sampled_at: str, note: str = "") -> None:
        """追加一行瓦片进度（幂等：续跑时由 completed_tiles 判重，不重复请求）。"""
        new = not os.path.exists(self.progress_csv)
        with open(self.progress_csv, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["tile_id", "status", "sampled_at", "note"])
            w.writerow([tid, status, sampled_at, note])

    def completed_tiles(self) -> set[str]:
        """已成功（status=ok）的瓦片 id 集合——续跑时跳过。"""
        done: set[str] = set()
        if not os.path.exists(self.progress_csv):
            return done
        with open(self.progress_csv, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "ok" and row.get("tile_id"):
                    done.add(row["tile_id"])
        return done

    # ------------------------------------------------------------------ 元数据

    def write_metadata(self, params: dict, stats: dict) -> None:
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        payload = {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "params": params,
            "stats": stats,
            **self.extra,
        }
        with open(self.metadata_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
