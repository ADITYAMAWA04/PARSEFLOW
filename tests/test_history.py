"""Document history without accounts: per-browser list, open by id, delete."""
import importlib
import time

import pytest
from fastapi.testclient import TestClient

from conftest import para_lines, text_pdf


@pytest.fixture()
def A():
    import app
    return importlib.reload(app)


def _process(c, tmp_path):
    items = [(72, 60, "Field Notes", 18)] + [(72, 100 + 14 * k, t, 11) for k, t in enumerate(
        para_lines("The river gauge read two metres on Monday and fell slowly through the week. " * 3))]
    with open(text_pdf(tmp_path / "notes.pdf", [items]), "rb") as f:
        r = c.post("/api/process", files={"file": ("notes.pdf", f, "application/pdf")}).json()
    t0 = time.time()
    while c.get(f"/api/status/{r['document_id']}").json()["status"] == "running":
        assert time.time() - t0 < 60
        time.sleep(0.2)
    return r["document_id"]


def test_history_per_browser_open_and_delete(A, tmp_path):
    a, b = TestClient(A.app), TestClient(A.app)
    doc = _process(a, tmp_path)
    assert a.cookies.get("pf_anon")
    h = a.get("/api/history").json()
    assert h["enabled"] and [d["doc_id"] for d in h["documents"]] == [doc]
    row = h["documents"][0]
    assert row["filename"] == "notes.pdf" and row["pages"] == 1 and row["status"] == "complete" and row["available"]
    assert b.get("/api/history").json()["documents"] == []          # another browser has its own list
    assert b.get(f"/api/document/{doc}").status_code == 200        # results still open by id
    assert b.delete(f"/api/history/{doc}").status_code == 404      # only the browser that made it can delete it
    assert a.delete(f"/api/history/{doc}").json()["ok"]
    assert a.get("/api/history").json()["documents"] == [] and a.get(f"/api/document/{doc}").status_code == 404
    assert not (A.DATA / doc).exists()


def test_google_sign_in_is_gone(A):
    c = TestClient(A.app)
    assert c.get("/auth/google/login", follow_redirects=False).status_code == 404
    assert A.health()["accounts"] is True and A.health()["storage"] == "sqlite"
