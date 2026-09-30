#!/usr/bin/env python
"""
09 — Propose new catalog rows for TDS with no existing match (API, cached).

Input is data/output/tds_unmatched.json from 08_match_unused_tds.py: every TDS
in tds_all/ that isn't already used by catalog_active.csv and couldn't be
confidently matched back onto an existing row. These are candidates for
products ABCO carries a TDS for but that aren't in the catalog yet.

Several TDS often describe the same product (a reprint, a different revision,
a regional variant filed under a slightly different name), so we first GROUP
unmatched TDS by (vendor folder, normalized product-identity key) and extract
ONE proposed row per group, from the largest file in the group (native PDF,
best fidelity). Extraction fills exactly catalog_active.csv's columns
(ACTIVE_CATALOG_COLUMNS) — nothing else is added to the template row itself.

Fields a TDS genuinely can't tell you: Pricing Model defaults to "Quote
Required" (matches how not-actively-stocked items are already listed).
Preferred Vendor defaults from the TDS's vendor folder, using the mapping the
existing catalog already implies (see VENDOR_TO_PREFERRED_VENDOR — e.g. every
MacDermid-manufactured row in catalog_active.csv already has Preferred Vendor
"MacDermid"). QB Item ID, DOJ Controlled, and the packaging/size portion of
Item Name are left BLANK — never fabricated. A few extra columns are appended
after the template's 20 for review context (source TDS, group size, a
free-text note); they are not part of catalog_active.csv's schema, which per
request stays exactly its 20 columns otherwise.

Outputs:
  data/output/proposed_new_items.csv  one row per proposed new product
  data/output/new_item_cache.json     group_key -> extraction (idempotent)
  data/output/new_item_errors.json    groups that failed extraction
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import lib

# --- TDS-to-TDS dedup key (deliberately NOT lib.tds_match_key) --------------
# tds_match_key()/normalize() strip bare "<digits><unit-letter>" tokens as
# package sizes (e.g. "5 gal"), which is right when comparing a TDS filename
# against a catalog item name. But here we're deduping TDS against EACH OTHER,
# and in practice that same pattern often IS the product code (e.g. "Metex
# Elite 543 L" vs "561 L" are different products, not a size variant of one).
# Stripping it would silently merge two distinct products into one proposed
# row and drop one. This key only strips what's unambiguously filename noise:
# duplicate-download suffixes, vendor/format stopwords, and revision dates.
_DEDUPE_STOP = {"tds", "sds", "msds", "ds", "meis", "pds", "na", "eu", "gl", "en",
                "letterhead", "new", "process", "operation", "guide", "send", "w",
                "datasheet", "data", "sheet", "technical", "rev", "final", "002",
                "003", "004"}
_DEDUPE_DATE = re.compile(r"^\d{1,2}[a-z]{3,4}\d{2,4}$")
_DUP_SUFFIX = re.compile(r"\s*\(\d+\)$")


def dedupe_key(filename: str) -> str:
    stem = _DUP_SUFFIX.sub("", Path(filename).stem.lower())
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"[^\w\s]", " ", stem)
    tokens = [t for t in stem.split() if t not in _DEDUPE_STOP and not _DEDUPE_DATE.match(t)]
    return " ".join(tokens)

# --- controlled-vocab text blocks (mirrors 03_enrich.py) --------------------
_LINES = "\n".join(f"  - {x}" for x in lib.APPLICABLE_LINES)
_POS = "\n".join(f"  - {x}" for x in lib.LINE_POSITIONS)
_SUB = "\n".join(f"  - {x}" for x in lib.LINE_SUB_POSITIONS)
_VOCAB = (
    f"Applicable Lines — choose zero or more EXACTLY from:\n{_LINES}\n\n"
    f"Primary Line Position — choose EXACTLY one from:\n{_POS}\n\n"
    f"Primary Line Sub Position — choose EXACTLY one from:\n{_SUB}\n\n"
    f"Use Type: standalone | system_component\n"
    f"Replenishment Role: makeup_only | replenishment_only | both | not_applicable\n"
)

_NS = ["string", "null"]
NEW_ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "canonical_product_name": {"type": "string"},
        "category": {"type": "string", "enum": lib.ACTIVE_CATEGORIES},
        "manufacturer": {"type": "string"},
        "manufacturer_product_code": {"type": _NS},
        "is_commodity": {"type": "boolean"},
        "product_category": {"type": "string"},
        "function_description": {"type": "string"},
        "applicable_lines": {"type": "array", "items": {"type": "string"}},
        "primary_line_position": {"type": "string"},
        "primary_line_sub_position": {"type": "string"},
        "use_type": {"type": "string"},
        "system_name": {"type": _NS},
        "system_components": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"},
                           "product_code": {"type": _NS},
                           "role": {"type": "string"}},
            "required": ["name", "product_code", "role"]}},
        "sequence_companions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"},
                           "relationship": {"type": "string"},
                           "direction": {"type": "string"}},
            "required": ["name", "relationship", "direction"]}},
        "replenishment_role": {"type": "string"},
        "confidence_notes": {"type": "string"},
    },
    "required": ["canonical_product_name", "category", "manufacturer",
                 "manufacturer_product_code", "is_commodity", "product_category",
                 "function_description", "applicable_lines", "primary_line_position",
                 "primary_line_sub_position", "use_type", "system_name",
                 "system_components", "sequence_companions", "replenishment_role",
                 "confidence_notes"],
}

NEW_ITEM_SYSTEM = f"""You are an expert in metal finishing, electroplating, and surface-treatment \
chemistry, cataloging a product that is NOT YET in ABCO's product catalog. You \
are given its Technical Data Sheet (TDS) as a native PDF. Extract a single \
structured record so a human can review and add it as a new catalog row.

EXTRACT VERBATIM from the TDS (no invention): canonical_product_name, \
manufacturer, manufacturer_product_code. Use null when genuinely absent.

INFER CONFIDENTLY from the chemistry, application, and companions when the TDS \
is implicit: category (almost always "Chemistry" for a TDS-backed product; use \
"Supplies"/"Equipment" only if the TDS is clearly for a physical, non-chemical \
item), product_category, function_description (1-3 plain sentences), \
applicable_lines, line position/sub-position, use_type, replenishment_role, \
is_commodity (true only for generic commodity chemicals).

system_components: every product in the same named system (incl. this one), \
names copied EXACTLY from the TDS. sequence_companions: products named as used \
before/after this one (relationship precedes|follows, direction before|after). \
Empty arrays if none. Do not invent companions.

confidence_notes: one sentence flagging anything uncertain or that a human \
should double-check (e.g. ambiguous line assignment, multiple products on one \
sheet, illegible sections).

CONTROLLED VOCABULARY — use these strings exactly:
{_VOCAB}
Return JSON only."""


# Empirically derived from catalog_active.csv: for rows whose Manufacturer or
# Preferred Vendor mentions each vendor-folder's brand, this is the Preferred
# Vendor value already used >=90% of the time (100% for every folder except
# ABCO, which splits between "Chemical Compounding" (the actual manufacturer
# for most ABCO-branded chemistry) and "ABCO" itself — defaulted to the
# majority, flagged for confirmation in _review_notes).
VENDOR_TO_PREFERRED_VENDOR = {
    "ABCO": "Chemical Compounding",
    "Florida Cirtech": "Florida CirTech",
    "MacDermid": "MacDermid",
    "Metal Chem": "Metal Chem",
    "Stellar Solutions": "Stellar Solutions",
    "Tolber": "Tolber",
    "US Specialty": "U.S. Specialty Color Corp.",
}
DEFAULT_PRICING_MODEL = "Quote Required"


def group_key(entry: dict) -> str:
    key = dedupe_key(entry["filename"])
    return f"{entry.get('vendor','')}::{key or entry['filename'].lower()}"


def pick_representative(entries: list[dict]) -> dict:
    """Largest file on disk = usually the most complete/highest-fidelity scan."""
    def size(e):
        try:
            return (lib.TDS_ALL_DIR / e["rel_path"]).stat().st_size
        except OSError:
            return 0
    return max(entries, key=size)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=lib.DEFAULT_MODEL,
                     help="use claude-opus-4-8 for a higher-accuracy pass")
    ap.add_argument("--dry-run", action="store_true",
                     help="show the groups that would be extracted — no API calls")
    ap.add_argument("--sample", type=int, default=0, help="extract only the first N groups")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--vendor", default="",
                     help="restrict to one tds_all/ vendor folder, e.g. MacDermid")
    args = ap.parse_args()

    unmatched = lib.load_json(lib.TDS_UNMATCHED_JSON, default=[])
    if not unmatched:
        sys.exit("No unmatched TDS found. Run 08_match_unused_tds.py first "
                 "(without --sample/--limit).")
    if args.vendor:
        unmatched = [e for e in unmatched if e.get("vendor") == args.vendor]
        if not unmatched:
            sys.exit(f"No unmatched TDS for vendor {args.vendor!r}.")

    groups: dict[str, list[dict]] = defaultdict(list)
    for e in unmatched:
        groups[group_key(e)].append(e)
    group_items = sorted(groups.items(), key=lambda kv: kv[0])

    print(f"{len(unmatched)} unmatched TDS -> {len(group_items)} distinct product group(s).")
    multi = [(k, v) for k, v in group_items if len(v) > 1]
    if multi:
        print(f"  {len(multi)} group(s) collapse multiple TDS into one proposed row:")
        for k, v in multi[:10]:
            print(f"    {k}: {[e['filename'] for e in v]}")

    if args.dry_run:
        print("\nDry run — no API calls. Groups that would be extracted:")
        for k, v in group_items:
            rep = pick_representative(v)
            print(f"  {k[:60]:60s} rep={rep['filename']} (n={len(v)})")
        print(f"\nWould make up to {len(group_items)} extraction call(s) with --model {args.model}.")
        return 0

    if args.sample:
        group_items = group_items[:args.sample]
    elif args.limit:
        group_items = group_items[:args.limit]

    cache = lib.load_json(lib.NEW_ITEM_CACHE, default={})
    errors = lib.load_json(lib.NEW_ITEM_ERRORS, default={})
    client = lib.get_client()
    n = len(group_items)
    cached = sum(1 for k, _ in group_items if k in cache)
    print(f"\nExtracting {n} group(s) ({cached} cached, ~{n - cached} to call). "
          f"Re-run after a failure to resume.", flush=True)

    proposals = []
    api_calls = 0
    try:
        for i, (key, entries) in enumerate(group_items, 1):
            rep = pick_representative(entries)
            try:
                if key in cache:
                    data = cache[key]
                    api = False
                else:
                    user = [lib.pdf_block(lib.TDS_ALL_DIR / rep["rel_path"]),
                            {"type": "text", "text": f"TDS FILENAME: {rep['filename']}\n"
                             "This product is not yet in the catalog. Extract its record."}]
                    data, _ = lib.call_structured(client, args.model, lib.cached_text(NEW_ITEM_SYSTEM),
                                                  user, NEW_ITEM_SCHEMA, max_tokens=8192)
                    cache[key] = data
                    api = True
                    api_calls += 1
                    if api_calls % 5 == 0:
                        lib.save_json(lib.NEW_ITEM_CACHE, cache)
                errors.pop(key, None)
                proposals.append(_to_row(data, entries, rep))
                print(f"[{i}/{n}] {'call ' if api else 'cache'} {rep['filename'][:55]:55s} "
                      f"-> {data.get('canonical_product_name','')[:40]}", flush=True)
            except Exception as e:
                errors[key] = f"{type(e).__name__}: {e} (rep={rep['filename']})"
                print(f"[{i}/{n}] ERROR {rep['filename'][:55]} :: {e}", flush=True)
    except KeyboardInterrupt:
        lib.save_json(lib.NEW_ITEM_CACHE, cache)
        lib.save_json(lib.NEW_ITEM_ERRORS, errors)
        print("\nInterrupted — progress saved. Re-run to resume (cached groups are skipped).")
        return 130

    lib.save_json(lib.NEW_ITEM_CACHE, cache)
    lib.save_json(lib.NEW_ITEM_ERRORS, errors)
    if not args.sample and not args.limit:
        _write_proposals(lib.NEW_ITEM_PROPOSALS_CSV, proposals)

    print(f"\nProposed {len(proposals)} new item(s) (this run touched {n} group(s)). "
          f"API calls: {api_calls}. Errors: {len(errors)}.")
    if not args.sample and not args.limit:
        print(f"  {lib.NEW_ITEM_PROPOSALS_CSV}")
    if errors:
        print(f"  {len(errors)} error(s) -> {lib.NEW_ITEM_ERRORS}")
    return 0


def _to_row(data: dict, entries: list[dict], rep: dict) -> dict:
    row = {c: "" for c in lib.ACTIVE_CATALOG_COLUMNS}
    row["Item Name"] = data.get("canonical_product_name", "") or ""
    row["Category"] = data.get("category", "") if data.get("category") in lib.ACTIVE_CATEGORIES else "Chemistry"
    row["Pricing Model"] = DEFAULT_PRICING_MODEL
    vendor_folder = rep.get("vendor", "")
    row["Preferred Vendor"] = VENDOR_TO_PREFERRED_VENDOR.get(vendor_folder, "")
    row["Manufacturer"] = data.get("manufacturer", "") or ""
    row["Manufacturer Product Code"] = data.get("manufacturer_product_code") or ""
    row["Is Commodity"] = lib.checked(data.get("is_commodity"))
    row["Product Category"] = data.get("product_category", "") or ""
    row["Function Description"] = data.get("function_description", "") or ""
    lines = [x for x in (data.get("applicable_lines") or []) if x in lib.APPLICABLE_LINES]
    row["Applicable Lines"] = json.dumps(lines, ensure_ascii=False)
    pos = data.get("primary_line_position") or ""
    row["Primary Line Position"] = pos if pos in lib.LINE_POSITIONS else ""
    sub = data.get("primary_line_sub_position") or ""
    row["Primary Line Sub Position"] = sub if sub in lib.LINE_SUB_POSITIONS else ""
    ut = data.get("use_type") or ""
    row["Use Type"] = ut if ut in lib.USE_TYPES else ""
    row["System Name"] = data.get("system_name") or ""
    row["System Components"] = json.dumps(data.get("system_components") or [], ensure_ascii=False)
    row["Sequence Companions"] = json.dumps(data.get("sequence_companions") or [], ensure_ascii=False)
    rr = data.get("replenishment_role") or ""
    row["Replenishment Role"] = rr if rr in lib.REPLENISHMENT_ROLES else ""
    row["TDS Filename"] = rep["filename"]
    # --- extra review-only columns (NOT part of catalog_active.csv) ---
    row["_source_tds_filenames"] = "; ".join(sorted({e["filename"] for e in entries}))
    row["_source_vendor"] = rep.get("vendor", "")
    row["_group_size"] = len(entries)
    notes = ["NEEDS: QB Item ID, DOJ Controlled, and a packaging/size decision for Item Name."]
    if vendor_folder == "ABCO":
        notes.append("Preferred Vendor defaulted to 'Chemical Compounding' (the majority case "
                     "for ABCO-folder TDS) but ~1/3 of ABCO-branded rows use 'ABCO' instead — confirm.")
    if data.get("confidence_notes"):
        notes.append(data["confidence_notes"])
    row["_review_notes"] = " ".join(notes)
    return row


def _write_proposals(path, rows) -> None:
    cols = lib.ACTIVE_CATALOG_COLUMNS + [
        "_source_tds_filenames", "_source_vendor", "_group_size", "_review_notes",
    ]
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


if __name__ == "__main__":
    sys.exit(main())
