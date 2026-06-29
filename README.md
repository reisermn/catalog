# ABCO Catalog Rebuild

Turns the ABCO product catalog plus a folder of Technical Data Sheets (TDS) into
a clean, fully-enriched catalog. The pipeline is **catalog-driven**: for every
catalog product it looks for a matching TDS and rebuilds the attribute columns —
it never adds products from available TDS.

Read **`METHODOLOGY.md`** for the data model, enrichment rules, and controlled
vocabularies. This file is just how to run it.

---

## Layout

```
data/
  inputs/
    catalog_list.csv        # the spine — read-only
    tds_ready/**.pdf        # TDS source files — read-only (every PDF is a TDS)
  output/                   # everything generated — safe to wipe & regenerate
scripts/
  lib.py                    # shared: normalize(), vocab, Claude client, PDF helper, caches
  00_validate_inputs.py     # no API
  01_index_tds.py           # no API
  02_match_catalog_to_tds.py# API
  03_enrich.py              # API
  04_assemble.py            # no API
  05_validate_output.py     # no API
```

The pipeline only writes to `data/output/`. `data/inputs/` is never modified.

---

## Setup

Requires Python 3.11 (via pyenv) and Poetry.

```bash
make install          # creates ./.venv and installs deps
```

The Anthropic API key is read from `.env` in the project root
(`ANTHROPIC_API_KEY=...`), which is already present and gitignored.

---

## Running

### Sample validation first (cheap — recommended)

Run the API stages on a small representative sample to eyeball quality before
committing to a full run:

```bash
make sample           # runs 00, 01, then 02 + 03 on ~8 sampled items
```

Inspect `data/output/catalog_tds_matches.csv` and `data/output/enriched.json`.

### Recommended flow: review matches between 02 and 03

A wrong TDS match produces wrong enrichment for that item *and* every legacy item
that inherits from it, so the right place to review is **after matching, before
enrichment**:

```bash
make validate-inputs index-tds   # 00, 01 (free)
make match                       # 02 — match all active chemistry to TDS
#   --> review data/output/catalog_match_review.csv
make enrich assemble validate-output   # 03, 04, 05
```

**What to review.** Tier-1 matches (confidence ≥ 85, verified) are auto-accepted
and need no action. Only the uncertain **tier-2/3** matches land in
`catalog_match_review.csv`, each with the verifier's `reason`. In that file:

| `approved` value | Effect |
|---|---|
| blank (default) | the match is **used** |
| `NO` | the match is **rejected** — the item falls back to name-based inference instead of extracting from a TDS you don't trust |

Edit `approved`, save, then run `make enrich ...`. Re-running `make match` later
**preserves** your `approved` edits and costs nothing (cached). If you'd rather
just run everything and review at the end, `make all` works too — every tier-2/3
match is also surfaced in the final `catalog_review.csv`.

### Full run

```bash
make all              # 00 -> 05 in order
```

or stage by stage:

```bash
make validate-inputs  # 00
make index-tds        # 01
make match            # 02
make enrich           # 03
make assemble         # 04
make validate-output  # 05
```

The final catalog is written to **`data/output/catalog_list.csv`**, with anything
that needs a human eye in **`data/output/catalog_review.csv`**.

### Progress & resuming

The API stages (`match`, `enrich`) print a `[i/N]` line per item as they go, and
save their disk cache every few calls. If a run dies partway (or you Ctrl-C it),
**just run the same command again** — already-processed items are served from
cache, so it picks up where it left off and costs nothing for the work already
done.

### Manually linking a TDS (when auto-matching misses one)

Sometimes a product's TDS exists but the matcher won't confidently link it (an
odd filename, an ambiguous variant). To force a link:

1. Add a row to **`data/inputs/manual_matches.csv`**:

   ```csv
   Item,tds_filename
   MacDermid Enova EF 587 AMR (gal),ENOVA EF 587
   ```

   `Item` is the exact catalog Item. `tds_filename` can be the exact filename **or
   any unique substring of it** (so `ENOVA EF 587` finds
   `ENOVA EF 587_MEIS_NA_12Jan24 (002).pdf`).

2. Re-enrich just those rows:

   ```bash
   make enrich-items ITEMS="MacDermid Enova EF 587 AMR (gal)"
   ```

   (multiple items: separate with `||`). Then `make assemble validate-output` to
   fold them into the catalog and propagate to any legacy items.

A manual link always wins over auto-matching, is tagged `match_basis=manual`, and
is also picked up automatically by a full `make match` / `make all`.

### Re-running specific rows

`make enrich-items ITEMS="..."` (or `python scripts/03_enrich.py --items "a||b"`,
or `--items-file path`) re-enriches only the named items and **forces a fresh
call** for each (ignores the cache for those rows) — useful after changing a
manual link or a prompt. Everything else is untouched.

### Model selection

Default model is `claude-sonnet-4-6`. Override per stage:

```bash
make enrich MODEL=claude-opus-4-8     # higher-accuracy pass
```

### Repeatability

Every API stage caches results to disk (`data/output/match_cache.json`,
`data/output/enrich_cache.json`). Re-running on unchanged inputs makes **zero**
API calls and reproduces identical output. Wipe and start fresh with:

```bash
make clean-output
```

---

## Output files

| File | Description |
|---|---|
| `input_validation_report.md` | 00 — structural check results |
| `tds_index.json` | 01 — indexed TDS metadata + text |
| `catalog_tds_matches.csv` | 02 — confirmed catalog↔TDS matches |
| `catalog_match_review.csv` | 02 — low-confidence matches to review |
| `enriched.json` | 03 — per-item enrichment records |
| `enrich_errors.json` | 03 — items that failed enrichment |
| `catalog_list.csv` | 04 — **the final enriched catalog** |
| `catalog_review.csv` | 04 — rows a human should verify |
| `output_validation_report.md` | 05 — final fail-loud validation |
