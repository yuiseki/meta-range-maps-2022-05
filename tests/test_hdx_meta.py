import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "hdx_meta", Path(__file__).parents[1] / "scripts" / "hdx_meta.py"
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _res(name, fmt="CSV", rid="fa8f788d-1279-4714-a9a7-090a9e37a1f3", url=None):
    return {
        "id": rid,
        "name": name,
        "format": fmt,
        "url": url or f"https://data.humdata.org/dataset/d/resource/{rid}/download/f",
    }


# --- file names --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "fmt", "want"),
    [
        # no extension at all: the format gives it one
        ("Movement Distribution 1 June - 15 June, 2026", "CSV",
         "movement-distribution-1-june-15-june-2026.csv"),
        # a doubled dot before the extension
        ("Movement Distribution 2026-07-17_to_2026-08-01..csv", "CSV",
         "movement-distribution-2026-07-17_to_2026-08-01.csv"),
        # a trailing underscore before the extension
        ("Meta Movement Distribution Maps_2026-08-18_to_2026-08-31_.csv", "CSV",
         "meta-movement-distribution-maps_2026-08-18_to_2026-08-31.csv"),
        # the listed format is wrong (TXT for a zip): the name's extension wins
        ("movement-range-data-2020-03-01--2020-12-31.zip", "TXT",
         "movement-range-data-2020-03-01--2020-12-31.zip"),
        ("How To Understand This Data.txt", "TXT", "how-to-understand-this-data.txt"),
        ("Movement Distribution Readme - Data for Good at Meta.pdf", "PDF",
         "movement-distribution-readme-data-for-good-at-meta.pdf"),
        ("business-activity-trends-crisis-flooding-rio-grande-do-sul-brazil - 20240501-20240709.csv",
         "CSV", "business-activity-trends-crisis-flooding-rio-grande-do-sul-brazil-20240501-20240709.csv"),
        ("Movement Distribution 2026_June_27-2026_July_01.csv", "CSV",
         "movement-distribution-2026_june_27-2026_july_01.csv"),
    ],
)  # fmt: skip
def test_local_name_is_a_tidy_name_with_an_extension(name, fmt, want):
    assert m.local_name(_res(name, fmt)) == want


def test_local_name_keeps_the_name_a_resource_was_first_stored_under():
    r = _res("Renamed on HDX later.csv")
    known = {r["id"]: "first-name.csv"}
    assert m.local_name(r, known) == "first-name.csv"


def test_local_names_do_not_collide():
    a = _res("Same.csv", rid="aaaaaaaa-0000")
    b = _res("same.csv", rid="bbbbbbbb-0000")
    got = m.assign_names([a, b], {})
    assert got[a["id"]] == "same.csv"
    assert got[b["id"]] == "same.bbbbbbbb.csv"


def test_assign_names_keeps_known_names_and_avoids_them_for_new_ones():
    a = _res("x.csv", rid="aaaaaaaa-0000")
    b = _res("x.csv", rid="bbbbbbbb-0000")
    # a is gone from HDX but its file stays; b must not take its name
    got = m.assign_names([b], {a["id"]: "x.csv"})
    assert got[b["id"]] == "x.bbbbbbbb.csv"


# --- which datasets and resources --------------------------------------------


def test_only_cc_by_packages_are_mirrored():
    assert m.is_cc_by({"license_id": "cc-by"})
    assert not m.is_cc_by({"license_id": "hdx-other"})
    assert not m.is_cc_by({"license_id": "other-pd-nr"})
    assert not m.is_cc_by({})


def test_resources_to_take_skips_those_without_a_download_url():
    pkg = {
        "resources": [
            _res("a.csv", rid="1"),
            {"id": "2", "name": "b", "format": "CSV", "url": ""},
            {"id": "3", "name": "api", "format": "Web App", "url": "https://example.org/app"},
        ]
    }
    assert [r["id"] for r in m.resources_to_take(pkg)] == ["1"]


# --- zips --------------------------------------------------------------------


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_the_data_member_of_a_zip_ignores_readme_and_macos_junk(tmp_path):
    p = tmp_path / "a.zip"
    p.write_bytes(_zip({
        "movement-range-2020.txt": b"ds\tcountry\n2020-03-01\tJPN\n",
        "README.txt": b"about",
        "__MACOSX/._movement-range-2020.txt": b"junk",
        "__MACOSX/._README.txt": b"junk",
    }))  # fmt: skip
    assert m.data_member(p) == "movement-range-2020.txt"


def test_the_data_member_must_be_unique(tmp_path):
    p = tmp_path / "a.zip"
    p.write_bytes(_zip({"a.txt": b"1", "b.txt": b"2"}))
    with pytest.raises(ValueError, match="data member"):
        m.data_member(p)


# --- counting the source -----------------------------------------------------


def test_text_stats_counts_rows_and_sums_a_column():
    data = b"ds,x,y\n2026-06-01,0.5,a\n2026-06-02,-0.25,b\n2026-06-03,NA,c\n"
    got = m.text_stats(io.BytesIO(data), ",", ["x"])
    assert got["rows"] == 3
    assert got["sums"]["x"] == pytest.approx(0.25)
    assert got["counts"]["x"] == 2


def test_text_stats_reads_tabs_bom_and_quoted_fields():
    data = '﻿id\tname\tv\n"A.1_1"\t"Taitō"\t1\nB\t"a, b"\t2\n'.encode()
    got = m.text_stats(io.BytesIO(data), "\t", ["v"])
    assert got["rows"] == 2
    assert got["sums"]["v"] == 3


def test_text_stats_treats_an_empty_field_as_missing():
    data = b"v\n1\n\n2\n"
    # a blank line is not a row; an empty field in a row is missing
    got = m.text_stats(io.BytesIO(b"k,v\na,1\nb,\nc,2\n"), ",", ["v"])
    assert got["rows"] == 3 and got["counts"]["v"] == 2
    assert m.text_stats(io.BytesIO(data), ",", ["v"])["rows"] == 2


def test_same_sum_allows_float_rounding_only():
    assert m.same_sum(1234567.123456789, 1234567.1234567892)
    assert not m.same_sum(1234567.12, 1234567.13)
    assert m.same_sum(0.0, 0.0)


# --- business activity ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("business-activity-trends-crisis-flooding-rio-grande-do-sul-brazil-20240501-20240709.csv",
         "flooding-rio-grande-do-sul-brazil"),
        ("business-activity-trends-crisis-hurricane-helene-20240926-20241021.csv", "hurricane-helene"),
        ("business-activity-trends-crisis-la_wildfires-usa-20250107-20250310.csv", "la_wildfires-usa"),
        ("business-activity-trends-flooding-central-eastern-europe-20240916-20241013.csv",
         "flooding-central-eastern-europe"),
    ],
)  # fmt: skip
def test_crisis_of_a_business_activity_file(name, want):
    assert m.crisis_of(name) == want


# --- building (DuckDB, on tiny files) ------------------------------------------

MD_HEADER = (
    "gadm_id,gadm_name,country,polygon_level,home_to_ping_distance_category,"
    "distance_category_ping_fraction,ds\n"
)


def _md(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(MD_HEADER + "".join(rows))
    return p


def _con():
    import duckdb

    return duckdb.connect()


def test_movement_distribution_types_and_keeps_negative_fractions(tmp_path):
    a = _md(tmp_path, "a.csv", [
        'JPN.41.51_1,Taitō,JPN,2,0,0.35,2026-07-13\n',
        'JPN.41.51_1,Taitō,JPN,2,"(0, 10)",-0.01,2026-07-13\n',
    ])  # fmt: skip
    con = _con()
    got = m.load_movement_distribution(con, [a])
    assert got == {"rows": 2, "duplicates": 0, "conflicts": 0}
    types = dict(con.sql("select column_name, column_type from (describe md)").fetchall())
    assert types["ds"] == "DATE"
    assert types["distance_category_ping_fraction"] == "DOUBLE"
    assert types["polygon_level"] == "INTEGER"
    assert types["gadm_id"] == "VARCHAR"
    rows = con.sql(
        "select home_to_ping_distance_category, distance_category_ping_fraction, source_file "
        "from md order by 1"
    ).fetchall()
    assert rows == [("(0, 10)", -0.01, "a.csv"), ("0", 0.35, "a.csv")]


def test_movement_distribution_drops_a_day_repeated_in_two_files(tmp_path):
    a = _md(tmp_path, "a.csv", [
        "A.1_1,A,AAA,2,0,0.5,2026-06-15\n",
        "A.1_1,A,AAA,2,0,0.4,2026-06-14\n",
    ])  # fmt: skip
    b = _md(tmp_path, "b.csv", [
        "A.1_1,A,AAA,2,0,0.5,2026-06-15\n",
        "A.1_1,A,AAA,2,0,0.6,2026-06-16\n",
    ])  # fmt: skip
    con = _con()
    got = m.load_movement_distribution(con, [a, b])
    assert got == {"rows": 3, "duplicates": 1, "conflicts": 0}
    assert con.sql("select count(*) from md_dropped").fetchone()[0] == 1


def test_movement_distribution_keeps_the_later_file_when_values_differ(tmp_path):
    a = _md(tmp_path, "a.csv", ["A.1_1,A,AAA,2,0,0.5,2026-06-15\n"])
    b = _md(tmp_path, "b.csv", ["A.1_1,A,AAA,2,0,0.7,2026-06-15\n"])
    con = _con()
    got = m.load_movement_distribution(con, [a, b])  # b is later in the list
    assert got == {"rows": 1, "duplicates": 1, "conflicts": 1}
    assert con.sql("select distance_category_ping_fraction, source_file from md").fetchone() == (
        0.7,
        "b.csv",
    )


def test_movement_distribution_rejects_files_with_other_columns(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("gadm_id,ds\nA,2026-06-01\n")
    with pytest.raises(ValueError, match="columns"):
        m.load_movement_distribution(_con(), [p])


def test_number_turns_na_into_null_and_fails_on_other_text():
    con = _con()
    expr = m.number("v")
    got = con.sql(f"select {expr} from (values ('1.5'), ('NA'), (''), (null)) t(v)").fetchall()
    assert got == [(1.5,), (None,), (None,), (None,)]
    import duckdb

    with pytest.raises(duckdb.ConversionException):
        con.sql(f"select {expr} from (values ('abc')) t(v)").fetchall()


def test_movement_range_keeps_the_string_na_in_names(tmp_path):
    p = tmp_path / "mr.txt"
    p.write_text(
        "ds\tcountry\tpolygon_source\tpolygon_id\tpolygon_name\t"
        "all_day_bing_tiles_visited_relative_change\tall_day_ratio_single_tile_users\t"
        "baseline_name\tbaseline_type\n"
        "2021-01-01\tJPN\tGADM\tJPN.41.51_1\tNA\t-0.2\t0.3\tfull_february\tDAY_OF_WEEK\n"
    )
    con = _con()
    m.load_movement_range(con, [p])
    row = con.sql("select ds, polygon_name, all_day_ratio_single_tile_users from mr").fetchone()
    import datetime

    assert row == (datetime.date(2021, 1, 1), "NA", 0.3)


def test_parts_by_year_names_one_file_per_year():
    assert m.part_name("movement_distribution", 2026) == "movement_distribution_2026.parquet"


# --- fetching decisions --------------------------------------------------------


def test_expected_bytes_is_the_listed_size_for_a_new_file():
    assert m.expected_bytes(100, {}) == 100


def test_expected_bytes_trusts_what_was_downloaded_when_hdx_lists_a_stale_size():
    # HDX lists 56561599 but S3 serves 56560052 (verified by its MD5 ETag)
    prev = {"size": 56561599, "bytes": 56560052}
    assert m.expected_bytes(56561599, prev) == 56560052


def test_expected_bytes_follows_a_new_listed_size():
    # the listed size changed: HDX has a new file under the resource
    assert m.expected_bytes(200, {"size": 100, "bytes": 99}) == 200


def test_etag_md5_only_for_a_plain_md5():
    assert m.etag_md5('"c8900ad15a314380b35b3f69dc530531"') == "c8900ad15a314380b35b3f69dc530531"
    assert m.etag_md5('"c8900ad15a314380b35b3f69dc530531-12"') is None  # multipart upload
    assert m.etag_md5(None) is None


def _cz(tmp_path, wkt):
    p = tmp_path / "cz.csv"
    p.write_text(
        "region,fbcz_id,name,fbcz_id_num,cz_gen_ds,win_population,win_roads_km,area,country,geography\n"
        f'Asia,Asia854,osaka,400854,3/5/23,4442659.362,64374.34424,4968.897342,Japan,"{wkt}"\n'
    )
    return p


def test_commuting_zones_get_a_geometry_and_keep_the_wkt(tmp_path):
    con = _con()
    con.execute("load spatial")
    m.load_commuting_zones(con, _cz(tmp_path, "POLYGON ((0 0, 1 0, 1 1, 0 0))"))
    got = con.sql("select st_area(geometry), geometry_truncated, cz_gen_ds from cz").fetchone()
    assert got == (0.5, False, "3/5/23")


def test_commuting_zones_flag_wkt_cut_at_the_known_length(tmp_path):
    wkt = ("POLYGON ((0 0, " + "1 1, " * 10000)[: m.WKT_CUT]
    con = _con()
    con.execute("load spatial")
    m.load_commuting_zones(con, _cz(tmp_path, wkt))
    assert con.sql("select geometry is null, geometry_truncated from cz").fetchone() == (True, True)


def test_commuting_zones_fail_on_other_broken_wkt(tmp_path):
    con = _con()
    con.execute("load spatial")
    with pytest.raises(RuntimeError, match="unreadable WKT"):
        m.load_commuting_zones(con, _cz(tmp_path, "POLYGON ((0 0, 1"))
