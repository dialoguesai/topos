from __future__ import annotations

import importlib.util
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_smoke_test",
    ROOT / "scripts" / "release_smoke_test.py",
)
assert _SPEC and _SPEC.loader
release_smoke_test = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_smoke_test)


def test_find_wheel_picks_newest(tmp_path):
    (tmp_path / "topos_node-1.0.1-py3-none-any.whl").write_bytes(b"a")
    (tmp_path / "topos_node-1.0.2-py3-none-any.whl").write_bytes(b"b")
    assert release_smoke_test._find_wheel(tmp_path).name == "topos_node-1.0.2-py3-none-any.whl"


def test_previous_pypi_version_returns_latest_older_release():
    payload = {
        "releases": {
            "1.0.0": [{}],
            "1.0.1": [{}],
            "1.0.2": [{}],
            "1.1.0a1": [],
        }
    }
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(payload).encode())
        assert release_smoke_test._previous_pypi_version("1.0.2") == "1.0.1"


def test_previous_pypi_version_none_when_no_older_release():
    payload = {"releases": {"1.0.0": [{}]}}
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(payload).encode())
        assert release_smoke_test._previous_pypi_version("1.0.0") is None
