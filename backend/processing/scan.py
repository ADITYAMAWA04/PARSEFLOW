"""Scanned-page preprocessing, applied selectively from measured image properties.

Each step records what it measured and whether it acted, so the page's `preprocessing` metadata
explains the transformation. The original rendered page image is never modified (the viewer and
the provenance crops use the untouched render).

  contrast  : stretch only when the 2-98 percentile range is narrow (faint / greyed scans)
  illumination: divide by a smoothed background only when the background is uneven
  deskew    : projection-profile search in [-5, 5] degrees; rotate only when |angle| >= 0.3
  orientation: Tesseract OSD, run only when the first OCR pass looks wrong (see ocr.ocr_page)
"""
import numpy as np
from PIL import Image
from scipy import ndimage as ndi


def _to_gray(img):
    return np.asarray(img.convert("L"), dtype=np.float32)


def estimate_skew(g, max_angle=5.0, step=0.2):
    """Return skew angle in degrees (positive = text rises to the right)."""
    h, w = g.shape
    f = max(1, int(w / 900))
    small = g[::f, ::f]
    thr = min(160.0, float(np.percentile(small, 50)) * 0.75)
    ys, xs = np.nonzero(small < thr)
    if len(ys) < 500:
        return 0.0
    if len(ys) > 120000:
        idx = np.random.default_rng(0).choice(len(ys), 120000, replace=False)
        ys, xs = ys[idx], xs[idx]
    best, best_a = -1.0, 0.0
    H = small.shape[0]
    for a in np.arange(-max_angle, max_angle + 1e-6, step):
        yy = ys - xs * np.tan(np.radians(a))
        hist = np.bincount(np.clip((yy + H).astype(np.int64), 0, 3 * H), minlength=3 * H + 1).astype(np.float64)
        score = float((hist ** 2).sum())
        if score > best:
            best, best_a = score, a
    return float(best_a)


def preprocess(img, deskew=True):
    """Return (processed PIL 'L' image, info dict)."""
    g = _to_gray(img)
    info = {}
    p2, p98 = np.percentile(g, 1), np.percentile(g, 99)
    info["contrast_range"] = round(float(p98 - p2), 1)
    # uneven illumination: background estimate on a downsampled image
    f = max(1, int(max(g.shape) / 600))
    small = g[::f, ::f]
    bg = ndi.grey_closing(small, size=(15, 15))
    bg = ndi.uniform_filter(bg, size=25)
    unevenness = float(np.std(bg))
    info["background_std"] = round(unevenness, 1)
    if unevenness > 18:
        big_bg = np.asarray(Image.fromarray(bg.astype(np.uint8)).resize((g.shape[1], g.shape[0]), Image.BILINEAR), dtype=np.float32)
        g = np.clip(g / np.maximum(big_bg, 1.0) * 255.0, 0, 255)
        info["illumination_corrected"] = True
        p2, p98 = np.percentile(g, 2), np.percentile(g, 98)
    if p98 - p2 < 150 and p98 > p2:
        g = np.clip((g - p2) * 255.0 / (p98 - p2), 0, 255)
        info["contrast_stretched"] = True
    out = Image.fromarray(g.astype(np.uint8), "L")
    if deskew:
        ang = estimate_skew(g)
        info["skew_deg"] = round(ang, 2)
        if abs(ang) >= 0.3:
            out = out.rotate(ang, resample=Image.BICUBIC, expand=False, fillcolor=255)
            info["deskewed"] = True
    return out, info
