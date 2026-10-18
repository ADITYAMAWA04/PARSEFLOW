"""High-accuracy OCR engine (olmOCR-2 over an OpenAI-compatible API), HTTP mocked."""
import httpx
import pymupdf
import pytest

from processing import vlm_ocr as V

FM = "---\nprimary_language: en\nis_rotation_valid: {rv}\nrotation_correction: {rc}\nis_table: False\nis_diagram: False\n---\n{t}"


class R:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}

    def json(self):
        return self._b


def reply(text, finish="stop", rv="True", rc=0, total=2000):
    return R(200, {"choices": [{"message": {"content": FM.format(rv=rv, rc=rc, t=text)}, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 1500, "completion_tokens": 500, "total_tokens": total}})


@pytest.fixture
def pdf(tmp_path):
    d = pymupdf.open()
    for i in range(2):
        pg = d.new_page()
        for k in range(6):
            pg.insert_text((72, 200 + 16 * k), f"Local text page {i + 1}, line {k + 1}: the committee reviewed the annual figures.", fontsize=11)
    p = tmp_path / "x.pdf"
    d.save(str(p))
    return str(p)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("VLM_SERVER", "http://vlm.test/v1")
    monkeypatch.setenv("VLM_API_KEY", "secret")
    return V.settings()


def test_request_matches_olmocr_and_temperature_ladder(monkeypatch, pdf, cfg):
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        seen.append((url, json, headers))
        return reply("x", finish="length") if len(seen) < 3 else reply("Final text")
    monkeypatch.setattr(httpx, "post", post)
    r = V.transcribe_page(pdf, 1, cfg, sleep=lambda s: None)
    assert r["ok"] and r["text"] == "Final text" and r["attempts"] == 3
    assert [b["temperature"] for _, b, _ in seen] == [0.1, 0.1, 0.2]          # olmOCR's ladder
    url, body, headers = seen[0]
    assert url == "http://vlm.test/v1/chat/completions" and headers["Authorization"] == "Bearer secret"
    assert body["max_tokens"] == 8000 and body["messages"][0]["content"][0]["text"] == V.OLMOCR_V4_PROMPT
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_rotation_feedback_is_applied(monkeypatch, pdf, cfg):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(json["messages"][0]["content"][1]["image_url"]["url"])
        return reply("", rv="False", rc=90) if len(calls) == 1 else reply("Upright text")
    monkeypatch.setattr(httpx, "post", post)
    r = V.transcribe_page(pdf, 1, cfg, sleep=lambda s: None)
    assert r["ok"] and r["rotation"] == 90 and r["text"] == "Upright text" and calls[0] != calls[1]


def test_over_context_response_is_not_accepted(monkeypatch, pdf, cfg):
    n = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: n.append(1) or (reply("too long", total=20000) if len(n) == 1 else reply("ok")))
    assert V.transcribe_page(pdf, 1, cfg, sleep=lambda s: None)["attempts"] == 2


def test_failed_pages_fall_back_to_parseflow_and_are_counted(monkeypatch, pdf, cfg):
    monkeypatch.setenv("VLM_MAX_RETRIES", "2")
    cfg = V.settings()

    def post(url, json=None, headers=None, timeout=None):
        return R(200, {"choices": [{"message": {"content": "no front matter"}, "finish_reason": "stop"}], "usage": {}})
    monkeypatch.setattr(httpx, "post", post)
    pages, st = V.convert_pdf(pdf, local_pages=lambda p: {1: "LOCAL 1", 2: "LOCAL 2"}, cfg=cfg, sleep=lambda s: None)
    assert pages == {1: "LOCAL 1", 2: "LOCAL 2"} and st["fallback_pages"] == [1, 2] and st["errors"]


def test_busy_server_is_retried_with_backoff_then_rejected_key_stops(monkeypatch, pdf, cfg):
    n, slept = [], []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: n.append(1) or (R(503) if len(n) < 3 else reply("after busy")))
    r = V.transcribe_page(pdf, 1, cfg, sleep=slept.append)
    assert r["ok"] and slept == [5, 10]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(401))
    with pytest.raises(V.VLMError) as e:
        V.transcribe_page(pdf, 1, cfg, sleep=lambda s: None)
    assert e.value.permanent and "rejected" in str(e.value)


def test_front_matter_rules():
    ok = V.parse_response(FM.format(rv="true", rc=0, t="# T\n\nbody"))
    assert ok["natural_text"] == "# T\n\nbody" and ok["is_rotation_valid"] is True
    for bad in ("plain text", FM.format(rv="True", rc=45, t="x"), FM.replace("is_diagram: False\n", "").format(rv="True", rc=0, t="x")):
        with pytest.raises(ValueError):
            V.parse_response(bad)


def test_pymupdf_fallback_renderer_matches_size(monkeypatch, pdf):
    a, ra = V.render_page_png(pdf, 1, 1288)
    monkeypatch.setattr(V.shutil, "which", lambda x: None)
    b, rb = V.render_page_png(pdf, 1, 1288)
    from PIL import Image
    import io
    sa, sb = Image.open(io.BytesIO(a)).size, Image.open(io.BytesIO(b)).size
    assert rb == "pymupdf" and abs(sa[0] - sb[0]) <= 1 and abs(sa[1] - sb[1]) <= 1 and max(sb) in (1287, 1288, 1289)


def test_app_pipeline_uses_model_pages_and_keeps_local_evidence(monkeypatch, pdf, cfg):
    from processing import ask, pipeline
    monkeypatch.setattr(httpx, "post", lambda *a, **k: reply("# Model heading\n\nModel reading of the page about exchanges."))
    out = pipeline.process_file(pdf)
    pt = [b for b in out["blocks"] if b["type"] == "page_text"]
    assert len(pt) == 2 and pt[0]["extractor"] == "ocr_vlm" and pt[0]["model"] == "allenai/olmOCR-2-7B-1025"
    local = [b for b in out["blocks"] if b["page"] == 1 and b["type"] != "page_text"]
    assert local and all(b["superseded_by"] == "vlm_1_01" for b in local)          # evidence kept, marked
    assert "Model reading of the page" in out["_markdown"] and "Local text page 1" not in out["_markdown"]
    r = ask.answer(out, "what is the page about exchanges?")
    assert "Model reading" in r["answer"]


def test_app_pipeline_keeps_local_pages_when_model_fails(monkeypatch, pdf, cfg):
    from processing import pipeline
    monkeypatch.setenv("VLM_MAX_RETRIES", "1")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(200, {"choices": [{"message": {"content": "junk"}, "finish_reason": "stop"}], "usage": {}}))
    out = pipeline.process_file(pdf)
    assert not any(b["type"] == "page_text" for b in out["blocks"]) and "Local text page 1" in out["_markdown"]
    assert any("High-accuracy OCR was not available for this document" in w for w in out["warnings"])
    assert "unusable response" in out["high_accuracy_ocr"]["reason"] and out["high_accuracy_ocr"]["pages_read"] == 0


# --- the web app must never wait indefinitely for the OCR server ------------------------------------

def _job(tmp_path):
    from processing import pipeline
    d = tmp_path / "job"
    d.mkdir()
    return pipeline.Progress(str(d / "status.json")), d


def test_app_stops_at_once_when_the_provider_needs_credit(monkeypatch, pdf, cfg, tmp_path):
    from processing import pipeline
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1) or R(402, {"error": {"message": "Insufficient balance"}}))
    prog, _ = _job(tmp_path)
    out = pipeline.process_file(pdf, prog=prog)
    w = " ".join(out["warnings"])
    assert "not available" in w and "402" not in w and "Insufficient" not in w     # no technical detail in the UI
    reason = out["high_accuracy_ocr"]["reason"]
    assert "needs credit" in reason and "Insufficient balance" in reason and "Local text page 1" in out["_markdown"]
    assert len(calls) <= 4                     # not 8 attempts per page
    ex = next(s for s in prog.stages if s["name"] == "EXTRACT")
    assert ex["state"] == "done" and ex["detail"] == "page text read by ParseFlow"
    # the next document does not wait for the same problem
    calls.clear()
    out2 = pipeline.process_file(pdf, prog=_job(tmp_path / "2")[0] if (tmp_path / "2").mkdir() is None else None)
    assert calls == [] and out2["high_accuracy_ocr"]["reason"] == reason


def test_app_gives_up_on_a_busy_server_within_the_page_budget(monkeypatch, pdf, cfg, tmp_path):
    import time
    from processing import pipeline
    monkeypatch.setenv("VLM_APP_PAGE_BUDGET_S", "1.5")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(429, {"error": {"message": "rate limited"}}))
    prog, _ = _job(tmp_path)
    t = time.time()
    out = pipeline.process_file(pdf, prog=prog)
    assert time.time() - t < 15
    assert "Local text page 2" in out["_markdown"] and any("not available for this document" in w for w in out["warnings"])


def test_skip_request_finishes_with_the_local_reading(monkeypatch, pdf, cfg, tmp_path):
    import threading
    import time
    from processing import pipeline
    release = threading.Event()

    def hanging_post(*a, **k):                 # a server that never answers in time
        release.wait(20)
        return reply("too late")
    monkeypatch.setattr(httpx, "post", hanging_post)
    prog, d = _job(tmp_path)
    threading.Timer(1.0, lambda: (d / "SKIP_VLM").write_text("1")).start()
    t = time.time()
    out = pipeline.process_file(pdf, prog=prog)
    release.set()
    assert time.time() - t < 10
    assert not any(b["type"] == "page_text" for b in out["blocks"]) and "Local text page 1" in out["_markdown"]
    assert out["high_accuracy_ocr"]["skipped"] and not out["warnings"]


def test_skip_endpoint(monkeypatch, tmp_path):
    import importlib
    from fastapi.testclient import TestClient
    import app as A
    A = importlib.reload(A)
    c = TestClient(A.app)
    assert c.post("/api/skip-ocr/000000000000").status_code == 404
    (A.DATA / "abcdefabcdef").mkdir()
    assert c.post("/api/skip-ocr/abcdefabcdef").json()["ok"] and (A.DATA / "abcdefabcdef" / "SKIP_VLM").exists()


def test_a_hanging_server_is_bounded_by_the_page_budget(monkeypatch, pdf, cfg):
    import time
    timeouts = []

    def slow_post(url, json=None, headers=None, timeout=None):
        timeouts.append(timeout)
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(httpx, "post", slow_post)
    monkeypatch.setenv("VLM_APP_PAGE_BUDGET_S", "2")
    c = V.app_settings()
    t = time.time()
    r = V.transcribe_page(pdf, 1, c, sleep=time.sleep)
    assert not r["ok"] and r["busy"] and time.time() - t < 6
    assert max(timeouts) <= 5.0 + 1e-6          # each request is limited to what is left of the budget (min 5 s)


def test_server_address_without_scheme_is_fixed_or_ignored(monkeypatch):
    for raw, want in [("api.deepinfra.com/v1/openai", "https://api.deepinfra.com/v1/openai"),
                      ('"https://api.deepinfra.com/v1/openai/chat/completions"', "https://api.deepinfra.com/v1/openai"),
                      ("localhost:8000/v1", "http://localhost:8000/v1"), ("   ", ""), ("your server here", "")]:
        monkeypatch.setenv("VLM_SERVER", raw)
        assert V.settings()["server"] == want, raw
    monkeypatch.setenv("VLM_SERVER", "your server here")
    assert not V.settings()["configured"]


def test_unreachable_address_stops_quickly_in_the_app(monkeypatch, pdf, cfg):
    import time
    n = []

    def refuse(*a, **k):
        n.append(1)
        raise httpx.ConnectError("[Errno -2] Name or service not known")
    monkeypatch.setattr(httpx, "post", refuse)
    t = time.time()
    with pytest.raises(V.VLMError) as e:
        V.transcribe_page(pdf, 1, V.app_settings(), sleep=lambda s: None)
    assert e.value.permanent and len(n) == 2 and time.time() - t < 5
