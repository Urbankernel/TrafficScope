"""RunContext：目录结构、瓦片 PNG 存档读写、断点续跑判重。"""

import os
import tempfile

from traffic_scope.metadata import RunContext


def test_run_dir_structure():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = RunContext(base_dir=tmp, slug="admin310110")
        assert os.path.isdir(ctx.outputs_dir)
        assert os.path.isdir(ctx.raw_dir)
        assert "admin310110" in ctx.dir


def test_tile_raw_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = RunContext(base_dir=tmp)
        tid = "15_6605_1778"
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00fake"
        ctx.write_tile_raw(tid, "2026-10-19T01:00:00", png)
        data, sampled = ctx.read_tile_raw(tid)
        assert data == png
        assert sampled == "2026-10-19T01:00:00"
        assert ctx.read_tile_raw("15_0_0") is None
        # 文件名安全：斜杠不进入文件名
        assert "/" not in os.path.basename(ctx.tile_raw_path(tid))


def test_completed_tiles_for_resume():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = RunContext(base_dir=tmp)
        assert ctx.completed_tiles() == set()
        ctx.record_tile("15_1_1", "ok", "t1")
        ctx.record_tile("15_1_2", "failed", "", "网络错误")
        ctx.record_tile("15_1_3", "ok", "t3")
        assert ctx.completed_tiles() == {"15_1_1", "15_1_3"}


def test_metadata_written():
    import json
    with tempfile.TemporaryDirectory() as tmp:
        ctx = RunContext(base_dir=tmp)
        ctx.write_metadata(params={"traffic": {"level": 15}}, stats={"segments": 5})
        with open(ctx.metadata_json, encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["stats"]["segments"] == 5
        assert meta["finished_at"]
