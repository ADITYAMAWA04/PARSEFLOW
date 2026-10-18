import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import para_lines, text_pdf


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app as A
    A = importlib.reload(A)
    return TestClient(A.app), A


def sample_pdf(tmp_path):
    items = [(72, 60, "Water Quality Report", 18)]
    for k, t in enumerate(para_lines("Nitrate levels stayed below the legal limit at every sampling site this year. " * 4)):
        items.append((72, 100 + 14 * k, t, 11))
    return text_pdf(tmp_path / "report.pdf", [items, items])


def wait(c, doc_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get(f"/api/status/{doc_id}").json()
        if st["status"] != "running":
            return st
        time.sleep(0.2)
    raise AssertionError("timeout")


def test_health(client):
    c, _ = client
    h = c.get("/api/health").json()
    assert h["ok"] and h["mode"] in ("worker", "serverless") and "llm" in h and "limits" in h
    assert "key" not in json.dumps(h).lower() or "api_key" not in json.dumps(h).lower()


def test_process_status_document_markdown_page_ask(client, tmp_path):
    c, _ = client
    with open(sample_pdf(tmp_path), "rb") as f:
        r = c.post("/api/process", files={"file": ("my report.pdf", f, "application/pdf")}).json()
    assert r["pages"] == 2 and "path" not in json.dumps(r)
    st = wait(c, r["document_id"])
    assert st["status"] == "complete" and st["completed_pages"] == 2 and st["progress_percent"] == 100.0
    d = c.get(f"/api/document/{r['document_id']}").json()
    assert d["blocks"] and all({"block_id", "page", "bbox", "confidence", "verification", "extractor"} <= set(b) for b in d["blocks"])
    md = c.get(f"/api/document/{r['document_id']}/markdown").text
    assert "Nitrate levels" in md
    ann = c.get(f"/api/document/{r['document_id']}/markdown?annotate=1").text
    assert "<!-- pf:" in ann and "<!-- page 2 -->" in ann
    png = c.get(f"/api/page/{r['document_id']}/1.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert c.get(f"/api/page/{r['document_id']}/9.png").status_code == 404
    a = c.post("/api/ask", json={"document_id": r["document_id"], "question": "Were nitrate levels below the limit?"}).json()
    assert a["mode"] == "search" and a["sources"]


def test_errors_are_json_not_500(client, tmp_path):
    c, _ = client
    r = c.post("/api/process", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PF-204"
    r = c.post("/api/process", files={"file": ("x.pdf", b"garbage", "application/pdf")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PF-201"
    assert c.get("/api/document/../../etc/passwd").status_code == 404
    assert c.get("/api/document/ZZZZ").json()["error"]["code"] == "PF-404"
    assert c.get("/api/status/abcdefabcdef").status_code == 404
    assert c.post("/api/ask", content=b"not json", headers={"content-type": "application/json"}).json()["error"]["code"] == "PF-400"


def test_upload_limit(client, tmp_path, monkeypatch):
    c, A = client
    monkeypatch.setattr(A, "MAX_BYTES", 1000)
    r = c.post("/api/process", files={"file": ("big.pdf", b"%PDF" + b"0" * 5000, "application/pdf")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PF-206"


def test_serverless_mode_processes_inline(monkeypatch, tmp_path):
    monkeypatch.setenv("PARSEFLOW_MODE", "sync")
    import app as A
    A = importlib.reload(A)
    c = TestClient(A.app)
    with open(sample_pdf(tmp_path), "rb") as f:
        r = c.post("/api/process", files={"file": ("r.pdf", f, "application/pdf")}).json()
    assert r["mode"] == "serverless" and r["status"] == "complete"
    assert r["document"]["blocks"] and "Nitrate levels" in r["markdown"]
    # a different instance would not have the files: Ask accepts the twin in the request
    a = c.post("/api/ask", json={"document_id": "000000000000", "question": "nitrate limit?", "document": r["document"]}).json()
    assert a["sources"]
    monkeypatch.setenv("SYNC_MAX_PAGES", "1")
    A = importlib.reload(A)
    c = TestClient(A.app)
    with open(sample_pdf(tmp_path), "rb") as f:
        r = c.post("/api/process", files={"file": ("r.pdf", f, "application/pdf")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PF-207"


def test_cli_folder_to_markdown(tmp_path):
    import cli
    src = tmp_path / "in"
    (src / "nested").mkdir(parents=True)
    text_pdf(src / "one.pdf", [[(72, 100, "First document body text.", 11)]])
    text_pdf(src / "nested" / "two (draft).pdf", [[(72, 100, "Second document body text.", 11)]])
    (src / "broken.pdf").write_bytes(b"nope")
    (src / "ignore.txt").write_text("not a pdf")
    out = tmp_path / "out"
    code = cli.main([str(src), "--output", str(out), "--recursive", "--json", "-q"])
    assert code == 1  # one failure -> nonzero, the others still converted
    assert (out / "one.md").read_text().strip() == "First document body text."
    assert (out / "nested" / "two (draft).md").exists() and (out / "nested" / "two (draft).json").exists()
    assert not (out / "broken.md").exists()
    code = cli.main([str(src / "one.pdf"), "--output", str(tmp_path / "o2"), "-q"])
    assert code == 0
    assert cli.main([str(tmp_path / "missing"), "--output", str(out)]) == 2


def test_safe_output_names():
    import cli
    assert ".." not in cli.safe_stem("../../etc/passwd.pdf").split("/")
    assert cli.safe_stem("a/b c?.pdf") == "a/b c_"
