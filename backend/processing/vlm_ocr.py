"""High-accuracy page OCR through a specialised document VLM (default: AllenAI olmOCR-2-7B), called
over any OpenAI-compatible endpoint: a local vLLM server on a GPU, or a hosted provider
(DeepInfra, Cirrascale, Parasail, ...). Optional: ParseFlow's own pipeline needs none of this.

Why: on olmOCR-Bench every system above 80 % is a vision model trained for document conversion
(olmOCR-2 82.4, Chandra, LightOnOCR-2, PaddleOCR-VL); general chat models score far lower and a
Tesseract pipeline lower still. This module reproduces AllenAI's request exactly so that the model
behaves as it does in AllenAI's own pipeline:

  * page rendered with pdftoppm (as olmOCR does) when available, else PyMuPDF, at the resolution
    whose longest side is 1288 px;
  * the v4 YAML prompt (copied verbatim from olmocr/prompts/prompts.py, Apache-2.0, (c) AllenAI);
  * temperature ladder 0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0 over up to 8 attempts, max_tokens 8000;
  * a response counts only if finish_reason == "stop" and it fits the 16384-token context;
  * the YAML front matter (primary_language, is_rotation_valid, rotation_correction, is_table,
    is_diagram) is parsed; a page the model reports as rotated is re-rendered with the cumulative
    correction and sent again.

When every attempt fails, the caller falls back to ParseFlow's local result for that page and
records that it did (the result says `fallback=True`): nothing is invented.

Configuration (environment / .env):
    VLM_SERVER   OpenAI-compatible base URL, e.g. https://api.deepinfra.com/v1/openai or http://localhost:8000/v1
    VLM_API_KEY  bearer token for hosted providers (not needed for a local vLLM)
    VLM_MODEL    served model name (default allenai/olmOCR-2-7B-1025)
"""
import base64
import io
import os
import re
import shutil
import subprocess
import time

import httpx
from PIL import Image

# Verbatim from olmocr.prompts.prompts.build_no_anchoring_v4_yaml_prompt (allenai/olmocr, Apache-2.0)
OLMOCR_V4_PROMPT = (
    "Attached is one page of a document that you must process. "
    "Just return the plain text representation of this document as if you were reading it naturally. Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax ![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for the primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram parameters."
)
TEMPERATURE_BY_ATTEMPT = [0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0]
MAX_TOKENS = 8000
MODEL_MAX_CONTEXT = 16384
FIELDS = ("primary_language", "is_rotation_valid", "rotation_correction", "is_table", "is_diagram")


class VLMError(Exception):
    def __init__(self, msg, permanent=False):
        super().__init__(msg)
        self.permanent = permanent


def _server_url(raw):
    """Accept what people paste: quotes, a missing https://, a trailing /chat/completions."""
    s = (raw or "").strip().strip('"').strip("'").strip()
    if not s:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        local = re.match(r"^(localhost|127\.|0\.0\.0\.0|\[::1\])", s)
        s = ("http://" if local else "https://") + s
    s = re.sub(r"/chat/completions/?$", "", s.rstrip("/"))
    ok = re.match(r"^https?://(localhost|[\w-]+(\.[\w-]+)*\.[a-z]{2,}|\d{1,3}(\.\d{1,3}){3}|\[[0-9a-f:]+\])(:\d+)?(/\S*)?$", s, re.I)
    return s if ok else ""


_DOWN = dict(until=0.0, why=None, server=None)


def mark_unavailable(why, seconds=None):
    """Remember that the server cannot be used (bad address, key, model or credit) so later uploads in the
    app do not wait for it. Cleared after VLM_RETRY_AFTER_S (default 10 min) or when the settings change."""
    _DOWN.update(until=time.time() + float(seconds or os.getenv("VLM_RETRY_AFTER_S", "600")), why=str(why)[:300],
                 server=(os.getenv("VLM_SERVER"), os.getenv("VLM_API_KEY"), os.getenv("VLM_MODEL")))


def unavailable():
    if _DOWN["why"] and time.time() < _DOWN["until"] and _DOWN["server"] == (os.getenv("VLM_SERVER"), os.getenv("VLM_API_KEY"), os.getenv("VLM_MODEL")):
        return _DOWN["why"]
    return None


def settings():
    server = _server_url(os.getenv("VLM_SERVER"))
    return dict(server=server, api_key=(os.getenv("VLM_API_KEY") or "").strip(),
                model=(os.getenv("VLM_MODEL") or "allenai/olmOCR-2-7B-1025").strip(),
                longest=int(os.getenv("VLM_IMAGE_DIM", "1288")), max_retries=int(os.getenv("VLM_MAX_RETRIES", "8")),
                timeout=float(os.getenv("VLM_TIMEOUT_S", "180")), backoff_tries=6, configured=bool(server))


def app_settings():
    """Settings for the web app, where a person is waiting: fewer retries, shorter waits, and a time
    budget per page after which ParseFlow's own reading is used. (The benchmark keeps olmOCR's
    patient defaults from settings().)"""
    s = settings()
    s.update(max_retries=int(os.getenv("VLM_APP_RETRIES", "3")), timeout=float(os.getenv("VLM_APP_TIMEOUT_S", "90")),
             backoff_tries=3, page_budget=float(os.getenv("VLM_APP_PAGE_BUDGET_S", "120")))
    return s


class Stopped(Exception):
    pass


def public():
    s = settings()
    return dict(configured=s["configured"], model=s["model"] if s["configured"] else None,
                server=re.sub(r"//[^/@]*@", "//", s["server"]) if s["configured"] else None)


# ------------------------------------------------------------------------------------ rendering

def _media_box(pdf_path, page_num):
    import pymupdf
    with pymupdf.open(pdf_path) as d:
        r = d[page_num - 1].mediabox
        return float(r.width), float(r.height)


def render_page_png(pdf_path, page_num, longest=1288):
    """PNG bytes of one page whose longest side is `longest` px (olmOCR's rendering rule:
    dpi = longest * 72 / longest media-box side). Uses pdftoppm like olmOCR when it is installed."""
    w, h = _media_box(pdf_path, page_num)
    dpi = longest * 72 / max(w, h)
    exe = shutil.which("pdftoppm")
    if exe:
        try:
            r = subprocess.run([exe, "-png", "-f", str(page_num), "-l", str(page_num), "-r", str(dpi), pdf_path],
                               capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout:
                return r.stdout, "pdftoppm"
        except Exception:
            pass
    import pymupdf
    with pymupdf.open(pdf_path) as d:
        z = dpi / 72.0
        pix = d[page_num - 1].get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
        return pix.tobytes("png"), "pymupdf"


def _rotate(png, rotation):
    if not rotation:
        return png
    tr = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}[rotation]
    with Image.open(io.BytesIO(png)) as im:
        out = io.BytesIO()
        im.transpose(tr).save(out, format="PNG")
        return out.getvalue()


def build_request(png, model):
    """Same request body as olmocr.pipeline.build_page_query (temperature set per attempt)."""
    b64 = base64.b64encode(png).decode("utf-8")
    return {"model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": OLMOCR_V4_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            "max_tokens": MAX_TOKENS, "temperature": 0.0}


# ------------------------------------------------------------------------------------ response

def _yaml_scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_response(content):
    """-> dict(front_matter fields..., natural_text). Same rules as olmocr.train.front_matter:
    front matter only when the reply starts with '---\\n'; all five fields required, no extras."""
    fm, text = {}, (content or "").strip()
    if content and content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            for line in content[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = _yaml_scalar(v)
            text = content[end + 4:].strip()
    missing = [f for f in FIELDS if f not in fm]
    if missing:
        raise ValueError(f"missing front matter field(s): {', '.join(missing)}")
    extra = set(fm) - set(FIELDS)
    if extra:
        raise ValueError(f"unexpected front matter field(s): {', '.join(sorted(extra))}")
    rot = fm["rotation_correction"]
    rot = int(rot) if isinstance(rot, (int, str)) and str(rot).lstrip("-").isdigit() else rot
    if rot not in (0, 90, 180, 270):
        raise ValueError("rotation_correction must be 0, 90, 180 or 270")
    as_bool = lambda x: x if isinstance(x, bool) else str(x).lower() == "true"
    lang = fm["primary_language"]
    return dict(primary_language=lang if isinstance(lang, str) and lang else None,
                is_rotation_valid=as_bool(fm["is_rotation_valid"]), rotation_correction=rot,
                is_table=as_bool(fm["is_table"]), is_diagram=as_bool(fm["is_diagram"]),
                natural_text=text if text else None)


# ------------------------------------------------------------------------------------ calling

def _post(cfg, body):
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    return httpx.post(cfg["server"] + "/chat/completions", json=body, headers=headers, timeout=cfg["timeout"])


def _detail(r):
    """The server's own error message, shortened (never includes the key)."""
    try:
        j = r.json()
        e = j.get("error") if isinstance(j, dict) else None
        msg = (e.get("message") if isinstance(e, dict) else e) or (j.get("detail") if isinstance(j, dict) else None) or ""
    except Exception:
        msg = (getattr(r, "text", "") or "")
    msg = re.sub(r"\s+", " ", str(msg)).strip()
    return msg[:160]


def _try_once(cfg, png, rotation, attempt):
    """One request. Returns (parsed or None, info). Raises VLMError for permanent errors and
    re-raises connection problems for the caller's back-off."""
    body = build_request(_rotate(png, rotation), cfg["model"])
    body["temperature"] = TEMPERATURE_BY_ATTEMPT[min(attempt, len(TEMPERATURE_BY_ATTEMPT) - 1)]
    r = _post(cfg, body)
    if r.status_code != 200:
        d = _detail(r)
        tail = f": {d}" if d else ""
        if r.status_code in (401, 403):
            raise VLMError(f"the OCR model server rejected the API key (HTTP {r.status_code}{tail})", permanent=True)
        if r.status_code == 404:
            raise VLMError(f"model '{cfg['model']}' or endpoint not found at {cfg['server']} (HTTP 404{tail})", permanent=True)
        if r.status_code == 402 or re.search(r"insufficient|balance|credit|billing|payment", d, re.I):
            raise VLMError(f"the OCR model provider needs credit on the account (HTTP {r.status_code}{tail})", permanent=True)
        if r.status_code in (408, 429, 500, 502, 503, 504, 529):
            raise httpx.HTTPStatusError(f"HTTP {r.status_code}{tail}", request=None, response=r)
        if r.status_code in (400, 422) and re.search(r"model", d, re.I):
            raise VLMError(f"the OCR server does not accept model '{cfg['model']}' (HTTP {r.status_code}{tail})", permanent=True)
        return None, dict(error=f"HTTP {r.status_code}{tail}")
    try:
        j = r.json()
        choice = j["choices"][0]
        usage = j.get("usage") or {}
        valid = choice.get("finish_reason") == "stop" and (usage.get("total_tokens") or 0) <= MODEL_MAX_CONTEXT
        parsed = parse_response(choice["message"]["content"])
        return parsed, dict(valid=valid, input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))
    except Exception as e:
        return None, dict(error=f"unusable response ({type(e).__name__}: {str(e)[:80]})")


def _with_backoff(cfg, png, rotation, attempt, sleep=time.sleep, check=None, report=None, deadline=None):
    tries = cfg.get("backoff_tries", 6)
    for k in range(tries):
        if check:
            check()
        c = cfg if not deadline else dict(cfg, timeout=max(5.0, min(cfg["timeout"], deadline - time.time())))
        try:
            return _try_once(c, png, rotation, attempt)
        except VLMError:
            raise
        except (httpx.UnsupportedProtocol, httpx.InvalidURL, httpx.LocalProtocolError) as e:
            raise VLMError(f"VLM_SERVER is not a valid address ({type(e).__name__})", permanent=True)
        except (httpx.HTTPError, OSError) as e:
            if isinstance(e, httpx.ConnectError) and cfg.get("page_budget") and k >= 1:
                # app: the address does not answer at all (wrong host, offline): stop instead of waiting
                raise VLMError(f"cannot connect to the OCR server ({str(e)[:100]})", permanent=True)
            why = (str(e) if isinstance(e, httpx.HTTPStatusError) else
                   "timed out" if isinstance(e, httpx.TimeoutException) else f"cannot reach the server ({type(e).__name__})")
            if k == tries - 1:
                return None, dict(error=f"server unreachable or busy ({why[:140]})", busy=True)
            wait = min(120, 5 * 2 ** k)
            if report:
                report(f"OCR server: {why[:90]}; retrying in {wait} s")
            sleep(wait)
    return None, dict(error="unreachable", busy=True)


def transcribe_page(pdf_path, page_num, cfg=None, sleep=time.sleep, stop=None, report=None):
    """olmOCR's per-page procedure (pipeline.process_page): first attempt; if the model reports the
    page rotated, retry sequentially with the cumulative correction; otherwise retry with rising
    temperature. Returns dict(text, ok, attempts, rotation, renderer, tokens, errors).
    stop: optional threading.Event; cfg['page_budget']: optional seconds after which the page gives up."""
    cfg = cfg or settings()
    if not cfg["configured"]:
        raise VLMError("VLM_SERVER is not set", permanent=True)
    t0 = time.time()
    budget = cfg.get("page_budget")

    def check():
        if stop is not None and stop.is_set():
            raise Stopped("stopped")
        if budget and time.time() - t0 > budget:
            raise Stopped(f"no answer within {int(budget)} s")
    raw_sleep = sleep

    def sleep(x):       # never sleep past the page's time budget
        raw_sleep(min(x, max(0.05, budget - (time.time() - t0))) if budget else x)
    png, renderer = render_page_png(pdf_path, page_num, cfg["longest"])
    rotation, errors, tokens = 0, [], [0, 0]
    last_valid = None
    busy = 0
    for attempt in range(cfg["max_retries"]):
        try:
            check()
            if report and attempt:
                report(f"attempt {attempt + 1} of {cfg['max_retries']}")
            parsed, info = _with_backoff(cfg, png, rotation, attempt, sleep=sleep, check=check, report=report,
                                         deadline=t0 + budget if budget else None)
        except Stopped as e:
            errors.append(str(e))
            busy += str(e) != "stopped"          # out of time: the server is too slow or not answering
            break
        tokens[0] += info.get("input_tokens", 0)
        tokens[1] += info.get("output_tokens", 0)
        if parsed is None:
            errors.append(info.get("error", "no result"))
            busy += bool(info.get("busy"))
            if busy >= 2 and budget:          # app: the server stayed unreachable/busy through two rounds of back-off
                break
            continue
        if not parsed["is_rotation_valid"]:
            rotation = (rotation + parsed["rotation_correction"]) % 360
            if info.get("valid"):
                last_valid = parsed
            continue
        if info.get("valid"):
            return dict(text=parsed["natural_text"] or "", ok=True, attempts=attempt + 1, rotation=rotation, renderer=renderer,
                        tokens=tokens, errors=errors, is_table=parsed["is_table"], is_diagram=parsed["is_diagram"],
                        language=parsed["primary_language"])
        errors.append("response cut off or over the context limit")
    if last_valid is not None:   # olmOCR returns the last valid response when rotations never settle
        return dict(text=last_valid["natural_text"] or "", ok=True, attempts=cfg["max_retries"], rotation=rotation,
                    renderer=renderer, tokens=tokens, errors=errors + ["rotation never confirmed"])
    return dict(text=None, ok=False, attempts=cfg["max_retries"], rotation=rotation, renderer=renderer, tokens=tokens, errors=errors,
                busy=busy > 0)


def convert_pdf(pdf_path, local_pages=None, cfg=None, sleep=time.sleep):
    """Markdown for every page of a PDF with the OCR model. `local_pages` is a callable returning
    {page: markdown} from ParseFlow's own pipeline; it is only called if some page needs a fallback.
    Returns (pages_md, stats) where stats records attempts, tokens, fallbacks and the renderer."""
    import pymupdf
    cfg = cfg or settings()
    with pymupdf.open(pdf_path) as d:
        n = d.page_count
    pages, stats = {}, dict(pages=n, fallback_pages=[], attempts=0, input_tokens=0, output_tokens=0, renderer=None, errors=[])
    local = None
    for p in range(1, n + 1):
        r = transcribe_page(pdf_path, p, cfg, sleep=sleep)
        stats["attempts"] += r["attempts"]
        stats["input_tokens"] += r["tokens"][0]
        stats["output_tokens"] += r["tokens"][1]
        stats["renderer"] = r["renderer"]
        if r["ok"]:
            pages[p] = r["text"]
            continue
        stats["fallback_pages"].append(p)
        stats["errors"] += [f"p{p}: {e}" for e in r["errors"][-2:]]
        if local is None and local_pages is not None:
            local = local_pages(pdf_path)
        pages[p] = (local or {}).get(p, "")
    return pages, stats
