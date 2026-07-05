#!/usr/bin/env python3
"""
provider.py - folder document provider. Reads PDFs from a directory, extracts
text with PyMuPDF (fitz), and yields dicts with a stable content hash so reruns
are incremental (a doc whose hash is already in docs_seen is skipped before any
LLM call).

Two render modes (Phase B):
  * "text" (default) - PyMuPDF plain text, PyMuPDF flattens tables into
    whitespace-separated runs (columns lose their row association).
  * "md" - Markdown that PRESERVES table structure as pipe tables. Uses
    pymupdf4llm when importable; otherwise a self-contained fallback built from
    page.find_tables() merged with the page's non-table prose. Either way the
    same boilerplate stripping runs, and the extracted text (and hence the
    content hash) differs from text mode, so the same PDF re-renders as a
    genuinely new document under a different mode.
"""

import datetime
import glob
import hashlib
import os
import re

try:
    import fitz  # PyMuPDF
except Exception as e:   # pragma: no cover
    fitz = None
    _IMPORT_ERR = e

try:
    import pymupdf4llm            # optional; only used for render="md"
except Exception:                # pragma: no cover
    pymupdf4llm = None

# Repeated running headers/footers PyMuPDF returns inline with body text. Left in,
# they bleed into quote-grounding sentences (an analyst note's first "sentence"
# becomes "CONFIDENTIAL // OSINT DIGEST ... Analyst Note: ..."). Stripped here so
# citations stay clean; this is a normal provider responsibility for scanned docs.
# The `#`/`**` prefixes let the same filter catch a boilerplate line after
# pymupdf4llm has wrapped it as a markdown heading ("# **CONFIDENTIAL ...**").
_BOILERPLATE = re.compile(
    r"(?i)^\s*[#*\s]*(CONFIDENTIAL\s*//.*|.*UNCLASSIFIED DRAFT.*|Page \d+\b.*)$")


def _strip_boilerplate(text):
    return "\n".join(ln for ln in text.splitlines()
                     if not _BOILERPLATE.match(ln))


def _extract_text_plain(path):
    doc = fitz.open(path)
    parts = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(parts)


def _rows_to_pipe(rows):
    """Render a list-of-rows table as a GitHub-style pipe table (header + a
    separator row so downstream md parsers read it as a table)."""
    out = []
    for i, r in enumerate(rows):
        cells = ["" if c is None else str(c).replace("\n", " ").strip()
                 for c in r]
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(out)


def _extract_md_fallback(path):
    """Self-contained markdown extraction (no pymupdf4llm): pipe tables from
    page.find_tables() plus the page's non-table prose blocks. Kept deliberately
    simple -- this path only runs when pymupdf4llm is unavailable (offline)."""
    doc = fitz.open(path)
    out = []
    for page in doc:
        try:
            tables = list(page.find_tables().tables)
        except Exception:
            tables = []
        rects = [fitz.Rect(t.bbox) for t in tables]
        prose = []
        for b in page.get_text("blocks"):
            bx = fitz.Rect(b[:4])
            if any(bx.intersects(r) for r in rects):
                continue          # drop text that belongs to a table region
            txt = (b[4] or "").strip()
            if txt:
                prose.append(txt)
        if prose:
            out.append("\n".join(prose))
        for t in tables:
            try:
                out.append(_rows_to_pipe(t.extract()))
            except Exception:
                pass
    doc.close()
    return "\n\n".join(p for p in out if p.strip())


def _extract_text(path, render="text"):
    if fitz is None:
        raise RuntimeError(f"PyMuPDF not available: {_IMPORT_ERR}")
    if render == "md":
        if pymupdf4llm is not None:
            text = pymupdf4llm.to_markdown(path)
        else:
            text = _extract_md_fallback(path)
    else:
        text = _extract_text_plain(path)
    return _strip_boilerplate(text)


def iter_documents(folder, render="text"):
    """Yield {doc_id,title,text,path,date,content_hash} for every PDF in folder,
    sorted by filename for deterministic ordering. `render` selects the text
    extraction mode ('text'|'md'); it is folded into the content hash so the same
    PDF under a different mode is a distinct document (never hash-skipped)."""
    for path in sorted(glob.glob(os.path.join(folder, "*.pdf"))):
        text = _extract_text(path, render=render)
        # fold the render mode into the hash so the two modes never collide even
        # if an all-prose doc happened to extract identically under both.
        h = hashlib.sha256(
            (render + "\x00" + text).encode("utf-8", "ignore")).hexdigest()[:16]
        base = os.path.splitext(os.path.basename(path))[0]
        # first non-empty line is the title (strip md heading/bold markers)
        title = base
        for ln in text.splitlines():
            s = re.sub(r"^[#*\s]+|\*+$", "", ln).strip()
            if s:
                title = s
                break
        mtime = datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        yield {
            "doc_id": base,
            "title": title,
            "text": text,
            "path": path,
            "date": mtime,
            "content_hash": h,
        }
