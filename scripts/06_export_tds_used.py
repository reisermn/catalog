#!/usr/bin/env python
"""
06 — Export the TDS used by catalog_active.csv, out of tds_all/ (no API).

catalog_active.csv (the current reference template) already carries a
"TDS Filename" column per row — no matching needed here, just resolution:
for every non-blank filename, find it in data/inputs/tds_all/ (searched
recursively across all vendor subfolders) and copy it flat into
data/output/tds_used/.

Whatever in tds_all/ is NOT referenced by any catalog row is exactly the
input to 07/08/09 (index, rematch, propose-new-items).

Outputs:
  data/output/tds_used/             flat copy of every TDS the catalog uses
  data/output/tds_used_manifest.csv one row per referenced filename: found?,
                                     which item(s) use it
"""

from __future__ import annotations

import csv
import shutil
import sys
from collections import defaultdict

import lib


def main() -> int:
    if not lib.TDS_ALL_DIR.exists():
        sys.exit(f"TDS directory not found: {lib.TDS_ALL_DIR}")

    _, rows = lib.load_catalog(lib.ACTIVE_CATALOG_CSV)

    all_pdfs = [p for p in lib.TDS_ALL_DIR.rglob("*.pdf")
                if p.is_file() and not p.name.startswith(".")]
    by_name = {p.name: p for p in all_pdfs}  # filenames are unique across tds_all (verified)

    used: dict[str, dict] = defaultdict(lambda: {"items": [], "found": False})
    for r in rows:
        fn = lib.g(r, "TDS Filename")
        if not fn:
            continue
        used[fn]["items"].append(lib.g(r, "Item Name"))
        if fn in by_name:
            used[fn]["found"] = True

    if not used:
        sys.exit("No 'TDS Filename' values found in catalog_active.csv.")

    if lib.TDS_USED_DIR.exists():
        shutil.rmtree(lib.TDS_USED_DIR)
    lib.TDS_USED_DIR.mkdir(parents=True, exist_ok=True)

    copied, missing = 0, []
    for fn, info in used.items():
        src = by_name.get(fn)
        if src is None:
            missing.append(fn)
            continue
        shutil.copy2(src, lib.TDS_USED_DIR / fn)
        copied += 1

    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(lib.TDS_USED_MANIFEST, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tds_filename", "found_in_tds_all", "used_by_count", "items"])
        for fn, info in sorted(used.items(), key=lambda kv: (-len(kv[1]["items"]), kv[0])):
            w.writerow([fn, info["found"], len(info["items"]),
                        " | ".join(sorted(set(info["items"])))])

    total_items_with_tds = sum(len(i["items"]) for i in used.values())
    print(f"catalog_active.csv references {len(used)} distinct TDS filename(s) "
          f"across {total_items_with_tds} item row(s).")
    print(f"  copied {copied} PDF(s) -> {lib.TDS_USED_DIR}/")
    print(f"  manifest -> {lib.TDS_USED_MANIFEST}")
    if missing:
        print(f"  WARNING: {len(missing)} referenced filename(s) NOT found anywhere in "
              f"tds_all/ (catalog points at a TDS you didn't bring over, or it was "
              f"renamed) — see manifest for which items they'd affect:")
        for fn in missing:
            print(f"    - {fn}")

    unused = sorted(set(by_name) - set(used))
    print(f"  {len(unused)} PDF(s) in tds_all/ are NOT referenced by any catalog row "
          f"-> input to 07_index_tds_all.py / 08_match_unused_tds.py / 09_propose_new_items.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
