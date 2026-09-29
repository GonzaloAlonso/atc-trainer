"""Coaching data: sessions, graded decisions, recordings for replay, and coach chats (SQLite).

A session is one sitting in one context: a tutorial or exercise sandbox (with a recording of the
traffic for the 3D replay), or a day of live traffic. Entries are graded decisions (see journal.py).
"""

import json
import sqlite3
import threading
import time
import zlib

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    context TEXT NOT NULL,              -- live | tutorial | exercise
    exercise TEXT,
    day TEXT,                           -- live sessions: one per user and UTC day
    parent INTEGER,                     -- the attempt this one rewound from
    started REAL NOT NULL,
    ended REAL,
    sim_t0 REAL,
    sim_t1 REAL,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user, started);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL,
    kind TEXT NOT NULL,
    opened_t REAL NOT NULL,
    closed_t REAL,
    score INTEGER,
    letter TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_session ON entries (session, opened_t);
CREATE TABLE IF NOT EXISTS recordings (
    session INTEGER PRIMARY KEY,
    frames BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL,
    role TEXT NOT NULL,                 -- user | coach
    text TEXT NOT NULL,
    created REAL NOT NULL
);
"""

SESSION_FIELDS = ("id", "user", "context", "exercise", "day", "parent", "started", "ended",
                  "sim_t0", "sim_t1", "status", "summary")


class CoachStore:
    def __init__(self, path=None):
        path = path or config.COACH_DB_PATH
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _exec(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).lastrowid

    @staticmethod
    def _session(row):
        s = dict(zip(SESSION_FIELDS, row))
        s["summary"] = json.loads(s["summary"]) if s["summary"] else None
        return s

    # ------------------------------------------------------------------ sessions
    def start_session(self, user, context, exercise=None, sim_t0=None, parent=None, day=None):
        return self._exec(
            "INSERT INTO sessions (user, context, exercise, day, parent, started, sim_t0) VALUES (?,?,?,?,?,?,?)",
            (user, context, exercise, day, parent, time.time(), sim_t0))

    def live_session(self, user, sim_t):
        """The user's live session for the UTC day of sim_t (created on first use)."""
        day = time.strftime("%Y-%m-%d", time.gmtime(sim_t))
        rows = self._q("SELECT id FROM sessions WHERE user = ? AND context = 'live' AND day = ?", (user, day))
        if rows:
            return rows[0][0]
        return self.start_session(user, "live", sim_t0=sim_t, day=day)

    def end_session(self, sid, status, sim_t1=None, summary=None):
        self._exec("UPDATE sessions SET ended = ?, status = ?, sim_t1 = COALESCE(?, sim_t1), "
                   "summary = COALESCE(?, summary) WHERE id = ?",
                   (time.time(), status, sim_t1, json.dumps(summary) if summary is not None else None, sid))

    def touch(self, sid, sim_t1):
        self._exec("UPDATE sessions SET ended = ?, sim_t1 = ? WHERE id = ?", (time.time(), sim_t1, sid))

    def session(self, sid):
        rows = self._q("SELECT %s FROM sessions WHERE id = ?" % ", ".join(SESSION_FIELDS), (sid,))
        return self._session(rows[0]) if rows else None

    def sessions(self, user=None, limit=50):
        if user is None:
            rows = self._q("SELECT %s FROM sessions ORDER BY started DESC LIMIT ?" % ", ".join(SESSION_FIELDS),
                           (limit,))
        else:
            rows = self._q("SELECT %s FROM sessions WHERE user = ? ORDER BY started DESC LIMIT ?"
                           % ", ".join(SESSION_FIELDS), (user, limit))
        return [self._session(r) for r in rows]

    # ------------------------------------------------------------------ graded decisions
    def add_entry(self, sid, e):
        return self._exec("INSERT INTO entries (session, kind, opened_t, closed_t, score, letter, data) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (sid, e["kind"], e["opened_t"], e.get("closed_t"),
                           (e.get("grade") or {}).get("score"), (e.get("grade") or {}).get("letter"),
                           json.dumps(e, separators=(",", ":"))))

    def entries(self, sid):
        return [json.loads(r[0]) for r in self._q("SELECT data FROM entries WHERE session = ? ORDER BY opened_t",
                                                  (sid,))]

    # ------------------------------------------------------------------ recordings
    def save_recording(self, sid, frames):
        blob = zlib.compress(json.dumps(frames, separators=(",", ":")).encode(), 6)
        self._exec("INSERT OR REPLACE INTO recordings (session, frames) VALUES (?, ?)", (sid, blob))

    def recording(self, sid):
        rows = self._q("SELECT frames FROM recordings WHERE session = ?", (sid,))
        return json.loads(zlib.decompress(rows[0][0])) if rows else None

    # ------------------------------------------------------------------ coach conversations
    def add_chat(self, sid, role, text):
        return self._exec("INSERT INTO chats (session, role, text, created) VALUES (?,?,?,?)",
                          (sid, role, text, time.time()))

    def chats(self, sid):
        return [{"role": r[0], "text": r[1], "created": r[2]}
                for r in self._q("SELECT role, text, created FROM chats WHERE session = ? ORDER BY id", (sid,))]

    def chats_today(self, user):
        """How many questions the user asked the coach in the last 24 h (daily budget)."""
        rows = self._q("SELECT COUNT(*) FROM chats c JOIN sessions s ON s.id = c.session "
                       "WHERE s.user = ? AND c.role = 'user' AND c.created > ?", (user, time.time() - 86400))
        return rows[0][0]
