"""Canonical Digital Twin schema helpers. Every processor emits blocks through make_block()."""
OK, REVIEW = 0.85, 0.60

def verdict(conf):
    return "verified" if conf >= OK else "review" if conf >= REVIEW else "failed"

def make_block(bid, type_, page, bbox, conf, ctype, extractor, content=None, **extra):
    b = dict(block_id=bid, type=type_, content=content, page=page,
             bbox=[int(round(v)) for v in bbox], reading_order=0,
             confidence=round(float(conf), 2), confidence_type=ctype, extractor=extractor,
             verification="review", relationships=[], warnings=[])
    b.update({k: v for k, v in extra.items() if v is not None})
    return b

def area(r):  # r = (x0,y0,x1,y1)
    return max(0, r[2]-r[0]) * max(0, r[3]-r[1])

def inter(a, b):
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))

def overlap_frac(a, b):
    """fraction of a covered by b"""
    return inter(a, b) / area(a) if area(a) else 0
