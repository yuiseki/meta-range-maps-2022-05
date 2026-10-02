---
license: cc-by-4.0
language:
- en
task_categories:
- time-series-forecasting
- tabular-regression
tags:
- human-mobility
- covid-19
- meta
- data-for-good
- hdx
- gadm
size_categories:
- 10M<n<100M
configs:
- config_name: default
  data_files: movement-range-maps/*.parquet
---

# meta-range-maps-2022-05

Meta's Movement Range Maps, the daily measure of how much people moved during
the COVID-19 pandemic, for 19,142 regions of 153 countries from 2020-03-01 to
2022-05-22, when Meta ended it. Two numbers a region and day: how much more
or less ground people covered than in February 2020, and the share who stayed
within one small area all day.

CC BY 4.0. Credit Data for Good at Meta and the Humanitarian Data Exchange
(see Licence).

## What is here

| | rows |
|---|---:|
| `movement-range-maps/movement_range_2020.parquet` | 5,229,342 |
| `movement-range-maps/movement_range_2021.parquet` | 5,287,242 |
| `movement-range-maps/movement_range_2022.parquet` | 1,662,956 |

| column | |
|---|---|
| `ds` | the day, a date |
| `country` | ISO 3166-1 alpha-3 |
| `polygon_source` | `GADM` for regions outside the United States, `FIPS` for US counties |
| `polygon_id`, `polygon_name` | the region (`JPN.41.51_1`) and its name |
| `all_day_bing_tiles_visited_relative_change` | change in the number of map tiles visited, against the same weekday in February 2020 |
| `all_day_ratio_single_tile_users` | share of users who stayed within a single tile all day |
| `baseline_name`, `baseline_type` | the February baseline (`full_february`, or for FIPS `full_february_except_presidents_day`), by `DAY_OF_WEEK` |
| `source_file` | added here: the file inside the zip the row came from |

Japan is 695 regions and 384,539 rows. `movement-range-maps/original/` holds
HDX's two zips and its note on reading them, byte for byte; `manifest.json`
records each with its size, sha256 and HDX's dates.

```sql
-- DuckDB, straight off the Hub: Taito ward before and after Japan's first state of emergency
SELECT ds, round(all_day_bing_tiles_visited_relative_change, 3) AS moved,
       round(all_day_ratio_single_tile_users, 3) AS stayed
FROM 'hf://datasets/yuiseki/meta-range-maps-2022-05/movement-range-maps/movement_range_2020.parquet'
WHERE polygon_id = 'JPN.41.51_1' AND ds IN (DATE '2020-03-02', DATE '2020-04-15');
-- 2020-03-02  -0.117  0.113
-- 2020-04-15  -0.387  0.230
```

## Things to know

**898 regions are named `NA`.** Taito ward among them. Join on `polygon_id`,
not on the name.

**The 2020 zip is not the size HDX's API says.** The API lists 56,561,599
bytes; HDX serves 56,560,052, which matches the S3 ETag and is a sound zip.
The listing looks stale.

**The data ended on 2022-05-22.** Nothing has changed on HDX since
2022-05-24, which is the month in the name.

## Licence

CC BY 4.0, as HDX lists the dataset (`cc-by`). Credit as HDX asks:

    Data for Good at Meta, Movement Range Maps, Humanitarian Data Exchange
    (https://data.humdata.org/dataset/movement-range-maps)

The zips are Meta's, unchanged. The Parquet is made from them; `LICENSE`
beside this file says what was done.
