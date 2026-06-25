#!/usr/bin/env python
"""
05 — Validate the final catalog (no API). Fail loud.

Hard errors (exit non-zero, the build must not ship):
  - output row count != input row count (no rows added or dropped)
  - the structural columns (Item, status, superseded_by, item_category) drifted
    from the input spine
  - a duplicate active Item
  - applicable_lines / line-position values outside the controlled vocabulary
  - malformed JSON in an array column

Warnings (reported, non-fatal):
  - dangling superseded_by (carried from the input)
  - active chemistry rows with no enrichment (and not logged as an enrich error)

Writes data/output/output_validation_report.md.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

import lib

STRUCTURAL = ["Item", "status", "superseded_by", "item_category"]


def main() -> int:
    if not lib.FINAL_CSV.exists():
        sys.exit(f"Final catalog not found: {lib.FINAL_CSV}. Run 04 first.")

    _, inp = lib.load_catalog()
    _, out = lib.load_catalog(lib.FINAL_CSV)
    errors, warnings, stats = [], [], {}

    # row count
    if len(out) != len(inp):
        errors.append(f"row count changed: input {len(inp)} -> output {len(out)}")

    # structural columns preserved (compare as ordered tuples)
    def key(rows):
        return [tuple(lib.g(r, c) for c in STRUCTURAL) for r in rows]
    if len(out) == len(inp) and key(out) != key(inp):
        drift = sum(1 for a, b in zip(key(inp), key(out)) if a != b)
        errors.append(f"structural columns drifted from the input in {drift} row(s)")

    # duplicate active Item
    active_counts = Counter(lib.g(r, "Item") for r in out if lib.g(r, "status") == "active")
    dups = {k: v for k, v in active_counts.items() if v > 1 and k}
    if dups:
        errors.append(f"{len(dups)} duplicate active Item value(s): {list(dups)[:5]}")

    # controlled-vocab compliance
    valid_lines = set(lib.APPLICABLE_LINES)
    valid_pos = set(lib.LINE_POSITIONS)
    valid_sub = set(lib.LINE_SUB_POSITIONS)
    bad_lines = bad_pos = bad_sub = bad_json = 0
    for r in out:
        raw = lib.g(r, "applicable_lines")
        if raw:
            try:
                arr = json.loads(raw)
                if any(x not in valid_lines for x in arr):
                    bad_lines += 1
            except json.JSONDecodeError:
                bad_json += 1
        for col, vocab, _ in (("primary_line_position", valid_pos, 0),
                              ("secondary_line_position", valid_pos, 0)):
            v = lib.g(r, col)
            if v and v not in vocab:
                bad_pos += 1
        for col in ("primary_line_sub_position", "secondary_line_sub_position"):
            v = lib.g(r, col)
            if v and v not in valid_sub:
                bad_sub += 1
        for col in ("system_components", "sequence_companions"):
            raw = lib.g(r, col)
            if raw:
                try:
                    json.loads(raw)
                except json.JSONDecodeError:
                    bad_json += 1
    if bad_lines:
        errors.append(f"{bad_lines} row(s) have applicable_lines outside the controlled vocab")
    if bad_pos:
        errors.append(f"{bad_pos} row(s) have a line_position outside the controlled vocab")
    if bad_sub:
        errors.append(f"{bad_sub} row(s) have a line_sub_position outside the controlled vocab")
    if bad_json:
        errors.append(f"{bad_json} array-column value(s) are malformed JSON")

    # superseded_by integrity (warn — carried from input)
    active_items = {lib.g(r, "Item") for r in out if lib.g(r, "status") == "active"}
    dangling = [lib.g(r, "Item") for r in out
                if lib.g(r, "superseded_by") and lib.g(r, "superseded_by") not in active_items]
    if dangling:
        warnings.append(f"{len(dangling)} row(s) have a dangling superseded_by (e.g. {dangling[:3]})")

    # active chemistry coverage (warn)
    errors_log = lib.load_json(lib.ENRICH_ERRORS, default={})
    uncovered = [lib.g(r, "Item") for r in out
                 if lib.g(r, "status") == "active" and lib.g(r, "item_category") == "Chemistry"
                 and not lib.g(r, "match_basis") and lib.g(r, "Item") not in errors_log]
    if uncovered:
        warnings.append(f"{len(uncovered)} active chemistry row(s) have no enrichment "
                        f"(e.g. {uncovered[:3]})")
    if errors_log:
        warnings.append(f"{len(errors_log)} item(s) failed enrichment (see enrich_errors.json)")

    # stats
    basis = Counter(lib.g(r, "match_basis") for r in out if lib.g(r, "match_basis"))
    stats["rows"] = len(out)
    stats["enriched (match_basis populated)"] = sum(basis.values())
    stats["by match_basis"] = dict(basis)

    _write_report(errors, warnings, stats)
    print(f"Output validation: {len(out)} rows, {len(errors)} error(s), {len(warnings)} warning(s).")
    print(f"Report: {lib.OUTPUT / 'output_validation_report.md'}")
    if errors:
        print("\nHARD ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1
    return 0


def _write_report(errors, warnings, stats):
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    lines = ["# Output Validation Report", ""]
    for k, v in stats.items():
        if isinstance(v, dict):
            lines.append(f"- {k}:")
            for kk, vv in sorted(v.items()):
                lines.append(f"    - {kk}: {vv}")
        else:
            lines.append(f"- {k}: {v}")
    lines += ["", f"## Errors ({len(errors)})"] + ([f"- {e}" for e in errors] or ["- none"])
    lines += ["", f"## Warnings ({len(warnings)})"] + ([f"- {w}" for w in warnings] or ["- none"])
    (lib.OUTPUT / "output_validation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
