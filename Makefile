# =============================================================================
# ABCO Catalog Rebuild Pipeline
# =============================================================================
# Catalog-driven TDS enrichment. Start from data/inputs/catalog_list.csv +
# data/inputs/tds_ready/**.pdf and rebuild every attribute column.
#
# Setup (once):
#   make install            # create .venv and install deps (needs poetry)
#   # ANTHROPIC_API_KEY must be in .env (already present) for the API stages.
#
# Full run (in order):
#   make validate-inputs    # 00: structural checks on the input catalog (no API)
#   make index-tds          # 01: index every PDF in tds_ready/ (no API)
#   make match              # 02: catalog -> TDS matching, chemistry only (API)
#   make enrich             # 03: extract TDS attrs + infer supplies/equipment (API)
#   make assemble           # 04: backward-flow + final catalog_list.csv (no API)
#   make validate-output    # 05: fail-loud checks on the final catalog (no API)
#   #   ...or just:  make all
#
# Sample validation (cheap, no full spend):
#   make sample             # run 02+03 on a small representative item sample
#
# Iterate:
#   make clean-output       # wipe data/output/ and start fresh
#
# Notes:
#   - API stages cache results to disk, so re-running on unchanged inputs makes
#     zero API calls and reproduces identical output.
#   - Override the model on any API stage, e.g.:
#       make enrich MODEL=claude-opus-4-8
# =============================================================================

PYTHON  = poetry run python
MODEL  ?= claude-sonnet-4-6
SAMPLE ?= 8

.PHONY: install validate-inputs index-tds match enrich assemble validate-output \
        all sample clean-output help

## Create the in-project .venv and install dependencies
install:
	poetry install

## 00 — Structural validation of the input catalog (no API)
validate-inputs:
	$(PYTHON) scripts/00_validate_inputs.py

## 01 — Index every TDS PDF in data/inputs/tds_ready/ (no API)
index-tds:
	$(PYTHON) scripts/01_index_tds.py

## 02 — Match active chemistry items to their TDS (API)
match:
	$(PYTHON) scripts/02_match_catalog_to_tds.py --model $(MODEL)

## 03 — Enrich: TDS extraction for chemistry + inference for supplies/equipment (API)
enrich:
	$(PYTHON) scripts/03_enrich.py --model $(MODEL)

## Re-enrich only specific rows, e.g.:
##   make enrich-items ITEMS="MacDermid Enova EF 587 AMR (gal)||Other Item (gal)"
## Use after editing data/inputs/manual_matches.csv to link items to a TDS.
enrich-items:
	$(PYTHON) scripts/03_enrich.py --model $(MODEL) --items "$(ITEMS)"

## Enrich only items that newly got a TDS match (weren't TDS-enriched before).
## Run this after re-running `make match` to upgrade just the newly-matched items.
enrich-new:
	$(PYTHON) scripts/03_enrich.py --model $(MODEL) --new-tds-only

## 04 — Assemble final catalog with backward propagation to legacy items (no API)
assemble:
	$(PYTHON) scripts/04_assemble.py

## 05 — Fail-loud validation of the final catalog (no API)
validate-output:
	$(PYTHON) scripts/05_validate_output.py

## Full pipeline: 00 -> 05 in sequence
all: validate-inputs index-tds match enrich assemble validate-output

## Sample validation — run the API stages on a small representative item sample
sample: validate-inputs index-tds
	$(PYTHON) scripts/02_match_catalog_to_tds.py --model $(MODEL) --sample $(SAMPLE)
	$(PYTHON) scripts/03_enrich.py --model $(MODEL) --sample $(SAMPLE)

## Export the TDS actually used (verified matches + manual links) into
## data/output/used_tds/ plus a used_tds_list.csv, for uploading to the cloud.
export-tds:
	$(PYTHON) scripts/export_used_tds.py

## Delete all generated output files (leaves data/output/ directory intact)
clean-output:
	rm -f data/output/*.json data/output/*.csv data/output/*.md

## Show this help
help:
	@echo ""
	@echo "ABCO Catalog Rebuild Pipeline"
	@echo "-----------------------------"
	@grep -E '^##' Makefile | sed 's/## /  /'
	@echo ""
