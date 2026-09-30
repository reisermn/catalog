#!/usr/bin/env python
"""
10 — Apply proposals + rematches onto catalog_active.csv (no API).

Produces a NEW version of the catalog (data/inputs/ stays read-only) that:

  1. Appends every row from proposed_new_items.csv to the bottom of
     catalog_active.csv, as new products.
  2. Updates the `TDS Filename` cell of existing rows per
     tds_matched_to_existing.csv (tier-1 suggested backfills only — there
     were 0 tier-2/3 rows to gate on this run).

Per instruction, this pass does NOT split multi-component systems into
separate rows (one row per TDS/system stands, same as the proposal file),
does NOT touch DOJ Controlled (left blank/unchecked), and does NOT rewrite
Function Description copy. Packaging/size is defaulted to a 55-gallon drum
for every new CHEMISTRY row (Supplies/Equipment proposals don't get a drum
size — flagged instead, since "55 gal" doesn't make sense for a test kit or
bagged media). New Item Names are prefixed with their Preferred Vendor to
match the existing "<Vendor> <Product>" naming convention, when not already
present (case-insensitive check).

Also flags (never silently fixes) any row — new or pre-existing — whose
Item Name has leading/trailing whitespace/newlines, a data-quality issue
found in 19 rows of the original catalog_active.csv.

Three review-only columns are appended to the RIGHT of catalog_active.csv's
20 template columns (never inserted in the middle, per instruction):
  _change_type       "" (untouched) | "new_proposal" | "updated_tds_match"
                      | "flagged_whitespace" (whitespace issue, nothing else changed)
  _change_notes       what to double check, and why
  _tds_filename_previous   the TDS Filename an updated row had before (blank
                      if it had none) — direct before/after for the rematch case

Output:
  data/output/catalog_active_updated.csv
"""

from __future__ import annotations

import csv
import re
import sys

import lib

DRUM_SUFFIX = " (55 gal)"
EXTRA_COLS = ["_change_type", "_change_notes", "_tds_filename_previous"]

# Cheap text heuristic (no API call) to flag when a "default to 55 gal drum"
# is likely wrong because the TDS itself describes a non-liquid form.
_NON_LIQUID_HINTS = re.compile(
    r"\b(powder|granular|granule|flake|paste|tablet|pellet|bead|dry concentrate|"
    r"solid concentrate)\b", re.IGNORECASE,
)


def blank_row(cols: list[str]) -> dict:
    return {c: "" for c in cols}


def apply_rematches(rows: list[dict], out_cols: list[str]) -> tuple[int, list[str]]:
    if not lib.TDS_MATCHED_TO_EXISTING_CSV.exists():
        return 0, []
    with open(lib.TDS_MATCHED_TO_EXISTING_CSV, newline="", encoding="utf-8") as f:
        rematches = list(csv.DictReader(f))
    if not rematches:
        return 0, []

    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(lib.g(r, "Item Name"), []).append(r)

    applied, warnings = 0, []
    for m in rematches:
        item = m["Item Name"]
        matches = by_name.get(item, [])
        if len(matches) != 1:
            warnings.append(f"'{item}': found {len(matches)} catalog row(s) with this exact "
                             f"Item Name (expected 1) — TDS Filename NOT updated, check by hand.")
            continue
        row = matches[0]
        previous = lib.g(row, "TDS Filename")
        row["TDS Filename"] = m["suggested_tds_filename"]
        row["_change_type"] = "updated_tds_match"
        row["_tds_filename_previous"] = previous
        row["_change_notes"] = (
            f"TDS Filename {'added' if not previous else 'changed'} via unused-TDS rematch "
            f"(tier {m['match_tier']}, confidence {m['confidence']}, basis {m['match_basis']}): "
            f"{m['reason']}"
        )
        applied += 1
    return applied, warnings


def build_new_rows(out_cols: list[str]) -> tuple[list[dict], int, int]:
    if not lib.NEW_ITEM_PROPOSALS_CSV.exists():
        sys.exit(f"{lib.NEW_ITEM_PROPOSALS_CSV} not found — run 09_propose_new_items.py first.")
    with open(lib.NEW_ITEM_PROPOSALS_CSV, newline="", encoding="utf-8") as f:
        proposals = list(csv.DictReader(f))

    new_rows, drummed, flagged_form, prefixed = [], 0, 0, 0
    for p in proposals:
        row = blank_row(out_cols)
        for c in lib.ACTIVE_CATALOG_COLUMNS:
            row[c] = p.get(c, "")

        notes = []
        # Match the catalog's existing naming convention: "<Vendor> <Product>"
        # (e.g. "MacDermid Enprep 109 (gal)") — not the longer Manufacturer
        # string. 239/248 existing MacDermid rows already follow this.
        vendor = row["Preferred Vendor"].strip()
        if vendor and not row["Item Name"].strip().lower().startswith(vendor.lower()):
            row["Item Name"] = f"{vendor} {row['Item Name']}".strip()
            prefixed += 1

        is_chem = row["Category"] == "Chemistry"
        if is_chem:
            row["Item Name"] = row["Item Name"] + DRUM_SUFFIX
            drummed += 1
            haystack = f"{p.get('Function Description','')} {p.get('Product Category','')}"
            hit = _NON_LIQUID_HINTS.search(haystack)
            if hit:
                flagged_form += 1
                notes.append(f"Defaulted to a 55 gal drum, but the TDS describes a "
                             f"'{hit.group(0)}' form — a drum is probably wrong; confirm real packaging.")
            else:
                notes.append("Packaging defaulted to a 55 gal drum (placeholder) — confirm real pack size.")
        else:
            notes.append(f"Category={row['Category']}: no drum size applied — confirm actual packaging.")

        notes.append("QB Item ID and DOJ Controlled intentionally left blank.")
        if p.get("_review_notes"):
            notes.append(p["_review_notes"])
        if int(p.get("_group_size", 1) or 1) > 1:
            notes.append(f"Extracted from {p['_group_size']} source TDS: {p.get('_source_tds_filenames','')}")

        row["_change_type"] = "new_proposal"
        row["_change_notes"] = " ".join(notes)
        row["_tds_filename_previous"] = ""
        new_rows.append(row)
    return new_rows, drummed, flagged_form, prefixed


def flag_whitespace(rows: list[dict]) -> int:
    """Flag (never silently fix) Item Name values with leading/trailing
    whitespace or newlines — a pre-existing catalog_active.csv data-quality
    issue, unrelated to this pass, that's easy to miss in a spreadsheet."""
    flagged = 0
    for r in rows:
        name = r.get("Item Name", "")
        if name != name.strip():
            note = "Item Name has leading/trailing whitespace/newline in the source data — cosmetic cleanup needed."
            r["_change_notes"] = f"{r['_change_notes']} {note}".strip() if r.get("_change_notes") else note
            if not r.get("_change_type"):
                r["_change_type"] = "flagged_whitespace"
            flagged += 1
    return flagged


def main() -> int:
    fieldnames, rows = lib.load_catalog(lib.ACTIVE_CATALOG_CSV)
    if fieldnames != lib.ACTIVE_CATALOG_COLUMNS:
        sys.exit("catalog_active.csv's columns don't match lib.ACTIVE_CATALOG_COLUMNS — "
                 "the template may have changed; update lib.py before re-running.")

    out_cols = lib.ACTIVE_CATALOG_COLUMNS + EXTRA_COLS
    for r in rows:
        for c in EXTRA_COLS:
            r.setdefault(c, "")

    rematched, rematch_warnings = apply_rematches(rows, out_cols)
    new_rows, drummed, flagged_form, prefixed = build_new_rows(out_cols)

    all_rows = rows + new_rows
    whitespace_flagged = flag_whitespace(all_rows)
    lib.write_catalog(lib.ACTIVE_CATALOG_UPDATED_CSV, out_cols, all_rows)

    print(f"catalog_active.csv: {len(rows)} existing row(s).")
    print(f"  {rematched} row(s) updated in place (TDS Filename backfilled from unused-TDS rematch).")
    if rematch_warnings:
        print(f"  {len(rematch_warnings)} rematch warning(s):")
        for w in rematch_warnings:
            print(f"    - {w}")
    print(f"proposed_new_items.csv: {len(new_rows)} row(s) appended as new products.")
    print(f"  {prefixed} prefixed with their Preferred Vendor to match the naming convention "
          f"(e.g. 'MacDermid <product>').")
    print(f"  {drummed} defaulted to a 55 gal drum Item Name suffix "
          f"({flagged_form} of those flagged — TDS text suggests a non-liquid form).")
    print(f"  {len(new_rows) - drummed} left without a size suffix (Supplies/Equipment) — flagged for review.")
    print(f"\n{whitespace_flagged} row(s) (new + pre-existing) flagged for leading/trailing "
          f"whitespace/newlines in Item Name — not fixed, just flagged.")
    print(f"\nTotal rows: {len(rows)} -> {len(all_rows)}")
    print(f"Wrote {lib.ACTIVE_CATALOG_UPDATED_CSV}")
    print(f"  Review columns (appended to the right): {EXTRA_COLS}")
    print(f"  Filter _change_type != '' to see everything touched this pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
