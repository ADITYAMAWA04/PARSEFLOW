"""Cloud storage: accounts and saved documents in PostgreSQL, so they survive restarts and work across
server instances (Vercel, Render free tier). Needs a database: set PARSEFLOW_TEST_DATABASE_URL."""
import importlib
import os
import time

import pytest
from fastapi.testclient import TestClient

from conftest import para_lines, text_pdf

PG = os.getenv("PARSEFLOW_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG, reason="set PARSEFLOW_TEST_DATABASE_URL to a scratch PostgreSQL database")


def _fresh_db():
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        for t in ("docs", "accounts", "sessions", "files"):
            c.execute(f"DROP TABLE IF EXISTS {t}")


def _instance(monkeypatch, tmp_path, name, serverless=False):
    """A new server process with its own (empty) disk, sharing the database."""
    monkeypatch.setenv("PARSEFLOW_DATA_DIR", str(tmp_path / name))
    monkeypatch.setenv("DATABASE_URL", PG)
    if serverless:
        monkeypatch.setenv("PARSEFLOW_MODE", "sync")
    else:
        monkeypatch.delenv("PARSEFLOW_MODE", raising=False)
    import app
    return importlib.reload(app)


def _pdf(tmp_path, name="notes.pdf"):
    items = [(72, 60, "Field Notes", 18)] + [(72, 100 + 14 * k, t, 11) for k, t in enumerate(
        para_lines("The river gauge read two metres on Monday and fell slowly through the week. " * 3))]
    return text_pdf(tmp_path / name, [items])


def _wait(c, doc):
    t0 = time.time()
    while c.get(f"/api/status/{doc}").json()["status"] == "running":
        assert time.time() - t0 < 60
        time.sleep(0.2)


def test_account_and_documents_survive_a_restart_and_other_instances(monkeypatch, tmp_path):
    _fresh_db()
    A = _instance(monkeypatch, tmp_path, "server1")
    assert A.STORE.kind == "postgres" and A.DURABLE
    a = TestClient(A.app)
    assert a.post("/api/auth/register", json=dict(username="asha_r", password="river-gauge-42")).json()["ok"]
    with open(_pdf(tmp_path), "rb") as f:
        doc = a.post("/api/process", files={"file": ("notes.pdf", f, "application/pdf")}).json()["document_id"]
    _wait(a, doc)
    md1 = a.get(f"/api/document/{doc}/markdown").text
    assert "river gauge" in md1

    # the server restarts with an empty disk (Render free tier) or another instance answers (Vercel)
    B = _instance(monkeypatch, tmp_path, "server2")
    assert not (B.DATA / doc).exists()
    b = TestClient(B.app)
    assert b.post("/api/auth/login", json=dict(username="asha_r", password="river-gauge-42")).json()["ok"]
    h = b.get("/api/history").json()
    assert [d["doc_id"] for d in h["documents"]] == [doc] and h["documents"][0]["available"]
    assert b.get(f"/api/document/{doc}/markdown").text == md1
    assert b.get(f"/api/document/{doc}").json()["blocks"]
    assert b.get(f"/api/page/{doc}/1.jpg?w=200").status_code == 200        # the source file was kept too
    assert TestClient(B.app).get(f"/api/document/{doc}").status_code == 404  # still private

    # the old session cookie also works on the new instance
    c = TestClient(B.app)
    c.cookies.set("pf_session", a.cookies.get("pf_session"))
    assert c.get("/api/me").json()["signed_in"]
    assert b.post("/api/auth/logout").json()["ok"] and not b.get("/api/me").json()["signed_in"]
    assert b.delete(f"/api/history/{doc}").status_code == 404           # logged out: not yours any more
    assert c.delete(f"/api/history/{doc}").json()["ok"]
    assert not B.STORE.has_files(doc)


def test_serverless_with_database_keeps_accounts(monkeypatch, tmp_path):
    _fresh_db()
    A = _instance(monkeypatch, tmp_path, "lambda1", serverless=True)
    assert A.SERVERLESS and A.DURABLE
    a = TestClient(A.app)
    assert a.post("/api/auth/register", json=dict(username="meera", password="first-password")).json()["ok"]
    with open(_pdf(tmp_path, "m.pdf"), "rb") as f:
        r = a.post("/api/process", files={"file": ("m.pdf", f, "application/pdf")}).json()
    assert r["status"] == "complete"
    B = _instance(monkeypatch, tmp_path, "lambda2", serverless=True)
    b = TestClient(B.app)
    b.cookies.set("pf_session", a.cookies.get("pf_session"))
    assert [d["doc_id"] for d in b.get("/api/history").json()["documents"]] == [r["document_id"]]
    assert "river gauge" in b.get(f"/api/document/{r['document_id']}/markdown").text


def test_serverless_without_database_says_accounts_are_off(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PARSEFLOW_DATA_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("PARSEFLOW_MODE", "sync")
    import app
    A = importlib.reload(app)
    c = TestClient(A.app)
    assert c.get("/api/me").json()["accounts"] is False
    assert c.post("/api/auth/register", json=dict(username="zed", password="long-password")).status_code == 403
