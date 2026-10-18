"""Optional AI reading of visual regions (figures, screenshots, charts, diagrams, handwriting).
Server-side only; see processing/llm.py for configuration. Without a key, available() is False and
the pipeline keeps the local result (OCR text if Tesseract is installed) and flags the rest.

Two entry points:
  read_visual(img)  -> one structured reading of a figure/screenshot (all visible text, description)
  analyze_crop(img) -> list of typed regions (used for unexplained ink on scanned pages)
Model output is self-reported: it is never marked verified unless independent evidence agrees
(see verifier.py)."""
import json

from . import llm

VALID = {"handwritten_text", "handwritten_code", "equation", "diagram", "chart", "figure", "table", "paragraph", "heading", "list", "unknown"}
VISUAL_TYPES = {"screenshot", "chart", "diagram", "flowchart", "table", "photo", "drawing", "equation", "handwriting", "text_image", "figure"}

READ_PROMPT = """You are reading ONE image cropped from a document page (a figure, screenshot, chart, diagram, scan or photo).
Return JSON only, exactly this shape:
{"visual_type": "screenshot|chart|diagram|flowchart|table|photo|drawing|equation|handwriting|text_image|figure",
 "title": "the visual's own title if one is printed in it, else null",
 "text": "EVERY piece of readable text in the image, transcribed exactly, in natural reading order; one line per visual line; keep numbers, units, symbols and capitalisation; for UI screenshots include every label, menu item, field value and button text",
 "description": "2-4 plain sentences: what the visual shows and what it is for (e.g. which dialog and which settings are selected, what the chart compares and the trend)",
 "elements": ["for diagrams/flowcharts/UI: each node, component or control label"],
 "relationships": ["for diagrams/flowcharts: edges as 'A -> B' when arrows/lines show them"],
 "chart": {"chart_type": null, "axes": null, "legend": null, "data": null},
 "rows": null,
 "latex": null,
 "handwritten": false,
 "illegible": false,
 "confidence": 0.0}
"handwritten" is true when the text is written by hand (pen, stylus or tablet notes), even if the image also has drawings.
For handwritten notes transcribe every word, arrow label and number as written, keep the layout's line order, and write math as LaTeX inside $...$; for drawn trees/graphs list each node value and its children in "relationships" (e.g. "30 -> 20 (left)").
Rules: transcribe only what is visible - never guess or complete words you cannot read (write [illegible] for them and set "illegible": true);
"text" is "" for a photo with no text; chart.data only when values are printed in the image; rows (list of lists, header first) only for tables;
latex only for mathematical expressions; confidence = how sure you are the transcription is exact (0-1). No prose outside the JSON."""

REGIONS_PROMPT = """You are a document-extraction engine. The image is a crop from one page of a document.
Identify each distinct region and answer with JSON only:
{"items":[{"type":"handwritten_text|handwritten_code|equation|diagram|chart|figure|table|paragraph|heading|list|unknown",
"content":"exact transcription or null","description":"one-sentence description for visuals or null","latex":"LaTeX for equations or null",
"rows":[["table cells, header row first"]] or null,"elements":["every node/label exactly as written"],"relationships":["every edge e.g. A -> B"],
"chart":{"title":null,"chart_type":null,"axes":null,"legend":null,"data":null},
"bbox":[x1,y1,x2,y2],"confidence":0.0}]}
bbox is normalised 0-1000 relative to this image. Rules: transcribe exactly what is written; never guess illegible text (use content null and confidence below 0.5);
keep code formatting; tables must be returned as type table with rows; never invent chart values; no prose outside the JSON."""

PROMPT = REGIONS_PROMPT  # backwards compatibility


def _cfg():
    return llm.config("vision")


def provider():
    return _cfg()["provider"]


def key():
    return _cfg()["_key"]


def available():
    return _cfg()["configured"]


def model():
    return llm.public(_cfg())["model"]


def info():
    return llm.public(_cfg())


def read_visual(img, timeout=90):
    """Returns (result_dict, error). Never raises."""
    cfg = _cfg()
    if not cfg["configured"]:
        return None, "no AI key configured"
    try:
        txt = llm.generate(cfg, READ_PROMPT, images=[img], json_mode=True, max_tokens=3000, timeout=timeout, temperature=0)
        d = json.loads(llm.strip_json_fence(txt))
        if not isinstance(d, dict):
            raise ValueError
    except llm.AIError as e:
        return None, str(e)
    except Exception as e:
        return None, f"AI response unusable ({type(e).__name__})"
    vt = str(d.get("visual_type") or "figure").lower()
    d["visual_type"] = vt if vt in VISUAL_TYPES else "figure"
    for k in ("text", "description", "title"):
        v = d.get(k)
        d[k] = v.strip() if isinstance(v, str) and v.strip() else None
    for k in ("elements", "relationships"):
        v = d.get(k)
        d[k] = [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    try:
        d["confidence"] = max(0.0, min(0.95, float(d.get("confidence", 0.6))))
    except Exception:
        d["confidence"] = 0.6
    rows = d.get("rows")
    d["rows"] = [[str(c or "") for c in r] for r in rows if isinstance(r, list)] if isinstance(rows, list) and len(rows) >= 2 else None
    d["latex"] = d["latex"].strip() if isinstance(d.get("latex"), str) and d["latex"].strip() else None
    d["handwritten"] = bool(d.get("handwritten")) or d["visual_type"] == "handwriting"
    ch = d.get("chart") if isinstance(d.get("chart"), dict) else {}
    d["chart"] = {k: ch.get(k) for k in ("chart_type", "axes", "legend", "data") if ch.get(k) not in (None, "", [], {})} or None
    return d, None


def analyze_crop(img, timeout=60):
    """returns (items, error). Never raises."""
    cfg = _cfg()
    if not cfg["configured"]:
        return [], "VISION_API_KEY not configured"
    try:
        txt = llm.generate(cfg, REGIONS_PROMPT, images=[img], json_mode=True, max_tokens=4096, timeout=timeout, temperature=0, retries=1)
        items = json.loads(llm.strip_json_fence(txt)).get("items", [])
        return [i for i in items if isinstance(i, dict)], None
    except llm.AIError as e:
        msg = str(e)
        return [], ("vision API timeout" if "timed out" in msg else f"vision API error: {msg}")
    except Exception as e:
        return [], f"vision response unusable ({type(e).__name__})"
