"""Digital-PDF extraction from the text layer and vector data (local, no AI).

All geometry here is in PDF points (1/72 in); the pipeline converts to viewer pixels.
"""
import re
import statistics

import pymupdf

from .schema import overlap_frac
from .textnorm import join_lines, normalize_ligatures

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
BULLET = re.compile(r"^(•|●|▪|◦|‣|⁃|·|[-*–]|\(?\d{1,3}[.)]|\(?[a-z][.)]|\(?[ivx]{1,4}[.)])\s+")
CAPTION = re.compile(r"^\s*(fig(ure)?|table|tab\.|chart|plate|scheme|algorithm|listing)\s*\.?\s*[\dIVXA-Z]+[.:\s]", re.I)


def analyze(page):
    """Page classification evidence: text-layer size, image coverage, invisible (OCR-layer) text."""
    W, H = page.rect.width, page.rect.height
    text = page.get_text("text", flags=TEXT_FLAGS)
    chars = len(text.strip())
    cov = 0.0
    for i in page.get_image_info():
        b = i["bbox"]
        cov += max(0, min(b[2], W) - max(b[0], 0)) * max(0, min(b[3], H) - max(b[1], 0))
    cov /= max(1.0, W * H)
    invisible = 0
    if chars and cov > 0.5:
        try:
            tr = page.get_texttrace()
            tot = sum(len(t["chars"]) for t in tr) or 1
            invisible = sum(len(t["chars"]) for t in tr if t.get("type") == 3) / tot
        except Exception:
            invisible = 0
    bad = sum(1 for ch in text if ch == "�" or 0xE000 <= ord(ch) <= 0xF8FF)
    quality = 1.0 - bad / max(1, chars)
    scanned = (cov > 0.85 and chars < 80)
    if chars < 30 and not scanned:
        # little or no text layer: OCR it if there is a picture or drawn (outlined) lettering to read;
        # a short page of real text with no images (a title slide, a small table) stays digital
        try:
            drawn = len(page.get_cdrawings()) if cov <= 0.3 else 0
        except Exception:
            drawn = 99
        scanned = cov > 0.3 or drawn > 30 or chars == 0 and drawn > 0
    return dict(chars=chars, image_coverage=round(cov, 2), has_text_layer=chars >= 30, scanned=scanned,
                ocr_layer=invisible > 0.5, text_quality=round(quality, 3))


def lines(page):
    """Text lines with spans (font, size, flags, bbox) and per-char boxes (for math reconstruction)."""
    out = []
    d = page.get_text("rawdict", flags=TEXT_FLAGS)
    fix = None
    for b in d["blocks"]:
        if b.get("type") == 0 and any(c["c"] == "\ufffd" for l in b["lines"] for s_ in l["spans"] for c in s_.get("chars") or []):
            from .glyphs import resolve_page
            fix = resolve_page(page)
            break
    if fix:
        for b in d["blocks"]:
            for l in b.get("lines", []):
                for s_ in l["spans"]:
                    keep = []
                    for c in s_.get("chars") or []:
                        if c["c"] == "\ufffd":
                            ch = fix.get((round(c["origin"][0], 1), round(c["origin"][1], 1)))
                            if ch == "":
                                continue  # extensible-glyph continuation piece
                            if ch:
                                c = dict(c, c=ch, from_glyph_name=True)
                        keep.append(c)
                    s_["chars"] = keep
    for bi, b in enumerate(d["blocks"]):
        if b.get("type") != 0:
            continue
        for li, l in enumerate(b["lines"]):
            spans = []
            prev = None
            gap_k = _space_gap_factor(l)
            for s in l["spans"]:
                chars = s.get("chars") or []
                if not chars:
                    continue
                # synthesise spaces the producer omitted (glyphs positioned with a word-sized gap)
                parts = []
                horiz = abs(l.get("dir", (1, 0))[1]) < 0.2
                for c in chars:
                    if horiz and prev is not None and c["c"] != " " and prev["c"] != " " and c["bbox"][0] - prev["bbox"][2] > gap_k * s["size"]:
                        parts.append(" ")
                    parts.append(c["c"])
                    prev = c
                txt = "".join(parts)
                if not txt:
                    continue
                spans.append(dict(text=txt, font=s["font"], size=s["size"], flags=s["flags"], bbox=tuple(s["bbox"]),
                                  origin=tuple(s["origin"]), chars=chars, ascender=s.get("ascender", 0.9),
                                  descender=s.get("descender", -0.2)))
            if not spans or not "".join(s["text"] for s in spans).strip():
                continue
            out.append(dict(block=bi, line=li, bbox=tuple(l["bbox"]), dir=tuple(l.get("dir", (1, 0))), spans=spans,
                            text=normalize_ligatures("".join(s["text"] for s in spans)).strip()))
    return _merge_fragments(out)


def _space_gap_factor(line):
    """Gap (in em) above which two glyphs are separate words. Most producers give glyph boxes that
    abut inside a word, so a word space shows as a clear gap; 0.2 em is safe. Some (e.g. captions in
    tightly set fonts) omit the space character and leave only ~0.13 em: when the glyph boxes inside
    words abut exactly, any gap of 0.1 em or more is a word break."""
    gaps, size = [], 0.0
    for sp in line["spans"]:
        ch = [c for c in sp.get("chars") or [] if c["c"] != " "]
        size = max(size, sp.get("size") or 0)
        gaps += [b["bbox"][0] - a["bbox"][2] for a, b in zip(ch, ch[1:])]
    if len(gaps) < 8 or size <= 0:
        return 0.2
    gaps.sort(key=abs)
    tight = abs(gaps[int(len(gaps) * 0.6)]) < 0.02 * size          # most neighbours touch exactly
    wide = [g for g in gaps if g > 0.1 * size]
    return 0.1 if tight and wide and min(wide) > 0.1 * size else 0.2


def _merge_fragments(ls):
    """Some producers (notably OCR text layers) emit each word or phrase as its own 'line'.
    Re-join horizontal fragments of the same block that share a baseline, ordered left to right."""
    by = {}
    for l in ls:
        by.setdefault(l["block"], []).append(l)
    out = []
    for bi, group in by.items():
        horiz = [l for l in group if abs(l["dir"][1]) < 0.2 and l["dir"][0] > 0]
        other = [l for l in group if l not in horiz]
        rows = []
        for l in sorted(horiz, key=lambda l: ((l["bbox"][1] + l["bbox"][3]) / 2, l["bbox"][0])):
            y0, y1 = l["bbox"][1], l["bbox"][3]
            placed = False
            for r in rows:
                ov = min(r["y1"], y1) - max(r["y0"], y0)
                sz = l["spans"][0]["size"]
                near = min(abs(l["bbox"][0] - m["bbox"][2]) if l["bbox"][0] >= m["bbox"][2] else abs(m["bbox"][0] - l["bbox"][2]) for m in r["lines"])
                if ov >= 0.5 * min(r["y1"] - r["y0"], y1 - y0) and near <= 3.0 * sz and not any(
                        min(m["bbox"][2], l["bbox"][2]) - max(m["bbox"][0], l["bbox"][0]) > 1 for m in r["lines"]):
                    r["lines"].append(l)
                    r["y0"], r["y1"] = min(r["y0"], y0), max(r["y1"], y1)
                    placed = True
                    break
            if not placed:
                rows.append(dict(y0=y0, y1=y1, lines=[l]))
        for r in rows:
            if len(r["lines"]) == 1:
                out.append(r["lines"][0])
                continue
            parts = sorted(r["lines"], key=lambda l: l["bbox"][0])
            spans, prev = [], None
            for l in parts:
                if prev is not None:
                    gap = l["bbox"][0] - prev["bbox"][2]
                    sz = prev["spans"][-1]["size"]
                    if gap > 0.12 * sz and not prev["text"].endswith(" ") and not l["text"].startswith(" "):
                        spans.append(dict(prev["spans"][-1], text=" ", chars=[], bbox=(prev["bbox"][2], prev["bbox"][1], l["bbox"][0], prev["bbox"][3])))
                spans += l["spans"]
                prev = l
            bb = (min(l["bbox"][0] for l in parts), min(l["bbox"][1] for l in parts), max(l["bbox"][2] for l in parts), max(l["bbox"][3] for l in parts))
            out.append(dict(block=bi, line=parts[0]["line"], bbox=bb, dir=parts[0]["dir"], spans=spans,
                            text=normalize_ligatures("".join(s["text"] for s in spans)).strip(), fragments=len(parts)))
        out += other
    return out


def lines_from_words(page):
    """Line reconstruction from word boxes, used for scans that carry an invisible OCR text layer
    (word-positioned text whose producer 'lines' are unreliable)."""
    ws = [w for w in page.get_text("words", flags=TEXT_FLAGS) if w[4].strip()]
    if not ws:
        return []
    hs = sorted(w[3] - w[1] for w in ws)
    mh = hs[len(hs) // 2] or 8.0
    rows = []
    for w in sorted(ws, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cy = (w[1] + w[3]) / 2
        best = None
        for r in rows[-6:]:
            if abs(r["cy"] - cy) <= 0.45 * mh and not any(min(o[2], w[2]) - max(o[0], w[0]) > 0.3 * (w[2] - w[0]) for o in r["w"]):
                best = r
                break
        if best:
            best["w"].append(w)
            best["cy"] = (best["cy"] * (len(best["w"]) - 1) + cy) / len(best["w"])
        else:
            rows.append(dict(cy=cy, w=[w]))
    out = []
    for r in rows:
        r["w"].sort(key=lambda w: w[0])
        segs, cur = [], [r["w"][0]]
        for a, b in zip(r["w"], r["w"][1:]):
            if b[0] - a[2] > 2.5 * mh:  # wide gap inside a visual row = column gutter
                segs.append(cur)
                cur = [b]
            else:
                cur.append(b)
        segs.append(cur)
        for sg in segs:
            bb = (min(w[0] for w in sg), min(w[1] for w in sg), max(w[2] for w in sg), max(w[3] for w in sg))
            text = normalize_ligatures(" ".join(w[4] for w in sg))
            size = statistics.median([w[3] - w[1] for w in sg]) * 0.8
            out.append(dict(block=None, line=0, bbox=bb, dir=(1.0, 0.0), text=text,
                            spans=[dict(text=text, font="ocr-text-layer", size=size, flags=0, bbox=bb, origin=(bb[0], bb[3]), chars=[])]))
    # paragraph ids: consecutive lines in the same column with small gaps share a block id
    out.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
    bid = 0
    open_blocks = []
    for l in out:
        hit = None
        for k, (b, last) in enumerate(open_blocks):
            gap = l["bbox"][1] - last["bbox"][3]
            ov = min(l["bbox"][2], last["bbox"][2]) - max(l["bbox"][0], last["bbox"][0])
            if -0.5 * mh <= gap <= 0.8 * mh and ov > 0.3 * min(l["bbox"][2] - l["bbox"][0], last["bbox"][2] - last["bbox"][0]):
                hit = k
                break
        if hit is None:
            bid += 1
            l["block"] = bid
            open_blocks.append((bid, l))
        else:
            l["block"] = open_blocks[hit][0]
            open_blocks[hit] = (open_blocks[hit][0], l)
        open_blocks = [(b, last) for b, last in open_blocks if l["bbox"][1] - last["bbox"][3] <= 3 * mh]
    return out


def words(page):
    return [(w[0], w[1], w[2], w[3], normalize_ligatures(w[4])) for w in page.get_text("words", flags=TEXT_FLAGS)]


def _may_have_ruled_table(page):
    """Cheap pre-check: a ruled table needs at least 3 horizontal rules (booktabs: top, mid, bottom)
    or a box drawn as rectangles. Counts thin horizontal strokes and filled cell rectangles."""
    try:
        paths = page.get_cdrawings()
    except Exception:
        return True
    horiz = rects = 0
    for p in paths:
        for it in p.get("items", ()):
            kind = it[0]
            if kind == "l":
                a, b = it[1], it[2]
                if abs(a[1] - b[1]) < 1.5 and abs(a[0] - b[0]) > 15:
                    horiz += 1
            elif kind == "re":
                r = it[1]
                w, h = abs(r[2] - r[0]), abs(r[3] - r[1])
                if (h < 2.5 and w > 15) or (w > 15 and h > 6):
                    rects += 1
            if horiz + rects >= 3:
                return True
    return False


def ruled_tables(page):
    """PyMuPDF find_tables (ruling-line strategy), with merged-cell spans recovered."""
    from .tables import spans_from_grid
    out = []
    if not _may_have_ruled_table(page):
        return out                      # find_tables costs ~0.5 s a page; without ruling lines it finds nothing
    try:
        found = page.find_tables()
    except Exception:
        return out
    for t in found.tables:
        try:
            data = t.extract()
        except Exception:
            continue
        rows = [[normalize_ligatures((c or "").strip()) for c in r] for r in data]
        keep = [i for i, r in enumerate(rows) if any(r)]
        rows = [rows[i] for i in keep]
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        cells_n = sum(len(r) for r in rows)
        filled = sum(1 for r in rows for c in r if c)
        if filled / cells_n < 0.3:
            continue
        cells = None
        try:
            grid = [list(t.rows[i].cells) for i in keep]
            full = [r for r in grid if r and all(c for c in r)]
            if full:   # column boundaries from a row with every cell present (stray edges would add fake columns)
                xs = sorted({round(c[0], 1) for c in full[0]} | {round(full[0][-1][2], 1)})
            else:
                xs = sorted({round(v, 1) for r in grid for c in r if c for v in (c[0], c[2])})
            ys = sorted({round(v, 1) for r in grid for c in r if c for v in (c[1], c[3])})
            cells = spans_from_grid(grid, xs, ys, rows)
            if not any(c["rs"] > 1 or c["cs"] > 1 for c in cells):
                cells = [dict(r=i, c=j, rs=1, cs=1, text=x) for i, r in enumerate(rows) for j, x in enumerate(r)]
        except Exception:
            cells = None
        try:
            colx = sorted({round(c[0], 1) for r in grid for c in r if c} | {round(max(c[2] for r in grid for c in r if c), 1)})
        except Exception:
            colx = None
        out.append(dict(bbox=tuple(t.bbox), rows=rows, cells=cells, n_rows=len(rows), n_cols=max(len(r) for r in rows),
                        filled=filled / cells_n, header_rows=1, evidence="ruling lines (find_tables)", col_x=colx))
    return out


def extend_header_row(table, words_, line_h):
    """Booktabs-style tables often have their header row outside the detected ruling grid. Add the
    text row immediately above the table when its words fall into >= 2 of the table's columns."""
    colx = table.get("col_x")
    if not colx or len(colx) < 3:
        return None
    x0, y0, x1, y1 = table["bbox"]

    def bucket(band):
        cols = [[] for _ in range(len(colx) - 1)]
        for w in sorted(band, key=lambda w: w[0]):
            cx = (w[0] + w[2]) / 2
            k = max(0, min(len(cols) - 1, sum(1 for b in colx[1:-1] if cx >= b)))
            cols[k].append(w[4])
        return cols

    # Look at up to three text lines above the rules. A line whose words all fall in ONE column
    # (a units note such as "(in millions)") is skipped; the first line spanning >= 2 columns is
    # taken as the header. Anything else stops the search: no header is invented.
    above = sorted({round(w[3], 1) for w in words_ if w[3] <= y0 + 1 and w[0] >= x0 - 3 and w[2] <= x1 + 3}, reverse=True)
    cols, band, ref = None, None, y0
    for yb in above[:3]:
        if ref - yb > 1.4 * line_h:
            break
        same = [w for w in words_ if abs(round(w[3], 1) - yb) < 0.6]
        cand = [w for w in same if w[0] >= x0 - 3 and w[2] <= x1 + 3]
        over = max(0.0, x0 - min(w[0] for w in same)) + max(0.0, max(w[2] for w in same) - x1) if same else 0.0
        if over > 0.25 * (x1 - x0):
            break                     # the line runs well past the table's edges: prose above the table, not a header
        c = bucket(cand)
        if sum(1 for x in c if x) >= 2 and len(cand) >= 2:
            cols, band = c, cand
            break
        if not cand or sum(1 for x in c if x) != 1:
            break
        ref = min(w[1] for w in cand)
    if cols is None:
        return None
    hdr = [" ".join(c) for c in cols]
    n = table["n_cols"]
    hdr = (hdr + [""] * n)[:n] if len(hdr) != n else hdr
    table["rows"] = [hdr] + table["rows"]
    if table.get("cells"):
        table["cells"] = [dict(c, r=c["r"] + 1) for c in table["cells"]] + [dict(r=0, c=j, rs=1, cs=1, text=t) for j, t in enumerate(hdr)]
    table["n_rows"] = len(table["rows"])
    top = min(w[1] for w in band)
    table["bbox"] = (x0, top, x1, y1)
    table["evidence"] += "; header row recovered from aligned text above the rules"
    return (x0, top, x1, y0)


def figures(page, avoid):
    """Raster images and clusters of vector drawings large enough to be figures."""
    W, H = page.rect.width, page.rect.height
    rects = [(tuple(i["bbox"]), "image") for i in page.get_image_info()]
    try:
        rects += [(tuple(r), "vector") for r in page.cluster_drawings()]
    except Exception:
        pass
    rects = [(r, k) for r, k in rects if (r[2] - r[0]) >= 3 and (r[3] - r[1]) >= 3 and not any(overlap_frac(r, a) > 0.5 for a in avoid)]
    g, merged = 12, True
    while merged:
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                (a, ka), (b, kb) = rects[i], rects[j]
                if a[0] - g < b[2] and b[0] - g < a[2] and a[1] - g < b[3] and b[1] - g < a[3]:
                    rects[i] = ((min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])), "image" if "image" in (ka, kb) else "vector")
                    del rects[j]
                    merged = True
                    break
            if merged:
                break
    out = []
    for r, k in rects:
        r = (max(0, r[0]), max(0, r[1]), min(W, r[2]), min(H, r[3]))
        w, h = r[2] - r[0], r[3] - r[1]
        if w >= 30 and h >= 20 and 0.01 * W * H <= w * h <= 0.97 * W * H:
            out.append(dict(bbox=r, kind=k))
    return out


def body_font_size(lines_):
    sizes = []
    for l in lines_:
        for s in l["spans"]:
            n = len(s["text"].strip())
            if n:
                sizes += [round(s["size"], 1)] * min(n, 200)
    return statistics.median(sizes) if sizes else 10.0


def _line_size(l):
    best, n = 0, 0
    for s in l["spans"]:
        k = len(s["text"].strip())
        if k > n:
            best, n = s["size"], k
    return best or l["spans"][0]["size"]


def _bold(l):
    tot = sum(len(s["text"].strip()) for s in l["spans"]) or 1
    b = sum(len(s["text"].strip()) for s in l["spans"] if (s["flags"] & 16) or re.search(r"bold|black|heavy|semibold", s["font"], re.I))
    return b / tot > 0.6


def group_blocks(lines_):
    """Group lines by PyMuPDF block number into block dicts."""
    by = {}
    for l in lines_:
        by.setdefault(l["block"], []).append(l)
    groups = []
    split = []
    for bi, ls in by.items():
        # a producer block that spans two columns (lines side by side with a wide gutter) -> split
        iv = sorted((l["bbox"][0], l["bbox"][2]) for l in ls)
        cl = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= cl[-1][1] + 2.0 * max(_line_size(l) for l in ls):
                cl[-1][1] = max(cl[-1][1], b)
            else:
                cl.append([a, b])
        if len(cl) > 1:
            for a, b in cl:
                split.append([l for l in ls if a <= l["bbox"][0] and l["bbox"][2] <= b])
        else:
            split.append(ls)
    for ls in split:
        if not ls:
            continue
        ls.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
        # split a producer block where the visual paragraph clearly changes: a large vertical gap or
        # a font-size jump (e.g. a footer line glued to the last paragraph)
        cur = [ls[0]]
        for prev, l in zip(ls, ls[1:]):
            sp, sl = _line_size(prev), _line_size(l)
            gap = l["bbox"][1] - prev["bbox"][3]
            if gap > 0.9 * max(sp, sl) or abs(sp - sl) > 0.2 * max(sp, sl):
                groups.append(cur)
                cur = [l]
            else:
                cur.append(l)
        groups.append(cur)
    blocks = []
    for ls in groups:
        x0 = min(l["bbox"][0] for l in ls)
        y0 = min(l["bbox"][1] for l in ls)
        x1 = max(l["bbox"][2] for l in ls)
        y1 = max(l["bbox"][3] for l in ls)
        sizes = []
        for l in ls:
            for sp in l["spans"]:
                sizes += [round(sp["size"], 2)] * sum(1 for ch in sp["text"] if ch.isalnum())
        sizes = sizes or [_line_size(l) for l in ls]
        blocks.append(dict(bbox=(x0, y0, x1, y1), lines=ls, size=statistics.median(sizes), bold=all(_bold(l) for l in ls),
                           text="\n".join(l["text"] for l in ls), n_lines=len(ls),
                           rotated=sum(1 for l in ls if abs(l["dir"][1]) > 0.5) > len(ls) / 2))
    return blocks


def _ends_sentence(t):
    return bool(re.search(r"[.!?:;\"'”’)\]]\s*$", t or ""))


def merge_paragraphs(blocks, body):
    """Merge consecutive same-column blocks that are really one paragraph split into line-level
    blocks (common for scans with an OCR text layer and for some generators). Input must already
    be in reading order."""
    out = []
    for b in blocks:
        if out:
            p = out[-1]
            px0, py0, px1, py1 = p["bbox"]
            x0, y0, x1, y1 = b["bbox"]
            lh = max(p["size"], b["size"]) * 1.25
            same_col = min(px1, x1) - max(px0, x0) > 0.5 * min(px1 - px0, x1 - x0)
            gap = y0 - py1
            similar = abs(p["size"] - b["size"]) <= 0.15 * max(p["size"], b["size"])
            heading_like = (p["size"] >= 1.15 * body) or (p["bold"] and p["n_lines"] <= 2 and not _ends_sentence(p["text"]))
            b_heading = (b["size"] >= 1.15 * body) or (b["bold"] and b["n_lines"] <= 2)
            if (same_col and similar and -0.5 * lh <= gap <= 0.45 * lh and not heading_like and not b_heading
                    and p.get("role", "body") == "body" and b.get("role", "body") == "body"
                    and p.get("kind", "text") == "text" and b.get("kind", "text") == "text"
                    and not BULLET.match(b["lines"][0]["text"]) and not CAPTION.match(b["lines"][0]["text"])):
                if p.get("line_texts") or b.get("line_texts"):
                    p["line_texts"] = (p.get("line_texts") or [l["text"] for l in p["lines"]]) + (b.get("line_texts") or [l["text"] for l in b["lines"]])
                    p["has_math"] = True
                p["lines"] = p["lines"] + b["lines"]
                p["bbox"] = (min(px0, x0), min(py0, y0), max(px1, x1), max(py1, y1))
                p["text"] = p["text"] + "\n" + b["text"]
                p["n_lines"] += b["n_lines"]
                p["merged"] = p.get("merged", 1) + 1
                continue
        out.append(b)
    return out


def classify(block, body, vocab=None, line_texts=None):
    """Assign (type, content, heading_level) to a text block. line_texts lets the caller supply
    math-marked line text instead of the plain text-layer string."""
    ls = line_texts or [l["text"] for l in block["lines"]]
    first = ls[0] if ls else ""
    text = join_lines(ls, vocab)
    n = len(ls)
    bullets = sum(1 for t in ls if BULLET.match(t))
    if (n >= 2 and bullets >= max(2, n // 2)) or (n == 1 and re.match(r"^(•|●|▪|◦)\s", first)):
        items, cur = [], []
        for t in ls:
            if BULLET.match(t) and cur:
                items.append(join_lines(cur, vocab))
                cur = [t]
            else:
                cur.append(t)
        if cur:
            items.append(join_lines(cur, vocab))
        return "list", "\n".join(items), None
    if CAPTION.match(first) and len(text) < 600:
        return "caption", text, None
    size = block["size"]
    short = len(text) < 150 and n <= 2
    if short and size >= 1.15 * body and not _ends_sentence(text.rstrip(")")) and text.count("$") < 2:
        lvl = 1 if size >= 1.6 * body else 2 if size >= 1.3 * body else 3
        return "heading", re.sub(r"\s+", " ", text), lvl
    if short and block["bold"] and len(text) < 120 and n <= 2 and not _ends_sentence(text) and size >= 0.95 * body:
        return "heading", re.sub(r"\s+", " ", text), 3
    return "paragraph", text, None
