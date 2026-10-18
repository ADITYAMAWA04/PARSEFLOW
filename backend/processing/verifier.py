"""Evidence verification: EXTRACT -> VALIDATE -> ACCEPT or FLAG.

Vocabulary (documented in docs/VERIFICATION.md):
  verification = "verified"  an independent check of the content passed (see `evidence`)
               = "review"    no independent check was possible, or it only partly agreed
               = "failed"    no usable content, or a check contradicted it
  confidence_level = "high" (>= 0.85) | "uncertain" (0.60-0.85) | "low" (< 0.60)

A model's or OCR engine's own confidence never makes content "verified" by itself; correlated
signals (e.g. two reads of the same text layer) are named as such in the evidence string.
"""
import re

from .schema import OK, REVIEW
from . import ocr


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _toks(s):
    return re.findall(r"[a-z0-9]{2,}", (s or "").lower())


def token_recall(content, reference):
    t = _toks(content)
    if not t:
        return 0.0
    ref = set(_toks(reference))
    return sum(1 for x in t if x in ref) / len(t)


def level(conf):
    return "high" if conf >= OK else "uncertain" if conf >= REVIEW else "low"


def verify(b, page_info, crop=None):
    """page_info: width, height, norm_text (normalised page text layer), text_quality,
    clip_text(bbox)->str|None (independent re-read of the text layer inside a viewer-space bbox).
    Mutates and returns the block."""
    c, ex = float(b.get("confidence") or 0), b.get("extractor", "")
    checks = []
    x0, y0, x1, y1 = b["bbox"]
    v = "review"
    ev = "none"
    if x0 < -2 or y0 < -2 or x1 > page_info["width"] + 2 or y1 > page_info["height"] + 2 or x1 <= x0 or y1 <= y0:
        v, ev = "failed", "bounding box outside page bounds"
        b["warnings"].append("Bounding box outside page bounds.")
    elif ex == "digital_text_parser":
        if not b.get("content"):
            v, ev = "failed", "empty text block"
        else:
            reread = page_info.get("clip_text")(b["bbox"]) if page_info.get("clip_text") else None
            rec = token_recall(b["content"], reread) if reread is not None else None
            q = page_info.get("text_quality", 1.0)
            if rec is not None:
                checks.append(dict(check="text-layer re-read inside bbox", result=round(rec, 3)))
            checks.append(dict(check="text-layer character quality", result=q))
            if rec is not None and rec >= 0.9 and q >= 0.98:
                v, ev = "verified", f"independent re-read of the PDF text layer inside the bbox reproduces {rec:.0%} of tokens"
            elif rec is None and q >= 0.98:
                v, ev = "review", "text layer only; bbox re-read unavailable"
            else:
                v, ev = "review", f"bbox re-read matched {0 if rec is None else rec:.0%} of tokens; text-layer quality {q:.2f}"
            if q < 0.98:
                b["warnings"].append("PDF text layer contains unmapped glyphs; some characters may be wrong.")
    elif ex in ("table_engine", "borderless_table"):
        cells = [x for r in b.get("rows") or [] for x in r if x]
        found = sum(1 for x in cells if _norm(x) and _norm(x) in page_info["norm_text"]) / max(1, len(cells))
        checks.append(dict(check="table cells found in page text layer", result=round(found, 3)))
        if not page_info["norm_text"]:
            v, ev = "review", "no text layer to check cells against"
        elif found >= 0.9:
            v, ev = "verified", f"independent: {found:.0%} of cell strings occur in the page text layer (structure itself is inferred)"
        else:
            v, ev = "review", f"only {found:.0%} of cell strings occur in the page text layer"
            b["warnings"].append("Some table cells were not found in the page text layer.")
    elif ex == "ocr":
        if not b.get("content"):
            v, ev = "failed", "no legible text"
        else:
            ref = page_info.get("ocr_layer_text")
            if ref:
                rec = token_recall(b["content"], ref)
                checks.append(dict(check="agreement with embedded OCR text layer", result=round(rec, 3)))
                if rec >= 0.85 and c >= OK:
                    v, ev = "verified", f"independent: {rec:.0%} of tokens also present in the PDF's embedded OCR layer"
                else:
                    v, ev = "review", f"single OCR engine (mean word confidence {c:.2f}); embedded-layer agreement {rec:.0%}"
            else:
                v, ev = "review", f"single OCR engine; mean word confidence {c:.2f} (not independently verified)"
    elif ex == "text_layer_math":
        cov = b.get("glyph_coverage")
        checks.append(dict(check="text-layer glyphs represented in LaTeX", result=cov))
        v = "review"
        ev = (f"glyph identities come from the text layer ({cov:.0%} of glyphs represented); "
              "2-D structure (fractions, scripts) is inferred from geometry and not independently checked")
    elif ex == "figure_ocr":
        v, ev = "review", "OCR of text inside a figure; single engine"
    elif ex == "fallback":
        v, ev = "failed", "no extractor available for this region"
    elif ex == "vision_visual":
        agree = b.get("ocr_agreement")
        if not b.get("content") and not b.get("latex") and not b.get("rows"):
            v, ev = "review", "AI model reports no readable text in this image"
        elif agree is not None:
            checks.append(dict(check="local OCR / PDF-label words found in the AI transcription", result=agree))
            if agree >= 0.6 and c >= OK:
                v, ev = "verified", f"independent: {agree:.0%} of the locally read words appear in the AI transcription"
            else:
                v, ev = "review", f"AI transcription; only {agree:.0%} of the locally read words agree"
        else:
            v, ev = "review", "AI transcription, self-reported (no independent local reading to compare)"
    else:  # model-based (vision_*, math_vision)
        if not b.get("content") and not b.get("latex"):
            v, ev = "failed", "model returned no content"
        else:
            v, ev = "review", "model self-reported only"
            if b["type"] == "table" and b.get("rows"):
                cells = [x for r in b["rows"] for x in r if x]
                fr = sum(1 for x in cells if _norm(x) and _norm(x) in page_info["norm_text"]) / max(1, len(cells))
                checks.append(dict(check="model table cells found in text layer", result=round(fr, 3)))
                if fr >= 0.9 and c >= OK and page_info["norm_text"]:
                    v, ev = "verified", f"independent: model cells found in text layer {fr:.0%}"
            if b["type"] in ("handwritten_text", "handwritten_code", "paragraph", "heading", "list") and crop is not None:
                try:
                    oc, ot = ocr.quick_conf(crop(b["bbox"]))
                    t, a = _toks(b.get("content")), set(_toks(ot))
                    if t and a:
                        ov = sum(1 for x in t if x in a) / len(t)
                        checks.append(dict(check="independent OCR token overlap", result=round(ov, 3)))
                        ev = f"independent OCR token overlap {ov:.0%}"
                        if ov >= 0.5 and oc >= 0.6 and c >= OK:
                            v, ev = "verified", "independent: " + ev
                except Exception:
                    pass
    b["verification"], b["evidence"], b["evidence_checks"] = v, ev, checks
    b["confidence_level"] = level(c)
    # the status (verified / review / not read) and the evidence say the rest; no generic warnings
    return b
