#!/usr/bin/env python
"""
01 — Index the TDS PDFs (no API).

Every PDF in data/inputs/tds_ready/ is treated as a usable TDS (the MacDermid
"MEIS" files are TDS despite the naming). For each file we record its path,
vendor folder, filename, and a short text snippet (first pages) used only for
cheap fuzzy candidate ranking in stage 02. The actual extraction in stage 03
reads the native PDF, so the snippet need not be exhaustive.

Writes data/output/tds_index.json. Idempotent.
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
    if not lib.TDS_DIR.exists():
        sys.exit(f"TDS directory not found: {lib.TDS_DIR}")

    non_pdf = [
        p for p in lib.TDS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in {".doc", ".docx"}
    ]

    pdfs = sorted(
        p for p in lib.TDS_DIR.rglob("*.pdf") if p.is_file() and not p.name.startswith(".")
    )

    index = []
    no_text = 0
    for p in pdfs:
        rel = p.relative_to(lib.TDS_DIR)
        vendor = rel.parts[0] if len(rel.parts) > 1 else ""
        snippet = extract_snippet(p)
        if not snippet:
            no_text += 1
        stem = p.stem
        index.append({
            "filename": p.name,
            "rel_path": str(rel),
            "vendor": vendor,
            "norm_filename": lib.normalize(stem),
            "snippet": snippet,
        })

    lib.save_json(lib.TDS_INDEX, index)

    print(f"Indexed {len(index)} TDS PDFs across "
          f"{len({e['vendor'] for e in index})} vendor folders.")
    print(f"  {no_text} file(s) yielded no extractable text (filename matching still works).")
    if non_pdf:
        print(f"  WARNING: {len(non_pdf)} .doc/.docx file(s) found and NOT indexed. "
              f"Pre-convert them to PDF and place them under tds_ready/. Examples: "
              f"{[p.name for p in non_pdf[:3]]}")
    print(f"Index: {lib.TDS_INDEX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
