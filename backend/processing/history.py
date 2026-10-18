"""Accounts (username + password) and document history, in one SQLite database (`parseflow.db` in
ParseFlow's data folder; on a cloud server put that folder on a persistent disk).

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


class Store:
    def __init__(self, data_dir):
        d = Path(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / "parseflow.db"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, owner TEXT, filename TEXT, pages INTEGER,
                    status TEXT, created REAL, updated REAL);
                CREATE INDEX IF NOT EXISTS docs_owner ON docs(owner, created);
                CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, pw TEXT,
                    created REAL, last_login REAL);
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, account_id TEXT, created REAL, expires REAL);
                CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);""")
            self._db.commit()

    def add_doc(self, doc_id, owner, filename, pages, status="processing"):
        now = time.time()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?)", (doc_id, owner, filename, pages, status, now, now))
            self._db.commit()

    def update_doc(self, doc_id, **kw):
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        with self._lock:
            self._db.execute(f"UPDATE docs SET {cols}, updated=? WHERE doc_id=?", (*kw.values(), time.time(), doc_id))
            self._db.commit()

    def docs(self, owner, limit=200):
        with self._lock:
            rows = self._db.execute("SELECT doc_id, filename, pages, status, created, updated FROM docs WHERE owner=? "
                                    "ORDER BY created DESC LIMIT ?", (owner, limit)).fetchall()
        return [dict(r) for r in rows]

    def owner_of(self, doc_id):
        with self._lock:
            r = self._db.execute("SELECT owner FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        return r["owner"] if r else None

    def delete_doc(self, doc_id):
        with self._lock:
            self._db.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            self._db.commit()

    # ------------------------------------------------------------------ accounts and sessions
    def create_account(self, username, password):
        """-> account id. Raises ValueError with a message for the user."""
        problem = check_new_credentials(username, password)
        if problem:
            raise ValueError(problem)
        uid = secrets.token_hex(8)
        try:
            with self._lock:
                self._db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (uid, username, hash_password(password), time.time(), time.time()))
                self._db.commit()
        except sqlite3.IntegrityError:
            raise ValueError("That username is taken. Choose another one, or log in.")
        return uid

    def check_login(self, username, password):
        """-> account id, or None (wrong username or password; the same answer for both)."""
        with self._lock:
            r = self._db.execute("SELECT id, pw FROM accounts WHERE username=?", (username or "",)).fetchone()
        if not r:
            verify_password(password or "", _DUMMY_HASH)          # same work either way: no username probing by timing
            return None
        if not verify_password(password or "", r["pw"]):
            return None
        with self._lock:
            self._db.execute("UPDATE accounts SET last_login=? WHERE id=?", (time.time(), r["id"]))
            self._db.commit()
        return r["id"]

    def account(self, uid):
        with self._lock:
            r = self._db.execute("SELECT id, username, created FROM accounts WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def new_session(self, uid):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (_sha(token), uid, time.time(), time.time() + SESSION_DAYS * 86400))
            self._db.commit()
        return token

    def session_account(self, token):
        if not token or len(token) > 100:
            return None
        with self._lock:
            r = self._db.execute("SELECT account_id, expires FROM sessions WHERE token_hash=?", (_sha(token),)).fetchone()
        if not r or r["expires"] < time.time():
            return None
        return self.account(r["account_id"])

    def end_session(self, token):
        if token:
            with self._lock:
                self._db.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))
                self._db.commit()

    def change_password(self, uid, old, new):
        with self._lock:
            r = self._db.execute("SELECT pw FROM accounts WHERE id=?", (uid,)).fetchone()
        if not r or not verify_password(old or "", r["pw"]):
            raise ValueError("The current password is not correct.")
        problem = check_new_credentials("valid_name", new)
        if problem:
            raise ValueError(problem)
        with self._lock:
            self._db.execute("UPDATE accounts SET pw=? WHERE id=?", (hash_password(new), uid))
            self._db.execute("DELETE FROM sessions WHERE account_id=?", (uid,))
            self._db.commit()

    def claim(self, from_owner, to_owner):
        """Move a browser's guest documents into an account after logging in."""
        with self._lock:
            n = self._db.execute("UPDATE docs SET owner=? WHERE owner=?", (to_owner, from_owner)).rowcount
            self._db.commit()
        return n

    def expired_guest_docs(self, hours):
        cutoff = time.time() - hours * 3600
        with self._lock:
            rows = self._db.execute("SELECT doc_id FROM docs WHERE owner LIKE 'anon:%' AND created < ?", (cutoff,)).fetchall()
        return [r["doc_id"] for r in rows]


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


_SCRYPT = dict(n=2 ** 14, r=8, p=1)


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${h.hex()}"


def verify_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), dklen=len(h) // 2, n=int(n), r=int(r), p=int(p))
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
