"""
storage.py - tiny SQLite persistence layer (no Streamlit imports).

Stores:
  * snapshot  - the full scheduler state + approval status (survives refreshes / restarts, shared by all users)
  * audit     - append-only audit log (agent plans, tool calls, rollbacks, approvals, logins)
  * meta      - the per-installation secret used to derive access codes
  * auth_fail - failed-login timestamps for lock-outs

If the database file cannot be used (read-only disk, etc.) every function degrades to an in-memory fallback,
so the app keeps working - it just stops persisting.
"""
import json
import os
import secrets
import sqlite3
import threading
import time
import datetime

DB_PATH = os.environ.get("JKLU_DB_PATH", "jklu_exam.db")

_lock = threading.Lock()
_mem = {"snapshot": None, "audit": [], "meta": {}, "fail": []}      # fallback when SQLite is unusable
_use_mem = False


# ---------------------------------------------------------------- state <-> JSON
def encode_state(state):
    """Scheduler state -> JSON-safe dict (tuples and sets become lists)."""
    s = dict(state)
    s["plan"] = {c: list(sl) for c, sl in state.get("plan", {}).items()}
    s["leave"] = {f: sorted(v) for f, v in state.get("leave", {}).items()}
    return s


def decode_state(data):
    s = dict(data)
    s["plan"] = {c: tuple(sl) for c, sl in data.get("plan", {}).items()}
    s["leave"] = {f: set(v) for f, v in data.get("leave", {}).items()}
    return s


# ---------------------------------------------------------------- connection
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT, base_state TEXT,"
                " status TEXT, version INTEGER, published_at TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, detail TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS auth_fail (key TEXT, ts REAL)")
    return con


def _run(fn_db, fn_mem):
    """Run against SQLite; on any database problem switch to the in-memory fallback."""
    global _use_mem
    if not _use_mem:
        try:
            with _lock:
                con = _conn()
                try:
                    out = fn_db(con)
                    con.commit()
                    return out
                finally:
                    con.close()
        except sqlite3.Error:
            _use_mem = True
    with _lock:
        return fn_mem()


def using_memory_fallback():
    _run(lambda con: None, lambda: None)
    return _use_mem


# ---------------------------------------------------------------- snapshot
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def save_snapshot(state, base_state, status, version, published_at=""):
    payload = (json.dumps(encode_state(state)), json.dumps(encode_state(base_state)) if base_state else None,
               status, int(version), published_at or "", _now())

    def db(con):
        con.execute("INSERT INTO snapshot (id, state, base_state, status, version, published_at, updated_at) VALUES (1,?,?,?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET state=excluded.state, base_state=excluded.base_state, status=excluded.status,"
                    " version=excluded.version, published_at=excluded.published_at, updated_at=excluded.updated_at", payload)
        return payload[-1]

    def mem():
        _mem["snapshot"] = payload
        return payload[-1]
    return _run(db, mem)


def snapshot_sig():
    """Cheap change-detector (timestamp of the last save) so portals only reload when something changed."""
    def db(con):
        row = con.execute("SELECT updated_at FROM snapshot WHERE id=1").fetchone()
        return row[0] if row else None
    return _run(db, lambda: _mem["snapshot"][-1] if _mem["snapshot"] else None)


def load_snapshot():
    def unpack(row):
        if not row:
            return None
        state, base, status, version, pub, sig = row
        return {"state": decode_state(json.loads(state)), "base_state": decode_state(json.loads(base)) if base else None,
                "status": status, "version": version, "published_at": pub, "sig": sig}

    def db(con):
        return unpack(con.execute("SELECT state, base_state, status, version, published_at, updated_at FROM snapshot WHERE id=1").fetchone())
    return _run(db, lambda: unpack(_mem["snapshot"]))


def clear_snapshot():
    def db(con):
        con.execute("DELETE FROM snapshot")

    def mem():
        _mem["snapshot"] = None
    _run(db, mem)


# ---------------------------------------------------------------- audit log
def append_audit(actor, action, detail):
    row = (_now()[:19], str(actor), str(action), str(detail))

    def db(con):
        con.execute("INSERT INTO audit (ts, actor, action, detail) VALUES (?,?,?,?)", row)

    def mem():
        _mem["audit"].append(row)
    _run(db, mem)


def load_audit(limit=5000):
    cols = ["Time", "Actor", "Action", "Detail"]

    def db(con):
        rows = con.execute("SELECT ts, actor, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in reversed(rows)]
    return _run(db, lambda: [dict(zip(cols, r)) for r in _mem["audit"][-limit:]])


# ---------------------------------------------------------------- secret for access codes
def get_secret():
    """Per-installation secret. APP_SECRET env var wins; otherwise one is generated once and kept in the DB."""
    env = os.environ.get("APP_SECRET")
    if env:
        return env

    def db(con):
        row = con.execute("SELECT value FROM meta WHERE key='secret'").fetchone()
        if row:
            return row[0]
        val = secrets.token_hex(32)
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('secret', ?)", (val,))
        return con.execute("SELECT value FROM meta WHERE key='secret'").fetchone()[0]

    def mem():
        return _mem["meta"].setdefault("secret", secrets.token_hex(32))
    return _run(db, mem)


# ---------------------------------------------------------------- login lock-out
def record_failure(key):
    key, now = str(key)[:80], time.time()

    def db(con):
        con.execute("DELETE FROM auth_fail WHERE ts < ?", (now - 3600,))          # keep the table small
        con.execute("INSERT INTO auth_fail (key, ts) VALUES (?, ?)", (key, now))

    def mem():
        _mem["fail"] = [(k, t) for k, t in _mem["fail"] if t > now - 3600] + [(key, now)]
    _run(db, mem)


def recent_failures(key, window=300):
    key, since = str(key)[:80], time.time() - window

    def db(con):
        return con.execute("SELECT COUNT(*) FROM auth_fail WHERE key=? AND ts>?", (key, since)).fetchone()[0]
    return _run(db, lambda: sum(1 for k, t in _mem["fail"] if k == key and t > since))


def is_locked(key, max_fail=5, window=300):
    return recent_failures(key, window) >= max_fail


def clear_failures(key):
    key = str(key)[:80]

    def db(con):
        con.execute("DELETE FROM auth_fail WHERE key=?", (key,))

    def mem():
        _mem["fail"] = [(k, t) for k, t in _mem["fail"] if k != key]
    _run(db, mem)
