"""Running header / footer / page-number detection.

Blocks are never deleted. They receive a `role` and `role_evidence`; the Markdown renderer leaves
page_header / page_footer / page_number out of the body (configurable) while the JSON keeps them.

Evidence used (single page): position in the top/bottom margin zone, isolation from the body by a
whitespace gap, length, font size relative to body text, capitalisation, page-number patterns and
typical running-header vocabulary (journal, volume, doi, (c), URLs ...).
Evidence used (multi-page): the same normalised text (digits masked) recurring at a similar vertical
position on several pages, or numeric edge blocks that increase from page to page.

Deliberately protected: headings set larger than body text, long footnotes (marker + sentence),
captions, equations and anything outside the margin zones.
"""
import re
from collections import defaultdict

ROMAN = r"(?=[ivxlcdm]+$)m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})"
PAGE_NUM = re.compile(
    r"^\s*(?:[-–—‒]\s*)?(?:page|pg\.?|p\.)?\s*(\d{1,4}|" + ROMAN + r")\s*(?:(?:of|/)\s*\d{1,4})?\s*(?:[-–—‒])?\s*$",
    re.I,
)
VOCAB = re.compile(
    r"(\bvol(\.|ume)?\b|\bissue\b|\bno\.\s*\d|\bdoi\b|\bissn\b|\bisbn\b|https?://|www\.|\.com\b|\.org\b|\.edu\b|"
    r"©|\(c\)|copyright|all rights reserved|\bjournal\b|\bproceedings\b|\barxiv:|downloaded from|published by|available (at|online)|retrieved from|printed in|"
    r"\bpp\.\s*\d|\bconfidential\b|\bpreprint\b|\bdraft\b|\bmanuscript\b|\bpage\s+\d+|licen[cs]e)",
    re.I,
)
FOOTNOTE_START = re.compile(r"^\s*(\d{1,2}|[*†‡§¶]|[a-z])[\s.)]")
CAPTION = re.compile(r"^\s*(fig(ure)?|table|tab\.|chart|plate|scheme)\s*[\dIVX]", re.I)

HEADER_ROLES = ("page_header", "page_footer", "page_number")


def mask(text):
    t = re.sub(r"\d+", "#", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _words(t):
    return len((t or "").split())


def classify_page(blocks, page_w, page_h, body_size=None, line_h=None, zone=0.085):
    """Assign roles on one page. blocks: dicts with bbox (x0,y0,x1,y1), text, size (font size or
    text height), n_lines and optionally rotated=True for vertical text. Only text-like blocks."""
    if not blocks:
        return
    sizes = sorted(b.get("size") or 0 for b in blocks if b.get("size"))
    body = body_size or (sizes[len(sizes) // 2] if sizes else 10.0)
    lh = line_h or 1.2 * body
    order = sorted(blocks, key=lambda b: b["bbox"][1])
    for b in order:
        b.setdefault("role", "body")
        x0, y0, x1, y1 = b["bbox"]
        text = (b.get("text") or "").strip()
        if not text:
            continue
        size = b.get("size") or body
        small = size <= 0.92 * body
        z = zone * 1.45 if small else zone  # small print may sit a little further from the edge
        # rotated text in a side margin ("Downloaded from ...", arXiv stamps, watermarks)
        if b.get("rotated") and (x1 <= page_w * 0.12 or x0 >= page_w * 0.88):
            b["role"], b["role_evidence"] = "page_header", "rotated text in side margin"
            continue
        below = [o["bbox"][1] for o in order if o is not b and o["bbox"][1] >= y1 - 0.3 * lh]
        above = [o["bbox"][3] for o in order if o is not b and o["bbox"][3] <= y0 + 0.3 * lh]
        gap_below = (min(below) - y1) if below else 10 * lh
        gap_above = (y0 - max(above)) if above else 10 * lh
        lone_num = bool(PAGE_NUM.match(text))
        in_top = y1 <= page_h * z or (not above and (y0 <= page_h * 0.14 or (lone_num and gap_below >= 1.6 * lh)))
        in_bot = y0 >= page_h * (1 - z) or (not below and (y1 >= page_h * 0.86 or (lone_num and gap_above >= 1.6 * lh)))
        if not (in_top or in_bot):
            continue
        # isolation: whitespace to the nearest block on the body side that does not share this line
        gap = gap_below if in_top else gap_above
        isolated = gap >= 1.6 * lh
        words = _words(text)
        big = size >= 1.25 * body
        short = words <= 25 and (b.get("n_lines") or 1) <= 4
        alpha = sum(c.isalpha() for c in text)
        caps = alpha >= 4 and sum(c.isupper() for c in text) >= 0.7 * alpha
        num_edge = bool(re.search(r"(\s|^)\d{1,4}\s*$|^\s*\d{1,4}(\s|$)|^\s*[ivxlc]{1,6}\s|\s[ivxlc]{1,6}\s*$", text, re.I))
        role, why = None, None
        if PAGE_NUM.match(text):
            role, why = "page_number", "page-number pattern in margin zone"
        elif CAPTION.match(text):
            continue
        elif in_bot and FOOTNOTE_START.match(text) and words >= 8 and not VOCAB.search(text[:60]):
            b["role"], b["role_evidence"] = "footnote", "marker + sentence at page bottom"
            continue
        elif big and not (VOCAB.search(text) or (in_top and caps and re.search(r"^\s*(\d{2,4}|[ivxlc]{2,6})\s|\s(\d{2,4}|[ivxlc]{2,6})\.?\s*$", text, re.I))):
            continue  # titles / headings are body content (unless a book running head with a page number)
        elif short and VOCAB.search(text):
            why = "running-header vocabulary"
        elif in_top and short and num_edge and (caps or small or isolated):
            why = "running head carrying a page number"
        elif short and small and isolated and not (words >= 8 and re.search(r"[a-z][.!?]\s*$", text)):
            # a complete sentence is body text; size alone is weak evidence (OCR size estimates vary)
            why = "small isolated text in margin zone"
        elif short and caps and isolated and in_top:
            why = "isolated all-caps running head"
        elif short and isolated and (y1 <= page_h * 0.06 or y0 >= page_h * 0.95):
            why = "isolated short text at extreme page edge"
        elif num_edge and isolated and short:
            why = "short isolated line carrying a page number"
        if why:
            b["role"] = role or ("page_header" if in_top else "page_footer")
            b["role_evidence"] = why
    # pieces of a running head/foot that share a line with an already identified one
    for b in order:
        if b.get("role", "body") != "body" or not (b.get("text") or "").strip():
            continue
        x0, y0, x1, y1 = b["bbox"]
        if not (y1 <= page_h * 0.16 or y0 >= page_h * 0.84) or _words(b.get("text")) > 12 or (b.get("n_lines") or 1) > 2:
            continue
        for o in order:
            if o is b or o.get("role") not in HEADER_ROLES:
                continue
            if min(y1, o["bbox"][3]) - max(y0, o["bbox"][1]) > 0.4 * min(y1 - y0, o["bbox"][3] - o["bbox"][1]):
                b["role"] = "page_header" if y1 <= page_h * 0.5 else "page_footer"
                b["role_evidence"] = "shares a line with a running header/footer element"
                break


def classify_document(pages):
    """Cross-page pass. pages: list of (page_h, blocks). Upgrades repeated edge text to header/footer
    and protects nothing it did not see repeated."""
    n = len(pages)
    if n < 2:
        return
    seen = defaultdict(list)
    for pi, (ph, blocks) in enumerate(pages):
        for b in blocks:
            y0, y1 = b["bbox"][1], b["bbox"][3]
            if y1 <= ph * 0.16 or y0 >= ph * 0.84:
                k = mask(b.get("text"))
                if k and len(k) >= 1:
                    seen[(k, "top" if y1 <= ph * 0.16 else "bot")].append((pi, (y0 + y1) / 2 / ph, b))
    need = 2 if n <= 4 else max(3, int(0.3 * n))
    for (k, where), occ in seen.items():
        pages_with = {pi for pi, _, _ in occ}
        if len(pages_with) < need or len(k.split()) > 15:
            continue
        ys = sorted(y for _, y, _ in occ)
        if ys[-1] - ys[0] > 0.05:
            continue
        for _, _, w in occ:
            b = w.get("ref", w)  # callers may pass light wrappers that point at the real block
            if b.get("role") in HEADER_ROLES:
                continue
            b["role"] = "page_number" if PAGE_NUM.match(w.get("text") or "") else ("page_header" if where == "top" else "page_footer")
            b["role_evidence"] = f"repeats at the same position on {len(pages_with)} of {n} pages"
