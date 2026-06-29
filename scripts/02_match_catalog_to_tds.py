#!/usr/bin/env python
"""
02 — Match active chemistry items to their TDS (API, cached).

Catalog-driven: for each ACTIVE Chemistry item we fuzzy-rank the indexed TDS by
filename, then ask Claude to verify the top candidate(s) against the TDS text
("is this the technical data sheet for THIS product?"). Supplies/Equipment have
no TDS and are skipped here — they are handled by 03 inference.

Verification is text-based (uses the cheap snippet from the index, or the native
PDF only when a file had no extractable text), so it stays inexpensive; the
high-fidelity native-PDF read happens in 03 for confirmed matches only.

Outputs:
  data/output/catalog_tds_matches.csv   confirmed/attempted matches
  data/output/catalog_match_review.csv  low-confidence matches to eyeball
  data/output/match_cache.json          (item, tds) -> verification (idempotent)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys

from rapidfuzz import fuzz, process

import lib

# --- config ----------------------------------------------------------------
FUZZY_FLOOR = 55          # ignore TDS whose filename score is below this
TOP_K = 3                 # verify at most this many candidates per item
ACCEPT_CONF = 85          # verified & confidence>=this -> tier 1
EARLY_ACCEPT = 95         # a verified hit this strong stops the candidate search
REVIEW_CONF = 60          # verified & confidence in [REVIEW_CONF, ACCEPT_CONF) -> tier 2/3
VERIFY_EFFORT = "medium"

VERIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verified": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "canonical_product_name": {"type": ["string", "null"]},
        "manufacturer_product_code": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["verified", "confidence", "canonical_product_name",
                 "manufacturer_product_code", "reason"],
}

VERIFY_SYSTEM = """You are an expert in metal-finishing chemistry verifying whether a Technical \
Data Sheet (TDS) is the correct data sheet for a specific catalog product.

You are given a CATALOG PRODUCT (name + brand) and the text of one TDS. Decide \
whether this TDS describes THAT EXACT product — not merely a related product, a \
different size, or a different variant in the same family.

Be strict about variants: "Meta-Plate 2500" and "Meta-Plate 2500-C" are \
different products; "Iridite 14" and "Iridite 14-2" are different products. \
Brand/size wording in the catalog name (e.g. "(MacDermid ... (gal))") is just \
packaging metadata — match on the product identity, not the packaging.

Return JSON only:
- verified: true only if this TDS is the data sheet for the catalog product.
- confidence: 0-100 (your certainty in the verified decision).
- canonical_product_name: the product name exactly as printed in the TDS (or null).
- manufacturer_product_code: the product/order code printed in the TDS, if any (or null).
- reason: one concise sentence.
"""


def rank_candidates(norm_name: str, index: list[dict], choices: list[str]):
    """Return up to TOP_K (entry, score) above the floor, by filename similarity."""
    hits = process.extract(norm_name, choices, scorer=fuzz.token_sort_ratio, limit=TOP_K)
    out = []
    for _, score, idx in hits:
        if score >= FUZZY_FLOOR:
            out.append((index[idx], score))
    return out


def verify(client, model, item_name, brand, entry) -> dict:
    snippet = entry.get("snippet") or ""
    user = [
        {"type": "text",
         "text": f"CATALOG PRODUCT: {item_name}\nBRAND: {brand or '(unknown)'}\n\n"
                 f"TDS FILENAME: {entry['filename']}\n\nTDS TEXT:\n"},
    ]
    if snippet.strip():
        user.append({"type": "text", "text": snippet})
    else:
        # no extractable text — fall back to the native PDF
        user.append(lib.pdf_block(lib.TDS_DIR / entry["rel_path"]))
    result, _ = lib.call_structured(
        client, model, lib.cached_text(VERIFY_SYSTEM), user, VERIFY_SCHEMA,
        effort=VERIFY_EFFORT, max_tokens=1024,
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=lib.DEFAULT_MODEL)
    ap.add_argument("--sample", type=int, default=0,
                    help="verify only the N active-chemistry items with the strongest TDS candidate")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N items")
    args = ap.parse_args()

    _, rows = lib.load_catalog()
    index = lib.load_json(lib.TDS_INDEX, default=[])
    if not index:
        sys.exit("No TDS index found. Run 01_index_tds.py first.")
    choices = [lib.tds_match_key(e["filename"]) for e in index]
    manual = lib.load_manual_matches(index)  # Item -> {tds_filename, rel_path}

    targets = [r for r in rows
               if lib.g(r, "status") == "active" and lib.g(r, "item_category") == "Chemistry"]

    # precompute best candidate per item (for sample selection / ordering)
    enriched_targets = []
    for r in targets:
        norm = lib.normalize(lib.g(r, "Item"))
        cands = rank_candidates(norm, index, choices)
        best = cands[0][1] if cands else 0
        enriched_targets.append((r, norm, cands, best))

    if args.sample:
        enriched_targets.sort(key=lambda t: t[3], reverse=True)
        enriched_targets = enriched_targets[:args.sample]
    elif args.limit:
        enriched_targets = enriched_targets[:args.limit]

    cache = lib.load_json(lib.MATCH_CACHE, default={})
    client = lib.get_client()
    n = len(enriched_targets)
    cached_at_start = sum(1 for (r, _, c, _) in enriched_targets
                          for e, _ in c if f"{lib.g(r,'Item')}||{e['filename']}" in cache)
    print(f"Matching {n} active-chemistry item(s) "
          f"({len(manual)} manual link(s); ~{cached_at_start} candidate verification(s) cached). "
          f"Re-run after a failure to resume.", flush=True)

    matches, review = [], []
    api_calls = 0
    try:
        for i, (r, norm, cands, best) in enumerate(enriched_targets, 1):
            item = lib.g(r, "Item")
            brand = lib.g(r, "Brand")

            # manual links win outright — no fuzzy/verify
            if item in manual:
                mm = manual[item]
                matches.append({
                    "Item": item, "tds_filename": mm["tds_filename"], "rel_path": mm["rel_path"],
                    "fuzzy_score": "", "verified": True, "confidence": 100,
                    "manufacturer_product_code": "", "canonical_product_name": "",
                    "match_tier": 1, "match_basis": "manual",
                    "reason": "manually linked", "alternatives": "",
                })
                print(f"[{i}/{n}] manual   {item[:60]} -> {mm['tds_filename']}", flush=True)
                continue

            # Verify candidates (fuzzy order); keep every VERIFIED one, stop early
            # only on a near-certain hit. Highest-confidence verified wins.
            verified = []
            for entry, fscore in cands:
                key = f"{item}||{entry['filename']}"
                if key in cache:
                    res = cache[key]
                else:
                    res = verify(client, args.model, item, brand, entry)
                    cache[key] = res
                    api_calls += 1
                    if api_calls % 10 == 0:
                        lib.save_json(lib.MATCH_CACHE, cache)
                if res.get("verified") and res.get("confidence", 0) >= REVIEW_CONF:
                    verified.append((entry, fscore, res))
                    if res.get("confidence", 0) >= EARLY_ACCEPT:
                        break

            if not verified:
                print(f"[{i}/{n}] no-match {item[:60]} ({len(cands)} candidate(s))", flush=True)
                continue

            chosen = max(verified, key=lambda c: c[2].get("confidence", 0))
            entry, fscore, res = chosen
            conf = res.get("confidence", 0)
            code = (res.get("manufacturer_product_code") or "").strip()
            if conf >= ACCEPT_CONF:
                tier, basis = 1, (f"product_code:{code}" if code else "tds_verified")
            else:
                tier = 2 if conf >= 75 else 3
                basis = "tds_verified_low_conf"
            alts = "; ".join(
                f"{e['filename']} (conf {r.get('confidence', 0)})"
                for e, _, r in verified if e["filename"] != entry["filename"]
            )
            matches.append({
                "Item": item, "tds_filename": entry["filename"], "rel_path": entry["rel_path"],
                "fuzzy_score": fscore, "verified": res.get("verified"), "confidence": conf,
                "manufacturer_product_code": code,
                "canonical_product_name": res.get("canonical_product_name") or "",
                "match_tier": tier, "match_basis": basis,
                "reason": res.get("reason", ""), "alternatives": alts,
            })
            if tier != 1:
                review.append(matches[-1])
            print(f"[{i}/{n}] tier-{tier}   {item[:60]} -> {entry['filename']}", flush=True)
    except KeyboardInterrupt:
        lib.save_json(lib.MATCH_CACHE, cache)
        print("\nInterrupted — verification cache saved. Re-run to resume "
              "(cached candidates are skipped).")
        return 130

    lib.save_json(lib.MATCH_CACHE, cache)
    _write_csv(lib.MATCHES_CSV, matches)
    _write_review(lib.MATCH_REVIEW_CSV, review)

    tier1 = sum(1 for m in matches if m["match_tier"] == 1)
    print(f"Matched {len(matches)} item(s) ({tier1} tier-1, {len(review)} for review) "
          f"from {len(enriched_targets)} active-chemistry target(s). API calls: {api_calls}.")
    print(f"  {lib.MATCHES_CSV}")
    print(f"  {lib.MATCH_REVIEW_CSV}  <- set `approved` = NO to reject a tier-2/3 match")
    return 0


def _write_csv(path, recs) -> None:
    cols = ["Item", "tds_filename", "rel_path", "fuzzy_score", "verified",
            "confidence", "manufacturer_product_code", "canonical_product_name",
            "match_tier", "match_basis", "reason", "alternatives"]
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow(r)


def _write_review(path, recs) -> None:
    """Write the tier-2/3 review file with an editable `approved` column, and
    preserve any approvals already entered on a prior run (don't clobber edits)."""
    prev = lib.read_match_approvals(path)
    cols = ["Item", "approved", "tds_filename", "fuzzy_score", "confidence",
            "manufacturer_product_code", "canonical_product_name",
            "match_tier", "match_basis", "reason", "alternatives"]
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            row = dict(r)
            row["approved"] = prev.get(r["Item"], "")  # blank = accept by default
            w.writerow(row)


if __name__ == "__main__":
    sys.exit(main())
