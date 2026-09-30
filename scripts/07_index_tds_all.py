#!/usr/bin/env python
"""
07 — Index every TDS in data/inputs/tds_all/ (no API).

Same approach as 01_index_tds.py (filename, vendor folder, normalized match
key, a short text snippet for cheap fuzzy ranking) but pointed at tds_all/,
which holds every TDS we have access to — including products not yet in the
catalog. This is the full universe that 08 (rematch) and 09 (propose new
items) work from.

Writes data/output/tds_all_index.json. Idempotent.
"""

from __future__ import annotations

import sys

import lib

SNIPPET_PAGES = 2       # pages of text to pull for fuzzy matching
SNIPPET_CHARS = 4000    # cap per file


def extract_snippet(path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            parts = []
            for page in pdf.pages[:SNIPPET_PAGES]:
                parts.append(page.extract_text() or "")
            return "\n".join(parts)[:SNIPPET_CHARS]
    except Exception:
        return ""


def main() -> int:
    if not lib.TDS_ALL_DIR.exists():
        sys.exit(f"TDS directory not found: {lib.TDS_ALL_DIR}")

    non_pdf = [
        p for p in lib.TDS_ALL_DIR.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() != ".pdf"
    ]
    pdfs = sorted(
        p for p in lib.TDS_ALL_DIR.rglob("*.pdf") if p.is_file() and not p.name.startswith(".")
    )

    index = []
    no_text = 0
    for i, p in enumerate(pdfs, 1):
        rel = p.relative_to(lib.TDS_ALL_DIR)
        vendor = rel.parts[0] if len(rel.parts) > 1 else ""
        snippet = extract_snippet(p)
        if not snippet:
            no_text += 1
        index.append({
            "filename": p.name,
            "rel_path": str(rel),
            "vendor": vendor,
            "norm_filename": lib.normalize(p.stem),
            "match_key": lib.tds_match_key(p.name),
            "snippet": snippet,
        })
        if i % 100 == 0:
            print(f"  indexed {i}/{len(pdfs)}...", flush=True)

    lib.save_json(lib.TDS_ALL_INDEX, index)

    print(f"Indexed {len(index)} TDS PDFs across "
          f"{len({e['vendor'] for e in index})} vendor folders.")
    print(f"  {no_text} file(s) yielded no extractable text (filename matching still works).")
    if non_pdf:
        print(f"  NOTE: {len(non_pdf)} non-PDF file(s) skipped (e.g. .DS_Store). "
              f"Examples: {[p.name for p in non_pdf[:3]]}")
    print(f"Index: {lib.TDS_ALL_INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
