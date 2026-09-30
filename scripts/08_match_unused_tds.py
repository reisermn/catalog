#!/usr/bin/env python
"""
08 — Try to match "unused" TDS back onto an existing catalog row (API, cached).

Inverse of the old 02_match_catalog_to_tds.py: there, we searched TDS for a
given catalog item. Here, tds_all/ has ~3-6x more files than the catalog
references, so for every TDS NOT already used by catalog_active.csv we ask
"does this actually belong to a catalog item we just never linked?" — e.g. a
TDS filed under a slightly different name, or a chemistry row that was never
given a TDS in the first place.

Method: fuzzy-rank catalog items against the TDS's normalized filename (same
normalize()/tds_match_key() used everywhere else), with a small score boost
when the TDS's vendor folder matches the catalog row's Manufacturer/Preferred
Vendor. Verify the top candidates with Claude (cheap text-snippet based, same
tiering as before). Never writes to catalog_active.csv (inputs are read-only)
— matches are a *suggestion* for a human to apply by hand.

Anything with no accepted match falls through to data/output/tds_unmatched.json
-> input to 09_propose_new_items.py.

Outputs:
  data/output/tds_matched_to_existing.csv  tier-1 suggested backfills
  data/output/tds_rematch_review.csv       tier-2/3 suggested backfills (editable `approved`)
  data/output/tds_unmatched.json           no confident match -> candidate for a new row
  data/output/tds_rematch_cache.json       (tds_filename, Item Name) -> verification (idempotent)
"""

from __future__ import annotations

import argparse
import csv
import sys

from rapidfuzz import fuzz, process

import lib

# --- config -----------------------------------------------------------------
# FUZZY_FLOOR is higher than 02's (55): this direction ranks each unused TDS
# against ALL ~800 catalog items rather than checking one known candidate, and
# product-family naming (e.g. "Isoprep 500L" vs "Isoprep 49L", "Kenvert 181" vs
# "Kenvert 11") makes short normalized names collide well above 55 purely on
# shared brand/base-name tokens with no real product-identity overlap — an
# empirical check of the 55-75 band found it ~100% false positives. 75 keeps
# recall for genuine misses while cutting that noise.
FUZZY_FLOOR = 75
TOP_K = 3
ACCEPT_CONF = 85
EARLY_ACCEPT = 95
REVIEW_CONF = 60
VERIFY_EFFORT = "medium"

VENDOR_HINTS = {
    "ABCO": ["abco"],
    "Florida Cirtech": ["cirtech", "florida"],
    "MacDermid": ["macdermid", "enthone"],
    "Metal Chem": ["metal chem"],
    "Stellar Solutions": ["stellar"],
    "Tolber": ["tolber"],
    "US Specialty": ["specialty", "u.s. specialty", "us specialty"],
}
VENDOR_BOOST = 8

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
Data Sheet (TDS) is the correct data sheet for a specific catalog product. The \
catalog product currently has NO TDS on file, so you are checking whether this \
TDS was simply never linked to it (a missed match), not re-confirming an \
existing link.

You are given a CATALOG PRODUCT (name + manufacturer/vendor) and the text of \
one TDS. Decide whether this TDS describes THAT EXACT product — not merely a \
related product, a different size, or a different variant in the same family.

Be strict about variants: "Meta-Plate 2500" and "Meta-Plate 2500-C" are \
different products; "Iridite 14" and "Iridite 14-2" are different products. \
Brand/size wording in the catalog name (e.g. "(100 lb)") is just packaging \
metadata — match on the product identity, not the packaging.

Return JSON only:
- verified: true only if this TDS is the data sheet for the catalog product.
- confidence: 0-100 (your certainty in the verified decision).
- canonical_product_name: the product name exactly as printed in the TDS (or null).
- manufacturer_product_code: the product/order code printed in the TDS, if any (or null).
- reason: one concise sentence.
"""


def vendor_boost(entry: dict, row: dict) -> int:
    hints = VENDOR_HINTS.get(entry.get("vendor", ""), [])
    if not hints:
        return 0
    haystack = (lib.g(row, "Manufacturer") + " " + lib.g(row, "Preferred Vendor")).lower()
    return VENDOR_BOOST if any(h in haystack for h in hints) else 0


def rank_candidates(entry: dict, rows: list[dict], norm_names: list[str]):
    """Up to TOP_K (row, score) above the floor, by filename similarity + vendor boost."""
    hits = process.extract(entry["match_key"], norm_names, scorer=fuzz.token_sort_ratio,
                            limit=TOP_K * 3)
    scored = []
    for _, score, idx in hits:
        s = score + vendor_boost(entry, rows[idx])
        scored.append((rows[idx], s))
    scored.sort(key=lambda t: t[1], reverse=True)
    return [(r, s) for r, s in scored[:TOP_K] if s >= FUZZY_FLOOR]


def verify(client, model, entry, item_name, manufacturer) -> dict:
    snippet = entry.get("snippet") or ""
    user = [
        {"type": "text",
         "text": f"CATALOG PRODUCT: {item_name}\nMANUFACTURER/VENDOR: {manufacturer or '(unknown)'}\n\n"
                 f"TDS FILENAME: {entry['filename']}\n\nTDS TEXT:\n"},
    ]
    if snippet.strip():
        user.append({"type": "text", "text": snippet})
    else:
        user.append(lib.pdf_block(lib.TDS_ALL_DIR / entry["rel_path"]))
    result, _ = lib.call_structured(
        client, model, lib.cached_text(VERIFY_SYSTEM), user, VERIFY_SCHEMA,
        effort=VERIFY_EFFORT, max_tokens=1024,
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=lib.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                     help="rank candidates and report cost/coverage — no API calls, no output files")
    ap.add_argument("--sample", type=int, default=0,
                     help="verify only the N unused TDS with the strongest candidate")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--vendor", default="",
                     help="restrict to one tds_all/ vendor folder, e.g. MacDermid "
                          "(tds_unmatched.json is fully overwritten each run — "
                          "re-run for a different vendor when ready to expand scope)")
    args = ap.parse_args()

    _, rows = lib.load_catalog(lib.ACTIVE_CATALOG_CSV)
    index = lib.load_json(lib.TDS_ALL_INDEX, default=[])
    if not index:
        sys.exit("No TDS index found. Run 07_index_tds_all.py first.")
    if args.vendor:
        index = [e for e in index if e.get("vendor") == args.vendor]
        if not index:
            sys.exit(f"No indexed TDS for vendor {args.vendor!r}.")

    used_filenames = {lib.g(r, "TDS Filename") for r in rows if lib.g(r, "TDS Filename")}
    unused = [e for e in index if e["filename"] not in used_filenames]
    norm_names = [lib.normalize(lib.g(r, "Item Name")) for r in rows]

    ranked = []  # (entry, [(row, score), ...])
    for e in unused:
        ranked.append((e, rank_candidates(e, rows, norm_names)))

    with_cand = [t for t in ranked if t[1]]
    print(f"{len(index)} TDS indexed, {len(used_filenames)} already used by the catalog, "
          f"{len(unused)} unused.")
    print(f"  {len(with_cand)} unused TDS have >=1 candidate catalog item above the fuzzy "
          f"floor ({FUZZY_FLOOR}) -> would need verification.")
    print(f"  {len(unused) - len(with_cand)} have no plausible candidate at all -> straight "
          f"to the new-item pool (zero API cost for these).")

    if args.dry_run:
        by_score = sorted(with_cand, key=lambda t: t[1][0][1], reverse=True)
        print("\nTop 15 candidates by fuzzy+vendor score (preview, no API calls made):")
        for e, cands in by_score[:15]:
            top_row, score = cands[0]
            print(f"  [{score:5.1f}] {e['filename'][:55]:55s} -> {lib.g(top_row,'Item Name')[:55]}")
        print(f"\nEstimated verification calls if run for real: up to "
              f"{sum(len(c) for _, c in with_cand)} (usually far fewer — verification "
              f"stops early on a confident hit per TDS).")
        return 0

    if args.sample:
        with_cand.sort(key=lambda t: t[1][0][1], reverse=True)
        with_cand = with_cand[:args.sample]
    elif args.limit:
        with_cand = with_cand[:args.limit]
    no_candidate = [] if (args.sample or args.limit) else [e for e, c in ranked if not c]

    cache = lib.load_json(lib.TDS_REMATCH_CACHE, default={})
    client = lib.get_client()
    n = len(with_cand)
    print(f"\nVerifying {n} unused TDS against their top candidate(s). "
          f"Re-run after a failure to resume.", flush=True)

    accepted, review, rejected = [], [], []
    api_calls = 0
    try:
        for i, (entry, cands) in enumerate(with_cand, 1):
            verified, considered = [], []
            for row, fscore in cands:
                item = lib.g(row, "Item Name")
                key = f"{entry['filename']}||{item}"
                if key in cache:
                    res = cache[key]
                else:
                    res = verify(client, args.model, entry, item, lib.g(row, "Manufacturer"))
                    cache[key] = res
                    api_calls += 1
                    if api_calls % 10 == 0:
                        lib.save_json(lib.TDS_REMATCH_CACHE, cache)
                considered.append((row, fscore, res))
                if res.get("verified") and res.get("confidence", 0) >= REVIEW_CONF:
                    verified.append((row, fscore, res))
                    if res.get("confidence", 0) >= EARLY_ACCEPT:
                        break

            if not verified:
                rejected.append(entry)
                print(f"[{i}/{n}] no-match {entry['filename'][:55]}", flush=True)
                continue

            row, fscore, res = max(verified, key=lambda c: c[2].get("confidence", 0))
            conf = res.get("confidence", 0)
            code = (res.get("manufacturer_product_code") or "").strip()
            tier = 1 if conf >= ACCEPT_CONF else (2 if conf >= 75 else 3)
            basis = (f"product_code:{code}" if (tier == 1 and code) else
                     ("tds_verified" if tier == 1 else "tds_verified_low_conf"))
            rec = {
                "Item Name": lib.g(row, "Item Name"), "suggested_tds_filename": entry["filename"],
                "rel_path": entry["rel_path"], "fuzzy_score": round(fscore, 1),
                "confidence": conf, "manufacturer_product_code": code,
                "canonical_product_name": res.get("canonical_product_name") or "",
                "match_tier": tier, "match_basis": basis, "reason": res.get("reason", ""),
                "current_tds_filename": lib.g(row, "TDS Filename"),
            }
            accepted.append(rec)
            if tier != 1:
                review.append(rec)
            print(f"[{i}/{n}] tier-{tier}   {entry['filename'][:45]:45s} -> {rec['Item Name'][:45]}",
                  flush=True)
    except KeyboardInterrupt:
        lib.save_json(lib.TDS_REMATCH_CACHE, cache)
        print("\nInterrupted — verification cache saved. Re-run to resume.")
        return 130

    lib.save_json(lib.TDS_REMATCH_CACHE, cache)

    if not args.sample and not args.limit:
        _write_matched(lib.TDS_MATCHED_TO_EXISTING_CSV, accepted)
        _write_review(lib.TDS_REMATCH_REVIEW_CSV, review)

        approvals = lib.read_match_approvals(lib.TDS_REMATCH_REVIEW_CSV, key_col="suggested_tds_filename")
        rejected_filenames = {r["suggested_tds_filename"] for r in review
                               if approvals.get(r["suggested_tds_filename"], "") == "NO"}
        unmatched_entries = rejected + no_candidate + [
            e for e, c in with_cand if e["filename"] in rejected_filenames
        ]
        # de-dup by filename, keep full entry dicts (filename/rel_path/vendor) for stage 09
        seen, unmatched_out = set(), []
        for e in unmatched_entries:
            if e["filename"] not in seen:
                seen.add(e["filename"])
                unmatched_out.append({"filename": e["filename"], "rel_path": e["rel_path"],
                                       "vendor": e["vendor"], "match_key": e["match_key"]})
        lib.save_json(lib.TDS_UNMATCHED_JSON, unmatched_out)

        tier1 = sum(1 for a in accepted if a["match_tier"] == 1)
        print(f"\nMatched {len(accepted)} unused TDS to an existing catalog row "
              f"({tier1} tier-1, {len(review)} for review). API calls: {api_calls}.")
        print(f"  {lib.TDS_MATCHED_TO_EXISTING_CSV}  <- tier-1 suggested backfills (apply by hand)")
        print(f"  {lib.TDS_REMATCH_REVIEW_CSV}  <- set `approved` = NO to reject a tier-2/3 suggestion")
        print(f"  {lib.TDS_UNMATCHED_JSON}  <- {len(unmatched_out)} TDS with no catalog match "
              f"-> input to 09_propose_new_items.py")
    else:
        print(f"\nSample/limit run — matched {len(accepted)}, no-match {len(rejected)}. "
              f"No output files written (run without --sample/--limit for the real pass).")
    return 0


def _write_matched(path, recs) -> None:
    tier1 = [r for r in recs if r["match_tier"] == 1]
    cols = ["Item Name", "current_tds_filename", "suggested_tds_filename", "rel_path",
            "fuzzy_score", "confidence", "manufacturer_product_code",
            "canonical_product_name", "match_tier", "match_basis", "reason"]
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in tier1:
            w.writerow(r)


def _write_review(path, recs) -> None:
    prev = lib.read_match_approvals(path, key_col="suggested_tds_filename")
    cols = ["suggested_tds_filename", "approved", "Item Name", "current_tds_filename",
            "rel_path", "fuzzy_score", "confidence", "manufacturer_product_code",
            "canonical_product_name", "match_tier", "match_basis", "reason"]
    lib.OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            row = dict(r)
            row["approved"] = prev.get(r["suggested_tds_filename"], "")
            w.writerow(row)


if __name__ == "__main__":
    sys.exit(main())
