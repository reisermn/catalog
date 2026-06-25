#!/usr/bin/env python
"""
04 — Assemble the final catalog with backward propagation (no API).

Starts from the input spine (every row, all structural columns preserved) and
overwrites only the rebuilt attribute columns:

  - active item enriched in 03            -> its own enrichment
  - legacy item whose superseded_by is an
    enriched active item                  -> inherits that enrichment
                                             (match_basis = inherited_from_active)
  - orphan legacy chemistry enriched in 03 -> its own (llm_inference)
  - anything else (orphan legacy supplies/
    equipment, dangling superseded_by)     -> rebuilt columns blanked
                                             (we never carry forward unverified
                                             old values), flagged for review

Marketing columns (Public Catalog Description, SEO *) and record_id are left
untouched.

Outputs:
  data/output/catalog_list.csv   the final enriched catalog
  data/output/catalog_review.csv rows a human should verify
"""

from __future__ import annotations

import csv
import json
import sys

import lib

# Columns this stage owns/overwrites. Everything else is preserved verbatim.
REBUILT_COLS = [
    "Brand", "canonical_product_name", "manufacturer", "manufacturer_product_code",
    "size", "is_commodity", "product_category", "function_description",
    "applicable_lines", "primary_line_position", "primary_line_sub_position",
    "secondary_line_position", "secondary_line_sub_position", "use_type",
    "system_name", "system_components", "sequence_companions", "replenishment_role",
    "application_method", "typical_concentration", "tds_filename", "match_tier",
    "match_score", "match_basis",
]
_ARRAY_COLS = {"applicable_lines", "system_components", "sequence_companions"}


def _bool(v):
    return "TRUE" if v else "FALSE"


def rec_to_cols(rec: dict) -> dict:
    """Map an enrichment record to catalog column string values."""
    out = {c: "" for c in REBUILT_COLS}
    out["Brand"] = rec.get("Brand", "") or ""
    out["canonical_product_name"] = rec.get("canonical_product_name", "") or ""
    out["manufacturer"] = rec.get("manufacturer", "") or ""
    out["manufacturer_product_code"] = rec.get("manufacturer_product_code") or ""
    out["size"] = rec.get("size") or ""
    if "is_commodity" in rec:
        out["is_commodity"] = _bool(rec["is_commodity"])
    out["product_category"] = rec.get("product_category", "") or ""
    out["function_description"] = rec.get("function_description", "") or ""
    out["applicable_lines"] = json.dumps(rec.get("applicable_lines", []), ensure_ascii=False)
    out["primary_line_position"] = rec.get("primary_line_position", "") or ""
    out["primary_line_sub_position"] = rec.get("primary_line_sub_position", "") or ""
    out["secondary_line_position"] = rec.get("secondary_line_position") or ""
    out["secondary_line_sub_position"] = rec.get("secondary_line_sub_position") or ""
    out["use_type"] = rec.get("use_type", "") or ""
    out["system_name"] = rec.get("system_name") or ""
    out["system_components"] = json.dumps(rec.get("system_components", []), ensure_ascii=False)
    out["sequence_companions"] = json.dumps(rec.get("sequence_companions", []), ensure_ascii=False)
    out["replenishment_role"] = rec.get("replenishment_role", "") or ""
    out["application_method"] = rec.get("application_method", "") or ""
    out["typical_concentration"] = rec.get("typical_concentration") or ""
    out["tds_filename"] = rec.get("tds_filename", "") or ""
    out["match_tier"] = str(rec.get("match_tier", "") or "")
    out["match_score"] = str(rec.get("match_score", "") or "")
    out["match_basis"] = rec.get("match_basis", "") or ""
    return out


def main() -> int:
    fieldnames, rows = lib.load_catalog()
    enriched = lib.load_json(lib.ENRICHED_JSON, default={})
    if not enriched:
        sys.exit("No enrichment found. Run 02 + 03 first.")

    active_items = {lib.g(r, "Item") for r in rows if lib.g(r, "status") == "active"}

    review = []
    n_self = n_inherited = n_blanked = 0

    for r in rows:
        item = lib.g(r, "Item")
        status = lib.g(r, "status")
        sb = lib.g(r, "superseded_by")
        cat = lib.g(r, "item_category")

        src = None
        if status == "active" and item in enriched:
            src = dict(enriched[item])
            n_self += 1
        elif status == "legacy" and sb in active_items and sb in enriched:
            src = dict(enriched[sb])
            src["match_basis"] = "inherited_from_active"
            n_inherited += 1
        elif status == "legacy" and item in enriched:  # orphan legacy chemistry
            src = dict(enriched[item])
            n_self += 1

        if src is not None:
            r.update(rec_to_cols(src))
        else:
            for c in REBUILT_COLS:
                r[c] = ""
            n_blanked += 1
            review.append({
                "Item": item, "status": status, "item_category": cat,
                "superseded_by": sb,
                "issue": ("dangling superseded_by — orphan" if sb and sb not in active_items
                          else "no enrichment (deprioritized / un-enriched)"),
                "match_tier": "", "match_score": "", "match_basis": "",
            })
            continue

        # flag low-confidence TDS matches for review
        if str(r.get("match_tier", "")) in ("2", "3"):
            review.append({
                "Item": item, "status": status, "item_category": cat,
                "superseded_by": sb, "issue": "low-confidence TDS match",
                "match_tier": r.get("match_tier", ""), "match_score": r.get("match_score", ""),
                "match_basis": r.get("match_basis", ""),
            })

    lib.write_catalog(lib.FINAL_CSV, fieldnames, rows)
    _write_review(review)

    print(f"Assembled {len(rows)} rows -> {lib.FINAL_CSV}")
    print(f"  self-enriched: {n_self} | inherited-from-active: {n_inherited} | blanked: {n_blanked}")
    print(f"  review rows: {len(review)} -> {lib.REVIEW_CSV}")
    return 0


def _write_review(review):
    cols = ["Item", "status", "item_category", "superseded_by", "issue",
            "match_tier", "match_score", "match_basis"]
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(lib.REVIEW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in review:
            w.writerow(r)


if __name__ == "__main__":
    sys.exit(main())
