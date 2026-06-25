# Agent guide

This repo rebuilds the ABCO product catalog by enriching it from Technical Data
Sheets. **Read `METHODOLOGY.md` first** — it defines the data model, the four
rules, and the controlled vocabularies. `README.md` covers how to run.

## Environment
- Poetry + in-project `.venv`, Python 3.11. Activate with `source .venv/bin/activate`
  or run via `poetry run python ...` / the Makefile.
- `ANTHROPIC_API_KEY` is loaded from `.env` automatically by `scripts/lib.py`.
- Add deps with `poetry add <pkg>` (keeps `pyproject.toml` / `poetry.lock` in sync).

## Conventions
- Scripts are numbered and run in order (`make all`). Each is standalone and
  idempotent, with a config block at the top.
- **`scripts/lib.py` is the single source of truth** for `normalize()`, the
  controlled vocabularies, the Anthropic client factory, the native-PDF document
  block helper, and the JSON disk-cache. Do not duplicate these into scripts.
- `data/inputs/` is read-only. Scripts only write to `data/output/`.
- The default model is `claude-sonnet-4-6`; everything takes `--model`.
  Use `claude-opus-4-8` for a final high-accuracy pass.

## Hard invariants (enforced by `05_validate_output.py`)
- Output row count == input row count. Never add or drop products.
- `status`, `superseded_by`, `Item`, `item_category` are preserved from the input.
- `applicable_lines` and line-position values must come from the controlled vocab.
- Legacy items inherit enrichment from their `superseded_by` active item
  (information flows backward).

## Claude API notes
- TDS are read as **native PDF** document blocks (better fidelity than text).
- Extraction/inference use **structured outputs** (json_schema) for guaranteed shape.
- The large static prompt is **prompt-cached**; per-item content follows it.
- All API stages cache to disk for repeatability — re-runs cost nothing.
