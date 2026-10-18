"""Ingest -> classify -> analyse regions -> specialised extraction -> reading order -> normalise ->
verify -> canonical Digital Twin -> Markdown / JSON.

Pages are processed one at a time (bounded OCR concurrency for scanned pages), so memory does not
grow with page count: only the current page image(s) are held. Each finished page is checkpointed to
<out_dir>/pages/pNNNNN.json; an interrupted job resumes from the checkpoints when the source file and
configuration are unchanged. A failed page becomes an explicit error page instead of failing the job.
"""
import contextlib
import hashlib
import json
import os
import re
import statistics
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pymupdf
from PIL import Image
from scipy import ndimage as ndi

from . import config, headers, layout, markdown, mathtext, ocr, pdf_parser, scan, verifier, vision
from . import tables as T
from . import visual
from . import formats, vlm_ocr
from .schema import make_block, overlap_frac
from .textnorm import Vocabulary, join_lines

STAGES = ["DETECT", "ANALYZE", "ROUTE", "EXTRACT", "ASSEMBLE", "VERIFY", "OUTPUT"]
SCHEMA_VERSION = "2.0"
VIS_EXT = {"handwritten_text": "vision_handwriting", "handwritten_code": "vision_handwriting", "paragraph": "vision_handwriting",
           "heading": "vision_handwriting", "list": "vision_handwriting", "equation": "math_vision", "diagram": "vision_diagram",
           "figure": "vision_diagram", "chart": "vision_chart", "unknown": "vision_diagram", "table": "vision_table"}
PREFIX = {"table": "tbl", "chart": "chart", "diagram": "diag", "equation": "eq", "handwritten_text": "hand", "handwritten_code": "code",
          "figure": "fig", "unknown": "unk", "caption": "cap"}


class PFError(Exception):
    def __init__(self, code, title, message):
        super().__init__(message)
        self.code, self.title, self.message = code, title, message

    def dict(self):
        return dict(code=self.code, title=self.title, message=self.message)


class Cancelled(Exception):
    pass


class Progress:
    """Job progress. When `path` is set the state is persisted (atomically) on every update so
    another process or a restarted server can report it."""

    def __init__(self, path=None):
        self.stages = [dict(name=s, state="pending", detail="") for s in STAGES]
        self.status, self.error = "running", None
        self.total_pages = self.done_pages = 0
        self.current_page = None
        self.failed_pages = []
        self.enrich = None          # background reading of images after the document is shown
        self.path = path
        self._cancel = threading.Event()
        self.started = time.time()
        self._last_save = 0.0

    def set(self, name, state, detail=""):
        for s in self.stages:
            if s["name"] == name:
                s["state"], s["detail"] = state, detail or s["detail"]
        self.save(force=True)

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        if self._cancel.is_set():
            return True
        if self.path and os.path.exists(os.path.join(os.path.dirname(self.path), "CANCEL")):
            return True
        return False

    def to_dict(self):
        pct = round(100.0 * self.done_pages / self.total_pages, 1) if self.total_pages else 0.0
        return dict(status=self.status, stages=self.stages, error=self.error, total_pages=self.total_pages,
                    completed_pages=self.done_pages, current_page=self.current_page, failed_pages=self.failed_pages,
                    progress_percent=pct, elapsed_seconds=round(time.time() - self.started, 1),
                    **({"enrich": self.enrich} if self.enrich else {}))

    def save(self, force=False):
        if not self.path:
            return
        now = time.time()
        if not force and now - self._last_save < 0.5:
            return
        self._last_save = now
        _atomic_json(self.path, self.to_dict())


_JSON_LOCK = threading.Lock()


def _atomic_json(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with _JSON_LOCK:
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        for k in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:      # Windows: a reader has the file open for a moment
                time.sleep(0.05 * (k + 1))
        try:
            os.remove(tmp)
        except OSError:
            pass


# ----------------------------------------------------------------------------------- ingest

def open_source(path, filename):
    pext = os.path.splitext(str(path))[1].lower().lstrip(".")
    # a converted file (report.docx -> source.pdf) is read by what it is now, shown by its original name
    ext = pext if pext in ("pdf", "png", "jpg", "jpeg") else (filename.lower().rsplit(".", 1)[-1] if "." in filename else "")
    if ext not in ("pdf", "png", "jpg", "jpeg"):
        raise PFError("PF-204", "Unsupported Document", "This file needs converting first (see processing/formats.py).")
    if os.path.getsize(path) == 0:
        raise PFError("PF-205", "Empty Document", "The file contains no data.")
    if ext == "pdf":
        try:
            d = pymupdf.open(path)
        except Exception:
            raise PFError("PF-201", "Corrupt Document", "The PDF could not be opened. It may be damaged or not a real PDF.")
        if d.needs_pass:
            d.close()
            raise PFError("PF-202", "Encrypted Document", "The PDF is password protected.")
        if d.page_count == 0:
            d.close()
            raise PFError("PF-205", "Empty Document", "The PDF has no pages.")
        return "pdf", d
    try:
        im = Image.open(path)
        im.load()
        im = im.convert("RGB")
    except Exception:
        raise PFError("PF-201", "Corrupt Document", "The image could not be decoded.")
    return "image", im


def page_count(path, filename):
    kind, src = open_source(path, filename)
    try:
        return src.page_count if kind == "pdf" else 1
    finally:
        if kind == "pdf":
            src.close()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------------- helpers

def _scale(r, s):
    return tuple(v * s for v in r)


def detect_ink_regions(img, ocr_boxes):
    """Regions with ink that OCR could not explain (handwriting, diagrams, charts). Ruled lines are removed first."""
    g = np.array(img.convert("L"))
    thr = min(170, np.median(g) * 0.65)
    mask = g < thr
    mask &= ~ndi.binary_dilation(ndi.binary_opening(mask, structure=np.ones((1, 90))) | ndi.binary_opening(mask, structure=np.ones((90, 1))), iterations=2)
    for (x0, y0, x1, y1) in ocr_boxes:
        mask[max(0, int(y0) - 3):int(y1) + 3, max(0, int(x0) - 3):int(x1) + 3] = False
    f = 4
    h, w = (mask.shape[0] // f) * f, (mask.shape[1] // f) * f
    if h == 0 or w == 0:
        return []
    coarse = mask[:h, :w].reshape(h // f, f, w // f, f).max(axis=(1, 3))
    lab, n = ndi.label(ndi.binary_dilation(coarse, structure=np.ones((3, 3)), iterations=10))
    out = []
    for sl in ndi.find_objects(lab):
        y0, y1, x0, x1 = sl[0].start * f, sl[0].stop * f, sl[1].start * f, sl[1].stop * f
        ink = int(mask[y0:y1, x0:x1].sum())
        bw, bh = x1 - x0, y1 - y0
        if ink < 400 or bw < 24 or bh < 24 or bw * bh > 0.9 * img.size[0] * img.size[1]:
            continue
        out.append((max(0, x0 - 8), max(0, y0 - 8), min(img.size[0], x1 + 8), min(img.size[1], y1 + 8)))
    return out


def vision_blocks(crop_img, bbox, hint, state):
    """Specialised extraction for one visual region via the optional vision model. Falls back to a
    flagged, evidence-preserving block. Returns a list of blocks (bbox in the same space as `bbox`)."""
    cx0, cy0, cx1, cy1 = bbox
    cw, ch = cx1 - cx0, cy1 - cy0
    err = None
    if state["vision_left"] > 0 and vision.available() and state.get("vision_enabled", True) and _ai_while_processing():
        state["vision_left"] -= 1
        state["vision_used"] += 1
        items, err = vision.analyze_crop(crop_img)
        blocks = []
        for it in items:
            t = it.get("type") if it.get("type") in vision.VALID else "unknown"
            rows = None
            if t == "table":
                rr = it.get("rows")
                rows = [[str(c or "").strip() for c in r] for r in rr if isinstance(r, list)] if isinstance(rr, list) else None
                rows = [r for r in (rows or []) if any(r)]
                if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
                    t, rows = "unknown", None
            content = it.get("content") or (it.get("description") if t in ("diagram", "chart", "figure", "unknown") else None)
            if t == "table":
                content = f"Table with {len(rows)} rows"
            try:
                conf = max(0.0, min(0.95, float(it.get("confidence", 0.5))))
            except Exception:
                conf = 0.5
            warns, bb = [], bbox
            raw = it.get("bbox")
            if isinstance(raw, list) and len(raw) == 4 and all(isinstance(v, (int, float)) and 0 <= v <= 1000 for v in raw) and raw[2] - raw[0] > 8 and raw[3] - raw[1] > 8:
                bb = (cx0 + raw[0] / 1000 * cw, cy0 + raw[1] / 1000 * ch, cx0 + raw[2] / 1000 * cw, cy0 + raw[3] / 1000 * ch)
            elif len(items) > 1:
                warns.append("Model gave no valid sub-region box; using the whole analysed region.")
            if not content and not it.get("latex"):
                conf = min(conf, 0.45)
                warns.append("Low confidence - content withheld rather than guessed.")
            ch_ = it.get("chart") if isinstance(it.get("chart"), dict) else {}
            data = ch_.get("data") if isinstance(ch_.get("data"), list) and ch_.get("data") else None
            b = make_block("", t, 0, bb, conf, "model (self-reported)", VIS_EXT[t], content,
                           latex=it.get("latex") if t == "equation" else None,
                           elements=it.get("elements") or None,
                           relationships=[str(r) for r in it.get("relationships") or []] if t == "diagram" else None,
                           chart_type=ch_.get("chart_type") if t == "chart" else None, title=ch_.get("title") if t == "chart" else None,
                           data=data if t == "chart" else None, rows=rows, axes=ch_.get("axes") if t == "chart" else None,
                           legend=ch_.get("legend") if t == "chart" else None,
                           description=it.get("description") if t in ("diagram", "chart", "figure", "unknown") else None)
            if t == "chart" and not data:
                b["warnings"].append("Chart values not extracted - no values invented.")
            b["warnings"] += warns
            blocks.append(b)
        if blocks:
            return blocks, None
        err = err or "vision model returned no regions"
    return [], err


# ----------------------------------------------------------------------------------- digital pages

def _has_ruling(page):
    try:
        for d in page.get_drawings():
            for it in d["items"]:
                if it[0] == "l":
                    return True
                if it[0] == "re":
                    r = it[1]
                    if min(r.width, r.height) < 2.5 and max(r.width, r.height) > 15:
                        return True
                    if d.get("type") in ("s", "fs") and r.width > 15 and r.height > 8:
                        return True
        return False
    except Exception:
        return True


def _cache_drawings(page):
    """page.get_drawings() is slow (pure Python over every path) and figures, ruling detection,
    maths and find_tables each called it: read it once per page. Callers only read the result."""
    if getattr(page, "_pf_drawings_cached", False):
        return
    orig, memo = page.get_drawings, {}

    def cached(*a, **k):
        key = (a, tuple(sorted(k.items())))
        if key not in memo:
            memo[key] = orig(*a, **k)
        return list(memo[key])
    page.get_drawings = cached
    page._pf_drawings_cached = True


def digital_page(page, n, cfg, state, vocab, S, ocr_layer=False):
    """Extract one page that has a usable text layer. Returns (blocks, meta)."""
    _cache_drawings(page)
    W, H = page.rect.width, page.rect.height
    lines = pdf_parser.lines_from_words(page) if ocr_layer else pdf_parser.lines(page)
    for l in lines:
        vocab.add_text(l["text"])
    body = pdf_parser.body_font_size(lines)
    t0 = time.perf_counter()
    tb = pdf_parser.ruled_tables(page) if _has_ruling(page) else []
    if tb:
        wds_all = pdf_parser.words(page)
        for t in tb:
            T.merge_currency_columns(t)
            pdf_parser.extend_header_row(t, wds_all, 1.2 * body)
    blks = pdf_parser.group_blocks(lines)
    blks = [b for b in blks if not any(overlap_frac(b["bbox"], t["bbox"]) > 0.5 for t in tb)]
    figs = pdf_parser.figures(page, [t["bbox"] for t in tb])
    timing = dict(tables_ruled=round(time.perf_counter() - t0, 4))

    # math first, so that equations are not mistaken for tables or vector figures
    t0 = time.perf_counter()
    eq_items = []
    ctx = mathtext.PageContext(page, body) if cfg.math_from_text_layer else None
    if ctx is not None:
        ctx.math_page = mathtext.page_is_mathematical(lines)
        for b in blks:
            b["_mk"] = mathtext.block_kind(b, ctx)
        blks = mathtext.absorb_inline_fragments(blks)
        used = set()
        for g in mathtext.group_display(blks, ctx):
            chars = [c for b in g["blocks"] for c in b.get("_chars") or []]
            res = mathtext.display_from_chars(chars, ctx)
            if res:
                for r in (res if isinstance(res, list) else [res]):
                    bb = r.get("bbox") if isinstance(res, list) else g["bbox"]
                    eq_items.append(dict(kind="equation", bbox=bb, latex=r["latex"], coverage=r["coverage"], number=r["number"]))
                used |= {id(b) for b in g["blocks"]}
        blks = [b for b in blks if id(b) not in used]
        if ctx.math_page:
            for b in blks:
                if b.get("_mk") in ("prose", "display", "script"):
                    mathtext.mark_inline(b, ctx)
        for b in blks:
            b.pop("_chars", None)
    figs = [f for f in figs if not any(overlap_frac(f["bbox"], e["bbox"]) > 0.3 for e in eq_items)]
    timing["math"] = round(time.perf_counter() - t0, 4)

    t0 = time.perf_counter()
    if cfg.borderless_tables:
        wds = pdf_parser.words(page)
        avoid = [t["bbox"] for t in tb] + [f["bbox"] for f in figs if f["kind"] == "image"] + [e["bbox"] for e in eq_items]
        for t in T.detect_borderless(wds, W, avoid=avoid):
            t["borderless"] = True
            tb.append(t)
        # a vector "figure" that is really the frame/shading of a detected table is not a figure
        figs = [f for f in figs if not (f["kind"] == "vector" and any(overlap_frac(t["bbox"], f["bbox"]) > 0.5 or overlap_frac(f["bbox"], t["bbox"]) > 0.5 for t in tb))]
        blks = [b for b in blks if not any(overlap_frac(b["bbox"], t["bbox"]) > 0.5 for t in tb if t.get("borderless"))]
    timing["tables_borderless"] = round(time.perf_counter() - t0, 4)
    if tb:
        absorbed = _absorb_spanning_headers(tb, pdf_parser.words(page), body)
        if absorbed:
            blks = [b for b in blks if not any(overlap_frac(b["bbox"], t["bbox"]) > 0.5 for t in absorbed)]

    # Text that lives inside a figure (axis labels, legends, diagram node text) becomes the figure's
    # detected text instead of body paragraphs.
    for f in figs:
        fb = f["bbox"]
        grown = (fb[0] - 4, fb[1] - 4, fb[2] + 4, fb[3] + 4)
        inside = [b for b in blks if overlap_frac(b["bbox"], grown) > 0.8]
        prose = sum(len(b["text"]) for b in inside if len(b["text"]) > 160)
        if prose > 0 or sum(len(b["text"]) for b in inside) > 1200:
            f["discard"] = True
            continue
        f["labels"] = inside
        blks = [b for b in blks if b not in inside]
    figs = [f for f in figs if not f.get("discard")]

    blks = _attach_drop_caps(blks, body)
    for b in blks:
        b["kind"] = "text"
    headers.classify_page(blks, W, H, body_size=body, line_h=1.2 * body)

    items = [dict(kind="text", bbox=b["bbox"], ref=b) for b in blks]
    items += [dict(kind="table", bbox=t["bbox"], ref=t) for t in tb]
    items += [dict(kind="figure", bbox=f["bbox"], ref=f) for f in figs]
    items += [dict(kind="equation", bbox=e["bbox"], ref=e) for e in eq_items]
    body_items = [i for i in items if not (i["kind"] == "text" and i["ref"].get("role") in headers.HEADER_ROLES)]
    edge_items = [i for i in items if i not in body_items]
    ordered, cols = layout.reading_order(body_items, line_height=1.2 * body)

    # paragraph merge across line-level blocks, in reading order
    merged, run = [], []
    for it in ordered:
        if it["kind"] == "text" and it["ref"].get("role", "body") == "body":
            run.append(it["ref"])
            continue
        if run:
            merged += [dict(kind="text", bbox=b["bbox"], ref=b) for b in pdf_parser.merge_paragraphs(run, body)]
            run = []
        merged.append(it)
    if run:
        merged += [dict(kind="text", bbox=b["bbox"], ref=b) for b in pdf_parser.merge_paragraphs(run, body)]
    ordered = merged + edge_items  # running heads keep their blocks but are not part of the body order

    blocks = []
    for it in ordered:
        k, r = it["kind"], it["ref"]
        bb = _scale(it["bbox"], S)
        if k == "text":
            line_texts = r.get("line_texts")
            typ, content, lvl = pdf_parser.classify(r, body, vocab, line_texts)
            b = make_block("", typ, n, bb, 0.99, "text-layer evidence", "digital_text_parser", content, level=lvl)
            b["role"] = r.get("role", "body")
            if r.get("role_evidence"):
                b["role_evidence"] = r["role_evidence"]
            if r.get("merged"):
                b["merged_from_blocks"] = r["merged"]
            if r.get("has_math"):
                b["contains_math"] = True
            b["font_size"] = round(r["size"], 2)
        elif k == "table":
            ext = "borderless_table" if r.get("borderless") else "table_engine"
            conf = min(0.95, 0.6 + 0.35 * r.get("filled", 0.8)) if not r.get("borderless") else 0.7
            b = make_block("", "table", n, bb, conf, "structural heuristic", ext, f"Table with {len(r['rows'])} rows",
                           rows=r["rows"], cells=r.get("cells"), n_rows=r.get("n_rows"), n_cols=r.get("n_cols"),
                           header_rows=r.get("header_rows", 1), structure_evidence=r.get("evidence"))
            b["html"] = T.to_html(dict(rows=r["rows"], cells=r.get("cells"), n_rows=r.get("n_rows"), header_rows=r.get("header_rows", 1)))
        elif k == "equation":
            b = make_block("", "equation", n, bb, round(0.5 + 0.4 * (r.get("coverage") or 0), 2), "glyph coverage", "text_layer_math",
                           r["latex"], latex=r["latex"], glyph_coverage=r.get("coverage"), equation_number=r.get("number"),
                           display=True)
        else:
            b = figure_block(page, n, r, cfg, state, S)
        blocks.append(b)
    if cols:
        blocks = _link_continuations(blocks, 1.2 * body * S)
    meta = dict(body_font_size=round(body, 2), multi_column=cols, tables=len(tb), figures=len(figs), equations=len(eq_items), timing=timing)
    return blocks, meta


_ENDS_MID = re.compile(r"[a-z,;\-–]$")


_STARTS_WORD = re.compile(r"^[a-z][a-z'’-]+\b(?![.)])")


def _link_continuations(blocks, line_h=24.0):
    """Multi-column pages: a paragraph that stops mid-sentence ("... examines whether a") continues in
    the column whose first paragraph starts in lower case ("tobacco company ..."). When the layout
    order put that column somewhere else, move the column's run of blocks right after the sentence
    start. Only body paragraphs are linked, each continuation at most once."""
    def body(b):
        return b["type"] == "paragraph" and b.get("role", "body") == "body" and (b.get("content") or "").strip()

    def same_col(a, b):
        ov = min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0])
        return ov > 0.5 * min(a["bbox"][2] - a["bbox"][0], b["bbox"][2] - b["bbox"][0])
    def col_top(c, blocks_):
        # nothing of the same column ends just above it: it starts a column (or the page)
        # (a block just above it that ends mid-sentence would be the one it continues)
        return not any(b is not c and same_col(b, c) and b["bbox"][3] <= c["bbox"][1] + 2 and c["bbox"][1] - b["bbox"][3] < 2.5 * line_h
                       and body(b) and _ENDS_MID.search(b["content"].rstrip()) for b in blocks_)
    def stack_top(p, blocks_):
        # top of the stack of blocks above p in its column: a continuation column starts no higher
        top, x0, x1 = p["bbox"][1], p["bbox"][0], p["bbox"][2]
        for _ in range(50):
            above = [b for b in blocks_ if b is not p and min(b["bbox"][2], x1) - max(b["bbox"][0], x0) > 0
                     and b["bbox"][3] <= top + 2 and top - b["bbox"][3] < 2.5 * line_h and b["bbox"][1] < top]
            if not above:
                break
            top = min(b["bbox"][1] for b in above)
        return top
    out = list(blocks)
    used = set()
    for _ in range(6):
        moved = False
        for i, p in enumerate(out):
            t = (p.get("content") or "").rstrip()
            if not body(p) or len(t) < 60 or not _ENDS_MID.search(t) or id(p) in used:
                continue
            nxt = out[i + 1] if i + 1 < len(out) else None
            if nxt is not None and not body(nxt) and same_col(p, nxt):
                continue                                    # the sentence runs into an equation, list or table
            if nxt is not None and body(nxt) and _STARTS_WORD.match(nxt["content"].lstrip()):
                if not same_col(p, nxt):
                    nxt["continues_previous"] = True        # same sentence across a column break
                used.add(id(p))
                continue                                    # already followed by its continuation
            cands = [j for j, c in enumerate(out) if j != i and body(c) and _STARTS_WORD.match(c["content"].lstrip())
                     and not same_col(p, c) and id(c) not in used and col_top(c, out)
                     and c["bbox"][0] > p["bbox"][0] and c["bbox"][1] >= stack_top(p, out) - line_h
                     and not (j > 0 and body(out[j - 1]) and _ENDS_MID.search(out[j - 1]["content"].rstrip()) and same_col(out[j - 1], c))]
            if not cands:
                continue
            j = min(cands, key=lambda j: (out[j]["bbox"][0], out[j]["bbox"][1]))   # top of the next column to the right
            run = [j]
            while run[-1] + 1 < len(out) and same_col(out[j], out[run[-1] + 1]) and out[run[-1] + 1].get("role", "body") == "body":
                run.append(run[-1] + 1)
            seg = [out[k] for k in run]
            rest = [b for k, b in enumerate(out) if k not in run]
            at = rest.index(p) + 1
            out = rest[:at] + seg + rest[at:]
            seg[0]["continues_previous"] = True
            used.add(id(p))
            used.add(id(seg[0]))
            moved = True
            break
        if not moved:
            break
    return out


def _absorb_spanning_headers(tables, words, body):
    """A short heading set just above a table's value columns ("Year Ended" over three date columns)
    is the table's top header row, spanning those columns. Returns the tables that changed."""
    changed = []
    for t in tables:
        cx = t.get("col_x")
        if not cx or len(cx) < 4 or not t.get("rows"):
            continue
        x0, y0, x1, _ = t["bbox"]
        band = [w for w in words if w[3] <= y0 + 1.5 and y0 - w[3] <= 1.6 * body and w[0] >= cx[1] - 2 and w[2] <= x1 + 2]
        if not band:
            continue
        # one text line: the band's lowest baseline
        ybot = max(w[3] for w in band)
        line = sorted([w for w in band if abs(w[3] - ybot) <= 0.4 * body], key=lambda w: w[0])
        text = " ".join(w[4] for w in line)
        if not line or len(line) > 6 or text.rstrip().endswith(".") or not any(ch.isalpha() for ch in text):
            continue
        # words above the first (label) column would make it a caption, not a column header
        if any(w[0] < cx[1] - 2 and abs(w[3] - ybot) <= 0.4 * body for w in words if w[3] <= y0 + 1.5):
            continue
        n = len(cx) - 1
        centers = [(cx[k] + cx[k + 1]) / 2 for k in range(n)]
        lx0, lx1 = line[0][0], line[-1][2]
        cov = [k for k in range(1, n) if lx0 - 0.6 * (cx[k + 1] - cx[k]) <= centers[k] <= lx1 + 0.6 * (cx[k + 1] - cx[k])]
        data_mid = (cx[1] + cx[-1]) / 2
        if len(cov) <= 1 and abs((lx0 + lx1) / 2 - data_mid) <= 0.15 * (cx[-1] - cx[1]):
            cov = list(range(1, n))                 # centred over all value columns: spans them all
        if not cov:
            continue
        c0, cs = cov[0], cov[-1] - cov[0] + 1
        cells = t.get("cells") or [dict(r=i, c=j, rs=1, cs=1, text=x) for i, r in enumerate(t["rows"]) for j, x in enumerate(r)]
        cells = [dict(c, r=c["r"] + 1) for c in cells]
        head = [dict(r=0, c=k, rs=1, cs=1, text="") for k in range(c0)] + [dict(r=0, c=c0, rs=1, cs=cs, text=text)]
        head += [dict(r=0, c=k, rs=1, cs=1, text="") for k in range(c0 + cs, n)]
        t["cells"] = head + cells
        t["rows"] = [["" if k != c0 else text for k in range(n)]] + t["rows"]
        t["n_rows"] = len(t["rows"])
        t["header_rows"] = t.get("header_rows", 1) + 1
        t["bbox"] = (x0, min(w[1] for w in line), x1, t["bbox"][3])
        t["evidence"] = (t.get("evidence") or "") + "; spanning header above the value columns"
        changed.append(t)
    return changed


def _attach_drop_caps(blks, body):
    """A single large initial letter set beside a paragraph (drop cap) is the paragraph's first letter."""
    out = []
    caps = [b for b in blks if len(b["text"].strip()) == 1 and b["text"].strip().isalpha() and b["text"].strip().isupper()
            and b["size"] >= 1.8 * body]
    for c in caps:
        cx1, cy0 = c["bbox"][2], c["bbox"][1]

        def beside(b):
            # the paragraph's first line starts just right of the letter; later lines may wrap under it
            fl = (b.get("lines") or [None])[0]
            fb = fl["bbox"] if fl and fl.get("bbox") else b["bbox"]
            return -2 <= fb[0] - cx1 <= 1.5 * body and abs(fb[1] - cy0) <= c["size"]
        tgt = [b for b in blks if b is not c and b["n_lines"] >= 1 and beside(b)]
        if tgt:
            t = min(tgt, key=lambda b: b["bbox"][1])
            first = t["lines"][0]
            t["lines"][0] = dict(first, text=c["text"].strip() + first["text"])
            if t.get("line_texts"):
                t["line_texts"][0] = c["text"].strip() + t["line_texts"][0]
            t["text"] = c["text"].strip() + t["text"]
            t["bbox"] = (min(t["bbox"][0], c["bbox"][0]), min(t["bbox"][1], c["bbox"][1]), t["bbox"][2], max(t["bbox"][3], c["bbox"][3]))
            c["_dropcap"] = True
    return [b for b in blks if not b.get("_dropcap")]


SCAN_READER_MSG = ("To read scanned pages, restart ParseFlow with run.bat (it installs the free OCR engine) "
                   "or connect an AI model with Connect AI.")
NO_READER_MSG = ("To read the text in this image, connect an AI model (Connect AI) or restart ParseFlow with run.bat, "
                 "which installs the free OCR engine.")


def _ai_while_processing():
    """Hosted APIs read images while the document is processed; a local model (Ollama on a laptop
    CPU, ~1 min per image) would stall the upload, so it reads on demand unless enabled."""
    if vision.info().get("local"):
        return os.getenv("LOCAL_AI_DURING_PROCESSING", "0") == "1"
    return True


def _tokset(s):
    return {t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower())}


def apply_ai_reading(b, d, model_name=None):
    """Merge an AI reading (vision.read_visual result) into a visual block. The local reading (OCR
    or PDF text labels) is kept as `local_text` and used as independent evidence: the share of its
    words that also occur in the AI transcription is stored as `ocr_agreement`."""
    local = b.get("content") if b.get("extractor") in ("figure_ocr", "figure_text_layer") else None
    local = local or b.get("ocr_text")
    if local:
        b["local_text"] = local
        b["local_extractor"] = b.get("extractor")
        lt, at = _tokset(local), _tokset(d.get("text"))
        b["ocr_agreement"] = round(len(lt & at) / len(lt), 3) if len(lt) >= 3 else None
    vt = d.get("visual_type") or "figure"
    b.update(content=d.get("text"), description=d.get("description"), visual_type=vt, title=d.get("title"),
             description_source="AI model (" + (model_name or vision.model()) + ")",
             extractor="vision_visual", confidence=d.get("confidence", 0.6), confidence_type="model (self-reported)",
             extraction_method=(b.get("extraction_method") or "region") + "+ai_reading", ai_read=True)
    if d.get("handwritten"):
        b["handwritten"] = True
    elif b.get("handwritten") and not d.get("handwritten"):
        b["handwritten"] = False
        b["warnings"] = [w for w in b.get("warnings", []) if not w.startswith("Looks handwritten")]
    if vt == "handwriting":
        b["type"] = "handwritten_text"
    elif b.get("type") in ("unknown",):
        b["type"] = "figure"
    for k in ("elements", "relationships"):
        if d.get(k):
            b[k] = list(d[k])
    for k in ("rows", "latex", "chart"):
        if d.get(k):
            b[k] = d[k]
    if d.get("illegible"):
        b["warnings"].append("Part of this image is illegible; unreadable words are marked [illegible], not guessed.")
    if not d.get("text"):
        b["warnings"].append("The AI found no readable text in this image.")
    b.pop("needs_reader", None)
    stale = ("No readable text found", "Visual/handwritten region", "Printed labels read by OCR", "Confidence ", "Looks handwritten",
             "Some labels were corrected", "Low OCR confidence", "Some words have OCR confidence", "Low confidence")
    b["warnings"] = [w for w in b.get("warnings", []) if w != NO_READER_MSG and not w.startswith(stale)]
    return b


def figure_block(page, n, f, cfg, state, S):
    """Figure / image region: keep the region, attach text found inside it (text layer first,
    OCR for raster images), and optionally a vision description."""
    bb = _scale(f["bbox"], S)
    detected, method = [], []
    for lb in f.get("labels") or []:
        for l in lb["lines"]:
            detected.append(dict(text=l["text"], bbox=[round(v * S) for v in l["bbox"]], confidence=0.99, source="text_layer"))
    if detected:
        method.append("text_layer_labels")
    vinfo, crop_size = dict(has_h=True, has_v=True), None   # vector figures are drawn with strokes
    ocr_missing = False
    if state.get("defer_visuals"):
        will_ocr = f.get("kind") == "image" and cfg.figure_ocr and not detected and ocr.available()
        will_ai = vision.available() and state.get("vision_enabled", True) and _ai_while_processing() and (f.get("kind") == "image" or not detected)
        if will_ocr or will_ai:
            pb = _pending_block(n, bb, "figure", f.get("kind"))
            pb["pending"]["page_bbox"] = [float(v) for v in f["bbox"]]
            return pb
    if f.get("kind") == "image" and cfg.figure_ocr and not detected:
        rect = pymupdf.Rect(f["bbox"])
        if not ocr.available():
            ocr_missing = True
        elif rect.width * rect.height >= 0.004 * page.rect.width * page.rect.height:
            try:
                pix = page.get_pixmap(dpi=200, clip=rect, colorspace=pymupdf.csGRAY)
                img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                crop_size = img.size
                k = 72.0 / 200 * S
                items, vinfo = visual.image_text(img, timeout=cfg.page_timeout_s)
                for d in items:
                    x0, y0, x1, y1 = d["bbox"]
                    e = dict(text=d["text"], bbox=[round(bb[0] + x0 * k), round(bb[1] + y0 * k), round(bb[0] + x1 * k), round(bb[1] + y1 * k)],
                             confidence=d["confidence"], source="ocr", in_markdown=d["keep"], row=d.get("row", 0))
                    if d.get("ocr_raw"):
                        e.update(ocr_raw=d["ocr_raw"], correction=d["corrected"])
                    detected.append(e)
                method.append("figure_ocr")
            except Exception as e:
                state.setdefault("warnings", []).append(f"Page {n}: figure OCR failed ({type(e).__name__}).")
    kept = [d for d in detected if d.get("in_markdown", True)]
    if detected and "figure_ocr" not in method:      # text-layer labels: order them like OCR labels
        detected = visual.reading_order([dict(d, bbox=tuple(d["bbox"])) for d in detected])
        for d in detected:
            d["bbox"] = [round(v) for v in d["bbox"]]
        kept = detected
        crop_size = (bb[2] - bb[0], bb[3] - bb[1])
        rel = [dict(d, bbox=(d["bbox"][0] - bb[0], d["bbox"][1] - bb[1], d["bbox"][2] - bb[0], d["bbox"][3] - bb[1])) for d in kept]
    else:
        k_ = 72.0 / 200 * S
        rel = [dict(d, bbox=tuple((v - o) / k_ for v, o in zip(d["bbox"], (bb[0], bb[1], bb[0], bb[1])))) for d in kept]
    conf = (sum(d["confidence"] for d in kept) / len(kept)) if kept else 0.0
    ext = "figure_ocr" if "figure_ocr" in method else ("digital_text_parser" if detected else "fallback")
    b = make_block("", "figure", n, bb, round(conf, 3) if kept else 0.3, "mean confidence of detected text" if kept else "no evidence",
                   ext, None, detected_text=detected or None, image_reference=None, preserved_image=True,
                   figure_kind=f.get("kind"), extraction_method="+".join(method) or "region_only")
    b["ocr_text"] = "\n".join(d["text"] for d in kept) or None
    if ext == "digital_text_parser":
        b["extractor"] = "figure_text_layer"
    # likely handwriting (offline evidence): ruled notebook lines, or OCR confidence typical of handwriting
    if f.get("kind") == "image" and "figure_ocr" in method:
        mean_c = (sum(d["confidence"] for d in detected) / len(detected)) if detected else 0.0
        why = ("ruled notebook lines (%d)" % vinfo["ruled"]) if vinfo.get("ruled") else \
              ("low OCR confidence (%.2f) typical of handwriting" % mean_c if detected and mean_c < 0.75 and len(detected) >= 3 else None)
        if why:
            b["handwritten"], b["handwritten_evidence"] = True, why
            b["warnings"].append("Looks handwritten (" + why + "). Offline OCR is unreliable on handwriting; "
                                 "the text shown is a rough reading. Use Read with AI for a proper transcription.")
    if kept:
        vt, ev, desc = visual.classify(rel, vinfo.get("has_h"), vinfo.get("has_v"), crop_size, f.get("kind"))
        b["content"] = visual.as_text(kept)
        b.update(visual_type=vt, visual_type_evidence=ev, description=desc, description_source="heuristic (local, no model)")
        if any(d.get("ocr_raw") for d in kept):
            b["warnings"].append("Some labels were corrected from sibling labels (see detected_text[].ocr_raw).")
    elif ocr_missing:
        b["needs_reader"] = True
        b["warnings"].append(NO_READER_MSG)
    # optional AI reading: all visible text + a description (local OCR result kept as evidence)
    if vision.available() and state.get("vision_enabled", True) and state["vision_left"] > 0 and _ai_while_processing() and \
            (f.get("kind") == "image" or not kept) and \
            (bb[2] - bb[0]) * (bb[3] - bb[1]) >= 0.008 * page.rect.width * page.rect.height * S * S:
        try:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(S * 1.5, S * 1.5), clip=pymupdf.Rect(f["bbox"]), alpha=False)
            crop = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            state["vision_left"] -= 1
            state["vision_used"] += 1
            d, err = vision.read_visual(crop)
            if d:
                apply_ai_reading(b, d)
            elif err:
                b["warnings"].append(f"AI reading failed ({err}); local result kept.")
        except Exception as e:
            b["warnings"].append(f"AI reading failed ({type(e).__name__}).")
    if b.get("extractor") == "vision_visual":
        b.pop("needs_reader", None)
        b["warnings"] = [w for w in b["warnings"] if w != NO_READER_MSG]
    if not detected and not b.get("content") and not ocr_missing and b.get("extractor") != "vision_visual":
        b["visual_type"], b["description"] = "picture", "No readable text was found in this visual."
        b["warnings"].append("No readable text found in this visual; region preserved. No description generated"
                             + ("." if vision.available() else " (no vision model configured)."))
    return b


# ----------------------------------------------------------------------------------- scanned pages

def _ocr_image(img_gray, cfg):
    """OCR with selective fallbacks. Returns (words, info)."""
    pre, info = scan.preprocess(img_gray)
    attempts = []
    ws = ocr.words(pre, psm=3, lang=cfg.ocr_lang, timeout=cfg.page_timeout_s, preprocessed=True)
    st = ocr.stats(ws)
    attempts.append(dict(variant="preprocessed psm3", **st))
    best_ws, best_score, best_img = ws, st["words"] * st["mean_conf"], pre
    # orientation check only when the first pass looks implausible
    if st["words"] < 20 or st["mean_conf"] < 0.5:
        rot, rconf = ocr.osd_rotation(pre, timeout=cfg.page_timeout_s)
        info["osd_rotation"], info["osd_confidence"] = rot, round(rconf, 2)
        if rot in (90, 180, 270) and rconf >= 5.0:
            rimg = pre.rotate(-rot, expand=True, fillcolor=255)
            ws2 = ocr.words(rimg, psm=3, lang=cfg.ocr_lang, timeout=cfg.page_timeout_s, preprocessed=True)
            st2 = ocr.stats(ws2)
            attempts.append(dict(variant=f"rotated {rot}", **st2))
            if st2["words"] * st2["mean_conf"] > best_score:
                best_ws, best_score, best_img = ws2, st2["words"] * st2["mean_conf"], rimg
                info["rotated"] = rot
    # low-confidence fallback: single uniform block segmentation
    # (not below 0.45: that is handwriting or drawings, where a second full-page pass does not help)
    if best_score and 0.45 <= attempts[-1]["mean_conf"] < 0.6 and attempts[-1]["words"] >= 30 and "rotated" not in info:
        ws3 = ocr.words(pre, psm=6, lang=cfg.ocr_lang, timeout=cfg.page_timeout_s, preprocessed=True)
        st3 = ocr.stats(ws3)
        attempts.append(dict(variant="preprocessed psm6", **st3))
        if st3["words"] * st3["mean_conf"] > 1.15 * best_score:
            best_ws, best_score = ws3, st3["words"] * st3["mean_conf"]
            info["fallback"] = "psm6"
    info["ocr_attempts"] = attempts
    return best_ws, info, best_img


def _readable(q):
    """OCR paragraphs too uncertain to present as text at all (kept only as raw evidence)."""
    # Below this mean word confidence Tesseract output is overwhelmingly noise (handwriting,
    # drawings, stains); it is kept as raw evidence (ocr_text) but not presented as document text.
    return q["conf"] >= float(os.getenv("OCR_MIN_PARAGRAPH_CONF", "0.45"))


def _explained(q):
    """OCR confidently explains this ink; anything else on the page is a candidate visual /
    handwriting region for review or for the optional vision model."""
    return q["conf"] >= 0.6


def scanned_page_blocks(img_rgb, n, ws, info, k, cfg, state, vocab, page_h_px, page_w_px, ocr_layer_text=None):
    """Turn OCR words (in OCR-image pixels) into blocks in viewer space (multiply by k)."""
    paras = [q for q in ocr.paragraphs(ws, 0.0) if len(re.sub(r"\W", "", q["text"])) >= 1]
    for q in paras:
        vocab.add_text(q["text"])
    hs = [q["height"] for q in paras if q["words"] >= 3] or [q["height"] for q in paras] or [10]
    med = float(statistics.median(hs))
    cands = []
    for q in paras:
        bb = tuple(v * k for v in q["bbox"])
        cands.append(dict(bbox=bb, text=q["text"], size=q["height"] * k, n_lines=q["n_lines"], q=q))
    headers.classify_page(cands, page_w_px, page_h_px, body_size=med * k, line_h=1.6 * med * k)
    blocks = []
    for c in cands:
        q = c["q"]
        conf = q["conf"]
        heading = med and q["height"] > 1.35 * med and q["n_lines"] <= 2 and len(q["text"]) < 160
        text = join_lines(q["lines"], vocab) if not heading else re.sub(r"\s+", " ", q["text"])
        if not _readable(q):
            b = make_block("", "unknown", n, c["bbox"], round(conf, 3), "ocr", "ocr", None, ocr_text=q["text"],
                           word_boxes=[dict(w, bbox=[round(v * k) for v in w["bbox"]]) for w in q["word_boxes"]])
            b["warnings"].append("OCR confidence too low to trust (possibly handwriting or a drawing); raw OCR kept in ocr_text, not in the body.")
        else:
            b = make_block("", "heading" if heading else "paragraph", n, c["bbox"], round(conf, 3), "ocr", "ocr", text,
                           level=2 if heading else None,
                           word_boxes=[dict(w, bbox=[round(v * k) for v in w["bbox"]]) for w in q["word_boxes"]])
            if conf < 0.6:
                b["warnings"].append(f"Low OCR confidence ({conf:.2f}): text may be wrong (possibly handwriting or a degraded scan).")
            elif q["min_conf"] < 0.5:
                b["warnings"].append("Some words have OCR confidence below 0.5 (see word_boxes).")
        b["role"] = c.get("role", "body")
        if c.get("role_evidence"):
            b["role_evidence"] = c["role_evidence"]
        blocks.append(b)
    # ink that OCR did not explain: handwriting / drawings
    boxes = [tuple(v * k for v in q["bbox"]) for q in paras if _explained(q)]
    view_img = img_rgb
    regions = detect_ink_regions(view_img, boxes) if view_img is not None else []
    for r in regions:
        if state.get("defer_visuals"):
            _insert_by_position(blocks, _pending_block(n, r, "ink"))
            continue
        blocks = _apply_ink_region(blocks, view_img, r, n, cfg, state)
    return _merge_handwritten_page(blocks, img_rgb, n)


def _pending_block(n, bbox, kind, fkind=None):
    """Placeholder for a region read in the background after the document is shown (web app)."""
    b = make_block("", "figure" if kind == "figure" else "unknown", n, tuple(bbox), 0.3, "not read yet", "pending", None,
                   preserved_image=True)
    b["pending"] = dict(kind=kind, bbox=[float(v) for v in bbox], figure_kind=fkind)
    b["warnings"].append("Reading the text in this region...")
    return b


def _apply_ink_region(blocks, view_img, r, n, cfg, state):
    """Read one region of unexplained ink (handwriting, drawing, screenshot) on a scanned page:
    the AI model when connected, else printed labels by OCR, else a flagged region."""
    crop = view_img.crop(tuple(int(v) for v in r))
    vb, err = vision_blocks(crop, r, "unknown", state)
    if vb:
        # the model's reading supersedes low-confidence OCR inside the region (kept as evidence)
        inside = [b for b in blocks if b["extractor"] == "ocr" and b["confidence"] < 0.6 and overlap_frac(tuple(b["bbox"]), r) > 0.5]
        for b in vb:
            b["page"] = n
            if inside:
                b["ocr_text"] = "\n".join(x.get("content") or x.get("ocr_text") or "" for x in inside)
        return [b for b in blocks if b not in inside] + vb
    # local fallback: printed labels inside a drawing / chart / screenshot are still read
    items, vinfo = [], {}
    if ocr.available():
        try:
            items, vinfo = visual.image_text(crop.convert("L"), timeout=cfg.page_timeout_s, min_conf=0.6)
        except Exception:
            items = []
    kept = [d for d in items if d["keep"] and visual.plausible(d["text"])]
    if kept:
        det = [dict(text=d["text"], bbox=[round(r[0] + d["bbox"][0]), round(r[1] + d["bbox"][1]), round(r[0] + d["bbox"][2]), round(r[1] + d["bbox"][3])],
                    confidence=d["confidence"], source="ocr", in_markdown=True, row=d.get("row", 0),
                    **({"ocr_raw": d["ocr_raw"]} if d.get("ocr_raw") else {})) for d in kept]
        vt, ev, desc = visual.classify(kept, vinfo.get("has_h"), vinfo.get("has_v"), crop.size)
        conf = sum(d["confidence"] for d in kept) / len(kept)
        b = make_block("", "figure", n, r, round(conf, 3), "mean confidence of detected text", "figure_ocr", visual.as_text(kept),
                       detected_text=det, ocr_text="\n".join(d["text"] for d in kept), preserved_image=True,
                       visual_type=vt, visual_type_evidence=ev, description=desc, description_source="heuristic (local, no model)",
                       extraction_method="figure_ocr")
        b["warnings"].append("Printed labels read by OCR; handwriting or drawn shapes in this region are not interpreted"
                             + ("." if vision.available() else " (no vision model configured)."))
    else:
        why = (f"Vision extraction failed ({err}) - region preserved, flagged." if err and vision.available()
               else "Visual/handwritten region: no offline recogniser for handwriting or drawings; configure VISION_API_KEY to interpret it. Region preserved.")
        b = make_block("", "unknown", n, r, 0.3, "no evidence", "fallback", None, preserved_image=True, needs_reader=True)
        b["warnings"].append(why)
    _insert_by_position(blocks, b)
    return blocks


def _merge_handwritten_page(blocks, img, n):
    """A scanned page that is handwritten notes (ruled notebook lines, OCR confidence below printed
    levels) becomes ONE handwritten block over the notes: offline OCR fragments of handwriting are
    not useful on their own, and one AI reading of the whole page keeps its context."""
    if img is None:
        return blocks
    try:
        ruled = visual.ruled_lines(img.convert("L"))
    except Exception:
        ruled = 0
    body = [b for b in blocks if b.get("role", "body") == "body" and b.get("extractor") in ("ocr", "figure_ocr", "fallback")]
    if ruled < 6 or not body:
        return blocks
    confs = [b["confidence"] for b in body if b.get("content")]
    if confs and sum(confs) / len(confs) >= 0.85:
        return blocks
    x0 = min(b["bbox"][0] for b in body); y0 = min(b["bbox"][1] for b in body)
    x1 = max(b["bbox"][2] for b in body); y1 = max(b["bbox"][3] for b in body)
    rough = "\n".join(b["content"] for b in sorted(body, key=lambda b: (b["bbox"][1], b["bbox"][0])) if b.get("content"))
    hb = make_block("", "figure", n, (x0, y0, x1, y1), round(sum(confs) / len(confs), 3) if confs else 0.3,
                    "mean OCR word confidence", "figure_ocr", rough or None, preserved_image=True, handwritten=True,
                    handwritten_evidence=f"ruled notebook lines ({ruled}) on a scanned page", ocr_text=rough or None,
                    visual_type="handwriting", extraction_method="page_ocr", merged_blocks=len(body))
    hb["warnings"].append("Looks handwritten (ruled notebook lines). Offline OCR is unreliable on handwriting; "
                          "the text shown is a rough reading. Use Read with AI for a proper transcription.")
    keep = [b for b in blocks if b not in body]
    return keep + [hb]


def _insert_by_position(blocks, b):
    """Place a visual region before the first block that starts below it, so figures read where
    they sit on the page instead of after all the text. Regions on the same row keep their
    left-to-right order (regions arrive sorted). Approximate on multi-column scans."""
    y0 = b["bbox"][1]
    for i, o in enumerate(blocks):
        if o["bbox"][1] > y0 + 2:
            blocks.insert(i, b)
            return
    blocks.append(b)


# ----------------------------------------------------------------------------------- driver

def _render(page, scale):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _page_job(kind, src, i, cfg, state, vocab):
    """Synchronous part of one page. Returns either a finished result or an OCR task to run."""
    n = i + 1
    S = cfg.render_scale
    if kind == "pdf":
        page = src[i]
        # viewer scale: 144 dpi by default, reduced for oversized pages (long side <= 2400 px)
        S = min(cfg.render_scale, 2400.0 / max(1.0, page.rect.width, page.rect.height))
        a = pdf_parser.analyze(page)
        W, H = page.rect.width * S, page.rect.height * S
        meta = dict(n=n, width=round(W), height=round(H), scale=S, chars=a["chars"], image_coverage=a["image_coverage"],
                    text_quality=a["text_quality"], ocr_layer=a["ocr_layer"])
        use_ocr = a["scanned"] or (a["ocr_layer"] and os.getenv("REOCR_OCR_LAYER", "0") == "1")
        if not use_ocr:
            blocks, m = digital_page(page, n, cfg, state, vocab, S, ocr_layer=a["ocr_layer"])
            meta.update(kind="mixed" if (m["tables"] or m["figures"]) else "digital", flags=dict(
                text_layer=True, scanned=False, ocr_text_layer=a["ocr_layer"], tables=m["tables"], figures=m["figures"],
                equations=m["equations"], multi_column=m["multi_column"] or None), body_font_size=m["body_font_size"])
            info = page_info_for(page, S, a)
            return dict(done=True, n=n, blocks=blocks, meta=meta, info=info)
        dpi = ocr_dpi_for(page, cfg)
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        gray = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        view = _render(page, S)
        k = (72.0 / dpi) * S
        layer = page.get_text("text") if a["ocr_layer"] else None
        meta.update(kind="scanned", flags=dict(text_layer=a["has_text_layer"], scanned=True), ocr_dpi=dpi)
        return dict(done=False, n=n, gray=gray, view=view, k=k, meta=meta, ocr_layer_text=layer,
                    info=dict(width=W, height=H, norm_text="", text_quality=1.0, clip_text=None))
    # single image input
    img = src
    W, H = img.size
    meta = dict(n=1, width=W, height=H, scale=1.0, chars=0, image_coverage=1.0, kind="image", flags=dict(text_layer=False, scanned=True))
    gray = img.convert("L")
    k = 1.0
    if max(W, H) < 1600:  # small photos/screenshots: upscale for OCR, keep viewer coordinates
        f = 1600.0 / max(W, H)
        gray = gray.resize((int(W * f), int(H * f)), Image.BICUBIC)
        k = 1.0 / f
    return dict(done=False, n=1, gray=gray, view=img, k=k, meta=meta, ocr_layer_text=None,
                info=dict(width=W, height=H, norm_text="", text_quality=1.0, clip_text=None))


MAX_OCR_PIXELS = 4200 * 4200


def ocr_dpi_for(page, cfg):
    """Resolution-aware rasterisation: never upsample a scan far beyond its native resolution,
    upsample low-resolution scans moderately (helps small text), and cap total pixels."""
    W, H = page.rect.width, page.rect.height
    native = None
    try:
        infos = [i for i in page.get_image_info() if (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) > 0.5 * W * H]
        if infos:
            i = max(infos, key=lambda i: i["width"] * i["height"])
            native = i["width"] / max(1e-6, (i["bbox"][2] - i["bbox"][0]) / 72.0)
    except Exception:
        native = None
    dpi = float(cfg.ocr_dpi)
    if native:
        dpi = min(dpi, native) if native >= 200 else min(dpi, max(native * 2, 200))
    max_dpi = (MAX_OCR_PIXELS / max(1.0, (W / 72.0) * (H / 72.0))) ** 0.5
    return int(max(72, min(dpi, max_dpi)))


def page_info_for(page, S, a):
    txt = page.get_text("text", flags=pdf_parser.TEXT_FLAGS)

    # Independent re-read: MuPDF's word list (a different extraction path from the line/span parse the
    # blocks come from), read ONCE per page; page.get_textbox() per block re-walks every glyph on the
    # page for each block and was ~60 % of the time on digital pages.
    try:
        words = page.get_text("words", flags=pdf_parser.TEXT_FLAGS)
    except Exception:
        words = None

    def clip_text(bb_px):
        if words is None:
            return None
        x0, y0, x1, y1 = (v / S for v in bb_px)
        x0, y0, x1, y1 = x0 - 1, y0 - 1, x1 + 1, y1 + 1
        inside = [w for w in words if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1]
        inside.sort(key=lambda w: (w[5], w[6], w[7]))          # block, line, word order
        return " ".join(w[4] for w in inside)
    return dict(width=page.rect.width * S, height=page.rect.height * S, norm_text=re.sub(r"[^a-z0-9]+", "", txt.lower()),
                text_quality=a["text_quality"], clip_text=clip_text)


def _finish_scanned(job, cfg, state, vocab):
    ws, info, _ = _ocr_image(job["gray"], cfg)
    meta = job["meta"]
    meta["preprocessing"] = {k: v for k, v in info.items() if k != "ocr_attempts"}
    meta["ocr"] = info.get("ocr_attempts")
    blocks = scanned_page_blocks(job["view"], job["n"], ws, info, job["k"], cfg, state, vocab, meta["height"], meta["width"],
                                 job.get("ocr_layer_text"))
    if job.get("ocr_layer_text"):
        job["info"]["ocr_layer_text"] = job["ocr_layer_text"]
    if any(b["extractor"].startswith(("vision", "math")) or b["extractor"] == "fallback" for b in blocks):
        meta["kind"] = "scanned + visual/handwriting"
    meta["flags"]["visual_regions"] = sum(1 for b in blocks if b["extractor"] == "fallback" or b["extractor"].startswith("vision"))
    return blocks


def _verify_page(blocks, info, view_img=None):
    crop = (lambda bb, im=view_img: im.crop(tuple(int(v) for v in bb))) if view_img is not None else None
    for b in blocks:
        verifier.verify(b, info, crop)


def _error_page(n, meta, err, cfg):
    W = meta.get("width") or 1224
    H = meta.get("height") or 1584
    b = make_block("", "unknown", n, (0, 0, W, H), 0.0, "no evidence", "fallback", None, preserved_image=True)
    b["warnings"].append(f"Page processing failed ({err}); the page is preserved for manual review.")
    b["verification"], b["evidence"], b["confidence_level"] = "failed", "page error", "low"
    meta.update(n=n, width=W, height=H, kind="error", error=err, flags=meta.get("flags", {}))
    return [b], meta


def _config_signature(cfg):
    keys = ("render_scale", "ocr_dpi", "ocr_lang", "strip_headers", "math_from_text_layer", "borderless_tables", "figure_ocr")
    return hashlib.sha1(json.dumps({k: getattr(cfg, k) for k in keys} | {"schema": SCHEMA_VERSION, "vision": vision.available()},
                                   sort_keys=True).encode()).hexdigest()[:16]


def _vlm_pass(path, flat, pages, prog, warnings):
    """Read every page with the configured document OCR model (e.g. olmOCR-2). Each page gets one
    'page_text' block; the local blocks of that page stay in the JSON as evidence but are marked
    superseded (left out of Markdown and Ask). Pages the model cannot do keep the local result.

    A person is waiting in the app, so this pass is bounded: a time budget per page, a stop as soon as
    the server reports a permanent problem (bad key, unknown model, no credit) or stays unreachable,
    and a "skip" request from the UI (file SKIP_VLM in the job folder) which keeps the local result."""
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    n = len(pages)
    cfg = vlm_ocr.app_settings()
    stop = threading.Event()
    state = dict(done=0, ok=0, fatal=None, busy_fail=0, closed=False)
    job_dir = os.path.dirname(prog.path) if getattr(prog, "path", None) else None
    t0 = time.time()

    def show():   # progress and time only; technical details go to the JSON and backend/ai_check.py
        if state["closed"]:      # a request abandoned after Skip may finish later: don't touch the stages then
            return
        prog.set("EXTRACT", "running", f"high-accuracy OCR {state['done']}/{n} pages ({int(time.time() - t0)} s)")

    def one(p):
        try:
            r = vlm_ocr.transcribe_page(path, p, cfg, sleep=stop.wait, stop=stop)
        except vlm_ocr.VLMError as e:
            r = dict(ok=False, errors=[str(e)], permanent=e.permanent)
            if e.permanent:
                state["fatal"] = str(e)
                stop.set()                      # the same problem would hit every page
        except Exception as e:                  # rendering problem etc.: this page only
            r = dict(ok=False, errors=[f"{type(e).__name__}: {str(e)[:100]}"])
        state["done"] += 1
        if r.get("ok"):
            state["ok"] += 1
        elif r.get("busy"):
            state["busy_fail"] += 1
            if state["busy_fail"] >= 3 and state["ok"] == 0:
                state["fatal"] = r["errors"][-1] if r.get("errors") else "the OCR server is unreachable"
                stop.set()
        show()
        return p, r

    show()
    results, skipped = {}, None
    ex = ThreadPoolExecutor(max(1, int(os.getenv("VLM_CONCURRENCY", "4"))))
    pending = {ex.submit(one, p) for p in range(1, n + 1)}
    try:
        while pending:
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            if not done and int(time.time() - t0) % 3 == 0:
                show()                          # keep the elapsed time moving while a request is in flight
            for f in done:
                p, r = f.result()
                results[p] = r
            if job_dir and os.path.exists(os.path.join(job_dir, "SKIP_VLM")):
                skipped = "skipped on request"
            elif prog.cancelled:
                skipped = "cancelled"
            if skipped or (state["fatal"] and stop.is_set()):
                stop.set()
                for f in pending:
                    f.cancel()
                # requests already in flight finish on their own; their results are not waited for
                for f in [f for f in pending if f.done() and not f.cancelled()]:
                    p, r = f.result()
                    results[p] = r
                break
    finally:
        state["closed"] = True
        ex.shutdown(wait=False, cancel_futures=True)
    if skipped == "cancelled":
        raise Cancelled()
    model = cfg["model"]
    out, failed = [], []
    for pg in pages:
        p = pg["n"]
        r = results.get(p) or {}
        local = [b for b in flat if b["page"] == p]
        if not r.get("ok"):
            failed.append(p)
            out += local
            continue
        text = r.get("text") or ""
        W, H = pg.get("width") or 1000, pg.get("height") or 1400
        b = make_block(f"vlm_{p}_01", "page_text", p, (0, 0, W, H), 0.9, "model (self-reported)", "ocr_vlm", text,
                       model=model, attempts=r.get("attempts"), rotation=r.get("rotation"), renderer=r.get("renderer"))
        layer = " ".join(x.get("content") or "" for x in local if x.get("extractor") in ("digital_text_parser", "table_engine", "borderless_table"))
        lt, vt = {t for t in re.findall(r"[a-z0-9]{3,}", layer.lower())}, {t for t in re.findall(r"[a-z0-9]{3,}", text.lower())}
        if len(lt) >= 20:
            agree = len(lt & vt) / len(lt)
            b["evidence_checks"] = [dict(check="PDF text-layer words found in the model's page text", result=round(agree, 3))]
            ok = agree >= 0.9
            b["verification"] = "verified" if ok else "review"
            b["evidence"] = (f"independent: {agree:.0%} of the PDF's own words appear in the model's reading" if ok
                             else f"only {agree:.0%} of the PDF's own words appear in the model's reading")
        else:
            b["verification"], b["evidence"] = "review", "model reading; the page has no text layer to check it against"
        b["reading_order"] = 0
        for x in local:
            x["superseded_by"] = b["block_id"]
        out += [b] + local
    errs = [e for p in failed for e in (results.get(p) or {}).get("errors", [])[-1:]]
    why = state["fatal"] or skipped or (max(set(errs), key=errs.count) if errs else None)
    if state["fatal"] and not state["ok"]:
        vlm_ocr.mark_unavailable(state["fatal"])     # don't make the next uploads wait for the same problem
    if failed and not skipped:
        warnings.append("High-accuracy OCR was not available for this document; ParseFlow's own reading was used."
                        if len(failed) == n else
                        f"High-accuracy OCR was not available for page(s) {_page_ranges(failed)}; ParseFlow's own reading was used there.")
    prog.set("EXTRACT", "done", f"high-accuracy OCR: {n - len(failed)}/{n} pages" if len(failed) < n else "page text read by ParseFlow")
    info = dict(model=model, pages_read=n - len(failed), fallback_pages=failed, seconds=round(time.time() - t0, 1),
                skipped=bool(skipped), reason=why if failed else None)
    return out, info


def _page_ranges(ps):
    ps, out = sorted(ps), []
    for p in ps:
        if out and p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out)


def process(path, filename, doc_id, out_dir, prog, cfg=None, write_outputs=True, defer_visuals=False):
    """Process a document. out_dir=None disables checkpoints/outputs (benchmark / library use).
    defer_visuals=True (web app): text inside images, handwriting regions and AI readings are left as
    'pending' placeholders so the document can be shown at once; enrich_visuals() reads them after."""
    cfg = cfg or config.settings()
    state = dict(vision_left=cfg.max_vision_regions, vision_used=0, vision_enabled=cfg.vision_enabled, warnings=[],
                 defer_visuals=defer_visuals)
    conv_tmp, converted = None, None
    if os.path.splitext(str(path))[1].lower() not in formats.NATIVE:
        # Word, Excel, PowerPoint, text, HTML, EPUB, TIFF ...: turn it into a PDF / PNG first
        conv_tmp = tempfile.TemporaryDirectory(prefix="pf_conv_")
        try:
            path, how = formats.to_pipeline_input(path, filename, conv_tmp.name)
        except formats.FormatError as e:
            conv_tmp.cleanup()
            raise PFError("PF-204", "Unsupported Document", str(e))
        converted = dict(from_extension=os.path.splitext(filename)[1].lower(), method=how)
    try:
        return _process(path, filename, doc_id, out_dir, prog, cfg, write_outputs, state, converted)
    finally:
        if conv_tmp is not None:
            conv_tmp.cleanup()


def _process(path, filename, doc_id, out_dir, prog, cfg, write_outputs, state, converted):
    warnings = state["warnings"]
    vocab = Vocabulary()
    prog.set("DETECT", "running")
    kind, src = open_source(path, filename)
    try:
        n_total = src.page_count if kind == "pdf" else 1
        n_pages = n_total
        if cfg.max_pages and n_total > cfg.max_pages:
            n_pages = cfg.max_pages
            warnings.append(f"PARSEFLOW_PAGE_LIMIT={cfg.max_pages} is set: processed the first {n_pages} of {n_total} pages.")
        prog.total_pages = n_pages
        prog.set("DETECT", "done", f"{kind.upper()} · {n_pages} page{'s' if n_pages != 1 else ''}")

        ck_dir = None
        resumed = 0
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            ck_dir = os.path.join(out_dir, "pages")
            os.makedirs(ck_dir, exist_ok=True)
            man_path = os.path.join(out_dir, "manifest.json")
            man = dict(source_sha256=file_sha256(path), config=_config_signature(cfg), n_pages=n_pages, schema=SCHEMA_VERSION)
            try:
                old = json.load(open(man_path))
            except Exception:
                old = None
            if old != man:
                for f in os.listdir(ck_dir):
                    os.remove(os.path.join(ck_dir, f))
                _atomic_json(man_path, man)

        prog.set("ANALYZE", "running", "page-by-page")
        prog.set("ROUTE", "running")
        prog.set("EXTRACT", "running")
        results = {}
        pool = ThreadPoolExecutor(max(1, cfg.ocr_workers)) if (kind == "image" or cfg.ocr_workers >= 1) else None
        pending = deque()
        limit = max(1, cfg.ocr_workers) * 2

        def store(n, blocks, meta, ok=True):
            results[n] = (blocks, meta)
            if ck_dir:
                _atomic_json(os.path.join(ck_dir, f"p{n:05d}.json"), dict(blocks=blocks, meta=meta))
            prog.done_pages += 1
            if not ok:
                prog.failed_pages.append(n)
            prog.save()

        def drain(block_all=False):
            while pending and (block_all or len(pending) >= limit or pending[0][1].done()):
                n, fut, job = pending.popleft()
                try:
                    blocks = fut.result()
                    _verify_page(blocks, job["info"], job["view"])
                    store(n, blocks, job["meta"])
                except Exception as e:
                    blocks, meta = _error_page(n, job["meta"], f"{type(e).__name__}: {str(e)[:120]}", cfg)
                    store(n, blocks, meta, ok=False)
                job.pop("gray", None)
                job.pop("view", None)

        for i in range(n_pages):
            n = i + 1
            if prog.cancelled:
                raise Cancelled()
            prog.current_page = n
            if ck_dir and os.path.exists(os.path.join(ck_dir, f"p{n:05d}.json")):
                try:
                    d = json.load(open(os.path.join(ck_dir, f"p{n:05d}.json")))
                    results[n] = (d["blocks"], d["meta"])
                    for b in d["blocks"]:
                        vocab.add_text(b.get("content") or "")
                    prog.done_pages += 1
                    resumed += 1
                    continue
                except Exception:
                    pass
            try:
                job = _page_job(kind, src, i, cfg, state, vocab)
            except Exception as e:
                blocks, meta = _error_page(n, dict(n=n), f"{type(e).__name__}: {str(e)[:120]}", cfg)
                store(n, blocks, meta, ok=False)
                continue
            if job["done"]:
                _verify_page(job["blocks"], job["info"])
                store(n, job["blocks"], job["meta"])
            elif not ocr.available():
                # no offline OCR engine: a connected AI model reads the scanned page instead
                meta = job["meta"]
                full = (0, 0, meta["width"], meta["height"])
                if vision.available() and state.get("vision_enabled", True) and _ai_while_processing():
                    blocks = [_pending_block(n, full, "ink")] if state.get("defer_visuals") else \
                        _apply_ink_region([], job["view"], full, n, cfg, state)
                else:
                    b = make_block("", "unknown", n, full, 0.0, "no evidence", "fallback", None, preserved_image=True,
                                   needs_reader=True)
                    b["warnings"].append(SCAN_READER_MSG)
                    blocks = [b]
                    state["unread_scans"] = state.get("unread_scans", 0) + 1
                _verify_page(blocks, job["info"])
                store(n, blocks, meta)
            else:
                pending.append((n, pool.submit(_finish_scanned, job, cfg, state, vocab), job))
                drain()
        drain(block_all=True)
        if pool:
            pool.shutdown(wait=True)
        if resumed:
            state["resumed_pages"] = resumed        # internal: not shown as a warning
        prog.current_page = None
        prog.set("ANALYZE", "done", f"{n_pages} pages")
        prog.set("ROUTE", "done", "text layer / tables / OCR / vision per region")
        prog.set("EXTRACT", "done", f"{sum(len(v[0]) for v in results.values())} blocks")
    finally:
        if kind == "pdf":
            src.close()

    # ---------------------------------------------------------------- assemble (document level)
    prog.set("ASSEMBLE", "running")
    pages, blocks = [], []
    for n in range(1, n_pages + 1):
        bl, meta = results[n]
        pages.append(meta)
        blocks.append(bl)
    if cfg.strip_headers:
        headers.classify_document([(pages[i]["height"], [dict(bbox=b["bbox"], text=b.get("content"), ref=b) for b in bl
                                                          if b.get("content") and b["type"] in ("paragraph", "heading", "list", "caption")])
                                   for i, bl in enumerate(blocks)])
        # classify_document sets role on the wrapper dicts; copy back
    else:
        for bl in blocks:
            for b in bl:
                if b.get("role") in headers.HEADER_ROLES:
                    b["role_suppressed_by_config"] = b["role"]
                    b["role"] = "body"
    flat = []
    for i, bl in enumerate(blocks):
        n = i + 1
        ctr = {}
        for k, b in enumerate(bl, 1):
            b["page"] = n
            b["reading_order"] = k
            pre = PREFIX.get(b["type"]) or ("ocr" if b["extractor"] == "ocr" else "txt")
            if b["type"] in ("heading", "paragraph", "list") and b["extractor"] not in ("ocr", "digital_text_parser"):
                pre = "hand"
            ctr[pre] = ctr.get(pre, 0) + 1
            b["block_id"] = f"{pre}_{n}_{ctr[pre]:02d}"
        targets = [b for b in bl if b["type"] in ("table", "chart", "diagram", "figure")]
        for c in [b for b in bl if b["type"] == "caption"]:
            near = sorted(targets, key=lambda t: min(abs(t["bbox"][1] - c["bbox"][3]), abs(c["bbox"][1] - t["bbox"][3])))
            if near and min(abs(near[0]["bbox"][1] - c["bbox"][3]), abs(c["bbox"][1] - near[0]["bbox"][3])) < 160:
                c["relationships"].append("caption_of:" + near[0]["block_id"])
                near[0]["relationships"].append("caption:" + c["block_id"])
        for f in [b for b in bl if b["type"] == "figure"]:
            f["image_reference"] = f"figure_{n:03d}_{f['block_id'].split('_')[-1]}"
        flat += bl
    prog.set("ASSEMBLE", "done", f"{len(flat)} blocks ordered")
    prog.set("VERIFY", "done", f"{sum(b['verification'] == 'verified' for b in flat)} verified · "
                               f"{sum(b['verification'] == 'review' for b in flat)} review · {sum(b['verification'] == 'failed' for b in flat)} failed")

    # ---------------------------------------------------------------- optional high-accuracy page OCR
    hao = None
    if kind == "pdf" and vlm_ocr.settings()["configured"] and os.getenv("HIGH_ACCURACY_OCR", "1") != "0":
        down = vlm_ocr.unavailable()
        if down:          # the server failed for a reason that does not fix itself; retried after a while
            hao = dict(model=vlm_ocr.settings()["model"], pages_read=0, fallback_pages=[pg["n"] for pg in pages], seconds=0,
                       skipped=False, reason=down)
        else:
            flat, hao = _vlm_pass(path, flat, pages, prog, warnings)

    # ---------------------------------------------------------------- output
    prog.set("OUTPUT", "running")
    if state.get("unread_scans"):
        k = state["unread_scans"]
        warnings.append(f"{k} scanned page{'s' if k > 1 else ''} could not be read on this computer yet. " + SCAN_READER_MSG)
    if not flat:
        warnings.append("No content could be extracted from this document.")
    failed = sorted(set(prog.failed_pages))
    status = "partial" if failed else "complete"
    vis = ("chart", "figure", "diagram", "equation", "handwritten_text", "handwritten_code", "unknown")
    doc = dict(document_id=doc_id, filename=filename, format=kind, real=True, schema_version=SCHEMA_VERSION, pages=pages, blocks=flat,
               warnings=warnings, processing_status=status, failed_pages=failed, total_pages_in_file=n_total,
               router=[dict(block_id=b["block_id"], region_type=b["type"], extractor=b["extractor"]) for b in flat],
               stats=dict(pages=n_pages, blocks=len(flat), tables=sum(b["type"] == "table" for b in flat),
                          equations=sum(b["type"] == "equation" for b in flat) + sum(1 for b in flat if b.get("contains_math")),
                          visuals=sum(b["type"] in vis for b in flat),
                          verified=sum(b["verification"] == "verified" for b in flat),
                          flagged=sum(b["verification"] != "verified" for b in flat),
                          excluded_headers_footers=sum(b.get("role") in headers.HEADER_ROLES for b in flat),
                          lanes=dict(local=sum(b["extractor"] in ("digital_text_parser", "table_engine", "borderless_table", "text_layer_math", "figure_text_layer") for b in flat),
                                     ocr=sum(b["extractor"] in ("ocr", "figure_ocr") for b in flat), vision=state["vision_used"],
                                     flagged=sum(b["extractor"] == "fallback" for b in flat))),
               engines=dict(ocr=ocr.available(), vision=vision.info(), pymupdf=pymupdf.VersionBind),
               high_accuracy_ocr=hao, pending_visuals=sum(1 for b in flat if b.get("pending")), converted=converted, resumed_pages=state.get("resumed_pages", 0),
               vision_state=dict(left=state["vision_left"], used=state["vision_used"]))
    md = markdown.render(doc)
    if out_dir and write_outputs:
        with open(os.path.join(out_dir, "document.md"), "w", encoding="utf-8") as f:
            f.write(md)
        _atomic_json(os.path.join(out_dir, "document.json"), doc)
    prog.status = status
    prog.set("OUTPUT", "done", "Markdown + JSON ready")
    doc["_markdown"] = md
    return doc


class _LockedPage:
    """Lets several threads use pages of one PyMuPDF document: every MuPDF call goes through one lock
    (rendering is quick; the slow part, OCR, runs outside it)."""

    def __init__(self, page, lock):
        self._page, self._lock = page, lock
        with lock:
            self.rect = pymupdf.Rect(page.rect)

    def get_pixmap(self, *a, **k):
        with self._lock:
            return self._page.get_pixmap(*a, **k)


def enrich_visuals(path, filename, out_dir, prog=None, cfg=None, lock=None):
    """Second phase in the web app: read the regions process(defer_visuals=True) left pending (text in
    images, handwriting, AI readings), in parallel, then update document.json and document.md.
    Returns the number of regions read."""
    cfg = cfg or config.settings()
    jp = os.path.join(out_dir, "document.json")
    doc = json.load(open(jp, encoding="utf-8"))
    pend = [b for b in doc["blocks"] if b.get("pending")]
    if not pend:
        return 0
    vs = doc.get("vision_state") or {}
    mlock = threading.RLock()
    state = dict(vision_left=vs.get("left", cfg.max_vision_regions), vision_used=vs.get("used", 0),
                 vision_enabled=cfg.vision_enabled, warnings=[], defer_visuals=False)
    pages = {pg["n"]: pg for pg in doc["pages"]}
    kind, src = open_source(path, filename)
    views, done = {}, [0]
    if prog is not None:
        prog.enrich = dict(state="running", total=len(pend), done=0)
        prog.save(force=True)

    def view_of(n):
        if n not in views:
            if len(views) >= 2:
                views.clear()
            if kind == "pdf":
                with mlock:
                    views[n] = _render(src[n - 1], pages[n].get("scale") or cfg.render_scale)
            else:
                views[n] = src.convert("RGB")
        return views[n]

    def one(b):
        n, pd = b["page"], b["pending"]
        try:
            if pd["kind"] == "figure" and kind == "pdf":
                with mlock:
                    page = _LockedPage(src[n - 1], mlock)
                new = [figure_block(page, n, dict(bbox=tuple(pd["page_bbox"]), kind=pd.get("figure_kind")), cfg, state,
                                    pages[n].get("scale") or cfg.render_scale)]
            else:
                new = _apply_ink_region([], view_of(n), tuple(pd["bbox"]), n, cfg, state)
        except Exception as e:
            nb = dict(b)
            nb.pop("pending", None)
            nb["extractor"] = "fallback"
            nb["warnings"] = [f"Reading this region failed ({type(e).__name__}); region preserved."]
            new = [nb]
        done[0] += 1
        if prog is not None:
            prog.enrich = dict(state="running", total=len(pend), done=done[0])
            prog.save()
        return b["block_id"], pd, new

    workers = max(1, min(cfg.ocr_workers, len(pend)))
    try:
        if workers > 1:
            with ThreadPoolExecutor(workers) as ex:
                results = list(ex.map(one, pend))
        else:
            results = [one(b) for b in pend]
    finally:
        if kind == "pdf":
            src.close()
    ctx = lock or contextlib.nullcontext()
    with ctx:
        doc = json.load(open(jp, encoding="utf-8"))       # re-read: "Read with AI" may have changed other blocks
        by_id = {bid: (pd, new) for bid, pd, new in results}
        out = []
        for b in doc["blocks"]:
            if b["block_id"] not in by_id or not b.get("pending"):
                out.append(b)
                continue
            pd, new = by_id[b["block_id"]]
            n = b["page"]
            if any(x.get("extractor", "").startswith("vision") for x in new):
                # an AI reading of a handwriting region supersedes low-confidence OCR inside it (kept as evidence)
                r = tuple(pd["bbox"])
                for x in doc["blocks"]:
                    if x["page"] == n and x.get("extractor") == "ocr" and x.get("confidence", 1) < 0.6 and overlap_frac(tuple(x["bbox"]), r) > 0.5:
                        x["superseded_by"] = b["block_id"]
            for k, x in enumerate(new):
                x["page"] = n
                x["block_id"] = b["block_id"] if k == 0 else f"{b['block_id']}_{k + 1}"
                x["reading_order"] = b["reading_order"] + k * 0.01
                if b.get("role"):
                    x.setdefault("role", b["role"])
                meta = pages.get(n, {})
                verifier.verify(x, dict(width=meta.get("width") or 10 ** 6, height=meta.get("height") or 10 ** 6, norm_text=""), None)
                if x["type"] == "figure":
                    x["image_reference"] = b.get("image_reference")
                out.append(x)
        doc["blocks"] = out
        st = doc.setdefault("stats", {})
        st["verified"] = sum(1 for x in out if x.get("verification") == "verified")
        st["flagged"] = len(out) - st["verified"]
        st["blocks"] = len(out)
        doc["pending_visuals"] = 0
        doc["vision_state"] = dict(left=state["vision_left"], used=state["vision_used"])
        doc["warnings"] = list(dict.fromkeys((doc.get("warnings") or []) + state["warnings"]))
        md = markdown.render(doc)
        _atomic_json(jp, doc)
        tmp = os.path.join(out_dir, "document.md.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(md)
        os.replace(tmp, os.path.join(out_dir, "document.md"))
    if prog is not None:
        prog.enrich = dict(state="done", total=len(pend), done=len(pend))
        prog.save(force=True)
    return len(pend)


def process_file(path, out_dir=None, cfg=None, prog=None):
    """Library / CLI / benchmark entry point."""
    prog = prog or Progress()
    doc = process(path, os.path.basename(path), hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:12], out_dir, prog, cfg)
    return doc
