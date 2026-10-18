"""Reading-order reconstruction with a recursive XY-cut variant.

1. If the region has a clean vertical gutter (no item crosses it) and both sides hold real content,
   it is split into columns, read left column first.
2. Otherwise the region is cut into horizontal bands at whitespace gaps. Consecutive bands that do
   not contain a wide (spanning) element are merged again, so an accidental alignment of paragraph
   gaps across two columns does not interleave the columns. Wide elements (titles, wide figures,
   full-width paragraphs) therefore separate column groups, which are then read column by column.
3. Overlapping content with no usable gap falls back to top-to-bottom, left-to-right.
"""
import re


def _merge(iv):
    iv = sorted(iv)
    out = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _gaps(intervals, min_gap):
    if not intervals:
        return []
    m = _merge(intervals)
    return [(a[1], b[0]) for a, b in zip(m, m[1:]) if b[0] - a[1] >= min_gap]


def _bb(it):
    return it["bbox"]


def _split_columns(items, min_col_gap):
    xs = [(_bb(i)[0], _bb(i)[2]) for i in items]
    vg = _gaps(xs, min_col_gap)
    if not vg:
        return None
    y_lo = min(_bb(i)[1] for i in items)
    y_hi = max(_bb(i)[3] for i in items)
    # Try gutters from widest to narrowest; accept the first with substantial content on both sides.
    for g in sorted(vg, key=lambda g: g[0] - g[1]):
        cut = (g[0] + g[1]) / 2
        left = [i for i in items if (_bb(i)[0] + _bb(i)[2]) / 2 < cut]
        right = [i for i in items if (_bb(i)[0] + _bb(i)[2]) / 2 >= cut]
        if not left or not right:
            continue
        lh = sum(_bb(i)[3] - _bb(i)[1] for i in left)
        rh = sum(_bb(i)[3] - _bb(i)[1] for i in right)
        la = sum((_bb(i)[2] - _bb(i)[0]) * (_bb(i)[3] - _bb(i)[1]) for i in left)
        ra = sum((_bb(i)[2] - _bb(i)[0]) * (_bb(i)[3] - _bb(i)[1]) for i in right)
        # Both sides must hold comparable content; a column of equation numbers or margin marks
        # beside the text is not a second column.
        if min(la, ra) < 0.08 * max(la, ra, 1.0):
            continue
        # columns run side by side: most items of the smaller side must have a vertically
        # overlapping neighbour on the other side (otherwise these are just staggered lines)
        small, other = (left, right) if len(left) <= len(right) else (right, left)
        paired = sum(1 for i in small if any(min(_bb(i)[3], _bb(o)[3]) - max(_bb(i)[1], _bb(o)[1]) > 0 for o in other))
        if paired < 0.5 * len(small):
            continue
        if min(lh, rh) >= 0.12 * max(1.0, y_hi - y_lo) or (len(left) >= 2 and len(right) >= 2):
            return left, right
    return None


def _lead_num(it):
    t = ((it.get("ref") or {}).get("text") or "") if isinstance(it.get("ref"), dict) else ""
    m = re.match(r"\s*(\d{1,3})[.)]?\s", t)
    return int(m.group(1)) if m else None


def _numbered_down_columns(left, right):
    """Numbered sections that run down the left column and continue in the right one (1, 2 | 3, 4)
    are read column by column even when the page looks like a grid of cells."""
    ln = [n for n in (_lead_num(i) for i in sorted(left, key=lambda i: _bb(i)[1])) if n is not None]
    rn = [n for n in (_lead_num(i) for i in sorted(right, key=lambda i: _bb(i)[1])) if n is not None]
    return len(ln) >= 2 and len(rn) >= 1 and ln == sorted(ln) and rn == sorted(rn) and rn[0] > ln[-1]


def xy_cut(items, min_col_gap=8.0, min_row_gap=4.0, depth=0):
    """Order items (dicts with 'bbox' = x0,y0,x1,y1). Returns (ordered_items, used_column_split)."""
    items = list(items)
    if len(items) <= 1 or depth > 60:
        return items, False
    cols = _split_columns(items, min_col_gap)
    if cols:
        # Grid of independent cells (cards, side-by-side panels): a wide horizontal gap that cuts
        # through BOTH columns at the same height means rows should be read first.
        strong = _gaps([(_bb(i)[1], _bb(i)[3]) for i in items], 4.4 * min_row_gap)
        if strong:
            cut = (strong[0][0] + strong[0][1]) / 2
            top = [i for i in items if (_bb(i)[1] + _bb(i)[3]) / 2 < cut]
            bot = [i for i in items if (_bb(i)[1] + _bb(i)[3]) / 2 >= cut]
            l_ids = {id(i) for i in cols[0]}
            def two_sided(g):
                return any(id(i) in l_ids for i in g) and any(id(i) not in l_ids for i in g)
            if top and bot and two_sided(top) and two_sided(bot) and not _numbered_down_columns(cols[0], cols[1]):
                a, c1 = xy_cut(top, min_col_gap, min_row_gap, depth + 1)
                b, c2 = xy_cut(bot, min_col_gap, min_row_gap, depth + 1)
                return a + b, c1 or c2
        a, _ = xy_cut(cols[0], min_col_gap, min_row_gap, depth + 1)
        b, _ = xy_cut(cols[1], min_col_gap, min_row_gap, depth + 1)
        return a + b, True

    ys = [(_bb(i)[1], _bb(i)[3]) for i in items]
    hg = _gaps(ys, min_row_gap)
    if hg:
        cuts = sorted((a + b) / 2 for a, b in hg)
        bands, cur, ci = [], [], 0
        for it in sorted(items, key=lambda i: (_bb(i)[1] + _bb(i)[3]) / 2):
            mid = (_bb(it)[1] + _bb(it)[3]) / 2
            while ci < len(cuts) and mid > cuts[ci]:
                if cur:
                    bands.append(cur)
                cur, ci = [], ci + 1
            cur.append(it)
        if cur:
            bands.append(cur)
        x_lo = min(_bb(i)[0] for i in items)
        x_hi = max(_bb(i)[2] for i in items)
        W = max(1.0, x_hi - x_lo)

        def wide(band):
            return any(_bb(i)[2] - _bb(i)[0] >= 0.6 * W for i in band)

        groups = []
        for band in bands:
            if groups and not wide(band) and not groups[-1][0]:
                groups[-1][1].extend(band)
            else:
                groups.append([wide(band), list(band)])
        if len(groups) > 1:
            out, used = [], False
            for _, g in groups:
                o, c = xy_cut(g, min_col_gap, min_row_gap, depth + 1)
                out += o
                used = used or c
            return out, used
    return sorted(items, key=lambda i: (round(_bb(i)[1] / 3), _bb(i)[0])), False


def reading_order(items, line_height=10.0):
    """Public entry point. Gap thresholds scale with the typical line height of the page."""
    lh = max(4.0, float(line_height))
    return xy_cut(items, min_col_gap=max(5.0, 0.8 * lh), min_row_gap=max(2.0, 0.5 * lh))
