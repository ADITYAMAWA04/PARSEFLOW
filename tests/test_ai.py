"""AI features with the provider mocked (no network, no key): provider adapter, AI reading of
visuals, on-demand reading endpoint, Connect-AI settings, chat-style Ask with history."""
import io
import json

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from conftest import FONT_TTF, needs_tesseract
from processing import ask, llm, pipeline, vision


class R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def gemini_text(t):
    return R(200, {"candidates": [{"content": {"parts": [{"text": t}]}}]})


READING = {"visual_type": "screenshot", "title": "Boss-Extrude",
           "text": "Boss-Extrude\nFrom\nSketch Plane\nDirection 1\nBlind\n10.00mm\nMerge result\nDraft outward",
           "description": "The Boss-Extrude property panel with a blind extrusion of 10 mm from the sketch plane.",
           "elements": ["From", "Direction 1", "Merge result"], "relationships": [], "chart": None, "rows": None,
           "latex": None, "illegible": False, "confidence": 0.9}


def screenshot_png():
    im = Image.new("RGB", (700, 420), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_TTF, 26)
    for i, t in enumerate(["Boss-Extrude", "From", "Sketch Plane", "Direction 1", "Blind", "10.00mm", "Merge result"]):
        d.text((30, 20 + i * 52), t, fill="black", font=f)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def figure_pdf(path):
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Create the base feature with the Boss-Extrude command.", fontsize=11)
    p.insert_image(pymupdf.Rect(72, 100, 400, 300), stream=screenshot_png())
    doc.save(str(path))
    return str(path)


# --- adapter ---------------------------------------------------------------------------------

def test_provider_is_inferred_from_the_key(monkeypatch):
    monkeypatch.setenv("VISION_PROVIDER", "anthropic")           # stale setting from an old .env
    monkeypatch.setenv("VISION_API_KEY", "AIza" + "x" * 35)       # but the key is a Google key
    c = llm.config("vision")
    assert c["provider"] == "gemini" and c["configured"] and c["model"] == llm.GEMINI_DEFAULT
    monkeypatch.setenv("AI_API_KEY", "sk-ant-" + "y" * 30)        # AI_* takes precedence
    assert llm.config("text")["provider"] == "anthropic"


def test_gemini_unknown_model_is_rediscovered(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    llm._resolved.clear()
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        seen.append(url)
        return R(404, {}) if "gemini-flash-latest" in url else gemini_text("OK")

    def get(url, headers=None, params=None, timeout=None):
        return R(200, {"models": [
            {"name": "models/gemini-3.0-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-preview", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]}]})
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    assert llm.generate(llm.config("text"), "hi") == "OK"
    assert "gemini-3.5-flash:generateContent" in seen[-1]
    assert llm.public(llm.config("text"))["model"] == "gemini-3.5-flash"
    llm._resolved.clear()


def test_rejected_key_is_not_retried(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    n = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: n.append(1) or R(403, {}))
    with pytest.raises(llm.AIError) as e:
        llm.generate(llm.config("text"), "hi")
    assert e.value.permanent and len(n) == 1 and "rejected" in str(e.value)


# --- AI reading of visuals -------------------------------------------------------------------

def _mock_reader(monkeypatch, reading=READING):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return gemini_text(__import__("json").dumps(reading))
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_visual_is_read_by_ai_without_tesseract(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    monkeypatch.setattr("processing.ocr.available", lambda: None)          # Tesseract not installed
    calls = _mock_reader(monkeypatch)
    out = pipeline.process_file(figure_pdf(tmp_path / "f.pdf"))
    fig = [b for b in out["blocks"] if b.get("ai_read")]
    assert len(fig) == 1 and calls and calls[0]["contents"][0]["parts"][0].get("inline_data")
    b = fig[0]
    assert "10.00mm" in b["content"] and b["visual_type"] == "screenshot" and "Boss-Extrude" in b["description"]
    assert b["extractor"] == "vision_visual" and b["verification"] == "review"     # no independent reading available
    assert not any("needs a reader" in w for w in b["warnings"])
    assert "10.00mm" in out["_markdown"]


@needs_tesseract()
def test_ai_reading_is_verified_only_when_local_ocr_agrees(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    _mock_reader(monkeypatch)
    out = pipeline.process_file(figure_pdf(tmp_path / "f.pdf"))
    b = [b for b in out["blocks"] if b.get("ai_read")][0]
    assert b["local_text"] and b["ocr_agreement"] >= 0.6 and b["verification"] == "verified"
    assert b["evidence"].startswith("independent")
    # a reading that contradicts the local OCR is not verified
    _mock_reader(monkeypatch, dict(READING, text="Completely different words here"))
    out = pipeline.process_file(figure_pdf(tmp_path / "g.pdf"))
    b = [b for b in out["blocks"] if b.get("ai_read")][0]
    assert b["verification"] == "review" and b["ocr_agreement"] < 0.6


def test_no_reader_message_when_neither_ocr_nor_ai(monkeypatch, tmp_path):
    monkeypatch.setattr("processing.ocr.available", lambda: None)
    out = pipeline.process_file(figure_pdf(tmp_path / "f.pdf"))
    b = [b for b in out["blocks"] if b["type"] == "figure"][0]
    assert b.get("needs_reader") and any("Connect AI" in w for w in b["warnings"])


def _client(monkeypatch):
    import importlib
    import app as A
    A = importlib.reload(A)
    return A, TestClient(A.app)


def _upload_and_wait(c, path):
    import time
    with open(path, "rb") as f:
        r = c.post("/api/process", files={"file": ("f.pdf", f, "application/pdf")}).json()
    for _ in range(200):
        st = c.get(f"/api/status/{r['document_id']}").json()
        if st["status"] in ("complete", "partial", "failed"):
            break
        time.sleep(0.05)
    return r["document_id"]


def test_read_with_ai_on_demand_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr("processing.ocr.available", lambda: None)
    A, c = _client(monkeypatch)
    doc_id = _upload_and_wait(c, figure_pdf(tmp_path / "f.pdf"))
    doc = c.get(f"/api/document/{doc_id}").json()
    fig = [b for b in doc["blocks"] if b["type"] == "figure"][0]
    r = c.post(f"/api/visual/{doc_id}/{fig['block_id']}")
    assert r.status_code == 412 and "Connect" in r.json()["error"]["message"]      # no key yet
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    _mock_reader(monkeypatch)
    r = c.post(f"/api/visual/{doc_id}/{fig['block_id']}").json()
    assert r["block"]["ai_read"] and "Sketch Plane" in r["block"]["content"]
    assert "Sketch Plane" in c.get(f"/api/document/{doc_id}/markdown").text           # Markdown regenerated
    saved = c.get(f"/api/document/{doc_id}").json()
    assert [b for b in saved["blocks"] if b["block_id"] == fig["block_id"]][0]["ai_read"]


def test_connect_ai_settings_local_only_and_tested(monkeypatch, tmp_path):
    A, c = _client(monkeypatch)
    key = "AIza" + "k" * 35
    r = c.post("/api/settings/ai", json={"api_key": key})                            # no X-ParseFlow header
    assert r.status_code == 403
    bad = R(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: bad)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: bad)
    r = c.post("/api/settings/ai", json={"api_key": key}, headers={"X-ParseFlow": "1"})
    assert r.status_code == 400 and "not saved" in r.json()["error"]["message"] and "API key not valid" in r.json()["error"]["message"]
    import os
    assert not os.getenv("AI_API_KEY")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(200, {"models": []}))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: gemini_text("OK"))
    r = c.post("/api/settings/ai", json={"api_key": key}, headers={"X-ParseFlow": "1"}).json()
    assert r["ok"] and r["ai"]["provider"] == "gemini" and key not in json.dumps(r)
    assert f"AI_API_KEY={key}" in (tmp_path / "test.env").read_text()
    h = c.get("/api/health").json()
    assert h["llm"]["configured"] and key not in json.dumps(h)
    assert c.request("DELETE", "/api/settings/ai", headers={"X-ParseFlow": "1"}).json()["ok"]
    assert not c.get("/api/health").json()["llm"]["configured"]


# --- chat-style Ask ----------------------------------------------------------------------------

def ask_doc():
    blocks = [dict(block_id="h_1", type="heading", content="Extrusions", page=1, reading_order=1, role="body"),
              dict(block_id="t_1", type="paragraph", content="Select the sketch and click Boss-Extrude. Set Direction 1 to Blind and enter 10 mm.",
                   page=1, reading_order=2, role="body")]
    return dict(filename="cad.pdf", blocks=blocks, pages=[{"n": 1}])


def test_chat_answer_uses_history_and_labels(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sk-ant-" + "z" * 30)
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent["body"] = json
        return R(200, {"content": [{"type": "text", "text": "1. Select the sketch.\n2. Click **Boss-Extrude** [S1].\n\n**Beyond the document:** you can also drag the arrow (general knowledge)."}]})
    monkeypatch.setattr(httpx, "post", post)
    hist = [dict(role="user", content="how do I extrude?"), dict(role="assistant", content="Use Boss-Extrude [S1].")]
    r = ask.answer(ask_doc(), "and the depth?", history=hist)
    assert r["mode"] == "llm" and r["cited"] == ["S1"]
    msgs = sent["body"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"] and "and the depth?" in msgs[-1]["content"]
    assert "Beyond the document" in sent["body"]["system"]
    # the follow-up still retrieves the right passage (previous question used for retrieval)
    assert r["sources"] and r["sources"][0]["block_ids"]


def test_question_outside_document_still_gets_help(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sk-ant-" + "z" * 30)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(200, {"content": [{"type": "text", "text": "The document doesn't cover this directly. **Beyond the document:** ..."}]}))
    r = ask.answer(ask_doc(), "what is the capital of France")
    assert r["mode"] == "llm" and any("general knowledge" in w for w in r["warnings"])


def test_tesseract_cmd_is_honoured(monkeypatch, tmp_path):
    import pytesseract
    from processing import ocr
    fake = tmp_path / "tesseract.exe"
    fake.write_text("")
    old = pytesseract.pytesseract.tesseract_cmd
    monkeypatch.setenv("TESSERACT_CMD", str(fake))
    try:
        assert ocr._locate_tesseract() == str(fake) and pytesseract.pytesseract.tesseract_cmd == str(fake)
    finally:
        pytesseract.pytesseract.tesseract_cmd = old


# --- local AI (Ollama), mocked HTTP --------------------------------------------------------------

def _mock_ollama(monkeypatch, reply="Ollama says hi", models=("llama3.2:3b", "gemma3:4b")):
    from processing import llm as L
    L._OLLAMA.update(t=0.0, models=None)
    seen = []

    def get(url, timeout=None, **k):
        assert url.endswith("/api/tags")
        return R(200, {"models": [{"name": m} for m in models]})

    def post(url, json=None, headers=None, timeout=None):
        if url.endswith("/api/show"):
            return R(200, {"capabilities": ["completion", "vision"] if "gemma3" in json["model"] else ["completion"]})
        assert url.endswith("/api/chat")
        seen.append(json)
        return R(200, {"message": {"role": "assistant", "content": reply if not callable(reply) else reply(json)}})
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    return seen


def test_ollama_is_used_automatically_without_a_key(monkeypatch):
    monkeypatch.setenv("OLLAMA_AUTODETECT", "1")
    seen = _mock_ollama(monkeypatch)
    t, v = llm.config("text"), llm.config("vision")
    assert t["provider"] == "ollama" and t["configured"] and t["model"] == "llama3.2:3b"
    assert v["model"] == "gemma3:4b"                                   # the model that reports "vision"
    r = ask.answer(ask_doc(), "how do I extrude?")
    assert r["mode"] == "llm" and r["answer"] == "Ollama says hi"
    assert seen[-1]["messages"][0]["role"] == "system" and seen[-1]["stream"] is False


def test_ollama_reads_handwriting_on_demand_only(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_AUTODETECT", "1")
    reading = dict(READING, visual_type="diagram", handwritten=True, text="AVL TREES\nBalance factor = Height of left subtree - Height of right subtree")
    seen = _mock_ollama(monkeypatch, reply=json.dumps(reading))
    monkeypatch.setattr("processing.ocr.available", lambda: None)
    out = pipeline.process_file(figure_pdf(tmp_path / "f.pdf"))
    assert not any(b.get("ai_read") for b in out["blocks"]) and not seen        # local model: not during processing
    A, c = _client(monkeypatch)
    doc_id = _upload_and_wait(c, figure_pdf(tmp_path / "g.pdf"))
    fig = [b for b in c.get(f"/api/document/{doc_id}").json()["blocks"] if b["type"] == "figure"][0]
    b = c.post(f"/api/visual/{doc_id}/{fig['block_id']}").json()["block"]
    assert b["handwritten"] and "Balance factor" in b["content"] and seen[-1]["messages"][-1]["images"]
    assert seen[-1]["model"] == "gemma3:4b" and seen[-1]["format"] == "json"


def test_ollama_missing_model_message(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "ollama")
    monkeypatch.setenv("AI_MODEL", "llama3.2:3b")
    _mock_ollama(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda url, **k: R(404, {"error": "model not found"}))
    with pytest.raises(llm.AIError) as e:
        llm.generate(llm.config("text"), "hi")
    assert "ollama pull llama3.2:3b" in str(e.value)


def test_connect_local_ai_from_settings(monkeypatch, tmp_path):
    A, c = _client(monkeypatch)
    _mock_ollama(monkeypatch, reply="OK")
    r = c.get("/api/settings/ai/local", headers={"X-ParseFlow": "1"}).json()
    assert r["reachable"] and {m["name"]: m["vision"] for m in r["models"]} == {"llama3.2:3b": False, "gemma3:4b": True}
    r = c.post("/api/settings/ai", json={"provider": "ollama", "model": "llama3.2:3b", "vision_model": "gemma3:4b"},
               headers={"X-ParseFlow": "1"}).json()
    assert r["ok"] and r["ai"]["provider"] == "ollama" and r["vision"]["model"] == "gemma3:4b"
    env = (tmp_path / "test.env").read_text()
    assert "AI_PROVIDER=ollama" in env and "AI_VISION_MODEL=gemma3:4b" in env


def test_search_fallback_quotes_best_sentences(monkeypatch):
    doc = dict(filename="avl.pdf", pages=[{"n": 1}], blocks=[
        dict(block_id="a", type="paragraph", page=3, reading_order=1, role="body", confidence=0.99,
             content="While creating BST, height of the tree is not in our hands. The height of BST may be log(n) or n."),
        dict(block_id="b", type="figure", page=9, reading_order=2, role="body", confidence=0.6, extractor="figure_ocr",
             content="Node Height pl) J] We will see height for the tree nodes here")])
    r = ask.answer(doc, "what is the height of bst")
    first = [l for l in r["answer"].split("\n") if l.startswith("- ")][0]
    assert r["mode"] == "search" and "log(n) or n" in first and first.endswith("[S1]")


def test_busy_google_does_not_block_saving_a_valid_key(monkeypatch, tmp_path):
    A, c = _client(monkeypatch)
    monkeypatch.setattr("processing.llm.time.sleep", lambda s: None)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(200, {"models": [{"name": "models/gemini-3.0-flash", "supportedGenerationMethods": ["generateContent"]}]}))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(503, {"error": {"message": "The model is overloaded."}}))
    r = c.post("/api/settings/ai", json={"api_key": "AIza" + "q" * 35}, headers={"X-ParseFlow": "1"}).json()
    assert r["ok"] and "busy" in r["note"] and "temporary" in r["note"]
    assert "AI_API_KEY=AIza" in (tmp_path / "test.env").read_text()


def test_overloaded_gemini_model_falls_back_to_another(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    llm._resolved.clear(); llm._GEM_LIST.clear()
    monkeypatch.setattr("processing.llm.time.sleep", lambda s: None)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(200, {"models": [
        {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]}]}))
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        seen.append(url.split("/models/")[1].split(":")[0])
        return R(503, {}) if "flash-latest" in url or url.endswith("gemini-3.5-flash:generateContent") else gemini_text("fine")
    monkeypatch.setattr(httpx, "post", post)
    assert llm.generate(llm.config("text"), "hi") == "fine"
    assert seen == ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    llm._resolved.clear(); llm._GEM_LIST.clear()


def _gem_models(*names):
    return R(200, {"models": [{"name": "models/" + n, "supportedGenerationMethods": ["generateContent"]} for n in names]})


def test_gemini_tries_every_model_and_remembers_only_one_that_answered(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    llm._resolved.clear(); llm._GEM_LIST.clear(); llm._BUSY.clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _gem_models("gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.0-flash"))
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        m = url.split("/models/")[1].split(":")[0]
        seen.append(m)
        return gemini_text("ok") if m == "gemini-3.0-flash" else R(503, {"error": {"message": "The model is overloaded."}})
    monkeypatch.setattr(httpx, "post", post)
    assert llm.generate(llm.config("text"), "hi") == "ok"
    assert seen[-1] == "gemini-3.0-flash" and len(seen) == 5
    assert llm.public(llm.config("text"))["model"] == "gemini-3.0-flash"
    seen.clear()
    assert llm.generate(llm.config("text"), "again") == "ok" and seen == ["gemini-3.0-flash"]   # straight to the working one
    llm._resolved.clear(); llm._GEM_LIST.clear(); llm._BUSY.clear()


def test_gemini_all_busy_message_lists_what_google_said(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    llm._resolved.clear(); llm._GEM_LIST.clear(); llm._BUSY.clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _gem_models("gemini-3.5-flash"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(503, {"error": {"message": "The model is overloaded."}}))
    with pytest.raises(llm.AIError) as e:
        llm.generate(llm.config("text"), "hi")
    assert not e.value.permanent and "busy for every available" in str(e.value) and "overloaded" in str(e.value)
    assert llm.public(llm.config("text"))["model"] == llm.GEMINI_DEFAULT       # a failing model is not recorded as current
    llm._resolved.clear(); llm._GEM_LIST.clear(); llm._BUSY.clear()


def test_ai_check_endpoint_reports_each_model(monkeypatch):
    A, c = _client(monkeypatch)
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    llm._GEM_LIST.clear(); llm._BUSY.clear(); llm._resolved.clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _gem_models("gemini-3.7-flash", "gemini-3.5-flash-lite"))
    monkeypatch.setattr(httpx, "post", lambda url, **k: gemini_text("OK") if "lite" in url else R(503, {"error": {"message": "overloaded"}}))
    r = c.post("/api/settings/ai/check", headers={"X-ParseFlow": "1"}).json()
    by = {x["model"]: x for x in r["results"]}
    assert by["gemini-3.5-flash-lite"]["ok"] and by["gemini-3.7-flash"]["status"] == 503 and r["working"] == ["gemini-3.5-flash-lite"]
    assert c.post("/api/settings/ai/check").status_code == 403                    # local + header only
    llm._resolved.clear(); llm._GEM_LIST.clear(); llm._BUSY.clear()


def test_gemini_busy_models_are_retried_once_after_a_short_wait(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "AIza" + "x" * 35)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _gem_models("gemini-3.5-flash"))
    waits, calls = [], []
    monkeypatch.setattr(llm, "_sleep", waits.append)

    def post(url, **k):
        calls.append(url)
        return gemini_text("recovered") if len(calls) > 2 else R(503, {"error": {"message": "The model is overloaded."}})
    monkeypatch.setattr(httpx, "post", post)
    assert llm.generate(llm.config("text"), "hi") == "recovered" and len(waits) == 1


def _openai_reply(t):
    return R(200, {"choices": [{"message": {"role": "assistant", "content": t}}]})


def test_groq_key_is_inferred_and_models_are_discovered(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "gsk_" + "a" * 40)
    c = llm.config("text")
    assert c["provider"] == "groq" and c["configured"] and c["model"] == "auto" and c["_base"].startswith("https://api.groq.com")
    listed = {"data": [{"id": "whisper-large-v3"}, {"id": "llama-3.1-8b-instant"}, {"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.8-27b"}]}
    monkeypatch.setattr(httpx, "get", lambda url, **k: R(200, listed))
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return _openai_reply("<think>hmm</think>The group is abelian [S1].")
    monkeypatch.setattr(httpx, "post", post)
    assert llm.generate(c, "Is it abelian?", system="sys") == "The group is abelian [S1]."
    url, body, h = sent[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions" and body["model"] == "openai/gpt-oss-120b"
    assert h["Authorization"] == "Bearer gsk_" + "a" * 40 and body["messages"][0] == {"role": "system", "content": "sys"}
    assert llm.public(c)["model"] == "openai/gpt-oss-120b"
    # images go to a model that accepts them, as OpenAI-style image parts
    v = llm.config("vision")
    assert v["model"] == "auto-vision"
    llm.generate(v, "read this", images=[Image.new("RGB", (20, 20), "white")])
    body = sent[-1][1]
    assert body["model"] == "qwen/qwen3.8-27b" and body["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_retired_compat_model_is_replaced_and_busy_is_retried(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "gsk_" + "b" * 40)
    monkeypatch.setattr(httpx, "get", lambda url, **k: R(200, {"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "openai/gpt-oss-20b"}]}))
    seq = []

    def post(url, json=None, **k):
        seq.append(json["model"])
        if json["model"] == "llama-3.3-70b-versatile":
            return R(400, {"error": {"message": "The model `llama-3.3-70b-versatile` has been decommissioned"}})
        return R(503, {"error": {"message": "busy"}}) if len(seq) == 2 else _openai_reply("ok")
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("AI_MODEL", "llama-3.3-70b-versatile")      # pinned in .env, later retired by the provider
    assert llm.generate(llm.config("text"), "hi") == "ok"
    assert seq == ["llama-3.3-70b-versatile", "openai/gpt-oss-20b", "openai/gpt-oss-20b"]


def test_compat_rejected_key_and_custom_server(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sk-" + "c" * 40)
    c = llm.config("text")
    assert c["provider"] == "openai"
    monkeypatch.setattr(httpx, "get", lambda url, **k: R(401, {"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(llm.AIError) as e:
        llm.check_key(c)
    assert e.value.permanent and "rejected" in str(e.value)
    monkeypatch.delenv("AI_API_KEY")
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1/")
    monkeypatch.setenv("AI_MODEL", "my-local-model")
    c = llm.config("text")
    assert c["provider"] == "compatible" and c["configured"] and c["_base"] == "http://localhost:1234/v1" and c["model"] == "my-local-model"
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, **k: _openai_reply("hello") if "Authorization" not in headers else R(500, {}))
    assert llm.generate(c, "hi") == "hello"


def test_connect_ai_accepts_a_groq_key(monkeypatch):
    A, c = _client(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda url, **k: R(200, {"data": [{"id": "openai/gpt-oss-120b"}]}))
    monkeypatch.setattr(httpx, "post", lambda url, **k: _openai_reply("OK"))
    r = c.post("/api/settings/ai", headers={"X-ParseFlow": "1"}, json={"api_key": "gsk_" + "d" * 40, "provider": "groq"}).json()
    assert r["ok"] and r["ai"]["provider"] == "groq" and r["ai"]["model"] == "openai/gpt-oss-120b"
