"""Authoritative Markdown renderer. Markdown and JSON are produced from the same block list.

Modes
  annotate=False  clean Markdown (CLI / benchmark default). Provenance lives in the JSON sidecar.
  annotate=True   every block is preceded by an HTML comment with block id, page, bbox, extractor,
                  confidence and verification, so any passage can be traced to its source region.

Escaping policy (deliberately minimal): we escape only what would change document *structure*
when rendered - pipes inside table cells, and a leading '#' or '>' in ordinary paragraphs. We do not
backslash-escape '*', '_' or '$' inside running text because that changes the literal text that
downstream consumers (and the olmOCR-Bench scorer) compare against.
"""
import re

from .headers import HEADER_ROLES
from . import tables as T

SKIP_TYPES = set()


def _norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


def _escape_par(text):
    lines = []
    for ln in text.split("\n"):
        if re.match(r"^\s*(#{1,6}\s|>)", ln):
            ln = "\\" + ln.lstrip()
        lines.append(ln)
    return "\n".join(lines)


def _prov(b):
    return (f"<!-- pf:{b.get('block_id')} p={b.get('page')} bbox={b.get('bbox')} "
            f"{b.get('extractor')} conf={b.get('confidence')} {b.get('verification')} -->")


def _figure_text(b):
    parts = []
    dt = b.get("detected_text")
    if isinstance(dt, list) and dt:
        parts.append("\n".join(d.get("text", "") for d in dt if d.get("text") and d.get("in_markdown", True)))
    elif b.get("ocr_text"):
        parts.append(b["ocr_text"])
    return "\n".join(p for p in parts if p.strip())


def render_block(b, annotate=False):
    t, c = b.get("type"), b.get("content")
    if t == "page_text":   # Markdown produced by the page OCR model, kept as it is
        return (c or "").strip() or None
    if t == "heading":
        lvl = min(6, max(1, int(b.get("level") or 2)))
        return "#" * lvl + " " + re.sub(r"\s+", " ", c or "").strip() if c else None
    if t == "table":
        tbl = dict(rows=b.get("rows") or [], cells=b.get("cells"), n_rows=b.get("n_rows"), header_rows=b.get("header_rows", 1))
        body = T.render(tbl) if (tbl["rows"] or tbl["cells"]) else None
        if not body:
            return None
        return body
    if t == "equation":
        latex = (b.get("latex") or "").strip()
        if latex:
            return "$$\n" + latex + "\n$$"
        return c or None
    if t == "list":
        if not c:
            return None
        return "\n".join(re.sub(r"^\s*[•●▪◦‣⁃·]\s*", "- ", ln) for ln in c.split("\n"))
    if t in ("handwritten_code",):
        return "```\n" + c + "\n```" if c else None
    if t in ("figure", "chart", "diagram", "unknown", "image"):
        parts = []
        if annotate and b.get("image_reference"):
            parts.append(f"![{t} on page {b.get('page')}]({b['image_reference']})")
        if b.get("description") and annotate:
            parts.append(f"*{b['description']}*")
        if c and c != b.get("description"):
            parts.append(c)
        ft = _figure_text(b) if not c else None   # content, when present, is built from the same labels
        if ft and ft not in "\n".join(parts):
            parts.append(ft)
        if b.get("latex"):
            parts.append("$$\n" + b["latex"] + "\n$$")
        if b.get("rows"):
            parts.append(T.to_markdown(b["rows"]))
        if not parts and annotate:
            parts.append(f"> [region not extracted - page {b.get('page')}, bbox {b.get('bbox')}; see source]")
        return "\n\n".join(parts) if parts else None
    if c is None:
        if annotate:
            return f"> [unreadable region - page {b.get('page')}, bbox {b.get('bbox')}; flagged for review]"
        return None
    return _escape_par(c)


def render(doc, annotate=False, include_headers=False, page_markers=None):
    """doc: Digital Twin dict with blocks. Returns Markdown text."""
    blocks = sorted(doc.get("blocks", []), key=lambda b: (b.get("page", 0), b.get("reading_order", 0)))
    out, last_page, seen = [], None, set()
    prev_para = False
    multi = len(doc.get("pages", [])) > 1
    if page_markers is None:
        page_markers = annotate and multi
    for b in blocks:
        if b.get("superseded_by"):
            b["in_markdown"] = False
            b["markdown_exclusion"] = "page read by the high-accuracy OCR model (" + b["superseded_by"] + ")"
            continue
        if b.get("role") in HEADER_ROLES and not include_headers:
            b["in_markdown"] = False
            b["markdown_exclusion"] = "running header/footer/page number (" + (b.get("role_evidence") or "") + ")"
            continue
        if b.get("page") != last_page:
            if page_markers:
                out.append(f"<!-- page {b.get('page')} -->")
            last_page = b.get("page")
        txt = render_block(b, annotate)
        if not txt:
            b["in_markdown"] = False
            b.setdefault("markdown_exclusion", "no extractable content")
            continue
        key = (b.get("type"), _norm(txt))
        if len(key[1]) > 12 and key in seen:
            b["in_markdown"] = False
            b["markdown_exclusion"] = "duplicate of an earlier block"
            continue
        seen.add(key)
        b["in_markdown"] = True
        if b.get("continues_previous") and not annotate and out and b.get("type") == "paragraph" and prev_para:
            # the same sentence continues after a column break: one paragraph in Markdown
            out[-1] = (out[-1][:-1] + txt) if out[-1].endswith("-") and txt[:1].islower() else out[-1].rstrip() + " " + txt
            continue
        out.append((_prov(b) + "\n" + txt) if annotate else txt)
        prev_para = b.get("type") == "paragraph"
    return "\n\n".join(out).strip() + "\n"
