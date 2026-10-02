#!/usr/bin/env python3
"""Fetch the Movement Range Maps originals from HDX, or adopt an earlier mirror's.

Movement Range Maps ended in May 2022: HDX still lists its two zips (2020,
and 2021 to 2022-05-22) and a note on reading them, and nothing has changed
since 2022-05-24. A run fetches what HDX lists and keeps what is here; the
record of every file, with its sha256 and HDX's dates, is data/manifest.json.

    uv run python scripts/01_download.py
    uv run python scripts/01_download.py --from /www/html/static/hdx-meta

--from adopts the files of an earlier mirror (study-geoai-algo-py's
mirror_hdx_meta.py keeps one on z.yuiseki.net): each file is copied only if
it has the size and sha256 recorded when it was fetched.
"""

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATASET = "movement-range-maps"

_spec = importlib.util.spec_from_file_location("hdx_meta", ROOT / "scripts" / "hdx_meta.py")
hdx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hdx)


def load(data: Path) -> dict:
    p = data / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {"datasets": {}}


def save(data: Path, manifest: dict) -> None:
    data.mkdir(parents=True, exist_ok=True)
    tmp = data / "manifest.json.part"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    tmp.replace(data / "manifest.json")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def adopt(src: Path, data: Path) -> dict:
    entry = json.loads((src / "manifest.json").read_text())["datasets"][DATASET]
    orig = data / DATASET / "original"
    orig.mkdir(parents=True, exist_ok=True)
    for s in entry["sources"]:
        p = src / DATASET / "original" / s["file"]
        if p.stat().st_size != s["bytes"] or sha256(p) != s["sha256"]:
            raise SystemExit(f"{p} is not the file its manifest recorded")
        tmp = orig / (s["file"] + ".part")
        shutil.copyfile(p, tmp)
        tmp.replace(orig / s["file"])
    entry = {k: v for k, v in entry.items() if k not in ("outputs", "built")}
    manifest = load(data)
    manifest["datasets"][DATASET] = entry
    save(data, manifest)
    return entry


def update(data: Path) -> dict:
    manifest = load(data)
    previous = manifest["datasets"].get(DATASET, {})
    entry = hdx.fetch_dataset(DATASET, data, previous, dry_run=False)
    manifest["datasets"][DATASET] = previous | entry
    save(data, manifest)
    return manifest["datasets"][DATASET]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", type=Path)
    a = ap.parse_args()
    entry = adopt(a.src, DATA) if a.src else update(DATA)
    on = sum(s.get("on_hdx", True) for s in entry["sources"])
    print(f"{len(entry['sources'])} files kept, {on} still listed on HDX")
    return 0


if __name__ == "__main__":
    sys.exit(main())
