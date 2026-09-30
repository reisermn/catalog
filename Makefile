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
        all sample clean-output help export-tds export-tds-used index-tds-all \
        match-unused-tds-dry-run match-unused-tds propose-new-items-dry-run \
        propose-new-items

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

# =============================================================================
# catalog_active.csv / tds_all/ reconciliation (06-09)
# =============================================================================
# catalog_active.csv is the current reference template (data/inputs/). tds_all/
# holds every TDS we have, including for products not yet in the catalog.
#
#   make export-tds-used     # 06: copy the TDS catalog_active.csv already uses (no API)
#   make index-tds-all       # 07: index every PDF in tds_all/ (no API)
#   make match-unused-tds-dry-run  # 08 preview: candidate counts, zero API calls
#   make match-unused-tds    # 08: try to link unused TDS back to an existing row (API)
#   make propose-new-items-dry-run # 09 preview: proposed groups, zero API calls
#   make propose-new-items   # 09: propose new catalog rows for the rest (API)

## 06 — Copy the TDS catalog_active.csv already references out of tds_all/ (no API)
export-tds-used:
	$(PYTHON) scripts/06_export_tds_used.py

## 07 — Index every TDS in data/inputs/tds_all/ (no API)
index-tds-all:
	$(PYTHON) scripts/07_index_tds_all.py

## 08 preview — candidate/cost preview for rematching unused TDS, no API calls
## Add VENDOR=MacDermid (any tds_all/ folder name) to scope to one vendor.
match-unused-tds-dry-run:
	$(PYTHON) scripts/08_match_unused_tds.py --dry-run $(if $(VENDOR),--vendor "$(VENDOR)")

## 08 — Try to match unused TDS back onto an existing catalog row (API)
match-unused-tds:
	$(PYTHON) scripts/08_match_unused_tds.py --model $(MODEL) $(if $(VENDOR),--vendor "$(VENDOR)")

## 09 preview — proposed new-item groups, no API calls
propose-new-items-dry-run:
	$(PYTHON) scripts/09_propose_new_items.py --dry-run $(if $(VENDOR),--vendor "$(VENDOR)")

## 09 — Propose new catalog rows for TDS with no existing match (API)
propose-new-items:
	$(PYTHON) scripts/09_propose_new_items.py --model $(MODEL) $(if $(VENDOR),--vendor "$(VENDOR)")

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
