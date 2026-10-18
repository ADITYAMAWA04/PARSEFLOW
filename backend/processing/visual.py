"""Text inside visuals (figures, charts, diagrams, screenshots) with local OCR only.

Why this module exists: Tesseract's layout analysis treats text that touches box outlines, arrows
or chart axes as part of a graphic and skips it (flowchart node labels are the typical loss). So:

1. OCR the crop in sparse-text mode (psm 11).
2. If the crop contains long straight strokes (box edges, axes, connectors), erase them and OCR
   again; merge the two passes, keeping the more confident reading where boxes overlap.
3. Correct one well-known OCR confusion only when the document itself supplies the evidence:
   a label such as "Ql" next to "Q2", "Q3" ... is read as "Q1". The original string is kept.
4. Order labels top-to-bottom, left-to-right, and describe the visual from measurable evidence
   (axis lines, numeric labels, prose lines). The description says it is a heuristic; no
   meaning is invented and a picture without text gets no text.
"""
import re

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage as ndi

from . import ocr

NUM = re.compile(r"^[\s$€£¥(+\-−–]*\d[\d.,]*\s*%?[)]?$")
UNIT = re.compile(r"^(?:[($]?(?:usd|eur|inr|gbp|\$|€|£|%|kg|g|mg|km|m|cm|mm|s|ms|sec|min|h|hz|khz|mhz|ghz|kb|mb|gb|tb|"
                   r"v|mv|a|ma|w|kw|°c|°f|k|bn|mn|millions?|billions?|thousands?|units?|count|pct|percent)[)]?\s*){1,3}$", re.I)
SEQ_FIX = {"l": "1", "I": "1", "|": "1", "O": "0", "o": "0"}


def strip_lines(gray):
    """Erase long horizontal / vertical strokes. Returns (image, has_horizontal, has_vertical)."""
    g = np.asarray(gray, dtype=np.uint8)
    ink = g < 160
    h, w = g.shape
    lh = ndi.binary_opening(ink, structure=np.ones((1, max(25, w // 25))))
    lv = ndi.binary_opening(ink, structure=np.ones((max(25, h // 12), 1)))
    has_h, has_v = bool(lh.any()), bool(lv.any())
    if not (has_h or has_v):
        return gray, False, False
    out = g.copy()
    out[ndi.binary_dilation(lh | lv, iterations=1)] = 255
    return Image.fromarray(out), has_h, has_v


def ruled_lines(gray):
    """Notebook / tablet-note ruling: >= 5 faint horizontal lines spanning most of the width at a
    near-constant spacing. Strong evidence that an image holds handwritten notes."""
    im = gray.convert("L") if hasattr(gray, "convert") else Image.fromarray(np.asarray(gray, dtype=np.uint8))
    if im.size[0] > 800:                       # rulings survive downscaling; keeps this cheap
        k = 800 / im.size[0]
        im = im.resize((800, max(1, round(im.size[1] * k))), Image.BILINEAR)
    g = np.asarray(im, dtype=np.uint8)
    h, w = g.shape
    if h < 60 or w < 120:
        return 0
    # a ruling row has faint ink across most of the width (text rows and blank rows do not)
    cover = (g < 238).mean(axis=1)
    rows = np.where(cover >= 0.6)[0]
    if len(rows) == 0:
        return 0
    groups, start, prev = [], rows[0], rows[0]
    for y in rows[1:]:
        if y > prev + 1:
            groups.append((start + prev) / 2)
            start = y
        prev = y
    groups.append((start + prev) / 2)
    if len(groups) < 5:
        return 0
    gaps = np.diff(groups)
    med = float(np.median(gaps))
    regular = np.mean(np.abs(gaps - med) <= 0.2 * med) if med > 4 else 0
    return len(groups) if regular >= 0.7 else 0


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if not inter:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return inter / small


def merge_passes(first, second):
    """Union of two OCR passes over the same crop; overlapping readings keep the more confident."""
    out = list(first)
    for d in second:
        hit = [i for i, e in enumerate(out) if _iou(e["bbox"], d["bbox"]) > 0.5]
        if not hit:
            out.append(d)
            continue
        if all(d["confidence"] > out[i]["confidence"] + 0.02 for i in hit) and len(hit) == 1:
            out[hit[0]] = d
    return out


def fix_sequence_labels(items):
    """'Ql' -> 'Q1' when sibling labels 'Q2', 'Q3' ... show the sequence. Returns count fixed."""
    texts = [d["text"] for d in items]
    fixed = 0
    for d in items:
        m = re.fullmatch(r"([A-Za-z]{1,4})([lI|Oo])", d["text"])
        if not m:
            continue
        prefix = m.group(1)
        sib = [t for t in texts if re.fullmatch(re.escape(prefix) + r"\d{1,2}", t)]
        if len(sib) >= 2:
            d["ocr_raw"] = d["text"]
            d["text"] = prefix + SEQ_FIX[m.group(2)]
            d["corrected"] = "OCR letter/digit confusion corrected from sibling labels " + ", ".join(sorted(sib)[:4])
            fixed += 1
    return fixed


def plausible(text):
    """A label worth reporting from a scanned drawing: a number, a word of >= 3 letters, or a
    short code with a digit (Q1, A4). Fragments like 'Lu' or 'ry)' from box corners are dropped."""
    t = text.strip()
    if NUM.match(t) or re.fullmatch(r"[A-Za-z]{1,3}\d{1,3}", t):
        return True
    letters = [c for c in t if c.isalpha()]
    if letters:
        # scribbles and zigzag strokes are read as runs of V / W / M / N / Z / X, or as one letter
        # repeated; such strings are evidence of drawing, not of text
        if all(c in "VWMNZXvwmnzx" for c in letters) or max(letters.count(c) for c in set(letters)) >= 0.6 * len(letters) >= 2:
            return False
    return bool(re.search(r"[A-Za-z]{3,}", t)) and sum(ch.isalnum() for ch in t) >= 0.6 * len(t.replace(" ", ""))


def reading_order(items):
    """Group labels into rows by vertical overlap, rows top-to-bottom, labels left-to-right."""
    rows = []
    for d in sorted(items, key=lambda d: (d["bbox"][1], d["bbox"][0])):
        y0, y1 = d["bbox"][1], d["bbox"][3]
        for r in rows:
            ry0, ry1 = r["y"]
            if min(y1, ry1) - max(y0, ry0) > 0.5 * min(y1 - y0, ry1 - ry0):
                r["items"].append(d)
                r["y"] = (min(y0, ry0), max(y1, ry1))
                break
        else:
            rows.append(dict(y=(y0, y1), items=[d]))
    out = []
    for i, r in enumerate(sorted(rows, key=lambda r: r["y"][0])):
        for d in sorted(r["items"], key=lambda d: d["bbox"][0]):
            d["row"] = i
            out.append(d)
    return out


def as_text(items):
    """Labels as text: one line per visual row, labels in a row separated by two spaces."""
    rows = {}
    for d in items:
        rows.setdefault(d.get("row", 0), []).append(d["text"])
    return "\n".join("  ".join(v) for _, v in sorted(rows.items())) or None


def classify(items, has_h, has_v, size=None, kind="image"):
    """Visual type from measurable evidence. Returns (visual_type, evidence, description)."""
    if not items:
        return ("picture", "no text found by OCR", "No readable text was found in this visual.")
    words = [d["text"] for d in items]
    nums = [t for t in words if NUM.match(t)]
    units = [t for t in words if UNIT.match(t)]
    prose = [t for t in words if len(t.split()) >= 5]
    labels = [t for t in words if t not in nums and t not in units]
    title = None
    if size and items:
        top = items[0]
        if len(top["text"].split()) >= 2 and not NUM.match(top["text"]) and top["bbox"][1] < 0.25 * size[1]:
            title = top["text"]
    if len(prose) >= 3 and len(prose) >= 0.5 * len(words):
        vt, ev = "text_image", f"{len(prose)} lines of running text"
    elif len(nums) >= 3 and has_h and has_v:
        vt, ev = "chart", f"horizontal and vertical axis-like lines and {len(nums)} numeric labels"
    elif (has_h or has_v) and len(labels) >= 2:
        vt, ev = "diagram", f"connector/box lines and {len(labels)} text labels"
    else:
        vt, ev = "figure", f"{len(words)} text labels"
    names = {"text_image": "Image containing text", "chart": "Likely a chart", "diagram": "Likely a diagram",
             "figure": "Figure"}
    parts = [f"{names[vt]} (heuristic: {ev})."]
    if title and vt in ("chart", "diagram", "figure"):
        parts.append(f"Title candidate: “{title}”.")
    lab = [t for t in labels if t != title]
    if lab and vt != "text_image":
        parts.append("Labels: " + ", ".join(lab[:12]) + ("…" if len(lab) > 12 else "") + ".")
    if nums and vt != "text_image":
        parts.append("Numeric labels: " + ", ".join(nums[:12]) + ("…" if len(nums) > 12 else "") + ".")
    if units:
        parts.append("Units: " + ", ".join(dict.fromkeys(units)) + ".")
    return vt, ev, " ".join(parts)


def image_text(gray, timeout=0, min_conf=0.5):
    """OCR a visual crop. Returns (items, info). items: dict(text, bbox, confidence, keep[, ocr_raw])
    in crop pixel coordinates, in reading order. info: has_h, has_v, passes, corrections."""
    gray = ImageOps.autocontrast(gray.convert("L"))
    # strip strokes at native resolution (upscaling first leaves anti-aliased stroke edges that
    # Tesseract reads as letters), then upscale small crops so label glyphs reach a readable height
    stripped, has_h, has_v = strip_lines(gray)
    k = 400 / min(gray.size) if min(gray.size) < 400 else 1.0

    def up(im):
        return im if k == 1.0 else im.resize((round(im.size[0] * k), round(im.size[1] * k)), Image.LANCZOS)

    first = ocr.region_text(up(gray), psm=11, min_conf=min_conf, timeout=timeout)
    passes = ["psm11"]
    items = first
    if has_h or has_v:
        second = ocr.region_text(up(stripped), psm=11, min_conf=min_conf, timeout=timeout)
        items = merge_passes(first, second)
        passes.append("psm11_lines_removed")
    if k != 1.0:
        for d in items:
            d["bbox"] = tuple(v / k for v in d["bbox"])
    fixed = fix_sequence_labels(items)
    items = reading_order(items)
    return items, dict(has_h=has_h, has_v=has_v, passes=passes, corrections=fixed, ruled=ruled_lines(gray))
