#!/usr/bin/env python3
"""Check the originals against their record and the Parquet against the originals.

Movement Range Maps ended in May 2022, so every figure is pinned exactly: a
different one means the files are not the ones this dataset describes.
"""

import hashlib
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATASET = "movement-range-maps"
EXPECTED = {
    "movement_range_2020.parquet": 5229342,
    "movement_range_2021.parquet": 5287242,
    "movement_range_2022.parquet": 1662956,
}
SPAN = ("2020-03-01", "2022-05-22")
JAPAN = (384539, 695)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    failures = []

    def check(ok: bool, what: str) -> None:
        print(("ok    " if ok else "FAIL  ") + what)
        if not ok:
            failures.append(what)

    entry = json.loads((DATA / "manifest.json").read_text())["datasets"][DATASET]
    orig = DATA / DATASET / "original"
    bad = [s["file"] for s in entry["sources"] if sha256(orig / s["file"]) != s["sha256"]]
    check(not bad, f"every original matches the manifest ({len(bad)} do not)")

    con = duckdb.connect()
    for name, n in EXPECTED.items():
        got = con.sql(f"select count(*) from '{DATA / DATASET / name}'").fetchone()[0]
        check(got == n, f"{name}: {got:,} rows, expected {n:,}")
    allp = f"{DATA / DATASET}/*.parquet"
    first, last, jp, jp_ids = con.sql(
        f"select min(ds)::varchar, max(ds)::varchar, count(*) filter (where country = 'JPN'), "
        f"count(distinct polygon_id) filter (where country = 'JPN') from '{allp}'"
    ).fetchone()
    check((first, last) == SPAN, f"days {first} to {last}")
    check((jp, jp_ids) == JAPAN, f"Japan {jp:,} rows, {jp_ids} regions")
    bloom = con.sql(
        f"select count(*) filter (where bloom_filter_offset is not null) from parquet_metadata('{allp}')"
    ).fetchone()[0]
    check(bloom == 0, "no Parquet carries bloom filters")

    if failures:
        print(f"\n{len(failures)} checks failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
