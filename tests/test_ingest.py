import json
import os

import pymupdf
import pytest

from conftest import image_with_text, needs_tesseract, para_lines, scanned_pdf, text_pdf
from processing import pipeline

LOREM = ("The committee reviewed the annual report and approved the budget for the coming year. "
         "Members discussed the maintenance schedule, the new library wing and the transport plan. ")


def test_corrupt_pdf(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(pipeline.PFError) as e:
        pipeline.process_file(str(p))
    assert e.value.code == "PF-201"


def test_empty_file(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(pipeline.PFError) as e:
        pipeline.process_file(str(p))
    assert e.value.code == "PF-205"


def test_unsupported_type(tmp_path):
    for name, data in (("tool.exe", b"MZ"), ("notes.docx", b"PK..")):   # unknown type; damaged Word file
        p = tmp_path / name
        p.write_bytes(data)
        with pytest.raises(pipeline.PFError) as e:
            pipeline.process_file(str(p))
        assert e.value.code == "PF-204"


def test_encrypted_pdf(tmp_path):
    src = text_pdf(tmp_path / "plain.pdf", [[(72, 100, "secret text", 12)]])
    doc = pymupdf.open(src)
    out = str(tmp_path / "enc.pdf")
    doc.save(out, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw2")
    with pytest.raises(pipeline.PFError) as e:
        pipeline.process_file(out)
    assert e.value.code == "PF-202"


def test_digital_ingest_basic(tmp_path):
    lines = para_lines(LOREM * 3)
    pdf = text_pdf(tmp_path / "a.pdf", [[(72, 100 + 14 * i, t, 11) for i, t in enumerate(lines)]])
    doc = pipeline.process_file(pdf)
    assert doc["processing_status"] == "complete"
    assert len(doc["pages"]) == 1 and doc["pages"][0]["kind"] in ("digital", "mixed")
    assert "approved the budget" in doc["_markdown"]


def test_hundreds_of_pages_no_limit(tmp_path):
    n = 300
    doc = pymupdf.open()
    for i in range(n):
        p = doc.new_page()
        p.insert_text((72, 300), f"Section {i + 1}: {['soil', 'water', 'air', 'light'][i % 4]} measurements", fontsize=14)
        p.insert_text((72, 330), f"This page carries the unique marker token PAGE{i + 1:04d} for the test.", fontsize=11)
    path = tmp_path / "long.pdf"
    doc.save(str(path))
    out = pipeline.process_file(str(path))
    assert len(out["pages"]) == n
    md = out["_markdown"]
    assert all(f"PAGE{i:04d}" in md for i in (1, 150, 300))
    assert out["processing_status"] == "complete"


def test_max_pages_is_opt_in(tmp_path, monkeypatch):
    doc = pymupdf.open()
    for i in range(5):
        doc.new_page().insert_text((72, 100), f"page {i}", fontsize=11)
    path = tmp_path / "five.pdf"
    doc.save(str(path))
    # a stale .env from an older version (MAX_PAGES=60 there) must NOT truncate documents
    monkeypatch.setenv("MAX_PAGES", "2")
    out = pipeline.process_file(str(path))
    assert len(out["pages"]) == 5 and not any("first" in w for w in out["warnings"])
    # the explicit opt-in cap still works and says so
    monkeypatch.setenv("PARSEFLOW_PAGE_LIMIT", "2")
    out = pipeline.process_file(str(path))
    assert len(out["pages"]) == 2
    assert any("PARSEFLOW_PAGE_LIMIT=2" in w for w in out["warnings"])


@needs_tesseract()
def test_mixed_digital_and_scanned(tmp_path):
    lines = ["The quarterly inspection found no structural damage.", "All emergency exits were clear and well marked."]
    img = image_with_text(lines)
    scan = scanned_pdf(tmp_path / "scan.pdf", img)
    dig = text_pdf(tmp_path / "dig.pdf", [[(72, 100, "Digital cover page for the inspection report", 14)]])
    merged = pymupdf.open(dig)
    merged.insert_pdf(pymupdf.open(scan))
    path = tmp_path / "mixed.pdf"
    merged.save(str(path))
    out = pipeline.process_file(str(path))
    kinds = [p["kind"] for p in out["pages"]]
    assert kinds[0] in ("digital", "mixed") and kinds[1].startswith("scanned")
    ocr_blocks = [b for b in out["blocks"] if b["page"] == 2 and b["extractor"] == "ocr" and b.get("content")]
    assert ocr_blocks and "inspection" in " ".join(b["content"] for b in ocr_blocks)
    assert all(0 <= b["confidence"] <= 1 for b in ocr_blocks)


def test_partial_failure_keeps_other_pages(tmp_path, monkeypatch):
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((72, 300), f"Healthy content on page {i + 1} about {['rivers', 'mountains', 'forests'][i]}.", fontsize=11)
    path = tmp_path / "three.pdf"
    doc.save(str(path))
    real = pipeline._page_job

    def flaky(kind, src, i, cfg, state, vocab):
        if i == 1:
            raise RuntimeError("simulated malformed page")
        return real(kind, src, i, cfg, state, vocab)
    monkeypatch.setattr(pipeline, "_page_job", flaky)
    out = pipeline.process_file(str(path))
    assert out["processing_status"] == "partial"
    assert out["failed_pages"] == [2]
    assert "page 1" in out["_markdown"] and "page 3" in out["_markdown"]
    err = [b for b in out["blocks"] if b["page"] == 2]
    assert err and err[0]["verification"] == "failed" and "preserved" in err[0]["warnings"][0]


def test_cancellation(tmp_path):
    pdf = text_pdf(tmp_path / "c.pdf", [[(72, 100, "x", 11)], [(72, 100, "y", 11)]])
    prog = pipeline.Progress()
    prog.cancel()
    with pytest.raises(pipeline.Cancelled):
        pipeline.process(pdf, "c.pdf", "id", None, prog)


def test_resume_from_checkpoints(tmp_path):
    pdf = text_pdf(tmp_path / "r.pdf", [[(72, 100, f"resume page {i}", 11)] for i in range(4)])
    work = tmp_path / "work"
    first = pipeline.process(pdf, "r.pdf", "id", str(work), pipeline.Progress(str(work / "status.json")))
    assert len(os.listdir(work / "pages")) == 4
    second = pipeline.process(pdf, "r.pdf", "id", str(work), pipeline.Progress(str(work / "status.json")))
    assert second["resumed_pages"] == 4
    assert [b["content"] for b in first["blocks"]] == [b["content"] for b in second["blocks"]]
    st = json.loads((work / "status.json").read_text())
    assert st["completed_pages"] == 4 and st["progress_percent"] == 100.0


def test_image_input(tmp_path):
    img = image_with_text(["Receipt total due on delivery"], size=(1200, 400), origin=(40, 120))
    p = tmp_path / "photo.png"
    img.save(p)
    out = pipeline.process_file(str(p))
    assert out["format"] == "image" and len(out["pages"]) == 1
