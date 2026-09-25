"""Users, password hashing, sessions and login throttling (SQLite, persisted in the data volume).

Command-line recovery (e.g. a forgotten admin password):

    python -m atc.auth list
    python -m atc.auth set-password <username> [--password P] [--role admin|controller] [--create]
"""

import argparse
import base64
import getpass
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque

from . import config

log = logging.getLogger("visor.auth")

ROLES = ("admin", "controller")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 10
PBKDF2_ITERATIONS = 600_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'controller')),
    disabled INTEGER NOT NULL DEFAULT 0,
    must_change INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
"""

USER_FIELDS = "id, username, role, disabled, must_change, created, updated, last_login"


class AuthError(ValueError):
    """Invalid input or a forbidden change (message is safe to show to users)."""


# ---------------------------------------------------------------------------- hashing
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, base64.b64encode(salt).decode(),
                                       base64.b64encode(dk).decode())


def verify_password(password, encoded):
    try:
        algo, iterations, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(dk, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = None


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    return _DUMMY_HASH


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def check_username(username):
    if not username or not USERNAME_RE.match(username):
        raise AuthError("username must be 3-32 characters: letters, digits, . _ -")


def check_password(password):
    if not password or len(password) < MIN_PASSWORD:
        raise AuthError("password must be at least %d characters" % MIN_PASSWORD)
    if len(password) > 256:
        raise AuthError("password is too long")


def check_role(role):
    if role not in ROLES:
        raise AuthError("role must be one of: %s" % ", ".join(ROLES))


# ---------------------------------------------------------------------------- store
class AuthStore:
    def __init__(self, path=None):
        path = path or config.USERS_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, username=None, password=None):
        """Create the first admin if there are no users. Returns the generated password, if any."""
        if self._one("SELECT COUNT(*) AS n FROM users")["n"]:
            return None
        username = username or config.ADMIN_USER
        password = password if password is not None else config.ADMIN_PASSWORD
        generated = None
        if not password:
            generated = password = secrets.token_urlsafe(12)
        self.create_user(username, password, "admin", must_change=bool(generated))
        if generated:
            log.warning(
                "\n" + "=" * 64 +
                "\n  Created admin account '%s' with a generated password:\n\n      %s\n\n"
                "  It must be changed at first login. Set VISOR_ADMIN_PASSWORD to\n"
                "  choose it yourself on a fresh install.\n" % (username, generated) + "=" * 64)
        else:
            log.info("created admin account '%s' from VISOR_ADMIN_USER/VISOR_ADMIN_PASSWORD", username)
        return generated

    # ------------------------------------------------------------------ users
    def list_users(self):
        return [dict(r) for r in self._q("SELECT %s FROM users ORDER BY username" % USER_FIELDS)]

    def get_user(self, user_id):
        return self._one("SELECT %s FROM users WHERE id = ?" % USER_FIELDS, (user_id,))

    def find_user(self, username):
        return self._one("SELECT %s FROM users WHERE username = ?" % USER_FIELDS, (username,))

    def admin_count(self, exclude_id=None):
        return self._one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                         (exclude_id if exclude_id is not None else -1,))["n"]

    def create_user(self, username, password, role="controller", must_change=False):
        check_username(username)
        check_password(password)
        check_role(role)
        now = time.time()
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO users (username, password_hash, role, must_change, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, hash_password(password), role, int(must_change), now, now))
        except sqlite3.IntegrityError:
            raise AuthError("username '%s' is already taken" % username)
        return self.get_user(cur.lastrowid)

    def update_user(self, user_id, actor_id=None, role=None, password=None, disabled=None, must_change=None):
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("no such user")
        losing_admin = user["role"] == "admin" and not user["disabled"] and (
            (role is not None and role != "admin") or disabled)
        if losing_admin and self.admin_count(exclude_id=user_id) == 0:
            raise AuthError("there must be at least one active admin")
        if actor_id == user_id and (disabled or (role is not None and role != user["role"])):
            raise AuthError("you can't disable yourself or change your own role")
        sets, args = [], []
        if role is not None:
            check_role(role)
            sets.append("role = ?"); args.append(role)
        if password is not None:
            check_password(password)
            sets.append("password_hash = ?"); args.append(hash_password(password))
        if disabled is not None:
            sets.append("disabled = ?"); args.append(int(bool(disabled)))
        if must_change is not None:
            sets.append("must_change = ?"); args.append(int(bool(must_change)))
        if sets:
            sets.append("updated = ?"); args.append(time.time())
            self._q("UPDATE users SET %s WHERE id = ?" % ", ".join(sets), (*args, user_id))
        if disabled or password is not None or (role is not None and role != user["role"]):
            self.revoke_user_sessions(user_id)
        return self.get_user(user_id)

    def delete_user(self, user_id, actor_id=None):
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("no such user")
        if actor_id == user_id:
            raise AuthError("you can't delete your own account")
        if user["role"] == "admin" and not user["disabled"] and self.admin_count(exclude_id=user_id) == 0:
            raise AuthError("there must be at least one active admin")
        self._q("DELETE FROM users WHERE id = ?", (user_id,))

    def authenticate(self, username, password):
        row = self._q("SELECT id, password_hash, disabled FROM users WHERE username = ?", (username or "",))
        if not row:
            verify_password(password or "", _dummy_hash())    # same cost: don't reveal usernames
            return None
        row = row[0]
        if not verify_password(password or "", row["password_hash"]) or row["disabled"]:
            return None
        self._q("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), row["id"]))
        return self.get_user(row["id"])

    def change_own_password(self, user_id, current, new):
        row = self._q("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        if not row or not verify_password(current or "", row[0]["password_hash"]):
            raise AuthError("current password is incorrect")
        if current == new:
            raise AuthError("choose a different password")
        return self.update_user(user_id, password=new, must_change=False)

    # ------------------------------------------------------------------ sessions
    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._q("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), user_id, now, now + config.SESSION_HOURS * 3600, now))
        return token

    def session_user(self, token):
        if not token:
            return None
        th = _token_hash(token)
        row = self._one(
            "SELECT s.expires, s.last_seen, %s FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?" % ", ".join("u." + f.strip() for f in USER_FIELDS.split(",")), (th,))
        now = time.time()
        if row is None or row["expires"] < now or row["disabled"]:
            return None
        if now - row["last_seen"] > 60:
            self._q("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, th))
        return {k: row[k] for k in (f.strip() for f in USER_FIELDS.split(","))}

    def revoke_session(self, token):
        if token:
            self._q("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def revoke_user_sessions(self, user_id, keep_token=None):
        if keep_token:
            self._q("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _token_hash(keep_token)))
        else:
            self._q("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired(self):
        self._q("DELETE FROM sessions WHERE expires < ?", (time.time(),))


# ---------------------------------------------------------------------------- throttling
class LoginThrottle:
    """Refuse logins after repeated failures (per username and per client address)."""

    def __init__(self, per_user=5, per_ip=20, window_s=300):
        self.limits = {"u": per_user, "ip": per_ip}
        self.window = window_s
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        q = self._fails[key]
        while q and q[0] < now - self.window:
            q.popleft()
        return q

    def retry_after(self, username, ip):
        now = time.time()
        with self._lock:
            for kind, key in (("u", "u:" + (username or "").lower()), ("ip", "ip:" + ip)):
                q = self._prune(key, now)
                if len(q) >= self.limits[kind]:
                    return int(q[0] + self.window - now) + 1
        return 0

    def failed(self, username, ip):
        now = time.time()
        with self._lock:
            self._fails["u:" + (username or "").lower()].append(now)
            self._fails["ip:" + ip].append(now)

    def succeeded(self, username):
        with self._lock:
            self._fails.pop("u:" + (username or "").lower(), None)


# ---------------------------------------------------------------------------- CLI
def _cli():
    ap = argparse.ArgumentParser(prog="python -m atc.auth", description="Manage Visor ATC users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list users")
    sp = sub.add_parser("set-password", help="set a user's password (and optionally create/promote)")
    sp.add_argument("username")
    sp.add_argument("--password", help="omit to be prompted")
    sp.add_argument("--role", choices=ROLES)
    sp.add_argument("--create", action="store_true", help="create the user if it doesn't exist")
    args = ap.parse_args()
    store = AuthStore()
    if args.cmd == "list":
        for u in store.list_users():
            print("%-24s %-10s %s" % (u["username"], u["role"], "disabled" if u["disabled"] else ""))
        return
    password = args.password or getpass.getpass("New password for %s: " % args.username)
    try:
        user = store.find_user(args.username)
        if user is None:
            if not args.create:
                sys.exit("no user '%s' (use --create)" % args.username)
            store.create_user(args.username, password, args.role or "controller")
            print("created %s" % args.username)
        else:
            store.update_user(user["id"], password=password, role=args.role, disabled=False, must_change=False)
            print("updated %s (sessions revoked)" % args.username)
    except AuthError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    _cli()
