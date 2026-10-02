# meta-range-maps-2022-05

Dataset: https://huggingface.co/datasets/yuiseki/meta-range-maps-2022-05

Meta's Movement Range Maps from the Humanitarian Data Exchange, 2020-03-01
to 2022-05-22, as last changed in May 2022, and the code that fetches,
checks and converts it. This repository holds the code; the data is on the
Hub.

## Where things are

```
scripts/hdx_meta.py           the HDX code shared with study-geoai-algo-py's mirror_hdx_meta.py
scripts/01_download.py        what HDX lists; or --from an earlier mirror, against its sha256
scripts/02_verify.py          originals against the manifest; rows a year, days, Japan, no bloom filters
scripts/03_export_parquet.py  the two zips -> one table a year
src/publish.py                pushes the data and its card to the Hub
tests/                        the HDX code's tests, and adopting

data/README.md                the dataset card. Uploaded as-is
data/LICENSE                  CC BY and what the Parquet changes
data/provenance.yaml          the vintage, the counts, and what was done
data/manifest.json            every file: HDX name and id, size, sha256, dates
data/movement-range-maps/     generated: original/ (the zips) and the Parquet
```

## Running it

```sh
uv run python scripts/01_download.py
uv run python scripts/03_export_parquet.py
uv run python scripts/02_verify.py
uv run python src/publish.py          # dry run; --push to upload
uv run pytest
```

## Two traps

**HDX's API gives the wrong size for the 2020 zip.** It lists 56,561,599
bytes and serves 56,560,052, which matches the S3 ETag. A download checked
against the API's size would fail on a sound file; the code checks the ETag.

**Regions named `NA`.** 898 regions have the name `NA`, Tokyo's Taito ward
among them. Join on `polygon_id`.

## Licence

The code here is MIT. The data it fetches is CC BY, from AI for Good at Meta:

    Data for Good at Meta, Movement Range Maps, Humanitarian Data Exchange
    (https://data.humdata.org/dataset/movement-range-maps)
