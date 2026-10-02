"""Fetch and build the CC BY datasets of AI for Good at Meta on HDX.

The same code as study-geoai-algo-py's scripts/mirror_hdx_meta.py, which keeps
the z.yuiseki.net mirror; this repository calls its fetch_dataset and
BUILDERS for one dataset only, from scripts/01_download.py and
scripts/03_export_parquet.py, and does not use its main.

HDX (https://data.humdata.org/) answers plain fetches of its pages with 403,
but the CKAN API works with a User-Agent, and every resource URL redirects to
a signed S3 URL. This script keeps every original file under
<dataset>/original/ and builds Parquet from all the originals kept locally:

- movement-distribution: HDX keeps only about 90 days and drops older files,
  so the originals here are never deleted and accumulate across runs
- movement-range-maps: discontinued in 2022; the TSV inside each zip
- commuting-zones: one CSV with WKT polygons; GeoParquet
- facebook-business-activity-trends-during-crisis: five CSVs, one per crisis

Rerunning downloads only what is new or changed and rebuilds the Parquet.

    uv run python scripts/mirror_hdx_meta.py
    uv run python scripts/mirror_hdx_meta.py --dry-run
    uv run python scripts/mirror_hdx_meta.py --fetch-only
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import time
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import IO

API = "https://data.humdata.org/api/3/action/package_show?id={}"
PAGE = "https://data.humdata.org/dataset/{}"
PUBLIC_PREFIX = "https://z.yuiseki.net/static/hdx-meta/"
DEST = Path("/www/html/static/hdx-meta")
SCRATCH = Path("/tmp/study-geoai-mirror-hdx-meta")
UA = "meta-range-maps-2022-05/1 (+https://github.com/yuiseki/meta-range-maps-2022-05)"
# Cloudflare in front of z.yuiseki.net does not cache files above 512 MB and
# then answers the first Range request with the whole body.
MAX_BYTES = 512 * 1024 * 1024
DATASETS = [
    "movement-distribution",
    "movement-range-maps",
    "commuting-zones",
    "facebook-business-activity-trends-during-crisis",
]
EXTENSIONS = {".csv", ".zip", ".pdf", ".txt", ".tsv", ".json", ".xlsx"}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- names and choices -------------------------------------------------------


def _slug(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^0-9a-z_.]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s


def local_name(resource: dict, known: dict[str, str] | None = None) -> str:
    """A tidy file name for a resource; the one it already has if known."""
    if known and resource["id"] in known:
        return known[resource["id"]]
    name = resource["name"].strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() not in EXTENSIONS:
        stem, ext = name, "." + (resource.get("format") or "bin").lower()
    stem = re.sub(r"[._\s]+$", "", stem)
    # keep a doubled hyphen that is part of a date range (2020-03-01--2020-12-31)
    slug = "--".join(_slug(p) for p in stem.split("--"))
    return slug + ext.lower()


def assign_names(resources: Iterable[dict], known: dict[str, str]) -> dict[str, str]:
    """A local name per resource id, unique within a dataset, stable across runs."""
    out = dict(known)
    taken = {v.lower() for v in known.values()}
    for r in resources:
        if r["id"] in out:
            continue
        name = local_name(r)
        if name.lower() in taken:
            stem, ext = os.path.splitext(name)
            name = f"{stem}.{r['id'][:8]}{ext}"
        out[r["id"]] = name
        taken.add(name.lower())
    return out


def is_cc_by(pkg: dict) -> bool:
    return pkg.get("license_id") == "cc-by"


def resources_to_take(pkg: dict) -> list[dict]:
    return [
        r for r in pkg.get("resources", [])
        if r.get("url", "").startswith("https://data.humdata.org/")
    ]  # fmt: skip


def data_member(path: Path) -> str:
    """The one data file of a zip, leaving out README files and __MACOSX junk."""
    with zipfile.ZipFile(path) as z:
        names = [
            n for n in z.namelist()
            if not n.endswith("/") and not n.startswith("__MACOSX/")
            and not Path(n).name.lower().startswith("readme")
        ]  # fmt: skip
    if len(names) != 1:
        raise ValueError(f"{path.name}: {len(names)} data members, not one: {names}")
    return names[0]


def crisis_of(file_name: str) -> str:
    """The crisis of a business activity trends file, from its name."""
    s = file_name.removesuffix(".csv")
    s = re.sub(r"-\d{8}-\d{8}$", "", s)
    return s.removeprefix("business-activity-trends-").removeprefix("crisis-")


# --- counting the source -----------------------------------------------------

MISSING = {"", "NA", "NaN", "nan", "null", "NULL"}


def text_stats(f: IO[bytes], delimiter: str, columns: Iterable[str]) -> dict:
    """Rows, and per column the sum and count of numbers, counted without DuckDB."""
    cols = list(columns)
    text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text, delimiter=delimiter)
    rows = 0
    parts: dict[str, list[float]] = {c: [] for c in cols}
    counts = dict.fromkeys(cols, 0)
    sums = dict.fromkeys(cols, 0.0)
    for row in reader:
        rows += 1
        for c in cols:
            v = row[c]
            if v in MISSING:
                continue
            parts[c].append(float(v))
            counts[c] += 1
            if len(parts[c]) >= 1 << 20:
                sums[c] = math.fsum([sums[c], *parts[c]])
                parts[c] = []
    for c in cols:
        sums[c] = math.fsum([sums[c], *parts[c]])
    text.detach()
    return {"rows": rows, "sums": sums, "counts": counts}


def same_sum(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


# --- fetching ----------------------------------------------------------------


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def package(dataset: str) -> dict:
    pkg = get_json(API.format(dataset))
    if not pkg.get("success"):
        raise RuntimeError(f"package_show {dataset} failed: {pkg}")
    return pkg["result"]


def expected_bytes(size: int | None, prev: dict) -> int | None:
    """The bytes a file kept from an earlier run should have.

    HDX's listed size can be stale (the served file differs by a few bytes);
    what was downloaded and checked last time wins while the listing is
    unchanged.
    """
    if prev.get("bytes") and prev.get("size") == size:
        return prev["bytes"]
    return size


def etag_md5(etag: str | None) -> str | None:
    """The MD5 in an S3 ETag, unless it is a multipart one (which is no MD5)."""
    e = (etag or "").strip('"')
    return e if re.fullmatch(r"[0-9a-f]{32}", e) else None


def fetch(url: str, dest: Path, expect: int | None) -> dict:
    """Download url to dest unless it is already there with the bytes expected.

    The download must have the length the server announced, and its MD5 must
    match the ETag when that is a plain MD5.
    """
    if dest.exists() and expect and dest.stat().st_size == expect:
        return {"skipped": True}
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_name(dest.name + ".part")
    headers = {}
    for attempt in range(3):
        try:
            md5 = hashlib.md5()
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                headers = dict(r.headers)
                for block in iter(lambda: r.read(1 << 20), b""):
                    md5.update(block)
                    f.write(block)
            length = headers.get("Content-Length")
            if length and tmp.stat().st_size != int(length):
                raise OSError(f"got {tmp.stat().st_size} bytes, announced {length}")
            want = etag_md5(headers.get("ETag"))
            if want and md5.hexdigest() != want:
                raise OSError(f"md5 {md5.hexdigest()}, ETag {want}")
            break
        except OSError as e:
            if attempt == 2:
                raise
            log(f"retry {dest.name}: {e}")
            time.sleep(5 * (attempt + 1))
    got = tmp.stat().st_size
    if expect and got != expect:
        log(f"note {dest.name}: {got} bytes served, HDX lists {expect}")
    if dest.exists() and sha256(dest) != sha256(tmp):
        # HDX replaced the file under the same resource: keep the old one aside
        old = dest.parent / "superseded"
        old.mkdir(exist_ok=True)
        stamp = dt.datetime.fromtimestamp(dest.stat().st_mtime).strftime("%Y%m%d")
        os.replace(dest, old / f"{dest.stem}.{stamp}{dest.suffix}")
    tmp.replace(dest)
    return {
        "last_modified": headers.get("Last-Modified"),
        "etag_md5": bool(etag_md5(headers.get("ETag"))),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def now() -> str:
    return dt.datetime.now(dt.UTC).astimezone().replace(microsecond=0).isoformat()


def load_manifest(dest: Path) -> dict:
    p = dest / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {"datasets": {}}


def fetch_dataset(dataset: str, dest: Path, previous: dict, dry_run: bool) -> dict:
    """Bring the originals of one dataset up to date; return its manifest entry."""
    pkg = package(dataset)
    entry = {
        "page": PAGE.format(dataset),
        "title": pkg.get("title"),
        "license_id": pkg.get("license_id"),
        "license_title": pkg.get("license_title"),
        "dataset_date": pkg.get("dataset_date"),
        "metadata_modified": pkg.get("metadata_modified"),
    }
    if not is_cc_by(pkg):
        log(f"{dataset}: license {pkg.get('license_id')}, not cc-by; skipped")
        return entry | {"skipped": "license is not cc-by"}
    orig = dest / dataset / "original"
    old = {s["resource_id"]: s for s in previous.get("sources", [])}
    res = resources_to_take(pkg)
    names = assign_names(res, {k: v["file"] for k, v in old.items()})
    sources = []
    listed = set()
    for r in res:
        listed.add(r["id"])
        f = orig / names[r["id"]]
        size = int(r["size"]) if r.get("size") else None
        if dry_run:
            want = expected_bytes(size, old.get(r["id"], {}))
            state = "have" if f.exists() and f.stat().st_size == want else "get "
            print(f"{state} {dataset}/{f.name} {size} {r['name']!r}")
            continue
        orig.mkdir(parents=True, exist_ok=True)
        prev = old.get(r["id"], {})
        info = fetch(r["url"], f, expected_bytes(size, prev))
        same = info.get("skipped") and prev.get("bytes") == f.stat().st_size
        sources.append({
            "resource_id": r["id"],
            "name": r["name"],
            "file": f.name,
            "url": r["url"],
            "format": r.get("format"),
            "size": size,
            "last_modified": r.get("last_modified"),
            "http_last_modified": info.get("last_modified") or prev.get("http_last_modified"),
            "bytes": f.stat().st_size,
            "sha256": prev["sha256"] if same and prev.get("sha256") else sha256(f),
            "md5_checked": info.get("etag_md5", prev.get("md5_checked")),
            "fetched": prev.get("fetched") if same else now(),
            "on_hdx": True,
        })  # fmt: skip
        log(f"{'have' if info.get('skipped') else 'got '} {dataset}/{f.name}")
    # resources no longer listed on HDX: their files stay, marked as gone
    for rid, s in old.items():
        if rid not in listed:
            sources.append(s | {"on_hdx": False})
    if dry_run:
        return entry
    return entry | {"sources": sources}


# --- building ----------------------------------------------------------------

MD_COLUMNS = [
    "gadm_id", "gadm_name", "country", "polygon_level",
    "home_to_ping_distance_category", "distance_category_ping_fraction", "ds",
]  # fmt: skip
MD_KEY = "gadm_id, polygon_level, home_to_ping_distance_category, ds"
MR_COLUMNS = [
    "ds", "country", "polygon_source", "polygon_id", "polygon_name",
    "all_day_bing_tiles_visited_relative_change", "all_day_ratio_single_tile_users",
    "baseline_name", "baseline_type",
]  # fmt: skip
MR_NUMBERS = MR_COLUMNS[5:7]


def number(col: str) -> str:
    """SQL: a text column as a double; NA and empty are missing, other text fails."""
    return (
        f"case when {col} is null or trim({col}) in ('', 'NA', 'NaN', 'nan', 'null', 'NULL') "
        f"then null else cast({col} as double) end"
    )


def header(path: Path, delimiter: str = ",") -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f, delimiter=delimiter))


def _read(path: Path, delimiter: str = ",") -> str:
    return (
        f"read_csv('{path}', header = true, all_varchar = true, delim = '{delimiter}', "
        "quote = '\"', escape = '\"', strict_mode = true)"
    )


def load_movement_distribution(con, files: list[Path]) -> dict:
    """Load the CSVs into md, a later file winning a row repeated in an earlier one.

    The rows left out go to md_dropped. Returns the rows kept, the rows left
    out and how many of those differ from the row kept.
    """
    parts = []
    for i, f in enumerate(files):
        cols = header(f)
        if cols != MD_COLUMNS:
            raise ValueError(f"{f.name}: columns {cols}, not {MD_COLUMNS}")
        parts.append(f"""
            select gadm_id, gadm_name, country, cast(polygon_level as integer) as polygon_level,
                   home_to_ping_distance_category,
                   {number("distance_category_ping_fraction")} as distance_category_ping_fraction,
                   cast(ds as date) as ds, '{f.name}' as source_file, {i} as file_rank
            from {_read(f)}""")
    con.execute(f"create or replace table md_raw as {' union all '.join(parts)}")
    con.execute(f"""
        create or replace table md_ranked as
        select *, row_number() over (partition by {MD_KEY} order by file_rank desc) as rn
        from md_raw""")  # fmt: skip
    con.execute("""
        create or replace table md as
        select * exclude (rn, file_rank) from md_ranked where rn = 1""")  # fmt: skip
    con.execute("""
        create or replace table md_dropped as
        select * exclude (rn, file_rank) from md_ranked where rn > 1""")  # fmt: skip
    conflicts = con.sql(f"""
        select count(*) from md_dropped d join md k using ({MD_KEY})
        where d.distance_category_ping_fraction is distinct from k.distance_category_ping_fraction
           or d.gadm_name is distinct from k.gadm_name or d.country is distinct from k.country
    """).fetchone()[0]  # fmt: skip
    con.execute("drop table md_ranked; drop table md_raw")
    return {
        "rows": con.sql("select count(*) from md").fetchone()[0],
        "duplicates": con.sql("select count(*) from md_dropped").fetchone()[0],
        "conflicts": conflicts,
    }


def load_movement_range(con, files: list[Path]) -> None:
    parts = []
    for f in files:
        cols = header(f, "\t")
        if cols != MR_COLUMNS:
            raise ValueError(f"{f.name}: columns {cols}, not {MR_COLUMNS}")
        nums = ", ".join(f"{number(c)} as {c}" for c in MR_NUMBERS)
        parts.append(f"""
            select cast(ds as date) as ds, country, polygon_source, polygon_id, polygon_name,
                   {nums}, baseline_name, baseline_type, '{f.name}' as source_file
            from {_read(f, chr(9))}""")
    con.execute(f"create or replace table mr as {' union all '.join(parts)}")


# 33 polygons of the March 2023 CSV stop mid-coordinate at this many characters
WKT_CUT = 32759


def load_commuting_zones(con, path: Path) -> None:
    con.execute(f"""
        create or replace table cz as
        select region, fbcz_id, name, cast(fbcz_id_num as bigint) as fbcz_id_num, cz_gen_ds,
               {number("win_population")} as win_population,
               {number("win_roads_km")} as win_roads_km,
               {number("area")} as area, country, geography,
               try(st_geomfromtext(geography)) as geometry,
               geometry is null as geometry_truncated
        from {_read(path)}""")  # fmt: skip
    # the only WKT that may fail to parse is one cut off at the source's limit
    bad = con.sql(f"""
        select fbcz_id, length(geography) from cz
        where geometry_truncated and length(geography) <> {WKT_CUT}""").fetchall()  # fmt: skip
    if bad:
        raise RuntimeError(f"commuting_zones: unreadable WKT not at the known cut: {bad[:5]}")


def load_business_activity(con, files: list[Path]) -> None:
    parts = []
    for f in files:
        cols = header(f)
        extra = [c for c in ("polygon_level", "polygon_version", "latitude", "longitude")
                 if c not in cols]  # fmt: skip
        fill = "".join(f", null as {c}" for c in extra)
        parts.append(f"""
            select * {fill}, '{crisis_of(f.name)}' as crisis, '{f.name}' as source_file
            from {_read(f)}""")
    con.execute(f"""
        create or replace table bat as
        select crisis, country, polygon_id, polygon_name, polygon_level, polygon_version,
               business_vertical,
               {number("activity_quantile")} as activity_quantile,
               {number("latitude")} as latitude, {number("longitude")} as longitude,
               cast(ds as date) as ds, source_file
        from ({" union all by name ".join(parts)})""")  # fmt: skip


def part_name(stem: str, year: int) -> str:
    return f"{stem}_{year}.parquet"


def write_parquet(con, query: str, path: Path) -> None:
    con.execute(
        f"copy ({query}) to '{path}' (format parquet, compression zstd, row_group_size 100000, write_bloom_filter false)"
    )


def parquet_stats(con, paths: list[Path], numbers: list[str]) -> dict:
    lst = ", ".join(f"'{p}'" for p in paths)
    sel = ", ".join([
        "count(*)",
        *[f"fsum({c})" for c in numbers],
        *[f"count({c})" for c in numbers],
    ])  # fmt: skip
    r = con.sql(f"select {sel} from read_parquet([{lst}])").fetchone()
    n = len(numbers)
    return {
        "rows": r[0],
        "sums": dict(zip(numbers, r[1 : 1 + n], strict=True)),
        "counts": dict(zip(numbers, r[1 + n :], strict=True)),
    }


def verify(name: str, got: dict, want: dict) -> None:
    """Fail unless rows, and the sum and count of every number column, agree."""
    if got["rows"] != want["rows"]:
        raise RuntimeError(f"{name}: {got['rows']} rows, the source has {want['rows']}")
    for c, s in want["sums"].items():
        if got["counts"][c] != want["counts"][c]:
            raise RuntimeError(
                f"{name}: {got['counts'][c]} values of {c}, the source has {want['counts'][c]}"
            )
        if not same_sum(got["sums"][c] or 0.0, s):
            raise RuntimeError(f"{name}: sum of {c} is {got['sums'][c]}, the source has {s}")


def add_stats(a: dict, b: dict) -> dict:
    return {
        "rows": a["rows"] + b["rows"],
        "sums": {c: math.fsum([a["sums"].get(c, 0.0), v]) for c, v in b["sums"].items()},
        "counts": {c: a["counts"].get(c, 0) + v for c, v in b["counts"].items()},
    }


EMPTY = {"rows": 0, "sums": {}, "counts": {}}


def source_stats(paths: list[Path], delimiter: str, numbers: list[str]) -> dict:
    total = EMPTY
    for p in paths:
        with open(p, "rb") as f:
            s = text_stats(f, delimiter, numbers)
        log(f"  {p.name}: {s['rows']:,} rows")
        total = add_stats(total, s)
    return total


def check_size(path: Path) -> int:
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise RuntimeError(f"{path.name}: {size} bytes, above the {MAX_BYTES} Cloudflare limit")
    return size


def by_year(con, table: str, stem: str, order: str, work: Path) -> list[Path]:
    years = [r[0] for r in con.sql(f"select distinct year(ds) from {table} order by 1").fetchall()]
    out = []
    for y in years:
        p = work / part_name(stem, y)
        write_parquet(con, f"select * from {table} where year(ds) = {y} order by {order}", p)
        out.append(p)
    return out


def describe(con, paths: list[Path], numbers: list[str], extra: dict | None = None) -> dict:
    out = {}
    for p in paths:
        st = parquet_stats(con, [p], numbers)
        rng = (
            con.sql(f"select min(ds), max(ds) from '{p}'").fetchone()
            if _has(con, p, "ds")
            else None
        )
        out[p.name] = {
            "rows": st["rows"],
            "bytes": check_size(p),
            **({"ds_min": str(rng[0]), "ds_max": str(rng[1])} if rng else {}),
        }
    if extra:
        next(iter(out.values())).update(extra)
    return out


def _has(con, p: Path, col: str) -> bool:
    return col in [r[0] for r in con.sql(f"select name from parquet_schema('{p}')").fetchall()]


def csvs(sources: list[dict], orig: Path, ext: str = ".csv") -> list[Path]:
    """The kept originals of a kind, oldest first by the time HDX last changed them."""
    kept = [s for s in sources if s["file"].endswith(ext) and (orig / s["file"]).exists()]
    kept.sort(key=lambda s: (s.get("last_modified") or "", s["file"]))
    return [orig / s["file"] for s in kept]


def build_movement_distribution(con, entry: dict, orig: Path, work: Path) -> dict:
    files = csvs(entry["sources"], orig)
    num = ["distance_category_ping_fraction"]
    src = source_stats(files, ",", num)
    info = load_movement_distribution(con, files)
    log(f"movement-distribution: {info}")
    dropped = _table_stats(con, "md_dropped", num)
    paths = by_year(con, "md", "movement_distribution",
                    "country, gadm_id, ds, home_to_ping_distance_category", work)  # fmt: skip
    verify("movement_distribution", add_stats(parquet_stats(con, paths, num), dropped), src)
    overlaps = con.sql("""
        select source_file, count(*) as rows, min(ds)::varchar, max(ds)::varchar
        from md_dropped group by 1 order by 1""").fetchall()  # fmt: skip
    missing = con.sql("""
        select strftime(d, '%Y-%m-%d') from generate_series(
            (select min(ds) from md), (select max(ds) from md), interval 1 day) t(d)
        where d::date not in (select distinct ds from md) order by 1""").fetchall()  # fmt: skip
    split = con.sql("""
        select ds::varchar from md group by ds having count(distinct source_file) > 1
        order by 1""").fetchall()  # fmt: skip
    return {
        "outputs": describe(con, paths, num),
        "source_rows": src["rows"],
        "dedup": info | {"dropped_by_file": [list(r) for r in overlaps]},
        "missing_days": [r[0] for r in missing],
        "days_split_across_files": [r[0] for r in split],
    }


def _table_stats(con, table: str, numbers: list[str]) -> dict:
    sel = ", ".join(
        ["count(*)", *[f"fsum({c})" for c in numbers], *[f"count({c})" for c in numbers]]
    )
    r = con.sql(f"select {sel} from {table}").fetchone()
    n = len(numbers)
    return {
        "rows": r[0],
        "sums": {c: r[1 + i] or 0.0 for i, c in enumerate(numbers)},
        "counts": {c: r[1 + n + i] for i, c in enumerate(numbers)},
    }


def build_movement_range(con, entry: dict, orig: Path, work: Path) -> dict:
    zips = csvs(entry["sources"], orig, ".zip")
    tsvs = []
    for z in zips:
        member = data_member(z)
        t = work / "extracted" / z.stem / Path(member).name
        t.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(z) as zf, zf.open(member) as r, open(t, "wb") as w:
            shutil.copyfileobj(r, w, 1 << 20)
        tsvs.append(t)
        log(f"  {z.name}: {member}")
    src = source_stats(tsvs, "\t", MR_NUMBERS)
    load_movement_range(con, tsvs)
    paths = by_year(con, "mr", "movement_range", "country, polygon_id, ds", work)
    verify("movement_range", parquet_stats(con, paths, MR_NUMBERS), src)
    q = "select count(distinct polygon_id) from mr where polygon_name = 'NA'"
    na = con.sql(q).fetchone()[0]
    return {"outputs": describe(con, paths, MR_NUMBERS), "source_rows": src["rows"],
            "polygon_name_na_ids": na}  # fmt: skip


def build_commuting_zones(con, entry: dict, orig: Path, work: Path) -> dict:
    (f,) = csvs(entry["sources"], orig)
    num = ["win_population", "win_roads_km", "area"]
    src = source_stats([f], ",", num)
    load_commuting_zones(con, f)
    p = work / "commuting_zones.parquet"
    write_parquet(con, "select * from cz order by country, fbcz_id", p)
    verify("commuting_zones", parquet_stats(con, [p], num), src)
    cut, invalid = con.sql(f"""
        select count(*) filter (where geometry_truncated),
               count(*) filter (where not st_isvalid(geometry)) from '{p}'""").fetchone()
    log(f"commuting_zones: {cut} truncated WKT, {invalid} invalid geometries")
    extra = {"truncated_wkt": cut, "invalid_geometries": invalid}
    return {"outputs": describe(con, [p], num, extra), "source_rows": src["rows"]}


def build_business_activity(con, entry: dict, orig: Path, work: Path) -> dict:
    files = csvs(entry["sources"], orig)
    num = ["activity_quantile"]
    src = source_stats(files, ",", num)
    load_business_activity(con, files)
    p = work / "business_activity_trends.parquet"
    write_parquet(con, "select * from bat order by crisis, country, polygon_id, "
                       "business_vertical, ds", p)  # fmt: skip
    verify("business_activity_trends", parquet_stats(con, [p], num), src)
    per = con.sql("""select crisis, count(*), min(ds)::varchar, max(ds)::varchar,
                            string_agg(distinct country, ' ' order by country)
                     from bat group by 1 order by 1""").fetchall()  # fmt: skip
    return {"outputs": describe(con, [p], num), "source_rows": src["rows"],
            "crises": [list(r) for r in per]}  # fmt: skip


BUILDERS = {
    "movement-distribution": build_movement_distribution,
    "movement-range-maps": build_movement_range,
    "commuting-zones": build_commuting_zones,
    "facebook-business-activity-trends-during-crisis": build_business_activity,
}


# --- README and LICENSE ------------------------------------------------------


def notes_md(e: dict) -> str:
    d = e.get("dedup", {})
    files = d.get("dropped_by_file") or []
    per = (
        "、".join(
            f"{f} の {n:,} 行 ({lo if lo == hi else f'{lo}〜{hi}'})" for f, n, lo, hi in files
        )
        or "無し"
    )
    gone = [s["file"] for s in e.get("sources", []) if not s.get("on_hdx")]
    return f"""住んでいる場所 (夜にいることが多い場所) から、その日どれだけ離れたかの分布。行政区域 (GADM) × 日 × 距離の区分 (`0`、`(0, 10)`、`[10, 100)`、`100+`、単位は km) ごとに、その区分の人の割合 (`distance_category_ping_fraction`) を持つ。作り方と注意は [README の PDF](movement-distribution/original/movement-distribution-readme-data-for-good-at-meta.pdf) にある。

- HDX には直近の約 90 日しか残らない。ここでは HDX から消えた元のファイルも消さずに残し、手元にある CSV すべてから Parquet を作り直す。HDX から消えたもの: {"、".join(gone) or "まだ無し"}。
- 期間の境目の日が 2 つのファイルに入っていることがある。同じ区域・区分・日の行が複数のファイルにあるときは、HDX の更新日が新しいファイルの行を残した。落とした行は {d.get("duplicates", 0):,} 行で、そのうち値が残した行と違うものは {d.get("conflicts", 0):,} 行 (内訳: {per})。どのファイルの行かは `source_file` 列にある。
- 同じ日が 2 つのファイルに分かれて入っていることもある ({"、".join(e.get("days_split_across_files", [])) or "無し"})。区域は重ならないので、両方を残している。
- ファイルの名前が示す期間の日がすべてあるとは限らない。{e["outputs"] and next(iter(e["outputs"].values())).get("ds_min", "")}〜{e["outputs"] and list(e["outputs"].values())[-1].get("ds_max", "")} のうち、無い日は {len(e.get("missing_days", []))} 日 ({"、".join(d[5:] for d in e.get("missing_days", [])) or "無し"})。どのファイルにも入っていない (例: 2026-08-18〜08-31 のファイルにある日は 5 日だけ)。
- 雑音が足されているため、割合が負の行があり、4 区分の和も 1 にならない。値はそのまま置いている。
- `polygon_level` は 0 (国)、1、2 の整数。`gadm_id` は文字列。
- 年ごとに 1 ファイル (`movement_distribution_<年>.parquet`)。country、gadm_id、ds、区分の順に並べてある。"""


def notes_mr(e: dict) -> str:
    return f"""COVID-19 の外出自粛への反応を見るためのデータで、2022-05-22 で更新を終えている。行政区域 × 日ごとに、訪れた Bing タイルの数の基準 (2020 年 2 月の曜日ごとの値) からの変化率 (`all_day_bing_tiles_visited_relative_change`) と、1 日中 1 つのタイルから出なかった人の割合 (`all_day_ratio_single_tile_users`) を持つ。列の説明は [how-to-understand-this-data.txt](movement-range-maps/original/how-to-understand-this-data.txt) にある (2 つの zip の中の README.txt と同じもの)。

- 2 つの zip の中の TSV 1 本ずつ (2020-03-01〜2020-12-31 と 2021-01-01〜2022-05-22) を読み、年ごとに 1 ファイルにした。期間の重なりは無く、同じ区域・日の行も無い。
- 区域は GADM (`polygon_source` が `GADM`) と、米国の郡の FIPS (`FIPS`)。基準は GADM が `full_february`、FIPS が `full_february_except_presidents_day`。
- 名前に長音記号などの入る区域は `polygon_name` が文字列の `NA` になっている (元のとおり。{e.get("polygon_name_na_ids", 0):,} 区域、台東区 `JPN.41.51_1`、江東区、文京区など)。結合は `polygon_id` で行うこと。
- 2020 年の zip は HDX の API が示す大きさ (56,561,599 bytes) と実際に配られるファイル (56,560,052 bytes) が違う。配られたものの MD5 が S3 の ETag と一致することを確かめて置いている。
- 2020 年の zip にある `__MACOSX/` は読んでいない (元の zip はそのまま置いてある)。
- country、polygon_id、ds の順に並べてある。"""


def notes_cz(e: dict) -> str:
    i = next(iter(e.get("outputs", {}).values()), {})
    return f"""通勤圏 (Commuting Zones)。人の移動のまとまりから求めた、都市とその周りの通勤の範囲のポリゴン (2023 年 3 月版)。1 行が 1 つの通勤圏で、推定人口 (`win_population`)、道路の長さ (`win_roads_km`)、面積 (`area`) を持つ。

- GeoParquet。元の CSV の WKT (`geography` 列、WGS 84) から `geometry` 列を作った。WKT の文字列も元のまま残してある。
- {i.get("truncated_wkt", 0)} 個の通勤圏 (Juneau、Zadar、Stanley など、海岸線の細かいもの) は、元の CSV の WKT が {WKT_CUT:,} 文字で切れていて、ポリゴンとして読めない。これらは `geometry` が NULL で、`geometry_truncated` が true。表計算ソフトの 1 セルの上限 (32,767 文字) で切れたものと思われる。
- ほかに {i.get("invalid_geometries", 0)} 個の通勤圏の geometry が ST_IsValid で false になる (そのまま置いている)。
- `win_population` は上下が切りそろえてあるように見える (winsorize、列名の win はこれと思われる)。最大値 4,442,659.362 と最小値 1,924.466 に 328 ずつ (全体の 5%) の通勤圏が並び、日本でも名古屋、大阪、千葉、横浜、さいたまが同じ値になる。大都市の人口の比較には使えない。
- `cz_gen_ds` は元のとおりの文字列 (`3/5/23`、2023-03-05 のことと思われる)。`fbcz_id_num` は整数、`fbcz_id` は文字列。
- country (英語の国名)、fbcz_id の順に並べてある。"""


def notes_bat(e: dict) -> str:
    rows = "\n".join(
        f"| {c} | {n:,} | {lo}〜{hi} | {countries} |"
        for c, n, lo, hi, countries in e.get("crises", [])
    )
    return f"""災害の前後で、Facebook のビジネスページの活動がどう変わったか。行政区域 × 業種 (`business_vertical`) × 日ごとに、その日の活動が平時の分布のどの分位にあたるか (`activity_quantile`) を持つ。5 つの災害の CSV を 1 つにまとめ、`crisis` 列 (ファイル名から取った災害の名前) と `source_file` 列を足した。

| crisis | 行数 | 期間 | country |
|---|---:|---|---|
{rows}

- ファイルによって列が違う。ブラジルの洪水のファイルだけ `polygon_level` (`GADM2`)、`polygon_version` (`3.6`)、`latitude`、`longitude` を持ち、ほかのファイルではこれらは NULL。
- ブラジルのファイルの country は 2 文字の `BR`、ほかは 3 文字 (`USA`、`ROU` など)。元のとおり。
- 業種の無い行が、ブラジルのファイルでは空 (NULL)、ほかのファイルでは文字列の `NA` になっている。`All` とは別の行。元のとおり。
- hurricane-helene、hurricane-beryl、la_wildfires-usa の 3 ファイルには、同じ区域・業種・日の行が 2 つ以上あるものが多く、値も少しずつ違う。どちらが正しいかは分からないので、両方そのまま置いている。
- crisis、country、polygon_id、business_vertical、ds の順に並べてある。"""


NOTES_MD, NOTES_MR, NOTES_CZ, NOTES_BAT = notes_md, notes_mr, notes_cz, notes_bat

CHANGES_MD = """    - The CSVs were combined into one Parquet file per year, with a
      source_file column naming the original file.
    - Where the same gadm_id, polygon_level, distance category and ds is in
      more than one CSV (the files overlap on the days at their ends), only
      the row from the file HDX changed last was kept; the counts are in
      manifest.json.
    - polygon_level became an integer, distance_category_ping_fraction a
      double and ds a date."""
CHANGES_MR = """    - The TSV inside each zip file was read (README.txt and the __MACOSX
      entries were left out) and the rows were split into one Parquet file
      per year, with a source_file column naming the TSV.
    - ds became a date and the two measures doubles; polygon_name keeps the
      string NA of the source."""
CHANGES_CZ = """    - Converted to GeoParquet: a geometry column (WGS 84) was made from the
      WKT in the geography column, which is kept. The WKT of some zones is
      cut off in the source and cannot be read; their geometry is NULL and
      geometry_truncated is true.
    - fbcz_id_num became an integer and win_population, win_roads_km and
      area doubles."""
CHANGES_BAT = """    - The five CSVs were combined into one Parquet file, with columns that
      only some files have filled with NULL, and a crisis column (from the
      file name) and a source_file column added.
    - activity_quantile, latitude and longitude became doubles and ds a
      date."""


TITLES = {
    "movement-distribution": "Movement Distribution",
    "movement-range-maps": "Movement Range Maps",
    "commuting-zones": "Commuting Zones",
    "facebook-business-activity-trends-during-crisis": "Business Activity Trends during Crisis",
}
# What each dataset is and what was found in it, in the README's language.
ABOUT = {
    "movement-distribution": NOTES_MD,
    "movement-range-maps": NOTES_MR,
    "commuting-zones": NOTES_CZ,
    "facebook-business-activity-trends-during-crisis": NOTES_BAT,
}
CHANGES = {
    "movement-distribution": CHANGES_MD,
    "movement-range-maps": CHANGES_MR,
    "commuting-zones": CHANGES_CZ,
    "facebook-business-activity-trends-during-crisis": CHANGES_BAT,
}


def credit(ds: str) -> str:
    return f"Data for Good at Meta, {TITLES[ds]}, Humanitarian Data Exchange ({PAGE.format(ds)})"


def render_license(manifest: dict) -> str:
    kept = [ds for ds in DATASETS if manifest["datasets"].get(ds, {}).get("outputs")]
    credits = "\n".join(f"  {credit(ds)}" for ds in kept)
    changes = "\n\n".join(f"  {TITLES[ds]} ({ds}/):\n{CHANGES[ds]}" for ds in kept)
    return f"""LICENSE

The files in this directory are copies and adaptations of datasets published
by Data for Good at Meta (now AI for Good at Meta) on the Humanitarian Data
Exchange (HDX):

{chr(10).join(f"  {PAGE.format(ds)}" for ds in kept)}

HDX lists each of these datasets under the Creative Commons Attribution
International licence (CC BY, license_id cc-by); they are used here under
CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Each dataset's
licence was read from the HDX API when it was fetched and is recorded in
manifest.json; a dataset whose licence is not cc-by is not mirrored.

Credit:

{credits}

The files under each <dataset>/original/ directory are the files as
downloaded from HDX, unchanged except for their names (a tidy name with an
extension; the name on HDX is in manifest.json). Files that HDX has since
removed are kept.

The Parquet files were changed from the originals ({manifest["built"][:10]}):

{changes}

In every Parquet file, no value was changed: text stays as in the source,
numbers and dates were only given a type. Rows were sorted. The row count and
the sum of every number column were checked against the originals.

These files are provided as is, without warranty of any kind. This server is
not affiliated with Meta or HDX.
"""


def _outputs_table(ds: str, entry: dict) -> str:
    rows = "\n".join(
        f"| [{f}]({ds}/{f}) | {i['rows']:,} | {i['bytes']:,} | "
        f"{i.get('ds_min', '')}〜{i.get('ds_max', '')} |".replace("| 〜 |", "| |")
        for f, i in entry["outputs"].items()
    )
    return "| ファイル | 行数 | 大きさ (bytes) | 期間 |\n|---|---:|---:|---|\n" + rows


def _sources_table(ds: str, entry: dict) -> str:
    rows = "\n".join(
        f"| [{s['file']}]({ds}/original/{s['file']}) | {s['name']} | {s['bytes']:,} | "
        f"{(s.get('last_modified') or '')[:10]} | {'あり' if s.get('on_hdx') else '消えた'} |"
        for s in sorted(entry["sources"], key=lambda s: s["file"])
    )
    return (
        "| ファイル | HDX での名前 | 大きさ (bytes) | HDX の更新日 | HDX に |\n"
        "|---|---|---:|---|---|\n" + rows
    )


def render_readme(manifest: dict) -> str:
    manifest = manifest | {"datasets": {d: manifest["datasets"][d] for d in DATASETS
                                        if d in manifest["datasets"]}}  # fmt: skip
    sections = []
    index = []
    for ds, e in manifest["datasets"].items():
        if not e.get("outputs"):
            if e.get("skipped"):
                index.append(f"- {TITLES[ds]}: 置いていない ({e['skipped']})")
            continue
        index.append(f"- [{TITLES[ds]}](#{ds}) ({ds}/)")
        sections.append(f"""## {ds}

{TITLES[ds]} ([HDX]({e["page"]}))。ライセンスは HDX の記載で {e["license_title"]}。

{ABOUT[ds](e)}

### Parquet

{_outputs_table(ds, e)}

### 元のファイル ({ds}/original/)

{_sources_table(ds, e)}
""")
    total_orig = sum(
        s["bytes"] for e in manifest["datasets"].values() for s in e.get("sources", [])
    )
    total_pq = sum(
        i["bytes"] for e in manifest["datasets"].values() for i in e.get("outputs", {}).values()
    )
    return f"""# AI for Good at Meta の HDX データのミラー

AI for Good at Meta (旧 Data for Good at Meta) が [Humanitarian Data Exchange (HDX)](https://data.humdata.org/organization/meta) で配っている CC BY のデータセットのうち 4 つを、元のファイルのままと、HTTP Range 要求で必要な列と行だけ読める Parquet の両方で置いたもの。

{chr(10).join(index)}

元のファイルは合計 {total_orig:,} bytes、Parquet は合計 {total_pq:,} bytes。

## 出典の表示

HDX の記載どおり CC BY (Creative Commons Attribution International) で、ここでは CC BY 4.0 として扱う。利用する際は、データセットごとに次のように表示すること。

{chr(10).join(f"- {credit(ds)}" for ds, e in manifest["datasets"].items() if e.get("outputs"))}

加工の中身は [LICENSE](LICENSE) にある。

## 読み方

どの Parquet も zstd で圧縮し、国と区域と日付の順に並べてある (Business Activity Trends は災害ごと)。DuckDB の httpfs なら、国で絞ると読む行グループが少なくて済む。

```sql
-- 台東区の Movement Distribution
select ds, home_to_ping_distance_category, distance_category_ping_fraction
from 'https://z.yuiseki.net/static/hdx-meta/movement-distribution/movement_distribution_2026.parquet'
where country = 'JPN' and gadm_id = 'JPN.41.51_1' order by ds;

-- 年ごとのファイルは並べて読む (HTTP の URL には * が使えない)
select * from read_parquet([
  'https://z.yuiseki.net/static/hdx-meta/movement-range-maps/movement_range_2020.parquet',
  'https://z.yuiseki.net/static/hdx-meta/movement-range-maps/movement_range_2021.parquet',
  'https://z.yuiseki.net/static/hdx-meta/movement-range-maps/movement_range_2022.parquet'
]) where country = 'JPN' and polygon_id = 'JPN.41.51_1';
```

どのファイルも 512MB 未満にしてある (それを超えると Cloudflare がキャッシュせず、初回の Range 要求に全体を返す)。Movement Distribution は年ごとにファイルが分かれ、年が変わると `movement_distribution_2027.parquet` のように増える。

{chr(10).join(sections)}
## 更新

取得は {manifest["built"]}。Movement Distribution は HDX に直近の約 90 日しか残らず、2 週間ごとに新しいファイルが足されて古いものが消える。取得スクリプト (study-geoai-algo-py の scripts/mirror_hdx_meta.py) を流し直すと、新しいファイルだけを落とし、HDX から消えたファイルも消さずに残したまま、手元にある元のファイルすべてから Parquet を作り直す。

新しく落としたファイルは、サーバーの示す長さと、S3 の ETag が MD5 のときはその MD5 と一致することを確かめてから置く。記録の本体は [manifest.json](manifest.json) (resource id、HDX での名前と URL と大きさ、更新日、sha256、取得時刻)。この README と LICENSE は、スクリプトを流すたびに作り直される。
"""


# --- main --------------------------------------------------------------------


def place(files: list[Path], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        tmp = dest / (f.name + ".part")
        shutil.copyfile(f, tmp)
        os.replace(tmp, dest / f.name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", type=Path, default=DEST)
    ap.add_argument("--scratch", type=Path, default=SCRATCH)
    ap.add_argument("--dry-run", action="store_true", help="list what would be fetched")
    ap.add_argument("--fetch-only", action="store_true", help="update the originals only")
    ap.add_argument("--build-only", action="store_true", help="rebuild from the originals kept")
    ap.add_argument("--only", nargs="*", default=DATASETS, help="datasets to handle")
    args = ap.parse_args()

    manifest = load_manifest(args.dest)
    if not args.build_only:
        for ds in args.only:
            entry = fetch_dataset(ds, args.dest, manifest["datasets"].get(ds, {}), args.dry_run)
            if args.dry_run:
                continue
            manifest["datasets"][ds] = manifest["datasets"].get(ds, {}) | entry
            # record the originals as soon as they are in place
            write_manifest(args.dest, manifest)
    if args.dry_run or args.fetch_only:
        return

    import duckdb

    # a new build directory per run: nothing under the scratch path is deleted
    work = args.scratch / f"build-{dt.datetime.now():%Y%m%dT%H%M%S}"
    (work / "duckdb-tmp").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("set memory_limit = '6GB'; set preserve_insertion_order = false")
    con.execute(f"set temp_directory = '{work / 'duckdb-tmp'}'")
    con.execute("install spatial; load spatial")
    for ds in args.only:
        entry = manifest["datasets"].get(ds, {})
        if entry.get("skipped") or not entry.get("sources"):
            log(f"{ds}: nothing to build")
            continue
        wd = work / ds
        wd.mkdir()
        log(f"{ds}: building")
        result = BUILDERS[ds](con, entry, args.dest / ds / "original", wd)
        place([wd / f for f in result["outputs"]], args.dest / ds)
        entry.update(result | {"built": now()})
        for f, i in result["outputs"].items():
            log(f"{ds}/{f}: {i['rows']:,} rows, {i['bytes']:,} bytes")
        manifest["built"] = now()
        write_manifest(args.dest, manifest)
    manifest["built"] = now()
    write_manifest(args.dest, manifest)
    (args.dest / "LICENSE").write_text(render_license(manifest))
    (args.dest / "README.md").write_text(render_readme(manifest))
    log(f"placed in {args.dest}")


def write_manifest(dest: Path, manifest: dict) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / "manifest.json.part"
    known = manifest["datasets"]
    order = [d for d in DATASETS if d in known] + [d for d in known if d not in DATASETS]
    manifest["datasets"] = {d: known[d] for d in order}
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    os.replace(tmp, dest / "manifest.json")


if __name__ == "__main__":
    main()
