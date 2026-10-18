"""Username + password accounts: saved private documents for members, temporary guest documents."""
import importlib
import time

import pytest
from fastapi.testclient import TestClient

from conftest import para_lines, text_pdf


@pytest.fixture()
def A(monkeypatch):
    monkeypatch.delenv("GUEST_RETENTION_HOURS", raising=False)
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


def test_register_login_and_private_saved_documents(A, tmp_path):
    a = TestClient(A.app)
    doc = _process(a, tmp_path)                                    # converted as a guest first
    assert a.get("/api/me").json() == dict(signed_in=False, user=None, accounts=True, guest_hours=24.0)
    r = a.post("/api/auth/register", json=dict(username="asha_r", password="river-gauge-42"))
    assert r.json()["ok"] and a.cookies.get("pf_session")
    me = a.get("/api/me").json()
    assert me["signed_in"] and me["user"] == dict(username="asha_r")
    h = a.get("/api/history").json()
    assert h["signed_in"] and [d["doc_id"] for d in h["documents"]] == [doc]   # guest document moved into the account

    other = TestClient(A.app)                                      # another browser, not logged in
    for path in (f"/api/document/{doc}", f"/api/status/{doc}", f"/api/page/{doc}/1.jpg", f"/api/document/{doc}/markdown"):
        assert other.get(path).status_code == 404, path
    assert other.post("/api/ask", json=dict(document_id=doc, question="river?")).status_code == 404
    assert other.get("/api/history").json()["documents"] == []

    assert other.post("/api/auth/login", json=dict(username="ASHA_R", password="river-gauge-42")).json()["ok"]  # case-insensitive name
    assert [d["doc_id"] for d in other.get("/api/history").json()["documents"]] == [doc]
    assert other.get(f"/api/document/{doc}").status_code == 200

    assert a.post("/api/auth/logout").json()["ok"]
    assert not a.get("/api/me").json()["signed_in"] and a.get("/api/history").json()["documents"] == []


def test_registration_rules_and_wrong_password(A):
    c = TestClient(A.app)
    bad = lambda u, p: c.post("/api/auth/register", json=dict(username=u, password=p)).json()["error"]["message"]
    assert "3 to 32" in bad("a", "long enough pw")
    assert "at least 8" in bad("ravi", "short")
    assert "must not contain the username" in bad("ravikumar", "ravikumar123")
    assert c.post("/api/auth/register", json=dict(username="ravi", password="tea-and-toast")).json()["ok"]
    assert "taken" in TestClient(A.app).post("/api/auth/register", json=dict(username="Ravi", password="another-pass-1")).json()["error"]["message"]
    d = TestClient(A.app)
    r = d.post("/api/auth/login", json=dict(username="ravi", password="wrong-password"))
    assert r.status_code == 401 and "not correct" in r.json()["error"]["message"]
    assert d.post("/api/auth/login", json=dict(username="nobody", password="whatever-123")).status_code == 401  # same answer
    for _ in range(8):
        d.post("/api/auth/login", json=dict(username="ravi", password="wrong-password"))
    assert d.post("/api/auth/login", json=dict(username="ravi", password="tea-and-toast")).status_code == 429


def test_passwords_are_hashed_and_change_ends_sessions(A):
    from processing import history as h
    stored = h.hash_password("tea-and-toast")
    assert stored.startswith("scrypt$") and "tea" not in stored and h.verify_password("tea-and-toast", stored)
    assert not h.verify_password("tea-and-toasT", stored)
    c = TestClient(A.app)
    c.post("/api/auth/register", json=dict(username="meera", password="first-password"))
    row = A.STORE._db.execute("SELECT pw FROM accounts WHERE username='meera'").fetchone()
    assert row["pw"].startswith("scrypt$") and "first-password" not in row["pw"]
    assert "not correct" in c.post("/api/auth/password", json=dict(current="nope-nope", new="second-password")).json()["error"]["message"]
    assert c.post("/api/auth/password", json=dict(current="first-password", new="second-password")).json()["ok"]
    assert not c.get("/api/me").json()["signed_in"]                # all sessions ended
    assert TestClient(A.app).post("/api/auth/login", json=dict(username="meera", password="second-password")).json()["ok"]


def test_guest_documents_expire_but_account_documents_stay(A, tmp_path, monkeypatch):
    g, m = TestClient(A.app), TestClient(A.app)
    gd = _process(g, tmp_path, "guest.pdf")
    m.post("/api/auth/register", json=dict(username="keeper", password="keep-it-safe"))
    md = _process(m, tmp_path, "member.pdf")
    old = time.time() - 30 * 3600
    A.STORE._db.execute("UPDATE docs SET created=?", (old,))
    A.STORE._db.commit()
    A._sweep_guest_docs(force=True)
    assert not (A.DATA / gd).exists() and g.get("/api/history").json()["documents"] == []
    assert (A.DATA / md).exists() and [d["doc_id"] for d in m.get("/api/history").json()["documents"]] == [md]


def test_login_page_is_served(A):
    t = TestClient(A.app).get("/login").text
    assert "Create account" in t and "Continue without an account" in t
