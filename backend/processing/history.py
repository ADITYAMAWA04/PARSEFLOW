"""Accounts (username + password), document history and saved documents.

Storage
  * Local / one server with a disk: one SQLite file (`parseflow.db` in ParseFlow's data folder). Documents
    stay in their folders on that disk.
  * Cloud (Vercel, Render, Railway, several instances, or a disk that is wiped on restart): set
    DATABASE_URL (or POSTGRES_URL) to a PostgreSQL database, e.g. a free Neon or Supabase database.
    Accounts, sessions, history AND each document's results (JSON, Markdown, source file) are then kept
    in the database, so any instance can open them, and nothing is lost when the server restarts.
    The local folder becomes a cache that is refilled from the database when needed.

Who sees what
  * Guests: documents are listed for their browser only (anonymous cookie `pf_anon`) and deleted after
    GUEST_RETENTION_HOURS (default 24).
  * Signed-in users: documents are kept in their account until they delete them, visible from any
    browser after logging in, and private to them. Logging in moves the browser's guest documents into
    the account.
  * Passwords are stored as scrypt hashes with a random salt (Python standard library). Sessions are
    random tokens; only their SHA-256 is stored.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ANON_COOKIE, SESSION_COOKIE = "pf_anon", "pf_session"
SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, owner TEXT, filename TEXT, pages INTEGER,
    status TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS docs_owner ON docs(owner, created);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, pw TEXT,
    created REAL, last_login REAL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, account_id TEXT, created REAL, expires REAL);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
CREATE TABLE IF NOT EXISTS files(doc_id TEXT, name TEXT, data BLOB, updated REAL, PRIMARY KEY(doc_id, name));
"""

_PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, owner TEXT, filename TEXT, pages INTEGER,
        status TEXT, created DOUBLE PRECISION, updated DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS docs_owner ON docs(owner, created)",
    """CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, username TEXT NOT NULL, pw TEXT,
        created DOUBLE PRECISION, last_login DOUBLE PRECISION)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS accounts_username ON accounts(lower(username))",
    """CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, account_id TEXT,
        created DOUBLE PRECISION, expires DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id)",
    """CREATE TABLE IF NOT EXISTS files(doc_id TEXT, name TEXT, data BYTEA, updated DOUBLE PRECISION,
        PRIMARY KEY(doc_id, name))""",
]


# library columns added in v0.9.1 (older databases are upgraded in place)
_EXTRA_DOC_COLS = [("summary", "TEXT"), ("starred", "INTEGER DEFAULT 0"), ("share", "TEXT"), ("size", "BIGINT")]
_DOC_COLS = "doc_id, owner, filename, pages, status, created, updated, summary, starred, share, size"


def database_url():
    for k in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING"):
        v = (os.getenv(k) or "").strip()
        if v.startswith(("postgres://", "postgresql://")):
            return v
    return ""


class _Postgres:
    """Small wrapper: SQLite-style '?' placeholders, dict rows, one shared connection that reconnects
    after the database closed it (serverless databases close idle connections)."""

    def __init__(self, url):
        import psycopg                                   # psycopg[binary] is in requirements.txt
        from psycopg.rows import dict_row
        self._psycopg, self._rows, self.url = psycopg, dict_row, url
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self.url, autocommit=True, row_factory=self._rows,
                                               connect_timeout=15, prepare_threshold=None)
        return self._conn

    def run(self, sql, args=(), fetch=None):
        sql = sql.replace("%", "%%").replace("?", "%s")
        with self._lock:
            for attempt in (0, 1):
                try:
                    with self._connect().cursor() as cur:
                        cur.execute(sql, args)
                        if fetch == "one":
                            return cur.fetchone()
                        if fetch == "all":
                            return cur.fetchall()
                        return cur.rowcount
                except self._psycopg.OperationalError:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                    if attempt:
                        raise


class Store:
    def __init__(self, data_dir, url=None):
        d = Path(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.url = database_url() if url is None else url
        self.remote = bool(self.url)                     # results also kept in the database
        self._lock = threading.Lock()
        if self.remote:
            self.kind = "postgres"
            self._pg = _Postgres(self.url)
            for stmt in _PG_SCHEMA:
                self._pg.run(stmt)
            for col, typ in _EXTRA_DOC_COLS:
                self._pg.run(f"ALTER TABLE docs ADD COLUMN IF NOT EXISTS {col} {typ}")
            self._db = None
        else:
            self.kind = "sqlite"
            self.path = d / "parseflow.db"
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            with self._lock:
                self._db.executescript(_SQLITE_SCHEMA)
                have = {r[1] for r in self._db.execute("PRAGMA table_info(docs)")}
                for col, typ in _EXTRA_DOC_COLS:
                    if col not in have:
                        self._db.execute(f"ALTER TABLE docs ADD COLUMN {col} {typ.replace('BIGINT', 'INTEGER')}")
                self._db.commit()

    # ------------------------------------------------------------------ one place that talks SQL
    def _run(self, sql, args=(), fetch=None):
        if self.remote:
            r = self._pg.run(sql, args, fetch)
            if fetch == "one":
                return dict(r) if r else None
            if fetch == "all":
                return [dict(x) for x in r]
            return r
        with self._lock:
            cur = self._db.execute(sql, args)
            if fetch == "one":
                r = cur.fetchone()
                return dict(r) if r else None
            if fetch == "all":
                return [dict(x) for x in cur.fetchall()]
            self._db.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ documents
    def add_doc(self, doc_id, owner, filename, pages, status="processing"):
        now = time.time()
        self._run("INSERT INTO docs(doc_id, owner, filename, pages, status, created, updated, starred) VALUES(?,?,?,?,?,?,?,0) "
                  "ON CONFLICT(doc_id) DO UPDATE SET owner=excluded.owner, filename=excluded.filename, pages=excluded.pages, "
                  "status=excluded.status, updated=excluded.updated", (doc_id, owner, filename, pages, status, now, now))

    def update_doc(self, doc_id, **kw):
        kw = {k: v for k, v in kw.items() if k in ("owner", "filename", "pages", "status", "summary", "starred", "share", "size")}
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        self._run(f"UPDATE docs SET {cols}, updated=? WHERE doc_id=?", (*kw.values(), time.time(), doc_id))

    def docs(self, owner, limit=200):
        return self._run(f"SELECT {_DOC_COLS} FROM docs WHERE owner=? ORDER BY created DESC LIMIT ?", (owner, limit), "all")

    def doc(self, doc_id):
        return self._run(f"SELECT {_DOC_COLS} FROM docs WHERE doc_id=?", (doc_id,), "one")

    def doc_by_share(self, token):
        if not token or len(token) > 64:
            return None
        return self._run(f"SELECT {_DOC_COLS} FROM docs WHERE share=?", (token,), "one")

    def delete_account(self, uid):
        self._run("DELETE FROM sessions WHERE account_id=?", (uid,))
        self._run("DELETE FROM accounts WHERE id=?", (uid,))

    def owner_of(self, doc_id):
        r = self._run("SELECT owner FROM docs WHERE doc_id=?", (doc_id,), "one")
        return r["owner"] if r else None

    def delete_doc(self, doc_id):
        self._run("DELETE FROM files WHERE doc_id=?", (doc_id,))
        self._run("DELETE FROM docs WHERE doc_id=?", (doc_id,))

    # ------------------------------------------------------------------ document results in the database
    def put_files(self, doc_id, files):
        """files: {name: bytes}. Only used with a database URL (otherwise the disk already keeps them)."""
        if not self.remote:
            return
        now = time.time()
        for name, data in files.items():
            self._run("INSERT INTO files VALUES(?,?,?,?) ON CONFLICT(doc_id, name) DO UPDATE SET data=excluded.data, "
                      "updated=excluded.updated", (doc_id, name, data, now))

    def has_files(self, doc_id):
        if not self.remote:
            return False
        return bool(self._run("SELECT 1 AS x FROM files WHERE doc_id=? LIMIT 1", (doc_id,), "one"))

    def get_files(self, doc_id):
        if not self.remote:
            return {}
        return {r["name"]: bytes(r["data"]) for r in
                self._run("SELECT name, data FROM files WHERE doc_id=?", (doc_id,), "all")}

    # ------------------------------------------------------------------ accounts and sessions
    def create_account(self, username, password):
        """-> account id. Raises ValueError with a message for the user."""
        problem = check_new_credentials(username, password)
        if problem:
            raise ValueError(problem)
        if self._run("SELECT id FROM accounts WHERE lower(username)=lower(?)", (username,), "one"):
            raise ValueError("That username is taken. Choose another one, or log in.")
        uid = secrets.token_hex(8)
        try:
            self._run("INSERT INTO accounts VALUES(?,?,?,?,?)", (uid, username, hash_password(password), time.time(), time.time()))
        except Exception as e:                           # two sign-ups with one name at the same moment
            if "unique" in str(e).lower() or isinstance(e, sqlite3.IntegrityError):
                raise ValueError("That username is taken. Choose another one, or log in.")
            raise
        return uid

    def check_password(self, uid, password):
        r = self._run("SELECT pw FROM accounts WHERE id=?", (uid,), "one")
        return bool(r) and verify_password(password or "", r["pw"])

    def check_login(self, username, password):
        """-> account id, or None (wrong username or password; the same answer for both)."""
        r = self._run("SELECT id, pw FROM accounts WHERE lower(username)=lower(?)", ((username or "").strip(),), "one")
        if not r:
            verify_password(password or "", _DUMMY_HASH)          # same work either way: no username probing by timing
            return None
        if not verify_password(password or "", r["pw"]):
            return None
        self._run("UPDATE accounts SET last_login=? WHERE id=?", (time.time(), r["id"]))
        return r["id"]

    def account(self, uid):
        return self._run("SELECT id, username, created FROM accounts WHERE id=?", (uid,), "one")

    def new_session(self, uid):
        token = secrets.token_urlsafe(32)
        self._run("INSERT INTO sessions VALUES(?,?,?,?)", (_sha(token), uid, time.time(), time.time() + SESSION_DAYS * 86400))
        return token

    def session_account(self, token):
        if not token or len(token) > 100:
            return None
        r = self._run("SELECT account_id, expires FROM sessions WHERE token_hash=?", (_sha(token),), "one")
        if not r or r["expires"] < time.time():
            return None
        return self.account(r["account_id"])

    def end_session(self, token):
        if token:
            self._run("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))

    def change_password(self, uid, old, new):
        r = self._run("SELECT pw FROM accounts WHERE id=?", (uid,), "one")
        if not r or not verify_password(old or "", r["pw"]):
            raise ValueError("The current password is not correct.")
        problem = check_new_credentials("valid_name", new)
        if problem:
            raise ValueError(problem)
        self._run("UPDATE accounts SET pw=? WHERE id=?", (hash_password(new), uid))
        self._run("DELETE FROM sessions WHERE account_id=?", (uid,))

    def claim(self, from_owner, to_owner):
        """Move a browser's guest documents into an account after logging in."""
        return self._run("UPDATE docs SET owner=? WHERE owner=?", (to_owner, from_owner))

    def expired_guest_docs(self, hours):
        cutoff = time.time() - hours * 3600
        rows = self._run("SELECT doc_id FROM docs WHERE owner LIKE 'anon:%' AND created < ?", (cutoff,), "all")
        return [r["doc_id"] for r in rows]


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


_SCRYPT = dict(n=2 ** 14, r=8, p=1)


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${h.hex()}"


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), dklen=len(h) // 2, n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


_DUMMY_HASH = hash_password("not-a-real-password")


def check_new_credentials(username, password):
    if not USERNAME_RE.match(username or ""):
        return "Usernames are 3 to 32 characters: letters, digits, dot, dash or underscore."
    if not password or len(password) < 8:
        return "Use a password of at least 8 characters."
    if len(password) > 200:
        return "That password is too long."
    if password.lower() in (username or "").lower() or (username or "").lower() in password.lower():
        return "The password must not contain the username."
    return None
