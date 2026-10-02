import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "download", Path(__file__).parents[1] / "scripts" / "01_download.py"
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _mirror(tmp_path, files: dict[str, bytes], recorded: dict[str, bytes] | None = None) -> Path:
    src = tmp_path / "mirror"
    orig = src / "movement-range-maps" / "original"
    orig.mkdir(parents=True)
    sources = []
    for name, data in files.items():
        (orig / name).write_bytes(data)
        rec = (recorded or files)[name]
        sources.append({"resource_id": name, "file": name, "bytes": len(rec),
                        "sha256": hashlib.sha256(rec).hexdigest(), "last_modified": "2026-09-16T20:16:06"})  # fmt: skip
    (src / "manifest.json").write_text(json.dumps({"datasets": {"movement-range-maps": {
        "page": "p", "license_id": "cc-by", "sources": sources}}}))  # fmt: skip
    return src


def test_adopt_takes_the_dataset_entry_and_its_files(tmp_path):
    src = _mirror(tmp_path, {"a.csv": b"x,y\n1,2\n", "readme.pdf": b"%PDF"})
    data = tmp_path / "data"
    entry = m.adopt(src, data)
    assert [s["file"] for s in entry["sources"]] == ["a.csv", "readme.pdf"]
    assert (data / "movement-range-maps" / "original" / "a.csv").read_bytes() == b"x,y\n1,2\n"
    assert (
        json.loads((data / "manifest.json").read_text())["datasets"]["movement-range-maps"]
        == entry
    )


def test_adopt_refuses_a_file_that_is_not_the_recorded_one(tmp_path):
    src = _mirror(tmp_path, {"a.csv": b"changed"}, recorded={"a.csv": b"original"})
    with pytest.raises(SystemExit, match="not the file"):
        m.adopt(src, tmp_path / "data")
