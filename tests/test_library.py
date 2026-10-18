"""Document library: summary per document, rename, star, downloads, share links, export, delete all, delete account."""
import importlib
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import para_lines, text_pdf


@pytest.fixture()
def A():
    import app
    return importlib.reload(app)


def _process(c, tmp_path, name="notes.pdf"):
    items = [(72, 60, "Field Notes", 18)] + [(72, 100 + 14 * k, t, 11) for k, t in enumerate(
        para_lines("The river gauge read two metres on Monday and fell slowly through the week. " * 3))]
    with open(text_pdf(tmp_path / name, [items]), "rb") as f:
        r = c.post("/api/process", files={"file": (name, f, "application/pdf")}).json()
    t0 = time.time()
    while c.get(f"/api/status/{r['document_id']}").json()["status"] == "running":
        assert time.time() - t0 < 60
        time.sleep(0.2)
    return r["document_id"]


def test_library_rows_rename_star_download(A, tmp_path):
    c = TestClient(A.app)
    doc = _process(c, tmp_path)
    h = c.get("/api/history").json()
    row = h["documents"][0]
    assert row["summary"]["pages"] == 1 and row["summary"]["blocks"] >= 2 and 0 <= row["summary"]["verified_pct"] <= 100
    assert row["size"] > 0 and not row["starred"] and not row["shared"] and "owner" not in row
    assert h["totals"] == dict(documents=1, pages=1, bytes=row["size"])

    assert c.patch(f"/api/history/{doc}", json=dict(filename="River log.pdf", starred=True)).json()["ok"]
    row = c.get("/api/history").json()["documents"][0]
    assert row["filename"] == "River log.pdf" and row["starred"]
    assert c.patch(f"/api/history/{doc}", json=dict(filename="  ")).status_code == 400

    md = c.get(f"/api/document/{doc}/markdown?download=1")
    assert 'filename="River log.md"' in md.headers["content-disposition"] and "river gauge" in md.text
    js = c.get(f"/api/document/{doc}?download=1")
    assert "River%20log.json" in js.headers["content-disposition"] and js.json()["blocks"]

    other = TestClient(A.app)
    assert other.patch(f"/api/history/{doc}", json=dict(filename="mine now")).status_code == 404
    assert other.post(f"/api/history/{doc}/share").status_code == 404

    z = zipfile.ZipFile(io.BytesIO(c.get("/api/history/export.zip").content))
    assert sorted(z.namelist()) == ["River log.json", "River log.md"]


def test_share_link_opens_a_private_document_read_only(A, tmp_path):
    owner = TestClient(A.app)
    owner.post("/api/auth/register", json=dict(username="sharer", password="share-it-please"))
    doc = _process(owner, tmp_path)
    friend = TestClient(A.app)
    assert friend.get(f"/api/document/{doc}").status_code == 404
    url = owner.post(f"/api/history/{doc}/share").json()["url"]
    token = url.rsplit("/", 1)[1]
    r = friend.get(f"/s/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/?open={doc}"
    assert friend.get(f"/api/document/{doc}").status_code == 200
    assert friend.get(f"/api/document/{doc}/markdown").status_code == 200
    assert friend.delete(f"/api/history/{doc}").status_code == 404           # read-only
    assert friend.get("/api/history").json()["documents"] == []              # not in their library
    assert owner.delete(f"/api/history/{doc}/share").json()["ok"]
    assert friend.get(f"/api/document/{doc}").status_code == 404             # link switched off
    assert TestClient(A.app).get(f"/s/{token}", follow_redirects=False).headers["location"] == "/?shared=gone"


def test_delete_all_and_delete_account(A, tmp_path):
    c = TestClient(A.app)
    c.post("/api/auth/register", json=dict(username="leaving", password="goodbye-for-now"))
    d1, d2 = _process(c, tmp_path, "a.pdf"), _process(c, tmp_path, "b.pdf")
    assert c.delete("/api/history").json()["deleted"] == 2
    assert c.get("/api/history").json()["documents"] == [] and not (A.DATA / d1).exists() and not (A.DATA / d2).exists()
    _process(c, tmp_path, "c.pdf")
    assert c.post("/api/auth/delete", json=dict(password="wrong-password")).status_code == 400
    assert c.post("/api/auth/delete", json=dict(password="goodbye-for-now")).json()["ok"]
    assert not c.get("/api/me").json()["signed_in"]
    assert TestClient(A.app).post("/api/auth/login", json=dict(username="leaving", password="goodbye-for-now")).status_code == 401
    assert c.get("/api/history").json()["documents"] == []


def test_old_database_is_upgraded(tmp_path):
    import sqlite3
    from processing import history
    d = tmp_path / "old"
    d.mkdir()
    db = sqlite3.connect(str(d / "parseflow.db"))
    db.execute("CREATE TABLE docs(doc_id TEXT PRIMARY KEY, owner TEXT, filename TEXT, pages INTEGER, status TEXT, created REAL, updated REAL)")
    db.execute("INSERT INTO docs VALUES('aaaaaaaaaaaa','anon:x','old.pdf',3,'complete',1,1)")
    db.commit()
    db.close()
    s = history.Store(d, url="")
    r = s.docs("anon:x")[0]
    assert r["filename"] == "old.pdf" and r["summary"] is None and not r["starred"]
