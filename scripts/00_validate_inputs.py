#!/usr/bin/env python
"""
00 — Validate the input catalog spine (no API).

Fail-loud structural checks on data/inputs/catalog_list.csv. The pipeline trusts
the structural columns (Item, status, superseded_by, item_category, ...) and must
never silently proceed on a broken spine.

Writes data/output/input_validation_report.md and exits non-zero on hard errors.
"""

from __future__ import annotations

import sys
from collections import Counter

import lib

REQUIRED_COLUMNS = ["Item", "status", "superseded_by", "item_category"]
VALID_STATUS = {"active", "legacy"}
VALID_CATEGORY = {"Chemistry", "Supplies", "Equipment"}


def main() -> int:
    fieldnames, rows = lib.load_catalog()
    errors: list[str] = []
    warnings: list[str] = []

    # Required columns
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        errors.append(f"Missing required columns: {missing}")
        # cannot continue meaningfully
        _write_report(fieldnames, rows, errors, warnings, {})
        print("\n".join(errors))
        return 1

    active_items = {lib.g(r, "Item") for r in rows if lib.g(r, "status") == "active"}

    # status values
    bad_status = Counter(lib.g(r, "status") for r in rows if lib.g(r, "status") not in VALID_STATUS)
    if bad_status:
        errors.append(f"Rows with status not in {VALID_STATUS}: {dict(bad_status)}")

    # blank Item
    blank_items = sum(1 for r in rows if not lib.g(r, "Item"))
    if blank_items:
        errors.append(f"{blank_items} row(s) have a blank Item")

    # duplicate active Item
    active_counts = Counter(lib.g(r, "Item") for r in rows if lib.g(r, "status") == "active")
    dups = {k: v for k, v in active_counts.items() if v > 1 and k}
    if dups:
        errors.append(f"{len(dups)} duplicate active Item value(s), e.g. {list(dups)[:5]}")

    # superseded_by integrity — warn-only: a dangling pointer just makes the row
    # an orphan during assembly (it inherits nothing and is flagged for review).
    sb_total = sum(1 for r in rows if lib.g(r, "superseded_by"))
    sb_bad = [
        (lib.g(r, "Item"), lib.g(r, "superseded_by"))
        for r in rows
        if lib.g(r, "superseded_by") and lib.g(r, "superseded_by") not in active_items
    ]
    if sb_bad:
        examples = "; ".join(f"{it!r} -> {sb!r}" for it, sb in sb_bad[:5])
        warnings.append(
            f"{len(sb_bad)} row(s) have superseded_by pointing to a non-active Item "
            f"(treated as orphans, flagged for review): {examples}"
        )

    # item_category (warn-only; trusted but should be present and valid)
    cat_counts = Counter(lib.g(r, "item_category") for r in rows)
    bad_cat = {k: v for k, v in cat_counts.items() if k not in VALID_CATEGORY}
    if bad_cat:
        warnings.append(f"item_category values outside {VALID_CATEGORY}: {bad_cat}")

    stats = {
        "total_rows": len(rows),
        "active": sum(1 for r in rows if lib.g(r, "status") == "active"),
        "legacy": sum(1 for r in rows if lib.g(r, "status") == "legacy"),
        "superseded_by_populated": sb_total,
        "by_status_category": dict(
            Counter((lib.g(r, "status"), lib.g(r, "item_category")) for r in rows)
        ),
    }

    _write_report(fieldnames, rows, errors, warnings, stats)

    print(f"Input validation: {len(rows)} rows, {len(errors)} error(s), {len(warnings)} warning(s).")
    print(f"Report: {lib.OUTPUT / 'input_validation_report.md'}")
    if errors:
        print("\nHARD ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1
    return 0


def _write_report(fieldnames, rows, errors, warnings, stats) -> None:
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    lines = ["# Input Validation Report", ""]
    lines.append(f"- columns: {len(fieldnames)}")
    for k, v in stats.items():
        if k == "by_status_category":
            lines.append("- status x item_category:")
            for (st, cat), n in sorted(v.items()):
                lines.append(f"    - {st} / {cat or '(blank)'}: {n}")
        else:
            lines.append(f"- {k}: {v}")
    lines += ["", f"## Errors ({len(errors)})"]
    lines += [f"- {e}" for e in errors] or ["- none"]
    lines += ["", f"## Warnings ({len(warnings)})"]
    lines += [f"- {w}" for w in warnings] or ["- none"]
    (lib.OUTPUT / "input_validation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
