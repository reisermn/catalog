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
