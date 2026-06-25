#!/usr/bin/env python
"""
03 — Enrich items by their trusted item_category (API, cached). The main spend.

Routing (per METHODOLOGY):
  - Chemistry WITH a confirmed TDS (from 02) -> extract full schema from the
    native PDF.
  - Chemistry WITHOUT a TDS (commodities / no match) -> infer the full schema
    from name + brand + domain knowledge.
  - Supplies / Equipment (no TDS exists) -> infer only the columns that apply
    (function_description, product_category, applicable_lines, size, name).

Scope = all active chemistry + active supplies/equipment + orphan legacy
chemistry. Legacy items that map to an active item inherit in 04 (not here).
Brand is filled where blank. Everything is provenance-tagged and cached.

Outputs:
  data/output/enriched.json     Item -> enrichment record
  data/output/enrich_errors.json
  data/output/enrich_cache.json (idempotent)
"""

from __future__ import annotations

import argparse
import csv
import sys

import lib

# --- controlled-vocab text blocks (embedded in the cached prompts) ----------
_LINES = "\n".join(f"  - {x}" for x in lib.APPLICABLE_LINES)
_POS = "\n".join(f"  - {x}" for x in lib.LINE_POSITIONS)
_SUB = "\n".join(f"  - {x}" for x in lib.LINE_SUB_POSITIONS)
_VOCAB = (
    f"applicable_lines — choose zero or more EXACTLY from:\n{_LINES}\n\n"
    f"primary/secondary_line_position — choose EXACTLY one from:\n{_POS}\n\n"
    f"primary/secondary_line_sub_position — choose EXACTLY one from:\n{_SUB}\n\n"
    f"use_type: standalone | system_component\n"
    f"replenishment_role: makeup_only | replenishment_only | both | not_applicable\n"
    f"application_method: immersion | electroplating | barrel | spray | brush_swab | multiple\n"
)

# --- JSON schemas (type-level; vocab enforced by prompt + post-coercion) -----
_NS = ["string", "null"]
CHEM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "canonical_product_name": {"type": "string"},
        "manufacturer": {"type": "string"},
        "manufacturer_product_code": {"type": _NS},
        "size": {"type": _NS},
        "is_commodity": {"type": "boolean"},
        "product_category": {"type": "string"},
        "function_description": {"type": "string"},
        "applicable_lines": {"type": "array", "items": {"type": "string"}},
        "primary_line_position": {"type": "string"},
        "primary_line_sub_position": {"type": "string"},
        "secondary_line_position": {"type": _NS},
        "secondary_line_sub_position": {"type": _NS},
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
        "application_method": {"type": "string"},
        "typical_concentration": {"type": _NS},
    },
    "required": ["canonical_product_name", "manufacturer", "manufacturer_product_code",
                 "size", "is_commodity", "product_category", "function_description",
                 "applicable_lines", "primary_line_position", "primary_line_sub_position",
                 "secondary_line_position", "secondary_line_sub_position", "use_type",
                 "system_name", "system_components", "sequence_companions",
                 "replenishment_role", "application_method", "typical_concentration"],
}
SUPPLY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "canonical_product_name": {"type": "string"},
        "size": {"type": _NS},
        "product_category": {"type": "string"},
        "function_description": {"type": "string"},
        "applicable_lines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["canonical_product_name", "size", "product_category",
                 "function_description", "applicable_lines"],
}

# --- prompts ----------------------------------------------------------------
CHEM_TDS_SYSTEM = f"""You are an expert in metal finishing, electroplating, and surface-treatment \
chemistry. You are given the catalog name of ONE product and the attached \
Technical Data Sheet (TDS) that has already been verified to describe it. \
Extract a single structured record for THAT product.

EXTRACT VERBATIM from the TDS (no invention): canonical_product_name, \
manufacturer, manufacturer_product_code, size, typical_concentration. Use null \
when a field is genuinely absent.

INFER CONFIDENTLY from the chemistry, application, and companions when the TDS \
is implicit: product_category, function_description (1-3 plain sentences), \
applicable_lines, line positions, use_type, replenishment_role, \
application_method, is_commodity (true only for generic commodity chemicals).

system_components: every product in the same named system (incl. this one), \
names copied EXACTLY from the TDS. sequence_companions: products named as used \
before/after this one (relationship precedes|follows, direction before|after). \
Empty arrays if none. Do not invent companions.

CONTROLLED VOCABULARY — use these strings exactly:
{_VOCAB}
Return JSON only."""

CHEM_INFER_SYSTEM = f"""You are an expert in metal finishing and electroplating chemistry. No TDS is \
available for the product below; infer a structured record from its catalog name, \
brand, and your domain knowledge. Be accurate and conservative — if you cannot \
determine a field, use null / empty / "not_applicable" rather than guessing wildly.

Set is_commodity true for generic commodity chemicals (e.g. Sodium Hydroxide, \
Sulfuric Acid, Nitric Acid). Provide a clear product_category and a 1-3 sentence \
function_description. Leave manufacturer_product_code, size, typical_concentration, \
system_components, and sequence_companions empty/null unless obvious from the name.

CONTROLLED VOCABULARY — use these strings exactly:
{_VOCAB}
Return JSON only."""

SUPPLY_INFER_SYSTEM = f"""You are an expert in metal-finishing shop supplies and equipment. The item \
below is a physical SUPPLY or EQUIPMENT item (an anode, racking wire, anode bag, \
basket, rectifier, pump, tank, filter, etc.) — NOT a chemical, and it has no TDS. \
Infer only what the catalog name supports:

- canonical_product_name: a clean product name.
- size: any dimension/gauge/capacity in the name (or null).
- product_category: e.g. "Anode", "Anode Bag", "Racking Wire", "Rectifier",
  "Pump", "Filter Media", "Anode Basket", "Plating Rack", "Heater".
- function_description: 1-2 plain sentences on what it is and how a plater uses it.
- applicable_lines: the plating lines this item plausibly serves (e.g. a zinc
  anode -> Zinc (pure)/Zinc-nickel; aluminum racking wire -> Anodize Type II/III;
  a general rectifier may serve none-in-particular -> empty array). Choose EXACTLY
  from the controlled list; use an empty array when it is line-agnostic.

CONTROLLED VOCABULARY for applicable_lines — use these strings exactly:
{_LINES}

Return JSON only."""


def coerce_vocab(rec: dict) -> dict:
    """Snap controlled-vocab fields to valid values; drop invalid ones."""
    if "applicable_lines" in rec:
        valid = set(lib.APPLICABLE_LINES)
        rec["applicable_lines"] = [x for x in (rec.get("applicable_lines") or []) if x in valid]
    for key, vocab in (("primary_line_position", lib.LINE_POSITIONS),
                       ("secondary_line_position", lib.LINE_POSITIONS),
                       ("primary_line_sub_position", lib.LINE_SUB_POSITIONS),
                       ("secondary_line_sub_position", lib.LINE_SUB_POSITIONS)):
        if key in rec:
            v = (rec.get(key) or "")
            rec[key] = v if v in vocab else ""
    for key, vocab, default in (("use_type", lib.USE_TYPES, "standalone"),
                                ("replenishment_role", lib.REPLENISHMENT_ROLES, "not_applicable"),
                                ("application_method", lib.APPLICATION_METHODS, "")):
        if key in rec:
            rec[key] = rec[key] if rec.get(key) in vocab else default
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=lib.DEFAULT_MODEL)
    ap.add_argument("--sample", type=int, default=0,
                    help="enrich a small representative set covering every path")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    _, rows = lib.load_catalog()
    active_items = {lib.g(r, "Item") for r in rows if lib.g(r, "status") == "active"}
    matches = _read_matches()

    def is_orphan_legacy(r):
        return lib.g(r, "status") == "legacy" and lib.g(r, "superseded_by") not in active_items

    # Build the work list with a route per item.
    work = []  # (row, route)  route in {tds, chem_infer, supply_infer}
    for r in rows:
        st, cat = lib.g(r, "status"), lib.g(r, "item_category")
        if st == "active" and cat == "Chemistry":
            work.append((r, "tds" if lib.g(r, "Item") in matches else "chem_infer"))
        elif st == "active" and cat in ("Supplies", "Equipment"):
            work.append((r, "supply_infer"))
        elif cat == "Chemistry" and is_orphan_legacy(r):
            work.append((r, "chem_infer"))

    if args.sample:
        work = _sample(work)
    elif args.limit:
        work = work[:args.limit]

    cache = lib.load_json(lib.ENRICH_CACHE, default={})
    enriched = lib.load_json(lib.ENRICHED_JSON, default={})
    errors = {}
    client = lib.get_client()
    api_calls = 0

    for r, route in work:
        item = lib.g(r, "Item")
        try:
            rec = _enrich_one(client, args.model, r, route, matches, cache)
            if rec.get("_api"):
                api_calls += 1
            rec.pop("_api", None)
            enriched[item] = rec
            if api_calls and api_calls % 10 == 0:
                lib.save_json(lib.ENRICH_CACHE, cache)
                lib.save_json(lib.ENRICHED_JSON, enriched)
        except Exception as e:  # keep going; log the failure
            errors[item] = f"{type(e).__name__}: {e}"

    lib.save_json(lib.ENRICH_CACHE, cache)
    lib.save_json(lib.ENRICHED_JSON, enriched)
    lib.save_json(lib.ENRICH_ERRORS, errors)

    print(f"Enriched {len(enriched)} item(s) (this run touched {len(work)}). "
          f"API calls: {api_calls}. Errors: {len(errors)}.")
    print(f"  {lib.ENRICHED_JSON}")
    if errors:
        print(f"  {len(errors)} error(s) -> {lib.ENRICH_ERRORS}")
    return 0


def _enrich_one(client, model, r, route, matches, cache) -> dict:
    item = lib.g(r, "Item")
    brand = lib.g(r, "Brand")

    if route == "tds":
        m = matches[item]
        key = f"tds::{item}::{m['tds_filename']}"
        if key in cache:
            data = cache[key]; api = False
        else:
            user = [lib.pdf_block(lib.TDS_DIR / m["rel_path"]),
                    {"type": "text", "text": f"CATALOG PRODUCT NAME: {item}\n"
                     f"BRAND: {brand or '(unknown)'}\n"
                     "Extract the single record for THIS product from the attached TDS."}]
            data, _ = lib.call_structured(client, model, lib.cached_text(CHEM_TDS_SYSTEM),
                                          user, CHEM_SCHEMA, max_tokens=4096)
            cache[key] = data; api = True
        rec = coerce_vocab(dict(data))
        rec["tds_filename"] = m["tds_filename"]
        rec["match_tier"] = m["match_tier"]
        rec["match_score"] = m["confidence"]
        rec["match_basis"] = m["match_basis"]
        if not (rec.get("manufacturer_product_code") or "").strip() and m.get("manufacturer_product_code"):
            rec["manufacturer_product_code"] = m["manufacturer_product_code"]

    elif route == "chem_infer":
        key = f"chem::{item}"
        if key in cache:
            data = cache[key]; api = False
        else:
            user = [{"type": "text", "text": f"PRODUCT (catalog name): {item}\n"
                     f"BRAND: {brand or '(unknown)'}\n"
                     "No TDS is available. Infer the record."}]
            data, _ = lib.call_structured(client, model, lib.cached_text(CHEM_INFER_SYSTEM),
                                          user, CHEM_SCHEMA, max_tokens=3072)
            cache[key] = data; api = True
        rec = coerce_vocab(dict(data))
        rec["tds_filename"] = ""
        rec["match_tier"] = 4
        rec["match_score"] = 70
        rec["match_basis"] = "llm_inference"

    else:  # supply_infer
        key = f"supply::{item}"
        if key in cache:
            data = cache[key]; api = False
        else:
            user = [{"type": "text", "text": f"ITEM (catalog name): {item}\n"
                     f"BRAND: {brand or '(unknown)'}\n"
                     f"item_category: {lib.g(r, 'item_category')}\n"
                     "Infer the record for this supply/equipment item."}]
            data, _ = lib.call_structured(client, model, lib.cached_text(SUPPLY_INFER_SYSTEM),
                                          user, SUPPLY_SCHEMA, max_tokens=1536)
            cache[key] = data; api = True
        rec = coerce_vocab(dict(data))
        rec["tds_filename"] = ""
        rec["match_tier"] = 4
        rec["match_score"] = 70
        rec["match_basis"] = "llm_inference"

    # Brand fill where blank
    rec_brand = brand
    if not rec_brand:
        rec_brand = (rec.get("manufacturer") or "").strip() or lib.brand_from_item_name(item)
    rec["Brand"] = rec_brand
    rec["_api"] = api
    return rec


def _read_matches() -> dict:
    out = {}
    if lib.MATCHES_CSV.exists():
        with open(lib.MATCHES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["Item"]] = row
    return out


def _sample(work):
    """A small representative set covering every enrichment path."""
    by_route = {"tds": [], "chem_infer": [], "supply_infer": []}
    for r, route in work:
        by_route[route].append((r, route))
    # supply_infer split into Supplies vs Equipment for coverage
    supplies = [t for t in by_route["supply_infer"] if lib.g(t[0], "item_category") == "Supplies"]
    equipment = [t for t in by_route["supply_infer"] if lib.g(t[0], "item_category") == "Equipment"]
    chosen = (by_route["tds"][:4] + by_route["chem_infer"][:2]
              + supplies[:2] + equipment[:1])
    # de-dup preserving order
    seen, out = set(), []
    for r, route in chosen:
        k = lib.g(r, "Item")
        if k not in seen:
            seen.add(k); out.append((r, route))
    return out


if __name__ == "__main__":
    sys.exit(main())
