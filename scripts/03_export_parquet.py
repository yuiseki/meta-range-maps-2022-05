#!/usr/bin/env python3
"""Build the Movement Range Maps Parquet from the two zips.

Each zip holds one tab separated file. The build reads both, writes one
table a year (2020, 2021, 2022), and checks rows and the sums of the two
numeric columns against the files before anything is kept. The details are
hdx_meta.py's build_movement_range, the code the z.yuiseki.net mirror uses.

    uv run python scripts/03_export_parquet.py
"""

import datetime as dt
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


def main() -> int:
    import duckdb

    manifest = json.loads((DATA / "manifest.json").read_text())
    entry = manifest["datasets"][DATASET]
    work = DATA / "work" / dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    (work / "duckdb-tmp").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("set memory_limit = '6GB'; set preserve_insertion_order = false")
    con.execute(f"set temp_directory = '{work / 'duckdb-tmp'}'")
    result = hdx.BUILDERS[DATASET](con, entry, DATA / DATASET / "original", work)
    out = DATA / DATASET
    for name in result["outputs"]:
        tmp = out / (name + ".part")
        shutil.copyfile(work / name, tmp)
        tmp.replace(out / name)
    manifest["datasets"][DATASET] = entry | result | {"built": hdx.now()}
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    shutil.rmtree(work)
    for name, info in result["outputs"].items():
        print(f"{name}: {info['rows']:,} rows, {info['bytes']:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
