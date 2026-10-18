import httpx
import pytest

from conftest import image_with_text, needs_tesseract, para_lines, scanned_pdf, text_pdf
from processing import ask, pipeline, vision


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


def ask_doc():
    blocks = []
    paras = [("Introduction", "heading"),
             ("Solar panels convert sunlight into electricity using photovoltaic cells made of silicon.", "paragraph"),
             ("Methods", "heading"),
             ("We measured output at three sites over twelve months and compared fixed and tracking mounts.", "paragraph"),
             ("Results", "heading"),
             ("Tracking mounts produced 23 percent more energy than fixed mounts on average.", "paragraph")]
    for i, (t, ty) in enumerate(paras):
        blocks.append(dict(block_id=f"b{i}", type=ty, content=t, page=1 + i // 2, reading_order=i + 1, bbox=[0, i * 10, 100, i * 10 + 8]))
    blocks.append(dict(block_id="tbl", type="table", content="Table", rows=[["Mount", "Energy"], ["Fixed", "100"], ["Tracking", "123"]],
                       page=3, reading_order=9, bbox=[0, 90, 100, 120]))
    return dict(filename="solar.pdf", pages=[dict(n=1), dict(n=2), dict(n=3)], blocks=blocks)


def test_ask_retrieval_finds_relevant_passages():
    chunks, how = ask.retrieve(ask_doc(), "How much more energy did tracking mounts produce?")
    assert how == "bm25"
    text = " ".join(c["text"] for c in chunks)
    assert "23 percent" in text and "Tracking" in text


def test_ask_global_question_uses_coverage():
    chunks, how = ask.retrieve(ask_doc(), "Summarize the document for a beginner")
    assert how == "coverage" and len({c["section"] for c in chunks}) >= 3


def test_ask_without_llm_is_explicit():
    r = ask.answer(ask_doc(), "What did the methods compare?")
    assert r["mode"] == "search" and r["model"] is None
    assert "AI answers are off" in r["answer"] and r["sources"]


def test_ask_with_llm_grounded_and_fake_citations_removed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    calls = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["body"] = json
        return FakeResp(200, {"content": [{"type": "text", "text": "**From the document:** Tracking mounts produced 23% more energy [S1]. Also [S9]."}]})
    monkeypatch.setattr(httpx, "post", fake_post)
    r = ask.answer(ask_doc(), "How much more energy did tracking mounts produce?")
    assert r["mode"] == "llm" and r["cited"] == ["S1"]
    assert "[S9]" not in r["answer"] and any("S9" in w for w in r["warnings"])
    assert "[S1]" in calls["body"]["messages"][-1]["content"]  # evidence was passed with labels


def test_ask_llm_timeout_falls_back_to_search(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setattr("processing.llm.time.sleep", lambda s: None)

    def boom(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(httpx, "post", boom)
    r = ask.answer(ask_doc(), "What did the methods compare?")
    assert r["mode"] == "search" and "timed out" in r["answer"]


def test_ask_permanent_error_not_retried(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "bad")
    n = {"calls": 0}

    def unauthorized(*a, **k):
        n["calls"] += 1
        return FakeResp(401, {})
    monkeypatch.setattr(httpx, "post", unauthorized)
    r = ask.answer(ask_doc(), "What did the methods compare?")
    assert r["mode"] == "search" and n["calls"] == 1


def test_vision_missing_key():
    assert not vision.available()
    items, err = vision.analyze_crop(image_with_text(["x"], size=(200, 100)))
    assert items == [] and "not configured" in err


def test_vision_timeout_is_reported(monkeypatch):
    monkeypatch.setenv("VISION_API_KEY", "k")

    def slow(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(httpx, "post", slow)
    items, err = vision.analyze_crop(image_with_text(["x"], size=(200, 100)))
    assert items == [] and "timeout" in err


@needs_tesseract()
def test_handwriting_region_without_vision_is_flagged_not_guessed(tmp_path):
    from PIL import ImageDraw
    img = image_with_text(["Printed heading of the form"], size=(1600, 1200))
    d = ImageDraw.Draw(img)
    for k in range(6):  # scribbles: ink that OCR cannot read
        d.line([(200 + 40 * k, 600), (230 + 40 * k, 520), (260 + 40 * k, 640), (300 + 40 * k, 560)], fill=0, width=6)
    out = pipeline.process_file(scanned_pdf(tmp_path / "hw.pdf", img))
    flagged = [b for b in out["blocks"] if b["extractor"] == "fallback"]
    assert flagged and all(b["content"] is None and b["verification"] == "failed" for b in flagged)
    assert any("VISION_API_KEY" in w for b in flagged for w in b["warnings"])


@needs_tesseract()
def test_handwriting_via_mocked_vision_is_distinguishable(tmp_path, monkeypatch):
    from PIL import ImageDraw
    img = image_with_text(["Printed heading of the form"], size=(1600, 1200))
    d = ImageDraw.Draw(img)
    for k in range(6):
        d.line([(200 + 40 * k, 600), (230 + 40 * k, 520), (260 + 40 * k, 640), (300 + 40 * k, 560)], fill=0, width=6)
    monkeypatch.setattr(vision, "available", lambda: True)
    monkeypatch.setattr(vision, "analyze_crop", lambda img, timeout=60: (
        [dict(type="handwritten_text", content="call the plumber", confidence=0.9, bbox=[0, 0, 1000, 1000])], None))
    out = pipeline.process_file(scanned_pdf(tmp_path / "hw.pdf", img))
    hw = [b for b in out["blocks"] if b["type"] == "handwritten_text"]
    assert hw and hw[0]["extractor"] == "vision_handwriting" and hw[0]["content"] == "call the plumber"
    assert hw[0]["confidence_type"] == "model (self-reported)" and hw[0]["verification"] != "verified"
