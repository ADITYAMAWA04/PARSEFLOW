"""One adapter for the optional AI provider (used by image reading and by Ask).

Configuration (server side only, never sent to the browser). The simplest setup is one key:

    AI_API_KEY=...            # a Google AI Studio key (starts with "AIza") or an Anthropic key ("sk-ant-")
    AI_PROVIDER=              # optional: gemini | anthropic (inferred from the key when empty)
    AI_MODEL=                 # optional model override

Older variable names keep working: VISION_API_KEY / VISION_PROVIDER / VISION_MODEL,
LLM_API_KEY / LLM_PROVIDER / LLM_MODEL, GEMINI_API_KEY, ANTHROPIC_API_KEY.

Local, free, offline alternative: Ollama (https://ollama.com). With AI_PROVIDER=ollama - or with
no key configured and Ollama running on this machine - the models you have pulled are used:
AI_MODEL for answers, AI_VISION_MODEL for images (auto-picked from models that report the
"vision" capability when empty). OLLAMA_URL defaults to http://localhost:11434.

OpenAI-compatible providers (a second route when Google is busy):
    gsk_...    Groq (free tier, fast)      sk-or-...  OpenRouter      sk-...  OpenAI
    AI_BASE_URL=http://host/v1 (+ AI_MODEL, key optional): any OpenAI-compatible server (LM Studio, vLLM, ...)
Model names change often; when AI_MODEL is empty the adapter asks the provider's /models list and picks
a suitable chat model (and, for images, a model that accepts images).

Gemini model names change often (gemini-2.5-flash was retired in 2026). The default is Google's
"gemini-flash-latest" alias; if the API answers 404 for a model, the adapter asks the API which
models this key can use and picks the newest Flash model that supports generateContent.
"""
import base64
import io
import os
import re
import threading
import time

import httpx

ANTHROPIC_DEFAULT = "claude-sonnet-5-5"
GEMINI_DEFAULT = "gemini-flash-latest"
_resolved = {}          # gemini: requested model -> model that actually works with this key
_lock = threading.Lock()


class AIError(Exception):
    """permanent=True: do not retry (bad key, bad request, unknown model)."""

    def __init__(self, msg, permanent=False):
        super().__init__(msg)
        self.permanent = permanent


def _infer(key):
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("sk-"):
        return "openai"
    return ""


# OpenAI-compatible chat APIs: base URL and model preferences (first match in the provider's /models list wins)
COMPAT = {
    "groq": dict(name="Groq", base="https://api.groq.com/openai/v1",
                 text=[r"^openai/gpt-oss-120b$", r"^openai/gpt-oss-20b$", r"^llama-3\.3-70b", r"^qwen/", r"^meta-llama/"],
                 vision=[r"^qwen/qwen3\.\d+-\d+b$", r"llama-4", r"vision|-vl\b|scout|maverick"]),
    "openai": dict(name="OpenAI", base="https://api.openai.com/v1",
                   text=[r"^gpt-4\.1-mini$", r"^gpt-4o-mini$", r"^gpt-5(\.\d+)?-mini$", r"^gpt-4\.1$", r"^gpt-4o$"],
                   vision=[r"^gpt-4\.1-mini$", r"^gpt-4o-mini$", r"^gpt-5(\.\d+)?-mini$", r"^gpt-4\.1$", r"^gpt-4o$"]),
    "openrouter": dict(name="OpenRouter", base="https://openrouter.ai/api/v1",
                       text=[r":free$"], vision=[r"(vl|vision|gemma-3|llama-4|qwen2\.5-vl).*:free$"]),
    "compatible": dict(name="OpenAI-compatible server", base=None, text=[r"."], vision=[r"vl|vision|llava|gemma-?3|minicpm-v|llama-4"]),
}


_OLLAMA = {"t": 0.0, "models": None}


def ollama_url():
    return (os.getenv("OLLAMA_URL") or "http://localhost:11434").rstrip("/")


def ollama_models(force=False):
    """[{name, vision}] of locally pulled models, or None when Ollama is not reachable (cached 30 s)."""
    now = time.time()
    if not force and now - _OLLAMA["t"] < 30:
        return _OLLAMA["models"]
    models = None
    try:
        r = httpx.get(ollama_url() + "/api/tags", timeout=0.8)
        if r.status_code == 200:
            models = []
            for m in r.json().get("models", []):
                name = m.get("name") or m.get("model")
                if not name or "embed" in name:
                    continue
                vis = None
                try:
                    sh = httpx.post(ollama_url() + "/api/show", json={"model": name}, timeout=2.0)
                    caps = sh.json().get("capabilities") if sh.status_code == 200 else None
                    if isinstance(caps, list):
                        vis = "vision" in caps
                except Exception:
                    pass
                if vis is None:   # older Ollama without capabilities: fall back to the model name
                    vis = bool(re.search(r"(vl\b|vl:|-vl|vision|llava|minicpm-v|moondream|gemma3|gemma4|granite.*vision|bakllava)", name))
                models.append(dict(name=name, vision=vis))
    except Exception:
        models = None
    _OLLAMA.update(t=now, models=models)
    return models


def _ollama_pick(purpose, models):
    if not models:
        return ""
    if purpose == "vision":
        vis = [m["name"] for m in models if m["vision"]]
        return vis[0] if vis else ""
    # text: any chat model; a vision model also answers text
    return models[0]["name"]


def _ollama_config(purpose):
    want = (os.getenv("AI_VISION_MODEL") if purpose == "vision" else os.getenv("AI_MODEL")) or ""
    if purpose == "vision" and not want:
        want = ""
    models = ollama_models()
    names = {m["name"] for m in models or []}
    model = want.strip() or _ollama_pick(purpose, models)
    if purpose == "vision" and model and models is not None and not any(m["name"] == model and m["vision"] for m in models) and not want:
        model = ""
    ok = bool(models is not None and model and (model in names or model + ":latest" in names))
    return dict(provider="ollama", model=model, configured=ok, _key="", local=True,
                reason=None if ok else ("Ollama is not running" if models is None else
                                        ("no vision model pulled" if purpose == "vision" else "no model pulled")))


def config(purpose="general"):
    """purpose: 'vision' or 'text'. Returns dict(provider, model, configured, _key)."""
    if (os.getenv("AI_PROVIDER") or "").strip().lower() == "ollama":
        return _ollama_config(purpose)
    if (os.getenv("AI_PROVIDER") or "").strip().lower() == "compatible" or (os.getenv("AI_BASE_URL") or "").strip():
        return _compat_config(purpose, "compatible", (os.getenv("AI_API_KEY") or "").strip())
    order = (["AI", "VISION", "LLM"] if purpose == "vision" else ["AI", "LLM", "VISION"])
    key, prov, model = "", "", ""
    for p in order:
        k = (os.getenv(f"{p}_API_KEY") or "").strip()
        if k:
            key = k
            prov = (os.getenv(f"{p}_PROVIDER") or "").strip().lower()
            model = (os.getenv(f"{p}_MODEL") or "").strip()
            break
    if not key:
        if os.getenv("GEMINI_API_KEY"):
            key, prov = os.getenv("GEMINI_API_KEY").strip(), "gemini"
        elif os.getenv("ANTHROPIC_API_KEY"):
            key, prov = os.getenv("ANTHROPIC_API_KEY").strip(), "anthropic"
    inferred = _infer(key) if key else ""
    if inferred and prov != inferred:
        prov = inferred                     # the key decides: a Gemini key cannot call Anthropic
        model = model if model and _model_fits(model, prov) else ""
    if not prov and key:
        prov = "gemini"
    if prov in COMPAT:
        return _compat_config(purpose, prov, key, model)
    if not model:
        model = ANTHROPIC_DEFAULT if prov == "anthropic" else GEMINI_DEFAULT if prov == "gemini" else ""
    cfg = dict(provider=prov or None, model=model, configured=bool(key and prov in ("anthropic", "gemini")), _key=key)
    if not cfg["configured"] and os.getenv("OLLAMA_AUTODETECT", "1") != "0":
        oc = _ollama_config(purpose)       # no key: use a local Ollama if one is running
        if oc["configured"]:
            return oc
    return cfg


def _model_fits(model, prov):
    if prov in COMPAT:
        return not model.startswith(("claude", "gemini"))
    return model.startswith("claude") if prov == "anthropic" else model.startswith("gemini")


def _compat_config(purpose, prov, key, model=""):
    base = (os.getenv("AI_BASE_URL") or "").strip().rstrip("/") or COMPAT[prov]["base"]
    if purpose == "vision":
        model = (os.getenv("AI_VISION_MODEL") or "").strip() or "auto-vision"
    model = model or (os.getenv("AI_MODEL") or "").strip() or "auto"
    ok = bool(base and (key or prov == "compatible"))
    return dict(provider=prov, model=model, configured=ok, _key=key, _base=base,
                reason=None if ok else "set AI_BASE_URL for an OpenAI-compatible server")


def public(cfg):
    m = _resolved.get(f"{cfg['provider']}:{cfg['model']}") or _resolved.get(cfg["model"], cfg["model"])
    d = dict(configured=cfg["configured"], provider=cfg["provider"], model="auto" if m == "auto-vision" else m)
    if cfg.get("local"):
        d["local"] = True
    return d


def _png_b64(img, max_side=1600):
    im = img.convert("RGB")
    if max(im.size) > max_side:
        k = max_side / max(im.size)
        im = im.resize((round(im.size[0] * k), round(im.size[1] * k)))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _gemini_models(key, timeout=20.0):
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": key},
                  params={"pageSize": 200}, timeout=timeout)
    if r.status_code >= 400:
        raise AIError(f"could not list Gemini models (HTTP {r.status_code})", permanent=r.status_code in (400, 401, 403))
    out = []
    for m in r.json().get("models", []):
        name = (m.get("name") or "").split("/")[-1]
        if "generateContent" in (m.get("supportedGenerationMethods") or []) and re.match(r"gemini-[\d.]+-flash(?!.*(image|tts|audio|live|thinking))", name):
            out.append(name)

    def ver(n):
        v = re.match(r"gemini-([\d.]+)", n).group(1)
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    # stable before preview/experimental; within each, newest first; full Flash before Flash-Lite
    out.sort(key=lambda n: ("preview" not in n and "exp" not in n, ver(n), "lite" not in n), reverse=True)
    return out


_GEM_LIST = {}


def _next_gemini(key, tried):
    """Another Flash model for this key (newest first), not yet tried; None if there is none."""
    try:
        if key not in _GEM_LIST:
            _GEM_LIST[key] = _gemini_models(key)
        for m in _GEM_LIST[key]:
            if m not in tried:
                return m
    except AIError:
        pass
    return None


def _error_detail(r):
    try:
        e = r.json().get("error")
        msg = e.get("message") if isinstance(e, dict) else e
        return str(msg)[:200] if msg else ""
    except Exception:
        return ""


def check_key(cfg):
    """Validate a key without spending generation capacity. Raises AIError(permanent=True) if the key
    is rejected; returns quietly when it works (or when only a temporary problem stops the check)."""
    if cfg.get("provider") in COMPAT:
        try:
            _compat_list(cfg, force=True)
        except AIError as e:
            if e.permanent:
                raise
        return
    if cfg.get("provider") == "gemini":
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": cfg["_key"]},
                      params={"pageSize": 5}, timeout=20)
        if r.status_code in (400, 401, 403):
            d = _error_detail(r)
            raise AIError("the API key was rejected" + (f": {d}" if d else ""), permanent=True)
        return
    generate(cfg, "Reply with the single word OK.", max_tokens=8, timeout=30, retries=1)


def generate(cfg, prompt, system=None, images=(), history=(), json_mode=False, max_tokens=2048, timeout=90.0, retries=2,
             temperature=0.2):
    """history: [(role, text)] with role 'user' | 'assistant'. Returns the model's text. Raises AIError."""
    if not cfg.get("configured"):
        raise AIError(cfg.get("reason") or "no AI provider configured", permanent=True)
    prov, key = cfg["provider"], cfg["_key"]
    model = _resolved.get(cfg["model"], cfg["model"])
    imgs = [_png_b64(i) for i in images]
    if prov == "ollama":
        return _ollama_generate(cfg, prompt, system, imgs, history, json_mode, max_tokens, timeout, temperature)
    if prov in COMPAT:
        return _compat_generate(cfg, prompt, system, imgs, history, max_tokens, timeout, retries, temperature)
    if prov == "gemini":
        return _gemini_generate(cfg, prompt, system, imgs, history, json_mode, max_tokens, timeout, temperature)
    last = None
    tried_discovery = False
    tried_models = {model}
    attempt = 0
    while attempt <= retries:
        if prov == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            msgs = [{"role": r, "content": t} for r, t in history]
            if imgs:
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}} for b in imgs]
                content.append({"type": "text", "text": prompt})
            else:
                content = prompt
            msgs.append({"role": "user", "content": content})
            body = {"model": model, "max_tokens": max_tokens, "messages": msgs, "temperature": temperature}
            if system:
                body["system"] = system

            def pick(j):
                return "".join(p.get("text", "") for p in j.get("content", []) if p.get("type") == "text")
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            headers = {"x-goog-api-key": key}
            contents = [{"role": "model" if r == "assistant" else "user", "parts": [{"text": t}]} for r, t in history]
            parts = [{"inline_data": {"mime_type": "image/png", "data": b}} for b in imgs] + [{"text": prompt}]
            contents.append({"role": "user", "parts": parts})
            gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
            if json_mode:
                gen["responseMimeType"] = "application/json"
            body = {"contents": contents, "generationConfig": gen}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}

            def pick(j):
                cands = j.get("candidates") or []
                if not cands:
                    fb = (j.get("promptFeedback") or {}).get("blockReason")
                    raise AIError(f"Gemini returned no answer{' (blocked: ' + fb + ')' if fb else ''}", permanent=True)
                return "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            last = AIError("the AI provider timed out")
            attempt += 1
            continue
        except httpx.HTTPError as e:
            last = AIError(f"could not reach the AI provider ({type(e).__name__})")
            attempt += 1
            continue
        if r.status_code == 404 and prov == "gemini" and not tried_discovery:
            tried_discovery = True
            cands = _gemini_models(key)
            if not cands:
                raise AIError(f"model '{model}' not found and no Gemini Flash model is available to this key", permanent=True)
            with _lock:
                _resolved[cfg["model"]] = cands[0]
            model = cands[0]
            continue
        if r.status_code in (429, 500, 502, 503, 504, 529):
            who = "Google" if prov == "gemini" else "Anthropic"
            last = AIError(f"{who} is rate-limiting requests (HTTP 429); wait a minute and retry" if r.status_code == 429 else
                           f"{who}'s servers are busy right now (HTTP {r.status_code}); this is temporary, try again in a minute")
            # an overloaded Gemini model: switch to another Flash model this key can use, instead of waiting
            if prov == "gemini" and r.status_code in (500, 503, 529):
                alt = _next_gemini(key, tried_models)
                if alt:
                    tried_models.add(alt)
                    model = alt
                    with _lock:
                        _resolved[cfg["model"]] = alt
                    attempt += 1
                    continue
            time.sleep(min(20.0, 2.0 * (attempt + 1) ** 2))
            attempt += 1
            continue
        detail = _error_detail(r)
        if r.status_code in (401, 403) or (r.status_code == 400 and re.search(r"api.?key", detail, re.I)):
            raise AIError("the API key was rejected" + (f": {detail}" if detail else f" (HTTP {r.status_code})") + " - check the key",
                          permanent=True)
        if r.status_code >= 400:
            raise AIError(f"the AI provider returned HTTP {r.status_code}" + (f": {detail}" if detail else ""), permanent=True)
        try:
            return pick(r.json())
        except AIError:
            raise
        except Exception:
            raise AIError("the AI response could not be parsed", permanent=True)
    raise last or AIError("AI request failed")


# ---------------------------------------------------------------- OpenAI-compatible providers

_COMPAT_LIST = {}     # (base, key) -> [model ids]
_sleep = time.sleep   # patched in tests


def _compat_headers(cfg):
    h = {"Content-Type": "application/json"}
    if cfg.get("_key"):
        h["Authorization"] = f"Bearer {cfg['_key']}"
    if cfg["provider"] == "openrouter":
        h.update({"HTTP-Referer": "https://github.com/ADITYAMAWA04/PARSERFLOW", "X-Title": "ParseFlow"})
    return h


def _compat_list(cfg, force=False):
    k = (cfg["_base"], cfg.get("_key", ""))
    if force or k not in _COMPAT_LIST:
        try:
            r = httpx.get(cfg["_base"] + "/models", headers=_compat_headers(cfg), timeout=20)
        except httpx.HTTPError as e:
            raise AIError(f"could not reach {COMPAT[cfg['provider']]['name']} ({type(e).__name__})")
        if r.status_code in (401, 403):
            d = _error_detail(r)
            raise AIError("the API key was rejected" + (f": {d}" if d else ""), permanent=True)
        if r.status_code >= 400:
            raise AIError(f"{COMPAT[cfg['provider']]['name']} could not list models (HTTP {r.status_code})", permanent=r.status_code == 404)
        try:
            data = r.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            _COMPAT_LIST[k] = [m["id"] for m in items if isinstance(m, dict) and m.get("id") and m.get("active", True) is not False]
        except Exception:
            raise AIError("the provider's model list could not be read", permanent=True)
    return _COMPAT_LIST[k]


def _compat_pick(cfg, exclude=()):
    """Resolve 'auto' / 'auto-vision' (or a model the provider no longer serves) to a listed model."""
    vision = cfg["model"] == "auto-vision" or cfg.get("_purpose") == "vision"
    ids = [m for m in _compat_list(cfg) if m not in exclude and not re.search(r"whisper|tts|embed|guard|orpheus|moderation|dall-e|image-|audio|realtime|transcribe|search", m)]
    for pat in COMPAT[cfg["provider"]]["vision" if vision else "text"]:
        for m in ids:
            if re.search(pat, m):
                return m
    if vision:
        raise AIError(f"{COMPAT[cfg['provider']]['name']} offers no model that can read images to this key; set AI_VISION_MODEL", permanent=True)
    if ids:
        return ids[0]
    raise AIError(f"{COMPAT[cfg['provider']]['name']} lists no chat model for this key", permanent=True)


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def _compat_generate(cfg, prompt, system, imgs, history, max_tokens, timeout, retries, temperature):
    name = COMPAT[cfg["provider"]]["name"]
    rk = f"{cfg['provider']}:{cfg['model']}"
    model = _resolved.get(rk) or cfg["model"]
    if model in ("auto", "auto-vision"):
        model = _compat_pick(cfg)
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": r, "content": t} for r, t in history]
    if imgs:
        content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in imgs]
    else:
        content = prompt
    msgs.append({"role": "user", "content": content})
    tok_field = "max_completion_tokens" if cfg["provider"] in ("openai", "groq") else "max_tokens"
    body = {"model": model, "messages": msgs, tok_field: max(max_tokens, 256), "temperature": temperature}
    tried, last, attempt, switched = {model}, None, 0, False
    while attempt <= retries:
        try:
            r = httpx.post(cfg["_base"] + "/chat/completions", json=body, headers=_compat_headers(cfg), timeout=timeout)
        except httpx.TimeoutException:
            last, attempt = AIError(f"{name} timed out"), attempt + 1
            continue
        except httpx.HTTPError as e:
            last, attempt = AIError(f"could not reach {name} ({type(e).__name__})"), attempt + 1
            continue
        detail = _error_detail(r)
        if r.status_code == 200:
            try:
                j = r.json()
                msg = (j.get("choices") or [{}])[0].get("message") or {}
                text = msg.get("content") or ""
            except Exception:
                raise AIError(f"the {name} response could not be parsed", permanent=True)
            with _lock:
                _resolved[rk] = model
            return _THINK.sub("", text).strip()
        if r.status_code in (401, 403):
            raise AIError("the API key was rejected" + (f": {detail}" if detail else "") + " - check the key", permanent=True)
        if r.status_code == 400 and "temperature" in detail.lower() and "temperature" in body:
            body.pop("temperature")            # some reasoning models accept only the default temperature
            continue
        if r.status_code == 400 and tok_field in body and re.search(r"max_(completion_)?tokens", detail):
            body["max_tokens" if tok_field == "max_completion_tokens" else "max_completion_tokens"] = body.pop(tok_field)
            tok_field = "max_tokens" if tok_field == "max_completion_tokens" else "max_completion_tokens"
            continue
        if (r.status_code == 404 or (r.status_code == 400 and re.search(r"model|decommission|not support.*image|image.*not support", detail, re.I))) \
                and not switched:
            switched = True                    # the model was retired or cannot do this: pick another listed one
            try:
                model = _compat_pick(dict(cfg, _purpose="vision" if imgs else "text"), exclude=tried)
            except AIError:
                raise AIError(f"{name} returned HTTP {r.status_code}" + (f": {detail}" if detail else ""), permanent=True)
            tried.add(model)
            body["model"] = model
            continue
        if r.status_code in (408, 409, 429, 500, 502, 503, 504, 529):
            last = AIError(f"{name} is rate-limiting requests (HTTP 429); wait a minute" if r.status_code == 429 else
                           f"{name}'s servers are busy right now (HTTP {r.status_code}); try again in a minute")
            ra = r.headers.get("retry-after") if hasattr(r, "headers") else None
            _sleep(min(20.0, float(ra) if ra and ra.replace(".", "", 1).isdigit() else 2.0 * (attempt + 1) ** 2))
            attempt += 1
            continue
        raise AIError(f"{name} returned HTTP {r.status_code}" + (f": {detail}" if detail else ""), permanent=True)
    raise last or AIError(f"{name} request failed")


def strip_json_fence(txt):
    return re.sub(r"^```(?:json)?|```$", "", (txt or "").strip(), flags=re.M).strip()


def _ollama_generate(cfg, prompt, system, imgs, history, json_mode, max_tokens, timeout, temperature):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": r, "content": t} for r, t in history]
    m = {"role": "user", "content": prompt}
    if imgs:
        m["images"] = imgs
    msgs.append(m)
    body = {"model": cfg["model"], "messages": msgs, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens}}
    if json_mode:
        body["format"] = "json"
    # local models on a laptop CPU are slow: allow much longer than for hosted APIs
    t = float(os.getenv("OLLAMA_TIMEOUT_S", str(max(300.0, timeout * 4))))
    try:
        r = httpx.post(ollama_url() + "/api/chat", json=body, timeout=t)
    except httpx.TimeoutException:
        raise AIError(f"the local model took longer than {int(t)} s (set OLLAMA_TIMEOUT_S or use a smaller model)")
    except httpx.HTTPError:
        _OLLAMA.update(t=0.0)
        raise AIError("Ollama is not running on this computer (start the Ollama app)", permanent=True)
    if r.status_code == 404:
        raise AIError(f"model '{cfg['model']}' is not installed - run: ollama pull {cfg['model']}", permanent=True)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or ""
        except Exception:
            msg = ""
        raise AIError(f"Ollama returned HTTP {r.status_code}{': ' + str(msg)[:160] if msg else ''}", permanent=True)
    try:
        return (r.json().get("message") or {}).get("content", "")
    except Exception:
        raise AIError("the local model's response could not be parsed", permanent=True)


_BUSY = {}           # gemini model -> time it last answered "overloaded"; skipped for BUSY_COOLDOWN_S
BUSY_COOLDOWN_S = 300


def _gemini_candidates(key, first):
    """Model order for one request: the last model that worked (or the configured one), then every
    other Flash / Flash-Lite model this key can use. Models that were overloaded in the last few
    minutes go to the back instead of being dropped (they may have recovered)."""
    try:
        if key not in _GEM_LIST:
            _GEM_LIST[key] = _gemini_models(key)
        others = [m for m in _GEM_LIST[key] if m != first]
    except AIError as e:
        if e.permanent:
            raise
        others = []
    order = [first] + others
    now = time.time()
    fresh = [m for m in order if now - _BUSY.get(m, 0) > BUSY_COOLDOWN_S]
    return fresh + [m for m in order if m not in fresh]


def _gemini_generate(cfg, prompt, system, imgs, history, json_mode, max_tokens, timeout, temperature):
    key = cfg["_key"]
    first = _resolved.get(cfg["model"], cfg["model"])
    contents = [{"role": "model" if r == "assistant" else "user", "parts": [{"text": t}]} for r, t in history]
    parts = [{"inline_data": {"mime_type": "image/png", "data": b}} for b in imgs] + [{"text": prompt}]
    contents.append({"role": "user", "parts": parts})
    gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    body = {"contents": contents, "generationConfig": gen}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    tried, busy_msgs = [], []
    candidates = [first]
    discovered = False
    i = 0
    while i < len(candidates) and len(tried) < 8:
        model = candidates[i]
        i += 1
        tried.append(model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = None
        for net_try in range(2):                       # one quick retry for network hiccups only
            try:
                r = httpx.post(url, json=body, headers={"x-goog-api-key": key}, timeout=timeout)
                break
            except httpx.TimeoutException:
                busy_msgs.append(f"{model}: timed out")
            except httpx.HTTPError as e:
                busy_msgs.append(f"{model}: connection error ({type(e).__name__})")
        if r is None:
            continue
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:
                raise AIError("the AI response could not be parsed", permanent=True)
            cands = j.get("candidates") or []
            if not cands:
                fb = (j.get("promptFeedback") or {}).get("blockReason")
                raise AIError(f"Gemini returned no answer{' (blocked: ' + fb + ')' if fb else ''}", permanent=True)
            with _lock:
                _resolved[cfg["model"]] = model       # remember only a model that actually answered
                _BUSY.pop(model, None)
            return "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
        detail = _error_detail(r)
        if r.status_code in (401, 403) or (r.status_code == 400 and re.search(r"api.?key", detail, re.I)):
            raise AIError("the API key was rejected" + (f": {detail}" if detail else f" (HTTP {r.status_code})") + " - check the key",
                          permanent=True)
        if r.status_code in (404, 429, 500, 502, 503, 504, 529):
            # this model is unavailable, overloaded or out of free quota: try the next one now
            if r.status_code != 404:
                _BUSY[model] = time.time()
            busy_msgs.append(f"{model}: HTTP {r.status_code}" + (f" ({detail[:90]})" if detail else ""))
            if not discovered:
                discovered = True
                candidates += [m for m in _gemini_candidates(key, first) if m not in candidates]
            continue
        raise AIError(f"Gemini returned HTTP {r.status_code}" + (f": {detail}" if detail else ""), permanent=True)
    busy5 = [m for m in tried if any(x.startswith(m + ": HTTP 5") for x in busy_msgs)][:2]
    if busy5:
        # "model overloaded" usually clears within seconds: wait once, then retry the two best models
        _sleep(float(os.getenv("GEMINI_BUSY_WAIT_S", "4")))
        for model in busy5:
            try:
                r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                               json=body, headers={"x-goog-api-key": key}, timeout=timeout)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                cands = (r.json().get("candidates") or [])
                if cands:
                    with _lock:
                        _resolved[cfg["model"]] = model
                        _BUSY.pop(model, None)
                    return "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
            busy_msgs.append(f"{model} (after waiting): HTTP {r.status_code}")
    quota = any("HTTP 429" in m for m in busy_msgs)
    msg = ("Google's free-tier limit was reached for every available model; wait a minute (or until tomorrow for the daily limit)"
           if quota and all("HTTP 429" in m for m in busy_msgs) else
           "Google's servers are busy for every available Gemini model right now; this is temporary. Try again in a few "
           "minutes, or connect a second provider such as Groq (free) in Connect AI")
    err = AIError(msg + ". Tried: " + "; ".join(busy_msgs[:6]))
    err.tried = tried
    raise err


def diagnose(cfg, limit=10):
    """Try a tiny request on every model this key can use. Returns [{model, status, ok, detail, seconds}]."""
    out = []
    if cfg.get("provider") != "gemini":
        t = time.time()
        try:
            generate(cfg, "Reply with the single word OK.", max_tokens=8, timeout=30, retries=0)
            out.append(dict(model=public(cfg)["model"], status=200, ok=True, detail="", seconds=round(time.time() - t, 2)))
        except AIError as e:
            out.append(dict(model=public(cfg)["model"], status=None, ok=False, detail=str(e), seconds=round(time.time() - t, 2)))
        return out
    key = cfg["_key"]
    models = []
    for m in [cfg["model"]] + _gemini_models(key):
        if m not in models:
            models.append(m)
    body = {"contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}],
            "generationConfig": {"maxOutputTokens": 8, "temperature": 0}}
    for m in models[:limit]:
        t = time.time()
        try:
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent", json=body,
                           headers={"x-goog-api-key": key}, timeout=30)
            out.append(dict(model=m, status=r.status_code, ok=r.status_code == 200,
                            detail="" if r.status_code == 200 else _error_detail(r), seconds=round(time.time() - t, 2)))
            if r.status_code == 200:
                with _lock:
                    _resolved.setdefault(cfg["model"], m)
                    _BUSY.pop(m, None)
            elif r.status_code in (429, 500, 503, 529):
                _BUSY[m] = time.time()
        except httpx.HTTPError as e:
            out.append(dict(model=m, status=None, ok=False, detail=type(e).__name__, seconds=round(time.time() - t, 2)))
    return out
