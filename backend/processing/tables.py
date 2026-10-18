"""Table structure: detection of borderless tables from word geometry, span recovery for ruled
tables, and rendering (Markdown for simple grids, HTML when cells are merged).

A table is represented as
    rows  : list[list[str]]                 (rectangular, kept for frontend compatibility)
    cells : list[dict(r, c, rs, cs, text)]  (only the anchor cell of a merged region)
"""
import html
import re
import statistics

# --------------------------------------------------------------------------- rendering


def _md_cell(s):
    s = (s or "").replace("\r", "")
    s = s.replace("|", "\\|")
    s = re.sub(r"\s*\n\s*", "<br>", s.strip())
    return s


def has_spans(table):
    return any(c.get("rs", 1) > 1 or c.get("cs", 1) > 1 for c in table.get("cells") or [])


def to_markdown(rows):
    rows = [r for r in rows if r is not None]
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    rows = [list(r) + [""] * (n - len(r)) for r in rows]
    out = ["| " + " | ".join(_md_cell(c) for c in rows[0]) + " |", "|" + "|".join(["---"] * n) + "|"]
    for r in rows[1:]:
        out.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
    return "\n".join(out)


def to_html(table):
    cells = table.get("cells")
    rows = table.get("rows") or []
    if not cells:
        cells = [dict(r=i, c=j, rs=1, cs=1, text=t) for i, r in enumerate(rows) for j, t in enumerate(r)]
    header_rows = table.get("header_rows", 1)
    by_row = {}
    for c in cells:
        by_row.setdefault(c["r"], []).append(c)
    n_rows = table.get("n_rows") or (max(by_row) + 1 if by_row else 0)
    out = ["<table>"]
    for r in range(n_rows):
        tag = "th" if r < header_rows else "td"
        tds = []
        for c in sorted(by_row.get(r, []), key=lambda c: c["c"]):
            attrs = ""
            if c.get("rs", 1) > 1:
                attrs += f' rowspan="{c["rs"]}"'
            if c.get("cs", 1) > 1:
                attrs += f' colspan="{c["cs"]}"'
            txt = html.escape(c.get("text") or "", quote=False).replace("\n", "<br>")
            tds.append(f"<{tag}{attrs}>{txt}</{tag}>")
        out.append("<tr>" + "".join(tds) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def render(table):
    """Markdown table when the grid is simple, HTML when spans must be preserved."""
    return to_html(table) if has_spans(table) else to_markdown(table.get("rows") or [])


# --------------------------------------------------------------------------- spans for ruled tables


def spans_from_grid(row_cells, col_x, row_y, texts):
    """Recover rowspan/colspan from a PyMuPDF-style grid where merged regions are represented by a
    bbox on the anchor cell and None on covered positions.
    row_cells: list[list[bbox|None]]; col_x: column boundaries; row_y: row boundaries; texts: list[list[str]]"""
    cells = []
    covered = set()
    for r, row in enumerate(row_cells):
        for c, bb in enumerate(row):
            if (r, c) in covered:
                continue
            if bb is None:      # an empty position nobody spans: keep it so later cells stay in their columns
                t = texts[r][c] if r < len(texts) and c < len(texts[r]) else ""
                cells.append(dict(r=r, c=c, rs=1, cs=1, text=(t or "").strip()))
                continue
            cs = sum(1 for k in range(c, len(col_x) - 1) if col_x[k] >= bb[0] - 1 and col_x[k + 1] <= bb[2] + 1) or 1
            rs = sum(1 for k in range(r, len(row_y) - 1) if row_y[k] >= bb[1] - 1 and row_y[k + 1] <= bb[3] + 1) or 1
            # a span only covers grid positions that have no cell of their own
            k = 1
            while k < cs and c + k < len(row) and row[c + k] is None:
                k += 1
            cs = k
            k = 1
            while k < rs and r + k < len(row_cells) and c < len(row_cells[r + k]) and row_cells[r + k][c] is None:
                k += 1
            rs = k
            for dr in range(rs):
                for dc in range(cs):
                    covered.add((r + dr, c + dc))
            t = texts[r][c] if r < len(texts) and c < len(texts[r]) else ""
            cells.append(dict(r=r, c=c, rs=rs, cs=cs, text=(t or "").strip()))
    return cells


# --------------------------------------------------------------------------- borderless detection


def _rows_from_words(words, tol=0.45):
    """Group word boxes (x0,y0,x1,y1,text) into visual rows by vertical overlap."""
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cy, h = (w[1] + w[3]) / 2, max(1.0, w[3] - w[1])
        if rows and abs(rows[-1]["cy"] - cy) <= tol * max(h, rows[-1]["h"]):
            r = rows[-1]
            r["words"].append(w)
            r["cy"] = (r["cy"] * (len(r["words"]) - 1) + cy) / len(r["words"])
            r["h"] = max(r["h"], h)
        else:
            rows.append(dict(cy=cy, h=h, words=[w]))
    for r in rows:
        r["words"].sort(key=lambda w: w[0])
        r["y0"] = min(w[1] for w in r["words"])
        r["y1"] = max(w[3] for w in r["words"])
    return rows


def _segments(row, gap):
    segs, cur = [], [row["words"][0]]
    for a, b in zip(row["words"], row["words"][1:]):
        if b[0] - a[2] > gap:
            segs.append(cur)
            cur = [b]
        else:
            cur.append(b)
    segs.append(cur)
    return [dict(x0=s[0][0], x1=s[-1][2], words=s, text=" ".join(w[4] for w in s)) for s in segs]


def _is_numeric(t):
    return bool(re.fullmatch(r"[\s$€£(+\-−–]*[\d.,]+%?[)\s]*[a-zA-Z%]{0,3}", t or ""))


def detect_borderless(words, page_w, avoid=(), min_rows=3):
    """Find aligned text grids without ruling lines. words: (x0,y0,x1,y1,text). Returns tables as
    dicts(bbox, rows, cells, evidence). Requires strong evidence: >=3 rows, >=2 columns that are
    consistently aligned, short cells (not prose), and either numbers or >=3 columns."""
    def outside(w):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        return not any(a[0] <= cx <= a[2] and a[1] <= cy <= a[3] for a in avoid)

    words = [w for w in words if (w[4] or "").strip() and outside(w)]
    if len(words) < 6:
        return []
    rows = _rows_from_words(words)
    hs = [r["h"] for r in rows]
    lh = statistics.median(hs) if hs else 10.0
    gap = max(1.1 * lh, 6.0)
    for r in rows:
        r["segs"] = _segments(r, gap)

    # candidate rows: >=2 short segments
    def cand(r):
        segs = r["segs"]
        if len(segs) < 2:
            return False
        mean_words = sum(len(s["words"]) for s in segs) / len(segs)
        return mean_words <= 5.0 and max(len(s["words"]) for s in segs) <= 12

    groups, cur = [], []
    for r in rows:
        if cand(r):
            if cur and r["y0"] - cur[-1]["y1"] > 2.2 * lh:
                groups.append(cur)
                cur = []
            cur.append(r)
        else:
            # a single-segment row can sit inside a table (wrapped cell / section label) if short
            if cur and len(r["segs"]) == 1 and len(r["words"]) <= 6 and r["y0"] - cur[-1]["y1"] <= 1.2 * lh:
                cur.append(r)
            else:
                if cur:
                    groups.append(cur)
                cur = []
    if cur:
        groups.append(cur)

    tables = []
    for g in groups:
        while g and len(g[-1]["segs"]) < 2:
            g = g[:-1]
        multi = [r for r in g if len(r["segs"]) >= 2]
        if len(multi) < min_rows:
            continue
        # column boundaries = gaps in the union of segment x-ranges across multi-segment rows
        iv = sorted((s["x0"], s["x1"]) for r in multi for s in r["segs"])
        merged = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        ncol = len(merged)
        if ncol < 2 or ncol > 25:
            continue
        # alignment evidence: most multi rows should use >= 2 of the columns and segments must not
        # straddle column gaps (guaranteed by construction); count rows hitting >= 2 columns
        def col_of(x0, x1):
            cx = (x0 + x1) / 2
            for k, (a, b) in enumerate(merged):
                if a - 1 <= cx <= b + 1:
                    return k
            return min(range(ncol), key=lambda k: min(abs(cx - merged[k][0]), abs(cx - merged[k][1])))

        filled = [len({col_of(s["x0"], s["x1"]) for s in r["segs"]}) for r in multi]
        if sum(1 for f in filled if f >= 2) < min_rows:
            continue
        all_text = [s["text"] for r in multi for s in r["segs"]]
        numeric = sum(_is_numeric(t) for t in all_text) / max(1, len(all_text))
        if ncol < 3 and numeric < 0.25:
            continue
        # prose guard: a two-column text page produces 2 "cells" per row of ~equal long width
        widths = [b - a for a, b in merged]
        if ncol == 2 and min(widths) > 0.3 * page_w:
            continue
        grid = [[[] for _ in range(ncol)] for _ in g]
        for i, r in enumerate(g):
            for s in r["segs"]:
                grid[i][col_of(s["x0"], s["x1"])].append(s["text"])
        rows_txt = [[" ".join(c).strip() for c in row] for row in grid]
        x0 = min(s["x0"] for r in g for s in r["segs"])
        x1 = max(s["x1"] for r in g for s in r["segs"])
        bbox = (x0, g[0]["y0"], x1, g[-1]["y1"])
        cells = [dict(r=i, c=j, rs=1, cs=1, text=t) for i, row in enumerate(rows_txt) for j, t in enumerate(row)]
        tables.append(dict(bbox=bbox, rows=rows_txt, cells=cells, n_rows=len(rows_txt), n_cols=ncol,
                           evidence=f"{len(multi)} aligned rows x {ncol} columns, numeric cells {numeric:.0%}"))
    return tables


CURRENCY = {"$", "€", "£", "¥", "₹", "US$", "C$", "A$", "CHF"}


def merge_currency_columns(table):
    """Financial tables often typeset the currency sign in its own ruled column ("$ | 178").
    Fold a column whose non-empty cells are ONLY currency signs into the value to its right
    ("$178"), which is how the value reads on the page. Tables with merged cells are left as
    they are unless the only spans are exactly the sign/value split being undone. Returns the
    number of columns folded."""
    rows = table.get("rows") or []
    if not rows:
        return 0
    n = max(len(r) for r in rows)
    rows = [list(r) + [""] * (n - len(r)) for r in rows]
    drop = []
    for j in range(n - 1):
        if j in drop or (j - 1) in drop:
            continue
        a = [r[j].strip() for r in rows]
        b = [r[j + 1].strip() for r in rows]
        if not any(x in CURRENCY for x in a):
            continue
        # The pair must be ONE logical column split by the ruling grid: on every row the left cell
        # is empty, a currency sign, or a number whose right neighbour is empty (right-aligned
        # numbers spilling into the sign column). Any other content means two real columns.
        if not all((not x) or x in CURRENCY or (_is_numeric(x) and not y) for x, y in zip(a, b)):
            continue
        for r, x, y in zip(rows, a, b):
            r[j + 1] = (x + y) if x in CURRENCY else (x or y)
        drop.append(j)
    if not drop:
        return 0
    # Merged cells are only compatible when they are exactly the split being undone: a one-row
    # cell spanning a folded sign column and its value column. Any other span -> leave the table.
    spans = [c for c in table.get("cells") or [] if c.get("rs", 1) > 1 or c.get("cs", 1) > 1]
    if any(c.get("rs", 1) != 1 or c.get("cs", 1) != 2 or c.get("c") not in drop for c in spans):
        return 0
    rows = [[c for k, c in enumerate(r) if k not in drop] for r in rows]
    table["rows"] = rows
    table["n_cols"] = len(rows[0])
    if table.get("cells"):
        table["cells"] = [dict(r=i, c=k, rs=1, cs=1, text=x) for i, r in enumerate(rows) for k, x in enumerate(r)]
    if table.get("col_x"):
        cx = table["col_x"]
        # column j spans cx[j]..cx[j+1]; folding j into j+1 removes the boundary between them
        table["col_x"] = [x for k, x in enumerate(cx) if k - 1 not in drop]
    table["evidence"] = table.get("evidence", "") + f"; {len(drop)} currency-sign column(s) folded into the value column"
    return len(drop)
