"""keys.txt 密钥读取与 CartoDB URL 拼接测试。"""

import pytest

from traffic_scope.cli import _get_carto_key, _get_key, _get_tianditu_tk
from traffic_scope.viz import _carto_tile_url


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    def _write(content: str) -> str:
        p = tmp_path / "keys.txt"
        p.write_text(content, encoding="utf-8")
        monkeypatch.setattr("traffic_scope.cli.KEYS_FILE", str(p))
        return str(p)

    return _write


def test_get_key_parses_value(keys_file):
    keys_file("# 注释\ntianditu_tk = abc123\ncarto = mycartokey\n")
    assert _get_key("tianditu_tk") == "abc123"
    assert _get_key("carto") == "mycartokey"


def test_get_key_missing_returns_empty(keys_file):
    keys_file("tianditu_tk = abc123\n")
    assert _get_key("carto") == ""


def test_get_carto_key(keys_file):
    keys_file("carto = key123\n")
    assert _get_carto_key() == "key123"


def test_get_tianditu_tk(keys_file):
    keys_file("tianditu_tk = tk123\n")
    assert _get_tianditu_tk() == "tk123"


def test_get_key_ignores_comments_and_blank(keys_file):
    keys_file("# carto = ignored\n\n  carto = real  \n其他key = x\n")
    assert _get_carto_key() == "real"


def test_carto_tile_url_with_key():
    url = _carto_tile_url("abc")
    assert url.endswith("?key=abc")
    assert "rastertiles/light_all" in url
    assert "{z}/{x}/{y}" in url


def test_carto_tile_url_without_key():
    url = _carto_tile_url("")
    assert "?key=" not in url
    assert "rastertiles/light_all" in url
