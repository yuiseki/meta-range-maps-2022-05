#!/usr/bin/env python3
"""Push the kept originals, the Parquet and the card to the Hugging Face Hub.

Every file listed in manifest.json goes up unchanged after its sha256 is
checked; files HDX has since removed are kept and pushed like the rest.

    uv run python src/publish.py             # dry run
    uv run python src/publish.py --push
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "yuiseki/meta-range-maps-2022-05"
DATASET = "movement-range-maps"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()

    data = ROOT / "data"
    if f"data_files: {DATASET}/*.parquet" not in (data / "README.md").read_text():
        raise SystemExit("the card does not declare the Parquet")
    entry = json.loads((data / "manifest.json").read_text())["datasets"][DATASET]
    files = ["README.md", "LICENSE", "provenance.yaml", "manifest.json"]
    for s in entry["sources"]:
        p = data / DATASET / "original" / s["file"]
        if sha256(p) != s["sha256"]:
            raise SystemExit(f"{p.name} is not the file in the manifest")
        files.append(f"{DATASET}/original/{s['file']}")
    files += [f"{DATASET}/{n}" for n in entry["outputs"]]
    total = sum((data / f).stat().st_size for f in files)
    print(f"{len(entry['sources'])} originals match the manifest")
    print(f"{a.repo}\n  {len(files)} files, {total / 1e9:.2f} GB")
    if not a.push:
        print("\ndry run. pass --push to upload")
        return 0

    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(a.repo, repo_type="dataset", exist_ok=True, private=False)
    api.upload_folder(
        folder_path=str(data), repo_id=a.repo, repo_type="dataset", allow_patterns=files
    )
    print(f"\npushed to https://huggingface.co/datasets/{a.repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
