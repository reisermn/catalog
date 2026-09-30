"""
Shared utilities for the ABCO catalog rebuild pipeline.

Single source of truth for:
  - paths and catalog I/O (column order preserved exactly)
  - normalize()  — name normalization for catalog <-> TDS matching
  - controlled vocabularies (lines, process stages/sub-steps, enums)
  - brand helpers
  - the Anthropic client + a structured-output call wrapper (native PDF, caching)
  - a small JSON disk cache for idempotency

Read METHODOLOGY.md before changing anything here.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INPUTS = DATA / "inputs"
CATALOG_CSV = INPUTS / "catalog_list.csv"
TDS_DIR = INPUTS / "tds_ready"
MANUAL_MATCHES = INPUTS / "manual_matches.csv"  # human-curated Item -> TDS links
OUTPUT = DATA / "output"

# Output artifacts
TDS_INDEX = OUTPUT / "tds_index.json"
MATCHES_CSV = OUTPUT / "catalog_tds_matches.csv"
MATCH_REVIEW_CSV = OUTPUT / "catalog_match_review.csv"
UNMATCHED_CSV = OUTPUT / "unmatched_chemistry.csv"
MATCH_CACHE = OUTPUT / "match_cache.json"
ENRICHED_JSON = OUTPUT / "enriched.json"
ENRICH_ERRORS = OUTPUT / "enrich_errors.json"
ENRICH_CACHE = OUTPUT / "enrich_cache.json"
FINAL_CSV = OUTPUT / "catalog_list.csv"
REVIEW_CSV = OUTPUT / "catalog_review.csv"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_EFFORT = "high"  # correctness over cost on the per-item calls

# ---------------------------------------------------------------------------
# catalog_active.csv — the current reference template (20 columns). This is
# a separate, newer spine from CATALOG_CSV/FINAL_CSV above: it's the
# up-to-date, hand-curated catalog the 06+ scripts read from and compare
# tds_all/ against. Never written to (data/inputs/ is read-only).
# ---------------------------------------------------------------------------

ACTIVE_CATALOG_CSV = INPUTS / "catalog_active.csv"
TDS_ALL_DIR = INPUTS / "tds_all"

# The exact column set/order of catalog_active.csv. Single source of truth so
# every script that writes catalog-shaped rows (e.g. new-item proposals) stays
# aligned with it. Anything outside this set is not part of the template.
ACTIVE_CATALOG_COLUMNS = [
    "Item Name", "Category", "Pricing Model", "Preferred Vendor", "QB Item ID",
    "DOJ Controlled", "Manufacturer", "Manufacturer Product Code", "Is Commodity",
    "Product Category", "Function Description", "Applicable Lines",
    "Primary Line Position", "Primary Line Sub Position", "Use Type",
    "System Name", "System Components", "Sequence Companions",
    "Replenishment Role", "TDS Filename",
]
ACTIVE_CATEGORIES = ["Chemistry", "Supplies", "Equipment"]

# Outputs for the tds_all reconciliation pipeline (06-09)
TDS_USED_DIR = OUTPUT / "tds_used"
TDS_USED_MANIFEST = OUTPUT / "tds_used_manifest.csv"
TDS_ALL_INDEX = OUTPUT / "tds_all_index.json"
TDS_REMATCH_CACHE = OUTPUT / "tds_rematch_cache.json"
TDS_MATCHED_TO_EXISTING_CSV = OUTPUT / "tds_matched_to_existing.csv"
TDS_REMATCH_REVIEW_CSV = OUTPUT / "tds_rematch_review.csv"
TDS_UNMATCHED_JSON = OUTPUT / "tds_unmatched.json"
NEW_ITEM_CACHE = OUTPUT / "new_item_cache.json"
NEW_ITEM_ERRORS = OUTPUT / "new_item_errors.json"
NEW_ITEM_PROPOSALS_CSV = OUTPUT / "proposed_new_items.csv"
ACTIVE_CATALOG_UPDATED_CSV = OUTPUT / "catalog_active_updated.csv"


def checked(v) -> str:
    """catalog_active.csv's boolean convention: 'checked' or ''."""
    return "checked" if v else ""

# ---------------------------------------------------------------------------
# Catalog I/O — preserve the exact 35-column header and row order
# ---------------------------------------------------------------------------


def load_catalog(path: Path = CATALOG_CSV) -> tuple[list[str], list[dict]]:
    """Return (fieldnames, rows). utf-8-sig strips any BOM on the first column."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fieldnames, rows


def write_catalog(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


def g(row: dict, key: str) -> str:
    """Safe trimmed string get."""
    return (row.get(key) or "").strip()


# ---------------------------------------------------------------------------
# Name normalization (the one matcher; used by 02)
# ---------------------------------------------------------------------------

_BRAND_PREFIXES = [
    "macdermid enthone", "macdermid", "enthone", "abco", "columbia chemical",
    "u.s. specialty color", "us specialty color", "metal chem, inc.",
    "metal chem", "seacole", "nislip", "surface technology inc.",
    "quaker houghton",
]
# number+unit tokens that appear outside parentheses (size variants)
_SIZE_TOKEN = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:gal|gallon|lb|lbs|#|kg|g|oz|ml|l|micron|um|ft|in|\")\b"
)
_PAREN = re.compile(r"\([^)]*\)")
_PUNCT = re.compile(r"[^\w\s-]")
_WS = re.compile(r"\s+")


def normalize(name: str) -> str:
    """
    Normalize a product name so a catalog item and its TDS name collapse to the
    same string. Order matters; keep identical to METHODOLOGY.
    """
    s = (name or "").lower()
    s = _PAREN.sub(" ", s)              # drop parenthetical size/brand info
    s = s.replace("_", " ")            # underscores are token separators (MacDermid MEIS files)
    for prefix in sorted(_BRAND_PREFIXES, key=len, reverse=True):
        s = s.replace(prefix, " ")
    s = _SIZE_TOKEN.sub(" ", s)         # drop bare size tokens
    s = _PUNCT.sub(" ", s)              # punctuation except hyphen (kept by \w/-)
    s = s.replace("-", " ")            # treat hyphen as space for token matching
    s = _WS.sub(" ", s).strip()
    return s


# Filename tokens that carry no product identity (vendor/date/format noise).
_FN_STOP = {"tds", "sds", "msds", "ds", "meis", "pds", "na", "eu", "gl", "en",
            "letterhead", "new", "process", "operation", "guide", "send", "w",
            "datasheet", "data", "sheet", "technical", "rev", "final", "002",
            "003", "004"}
_DATE_TOKEN = re.compile(r"^\d{1,2}[a-z]{3,4}\d{2,4}$")  # 12jan24, 5apr19, ...


def clean_fname(norm: str) -> str:
    """Drop filename noise tokens (format markers, vendor codes, dates)."""
    return " ".join(t for t in norm.split()
                    if t not in _FN_STOP and not _DATE_TOKEN.match(t)).strip()


def tds_match_key(filename: str) -> str:
    """
    The product-identity portion of a TDS filename, normalized for matching.
    MacDermid files are '<PRODUCT>_MEIS_<region>_<date>.pdf' — everything from
    '_MEIS' on is vendor metadata, so cut it before normalizing.
    """
    stem = Path(filename).stem
    low = stem.lower()
    for marker in ("_meis", " meis", "_sds", " sds", "_msds", " msds"):
        i = low.find(marker)
        if i > 0:
            stem = stem[:i]
            break
    return clean_fname(normalize(stem))


# ---------------------------------------------------------------------------
# Brand helpers
# ---------------------------------------------------------------------------

KNOWN_BRANDS = [
    "MacDermid Enthone", "MacDermid", "Enthone", "ABCO", "Metal Chem",
    "Seacole", "Surface Technology Inc.", "Columbia Chemical",
    "U.S. Specialty Color", "Quaker Houghton", "Mitsubishi Materials",
    "Luvata", "BASF", "SurTec", "Mefiag", "Flo King", "Nislip", "Phifer",
    "Bob Martin", "Chemical Compounding", "Miles Chemical",
]


def brand_from_item_name(item_name: str) -> str:
    """Pull a known brand from the parenthetical of an item name; '' if none."""
    m = re.search(r"\(([^)]+)", item_name or "")
    if not m:
        return ""
    paren = m.group(1).lower()
    for brand in sorted(KNOWN_BRANDS, key=len, reverse=True):
        if brand.lower() in paren:
            return brand
    return ""


# ---------------------------------------------------------------------------
# Controlled vocabularies (METHODOLOGY)
# ---------------------------------------------------------------------------

APPLICABLE_LINES = [
    "Zinc (pure)", "Zinc-nickel", "Zinc-iron",
    "Electrolytic nickel", "Electroless nickel",
    "Electrolytic copper", "Electroless copper",
    "Decorative chrome", "Hard chrome",
    "Tin", "Tin-copper", "Tin-silver", "Tin-bismuth",
    "Gold", "Silver", "Palladium", "Palladium-nickel", "Platinum", "Rhodium",
    "Cobalt", "Iron", "Indium", "Cadmium",
    "Anodize Type II", "Anodize Type III",
    "Chromate Conversion",
]

LINE_POSITIONS = [
    "1. Pre-treatment / Cleaning",
    "2. Rinse",
    "3. Pre-plate / Strike",
    "4. Plating (Main Bath)",
    "5. Post-plate Treatment",
    "6. Final Rinse & Dry",
]

LINE_SUB_POSITIONS = [
    "1.1 Mechanical Surface Prep", "1.2 Solvent / Degreasing", "1.3 Soak Cleaning",
    "1.4 Electrocleaning", "1.5 Acid Activation / Pickling", "1.6 Etch",
    "1.7 Desmut / Deoxidize", "1.8 Additives",
    "2.1 Rinse Water", "2.2 Rinse Tank Configurations", "2.3 Rinse Aids / Additives",
    "3.1 Copper Strike", "3.2 Nickel Strike", "3.3 Acid Dip / Pre-dip",
    "3.4 Specialty Strikes", "3.5 Adhesion Promoters",
    "4.1 Electroless Plating", "4.2 Electrolytic Plating — Nickel",
    "4.3 Electrolytic Plating — Copper", "4.4 Electrolytic Plating — Zinc",
    "4.5 Electrolytic Plating — Chrome", "4.6 Electrolytic Plating — Precious Metals",
    "4.7 Electrolytic Plating — Tin & Alloys", "4.8 Anodizing",
    "4.9 Bath Additives & Replenishers",
    "5.1 Chromate Conversion Coatings", "5.2 Passivation", "5.3 Phosphate Coatings",
    "5.4 Sealers", "5.5 Topcoats / Final Coatings", "5.6 Specialty Post-treatments",
    "6.1 Final Rinse", "6.2 Drying Methods", "6.3 Final Rinse Additives",
]

USE_TYPES = ["standalone", "system_component"]
REPLENISHMENT_ROLES = ["makeup_only", "replenishment_only", "both", "not_applicable"]
APPLICATION_METHODS = ["immersion", "electroplating", "barrel", "spray", "brush_swab", "multiple"]


# ---------------------------------------------------------------------------
# Anthropic client + structured-output call wrapper
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Load ANTHROPIC_API_KEY (and friends) from .env if present."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass


def get_client():
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set. Add it to .env or export it.")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def pdf_block(path: Path) -> dict:
    """Native-PDF document content block (base64, no newlines)."""
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
    }


def cached_text(text: str) -> list[dict]:
    """A single cached system text block (prompt caching on the static prefix)."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def call_structured(
    client,
    model: str,
    system: list[dict],
    user_content,
    schema: dict,
    *,
    effort: str = DEFAULT_EFFORT,
    max_tokens: int = 4096,
    retries: int = 3,
):
    """
    Call Claude with a cached system prompt + structured JSON output.
    Returns (parsed_dict, usage). Raises on persistent failure.
    """
    import anthropic

    last_err = None
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            if resp.stop_reason == "refusal":
                raise RuntimeError(f"refusal: {getattr(resp, 'stop_details', None)}")
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return json.loads(text), resp.usage
        except (anthropic.RateLimitError, anthropic.APIStatusError,
                anthropic.APIConnectionError, json.JSONDecodeError, RuntimeError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"call_structured failed after {retries} attempts: {last_err}")


# ---------------------------------------------------------------------------
# JSON disk cache (idempotency)
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {} if default is None else default


def save_json(path: Path, obj) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def load_manual_matches(index: list) -> dict:
    """
    Read data/inputs/manual_matches.csv (columns: Item, tds_filename) and resolve
    each to an indexed TDS. The tds_filename may be the exact filename or any
    unique substring of one (case-insensitive), so you can type 'ENOVA EF 587'
    instead of the full vendor filename. Returns Item -> {tds_filename, rel_path}.
    Unknown/ambiguous entries are warned and skipped.
    """
    out: dict = {}
    if not MANUAL_MATCHES.exists():
        return out
    by_name = {e["filename"].lower(): e for e in index}
    with open(MANUAL_MATCHES, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            item = (row.get("Item") or "").strip()
            q = (row.get("tds_filename") or "").strip()
            if not item or not q:
                continue
            e = by_name.get(q.lower())
            if e is None:
                hits = [x for x in index if q.lower() in x["filename"].lower()]
                if len(hits) == 1:
                    e = hits[0]
                elif not hits:
                    print(f"  manual_matches: no TDS file matches {q!r} (item {item!r}) — skipped")
                    continue
                else:
                    print(f"  manual_matches: {q!r} matches {len(hits)} files — be more specific; skipped")
                    continue
            out[item] = {"tds_filename": e["filename"], "rel_path": e["rel_path"]}
    return out


def read_match_approvals(path: Path = MATCH_REVIEW_CSV, key_col: str = "Item") -> dict:
    """key_col value -> approved value (upper-cased) from a review file. A
    tier-2/3 match is rejected only when approved == 'NO'; blank means accept
    by default."""
    out = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row.get(key_col, "")] = (row.get("approved") or "").strip().upper()
    return out
