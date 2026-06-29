#!/usr/bin/env python
"""
Utility — export the TDS files actually used by the catalog (no API).

"Used" = every TDS that currently backs at least one catalog item: verified
auto-matches from 02 (minus any rejected with approved=NO in the review file)
plus the manual links in data/inputs/manual_matches.csv. Mirrors exactly what
03_enrich would extract from, so the export reflects reality without needing a
full pipeline run.

Writes:
  data/output/used_tds_list.csv   one row per used TDS (filename, count, items)
  data/output/used_tds/           a flat copy of just those PDFs, ready to upload
"""

from __future__ import annotations

import csv
import shutil
import sys
from collections import defaultdict

import lib

OUT_DIR = lib.OUTPUT / "used_tds"
OUT_LIST = lib.OUTPUT / "used_tds_list.csv"


def main() -> int:
    index = lib.load_json(lib.TDS_INDEX, default=[])
    approvals = lib.read_match_approvals()

    # filename -> {"rel_path": str, "items": [..]}
    used: dict[str, dict] = defaultdict(lambda: {"rel_path": "", "items": []})

    # 1) verified auto-matches (respect approvals)
    if lib.MATCHES_CSV.exists():
        with open(lib.MATCHES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("match_tier")) in ("2", "3") and \
                        approvals.get(row["Item"], "") == "NO":
                    continue
                fn = row["tds_filename"]
                used[fn]["rel_path"] = row.get("rel_path", "")
                used[fn]["items"].append(row["Item"])

    # 2) manual links
    for item, mm in lib.load_manual_matches(index).items():
        fn = mm["tds_filename"]
        used[fn]["rel_path"] = mm["rel_path"]
        used[fn]["items"].append(item)

    if not used:
        sys.exit("No used TDS found. Run 02_match (and/or add manual_matches.csv) first.")

    # copy the files into a clean folder
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    copied, missing = 0, []
    for fn, info in sorted(used.items()):
        src = lib.TDS_DIR / info["rel_path"] if info["rel_path"] else lib.TDS_DIR / fn
        if not src.exists():
            missing.append(fn)
            continue
        shutil.copy2(src, OUT_DIR / fn)
        copied += 1

    # write the list (most-reused TDS first)
    with open(OUT_LIST, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tds_filename", "used_by_count", "rel_path", "items"])
        for fn, info in sorted(used.items(), key=lambda kv: (-len(kv[1]["items"]), kv[0])):
            w.writerow([fn, len(info["items"]), info["rel_path"],
                        " | ".join(sorted(set(info["items"])))])

    total_items = sum(len(i["items"]) for i in used.values())
    print(f"Used TDS: {len(used)} distinct file(s) backing {total_items} catalog item(s).")
    print(f"  copied {copied} PDF(s) -> {OUT_DIR}/")
    print(f"  list -> {OUT_LIST}")
    if missing:
        print(f"  WARNING: {len(missing)} used filename(s) not found on disk: {missing[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
