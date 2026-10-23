#!/usr/bin/env python3
"""
Piano Tasks - a small self-hosted, multi-user task server.

  * several users, each with a private task list
      - passwords hashed with PBKDF2-HMAC-SHA256, 200k iterations, per-user salt
      - server-side session tokens, accepted as a cookie *or* as an
        `Authorization: Bearer <token>` header (the Bearer path is what the
        phone / desktop apps will use later)
  * long-term tasks: one-off items with an optional due date, they stay until done
  * daily recurring tasks: reset every day, per-day history, streaks
  * zero third-party dependencies - Python standard library only

Run:   python3 server.py

Env:   PIANO_TASKS_HOST         default 0.0.0.0
       PIANO_TASKS_PORT         default 8080
       PIANO_TASKS_DB           default <script dir>/data/tasks.db
       PIANO_TASKS_SESSION_DAYS default 90
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

VERSION = "1.0.0"

BASE_DIR = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("PIANO_TASKS_DB") or os.path.join(BASE_DIR, "data", "tasks.db")
HOST = os.environ.get("PIANO_TASKS_HOST", "0.0.0.0")
PORT = int(os.environ.get("PIANO_TASKS_PORT", "8080"))
SESSION_DAYS = int(os.environ.get("PIANO_TASKS_SESSION_DAYS", "90"))

PBKDF2_ITERATIONS = 200_000
COOKIE_NAME = "pt_session"
COOKIE_SECURE = os.environ.get("PIANO_TASKS_COOKIE_SECURE", "") == "1"

MAX_BODY = 256 * 1024
MAX_TITLE = 500
MAX_NOTES = 20_000
MAX_PASSWORD = 200
MIN_PASSWORD = 6
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RECURRENCES = ("none", "daily")
HISTORY_DAYS = 14
DAY_WINDOW = 730  # how far from today a client may read / write a day

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def resp(payload, status=200, headers=()):
    return (status, payload, list(headers))


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today():
    return date.today()


def parse_day(value):
    """'YYYY-MM-DD' -> date, or None if it is not a real calendar day."""
    if not isinstance(value, str) or not DAY_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def clamp_day(d, name="date"):
    t = today()
    if abs((d - t).days) > DAY_WINDOW:
        raise ApiError(400, "%s is outside the allowed range" % name)
    return d


def streak(done_days, ref):
    """Consecutive completed days ending at `ref` (or the day before, so a
    streak still shows during the current day until it is checked off)."""
    if not done_days:
        return 0
    d = ref
    if d.isoformat() not in done_days:
        d = d - timedelta(days=1)
    n = 0
    while d.isoformat() in done_days and n < 5000:
        n += 1
        d = d - timedelta(days=1)
    return n


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL COLLATE NOCASE UNIQUE,
    display_name  TEXT    NOT NULL DEFAULT '',
    pw_hash       TEXT    NOT NULL,
    pw_salt       TEXT    NOT NULL,
    iterations    INTEGER NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    label       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL,
    notes         TEXT    NOT NULL DEFAULT '',
    recurrence    TEXT    NOT NULL DEFAULT 'none',   -- 'none' | 'daily'
    due_date      TEXT,                              -- 'YYYY-MM-DD', one-off tasks
    sort_order    INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    completed_at  TEXT,                              -- one-off tasks only
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, archived);

CREATE TABLE IF NOT EXISTS task_days (
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day          TEXT    NOT NULL,                   -- 'YYYY-MM-DD'
    completed_at TEXT    NOT NULL,
    PRIMARY KEY (task_id, day)
);
CREATE INDEX IF NOT EXISTS idx_task_days_day ON task_days(day);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


class tx:
    """Explicit transaction; connections run in autocommit otherwise."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return False


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # housekeeping on boot
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        conn.execute("DELETE FROM task_days WHERE day < ?",
                     ((today() - timedelta(days=365 * 3)).isoformat(),))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# passwords & sessions
# --------------------------------------------------------------------------

def hash_password(password, salt_hex=None, iterations=PBKDF2_ITERATIONS):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return dk.hex(), salt.hex(), iterations


def check_password(password, stored_hash, stored_salt, iterations):
    try:
        salt = bytes.fromhex(stored_salt)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(dk.hex(), stored_hash)


def new_session(conn, user_id, label=""):
    token = secrets.token_urlsafe(32)
    expires = datetime.now().astimezone() + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at, label) VALUES (?,?,?,?,?)",
        (token, user_id, now_iso(), expires.isoformat(timespec="seconds"), label),
    )
    return token


# --------------------------------------------------------------------------
# input cleaning
# --------------------------------------------------------------------------

def clean_title(value):
    if not isinstance(value, str):
        raise ApiError(400, "title must be a string")
    value = " ".join(value.split())
    if not value:
        raise ApiError(400, "title cannot be empty")
    if len(value) > MAX_TITLE:
        raise ApiError(400, "title is too long (max %d characters)" % MAX_TITLE)
    return value


def clean_notes(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "notes must be a string")
    if len(value) > MAX_NOTES:
        raise ApiError(400, "notes are too long (max %d characters)" % MAX_NOTES)
    return value


def clean_recurrence(value):
    if value is None:
        return "none"
    if value not in RECURRENCES:
        raise ApiError(400, "recurrence must be one of: %s" % ", ".join(RECURRENCES))
    return value


def clean_due(value, name="due_date"):
    if value is None or value == "" or value is False:
        return None
    d = parse_day(value)
    if d is None:
        raise ApiError(400, "%s must be a date in YYYY-MM-DD form" % name)
    return d.isoformat()


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------

def serialize(row, ref, done_days, t):
    rec = row["recurrence"]
    out = {
        "id": row["id"],
        "title": row["title"],
        "notes": row["notes"],
        "recurrence": rec,
        "due_date": row["due_date"],
        "archived": bool(row["archived"]),
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if rec == "daily":
        days = done_days or set()
        out["done"] = ref.isoformat() in days
        out["streak"] = streak(days, ref)
        out["total_done"] = len(days)
        # oldest -> newest, ending on the requested day (for the little strip)
        out["history"] = [
            (ref - timedelta(days=n)).isoformat() in days
            for n in range(HISTORY_DAYS - 1, -1, -1)
        ]
        out["completed_at"] = None
    else:
        out["done"] = row["completed_at"] is not None
        out["completed_at"] = row["completed_at"]
        out["overdue"] = bool(
            row["due_date"] and row["completed_at"] is None and row["due_date"] < t.isoformat()
        )
        out["total_done"] = 1 if row["completed_at"] else 0
        out["streak"] = 0
        out["history"] = []
    return out


def load_tasks(conn, user_id, ref, include_archived=False):
    sql = "SELECT * FROM tasks WHERE user_id=?"
    params = [user_id]
    if not include_archived:
        sql += " AND archived=0"
    sql += " ORDER BY sort_order ASC, id ASC"
    rows = conn.execute(sql, params).fetchall()

    daily_ids = [r["id"] for r in rows if r["recurrence"] == "daily"]
    done = {}
    if daily_ids:
        marks = ",".join("?" * len(daily_ids))
        for r in conn.execute(
            "SELECT task_id, day FROM task_days WHERE task_id IN (%s)" % marks, daily_ids
        ):
            done.setdefault(r["task_id"], set()).add(r["day"])

    t = today()
    return [serialize(r, ref, done.get(r["id"]), t) for r in rows]


def summarise(tasks, ref):
    dailies = [x for x in tasks if x["recurrence"] == "daily"]
    longterm = [x for x in tasks if x["recurrence"] == "none"]
    open_long = [x for x in longterm if not x["done"]]
    t = today().isoformat()
    return {
        "date": ref.isoformat(),
        "server_today": t,
        "daily_total": len(dailies),
        "daily_done": sum(1 for x in dailies if x["done"]),
        "best_streak": max([x["streak"] for x in dailies] or [0]),
        "longterm_total": len(longterm),
        "longterm_open": len(open_long),
        "longterm_done": len(longterm) - len(open_long),
        "overdue": sum(1 for x in longterm if x.get("overdue")),
        "due_today": sum(
            1 for x in longterm if not x["done"] and x["due_date"] == t
        ),
    }


# --------------------------------------------------------------------------
# login throttle (per IP + username)
# --------------------------------------------------------------------------

_fails = {}
_fails_lock = threading.Lock()


def throttle(key, ok):
    """Returns the number of seconds the caller must wait before retrying."""
    with _fails_lock:
        if ok:
            _fails.pop(key, None)
            return 0
        rec = _fails.get(key)
        now = time.time()
        if rec is None or now - rec[1] > 900:
            rec = [0, now]
            _fails[key] = rec
        rec[0] += 1
        if rec[0] > 8:
            return min(900, 30 * (rec[0] - 8))
        return 0


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PianoTasks/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---- plumbing --------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s %s\n" % (now_iso(), self.client_address[0], fmt % args))

    def _cors(self):
        origin = self.headers.get("Origin")
        if not origin:
            return []
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Vary", "Origin"),
            ("Access-Control-Allow-Headers", "Authorization, Content-Type"),
            ("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"),
            ("Access-Control-Max-Age", "600"),
        ]

    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _read_body(self):
        """Consume the request body exactly once.

        Always called from _handle() before dispatch, so a handler that errors
        out early (401/404/400) can never leave unread bytes in the socket and
        desync the next request on a keep-alive connection - which is exactly
        what a browser reusing one connection would hit."""
        if self._body is not None:
            return self._body
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            self._body = b""
        elif n > MAX_BODY:
            # Refuse, but drain a bounded amount first so a well-behaved client
            # still reads the 413 instead of a broken pipe. Beyond this we can
            # no longer resynchronise the stream, so hang up.
            remaining = min(n, MAX_BODY * 4)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            raise ApiError(413, "request body is too large")
        else:
            self._body = self.rfile.read(n)
        return self._body

    def _read_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body must be valid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "request body must be a JSON object")
        return data

    def _cookie_header(self, token, max_age):
        bits = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            COOKIE_NAME, token, max_age)
        if COOKIE_SECURE:
            bits += "; Secure"
        return ("Set-Cookie", bits)

    def _bearer_or_cookie_token(self):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                return token
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
        return None

    def _user(self, required=True):
        token = self._bearer_or_cookie_token()
        if not token:
            if required:
                raise ApiError(401, "not signed in")
            return None
        conn = connect()
        try:
            row = conn.execute(
                """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token = ? AND s.expires_at > ?""",
                (token, now_iso()),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            if required:
                raise ApiError(401, "session expired, please sign in again")
            return None
        return row

    def _ref_day(self, value):
        if value is None or value == "":
            return today()
        d = parse_day(value)
        if d is None:
            raise ApiError(400, "date must be YYYY-MM-DD")
        return clamp_day(d)

    # ---- HTTP verbs ------------------------------------------------------

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_OPTIONS(self):
        self._send(204, b"", extra=self._cors())

    def _handle(self):
        started = time.time()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        status = 0
        try:
            # Android's HttpURLConnection refuses to send PATCH, and some older
            # proxies do too, so accept the conventional override header.
            override = self.headers.get("X-HTTP-Method-Override")
            if self.command == "POST" and override:
                override = override.strip().upper()
                if override in ("PATCH", "PUT", "DELETE"):
                    self.command = override
            self._body = None
            self._read_body()   # keep the connection in sync no matter what follows
            if path == "/health":
                status = 200
                self._send(200, {
                    "ok": True,
                    "service": "piano-tasks",
                    "version": VERSION,
                    "server_today": today().isoformat(),
                    "server_time": now_iso(),
                    "users": count_users(),
                }, extra=self._cors())
                return
            if path.startswith("/api/"):
                status, payload, headers = self._dispatch(path, query)
                self._send(status, payload, extra=self._cors() + headers)
                return
            if self.command in ("GET", "HEAD"):
                status = 200
                self._serve_static(path)
                return
            raise ApiError(405, "method not allowed")
        except ApiError as err:
            status = err.status
            try:
                self._send(err.status, {"error": err.message}, extra=self._cors())
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            status = -1
        except Exception:
            status = 500
            traceback.print_exc()
            try:
                self._send(500, {"error": "internal server error"}, extra=self._cors())
            except Exception:
                pass
        finally:
            if status and status != -1:
                sys.stderr.write("[%s] %s %s -> %s (%.0f ms) %s\n" % (
                    now_iso(), self.command, path, status,
                    (time.time() - started) * 1000.0,
                    self.headers.get("X-Client") or "",
                ))

    def _dispatch(self, path, query):
        method = self.command

        if path == "/api/register" and method == "POST":
            return self.api_register()
        if path == "/api/login" and method == "POST":
            return self.api_login()
        if path == "/api/logout" and method == "POST":
            return self.api_logout()
        if path == "/api/password" and method == "POST":
            return self.api_password()

        if path == "/api/me" and method == "GET":
            return self.api_me(query)
        if path == "/api/tasks" and method == "GET":
            return self.api_list(query)
        if path == "/api/tasks" and method == "POST":
            return self.api_create()
        if path == "/api/summary" and method == "GET":
            return self.api_summary(query)
        if path == "/api/export" and method == "GET":
            return self.api_export()

        m = re.match(r"^/api/tasks/(\d+)$", path)
        if m:
            task_id = int(m.group(1))
            if method == "GET":
                return self.api_get(task_id, query)
            if method == "PATCH":
                return self.api_edit(task_id)
            if method == "DELETE":
                return self.api_delete(task_id)

        m = re.match(r"^/api/tasks/(\d+)/toggle$", path)
        if m and method == "POST":
            return self.api_toggle(int(m.group(1)))

        raise ApiError(404, "no such endpoint: %s %s" % (method, path))

    # ---- static files ----------------------------------------------------

    def _serve_static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if target != STATIC_DIR and not target.startswith(STATIC_DIR + os.sep):
            raise ApiError(403, "forbidden")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            raise ApiError(404, "not found: %s" % path)
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype=MIME.get(ext, "application/octet-stream"))

    # ---- auth endpoints --------------------------------------------------

    def api_register(self):
        body = self._read_json()
        username = str(body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        display = str(body.get("display_name") or "").strip()[:60] or username

        if not USERNAME_RE.match(username):
            raise ApiError(400, "username must be 3-32 characters of a-z 0-9 . _ - "
                                "and start with a letter or digit")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise ApiError(400, "password must be at least %d characters" % MIN_PASSWORD)
        if len(password) > MAX_PASSWORD:
            raise ApiError(400, "password is too long")

        pw_hash, pw_salt, iterations = hash_password(password)
        conn = connect()
        try:
            with tx(conn):
                first = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
                try:
                    cur = conn.execute(
                        """INSERT INTO users(username, display_name, pw_hash, pw_salt,
                                             iterations, is_admin, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (username, display, pw_hash, pw_salt, iterations,
                         1 if first else 0, now_iso()),
                    )
                except sqlite3.IntegrityError:
                    raise ApiError(409, "that username is already taken")
                user_id = cur.lastrowid
                token = new_session(conn, user_id)
        finally:
            conn.close()

        sys.stderr.write("[%s] new account: %s\n" % (now_iso(), username))
        return resp({
            "token": token,
            "user": {"id": user_id, "username": username, "display_name": display,
                     "is_admin": bool(first), "created_at": now_iso()},
            "server_today": today().isoformat(),
        }, status=201, headers=[self._cookie_header(token, SESSION_DAYS * 86400)])

    def api_login(self):
        body = self._read_json()
        username = str(body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        key = "%s|%s" % (self.client_address[0], username)

        conn = connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            good = bool(row) and isinstance(password, str) and check_password(
                password, row["pw_hash"], row["pw_salt"], row["iterations"])
            wait = throttle(key, good)
            if wait:
                raise ApiError(429, "too many failed attempts, wait %d seconds" % wait)
            if not good:
                sys.stderr.write("[%s] failed login for %r from %s\n"
                                 % (now_iso(), username, self.client_address[0]))
                raise ApiError(401, "wrong username or password")
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
            token = new_session(conn, row["id"], label=str(body.get("label") or "")[:60])
            user = {"id": row["id"], "username": row["username"],
                    "display_name": row["display_name"], "created_at": row["created_at"]}
        finally:
            conn.close()
        return resp({
            "token": token,
            "user": user,
            "server_today": today().isoformat(),
        }, headers=[self._cookie_header(token, SESSION_DAYS * 86400)])

    def api_logout(self):
        token = self._bearer_or_cookie_token()
        if token:
            conn = connect()
            try:
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            finally:
                conn.close()
        return resp({"ok": True},
                    headers=[self._cookie_header("", 0)])

    def api_password(self):
        user = self._user()
        body = self._read_json()
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        if not check_password(current, user["pw_hash"], user["pw_salt"], user["iterations"]):
            raise ApiError(403, "current password is wrong")
        if not isinstance(new, str) or len(new) < MIN_PASSWORD:
            raise ApiError(400, "password must be at least %d characters" % MIN_PASSWORD)
        if len(new) > MAX_PASSWORD:
            raise ApiError(400, "password is too long")
        pw_hash, pw_salt, iterations = hash_password(new)
        conn = connect()
        try:
            with tx(conn):
                conn.execute(
                    "UPDATE users SET pw_hash=?, pw_salt=?, iterations=? WHERE id=?",
                    (pw_hash, pw_salt, iterations, user["id"]),
                )
                # keep only the session doing the change
                keep = self._bearer_or_cookie_token()
                conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                             (user["id"], keep or ""))
        finally:
            conn.close()
        return resp({"ok": True})

    def _public_user(self, row):
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
        }

    def api_me(self, query):
        user = self._user(required=False)
        if user is None:
            return resp({"authenticated": False,
                         "server_today": today().isoformat()})
        return resp({"authenticated": True, "user": self._public_user(user),
                     "server_today": today().isoformat()})

    # ---- task endpoints --------------------------------------------------

    def api_list(self, query):
        user = self._user()
        ref = self._ref_day(query.get("date"))
        include_archived = query.get("archived") in ("1", "true", "yes")
        conn = connect()
        try:
            tasks = load_tasks(conn, user["id"], ref, include_archived)
        finally:
            conn.close()
        return resp({
            "date": ref.isoformat(),
            "server_today": today().isoformat(),
            "tasks": tasks,
            "summary": summarise(tasks, ref),
        })

    def api_summary(self, query):
        user = self._user()
        ref = self._ref_day(query.get("date"))
        conn = connect()
        try:
            tasks = load_tasks(conn, user["id"], ref)
        finally:
            conn.close()
        return resp(summarise(tasks, ref))

    def api_get(self, task_id, query):
        user = self._user()
        ref = self._ref_day(query.get("date"))
        conn = connect()
        try:
            task = self._owned_task(conn, user["id"], task_id)
            tasks = load_tasks(conn, user["id"], ref, include_archived=True)
        finally:
            conn.close()
        for item in tasks:
            if item["id"] == task["id"]:
                return resp(item)
        raise ApiError(404, "task not found")

    def api_create(self):
        user = self._user()
        body = self._read_json()
        title = clean_title(body.get("title"))
        notes = clean_notes(body.get("notes"))
        recurrence = clean_recurrence(body.get("recurrence"))
        due = clean_due(body.get("due_date"))
        if recurrence == "daily":
            due = None
        stamp = now_iso()
        conn = connect()
        try:
            with tx(conn):
                cur = conn.execute(
                    """INSERT INTO tasks(user_id, title, notes, recurrence, due_date,
                                         sort_order, archived, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,0,?,?)""",
                    (user["id"], title, notes, recurrence, due, 0, stamp, stamp),
                )
                task_id = cur.lastrowid
            tasks = load_tasks(conn, user["id"], today(), include_archived=True)
        finally:
            conn.close()
        return resp(self._find(tasks, task_id), status=201)

    def api_edit(self, task_id):
        user = self._user()
        body = self._read_json()
        conn = connect()
        try:
            self._owned_task(conn, user["id"], task_id)
            sets = []
            params = []
            if "title" in body:
                sets.append("title=?")
                params.append(clean_title(body.get("title")))
            if "notes" in body:
                sets.append("notes=?")
                params.append(clean_notes(body.get("notes")))
            if "archived" in body:
                sets.append("archived=?")
                params.append(1 if body.get("archived") else 0)
            new_recurrence = None
            if "recurrence" in body:
                new_recurrence = clean_recurrence(body.get("recurrence"))
                sets.append("recurrence=?")
                params.append(new_recurrence)
            if "due_date" in body:
                sets.append("due_date=?")
                params.append(clean_due(body.get("due_date")))
            if "completed" in body:
                sets.append("completed_at=?")
                params.append(now_iso() if body.get("completed") else None)

            if not sets:
                raise ApiError(400, "nothing to update")

            with tx(conn):
                if new_recurrence == "daily":
                    # a daily task has no due date and no single completion
                    if "completed" not in body:
                        sets.append("completed_at=?")
                        params.append(None)
                    if "due_date" not in body:
                        sets.append("due_date=?")
                        params.append(None)
                elif new_recurrence == "none":
                    pass
                sets.append("updated_at=?")
                params.append(now_iso())
                params.append(task_id)
                conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets), params)
            tasks = load_tasks(conn, user["id"], today(), include_archived=True)
        finally:
            conn.close()
        return resp(self._find(tasks, task_id))

    def api_delete(self, task_id):
        user = self._user()
        conn = connect()
        try:
            self._owned_task(conn, user["id"], task_id)
            with tx(conn):
                conn.execute("DELETE FROM task_days WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM tasks WHERE id=? AND user_id=?",
                             (task_id, user["id"]))
        finally:
            conn.close()
        return resp({"ok": True, "deleted": task_id})

    def api_toggle(self, task_id):
        user = self._user()
        body = self._read_json()
        ref = self._ref_day(body.get("date"))
        conn = connect()
        try:
            row = self._owned_task(conn, user["id"], task_id)
            with tx(conn):
                if row["recurrence"] == "daily":
                    day = ref.isoformat()
                    hit = conn.execute(
                        "SELECT 1 FROM task_days WHERE task_id=? AND day=?",
                        (task_id, day)).fetchone()
                    if hit:
                        conn.execute("DELETE FROM task_days WHERE task_id=? AND day=?",
                                     (task_id, day))
                    else:
                        conn.execute(
                            "INSERT INTO task_days(task_id, day, completed_at) VALUES (?,?,?)",
                            (task_id, day, now_iso()))
                else:
                    if row["completed_at"]:
                        conn.execute(
                            "UPDATE tasks SET completed_at=NULL, updated_at=? WHERE id=?",
                            (now_iso(), task_id))
                    else:
                        conn.execute(
                            "UPDATE tasks SET completed_at=?, updated_at=? WHERE id=?",
                            (now_iso(), now_iso(), task_id))
            tasks = load_tasks(conn, user["id"], ref, include_archived=True)
        finally:
            conn.close()
        return resp(self._find(tasks, task_id))

    def api_export(self):
        user = self._user()
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? ORDER BY id", (user["id"],)
            ).fetchall()
            ids = [r["id"] for r in rows]
            marks = {}
            if ids:
                q = "SELECT task_id, day, completed_at FROM task_days WHERE task_id IN (%s)" % (
                    ",".join("?" * len(ids)))
                for r in conn.execute(q, ids):
                    marks.setdefault(r["task_id"], []).append(
                        {"day": r["day"], "completed_at": r["completed_at"]})
        finally:
            conn.close()

        payload = {
            "service": "piano-tasks",
            "version": VERSION,
            "exported_at": now_iso(),
            "user": self._public_user(user),
            "tasks": [{
                "id": r["id"],
                "title": r["title"],
                "notes": r["notes"],
                "recurrence": r["recurrence"],
                "due_date": r["due_date"],
                "archived": bool(r["archived"]),
                "completed_at": r["completed_at"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "completed_days": sorted(marks.get(r["id"], []), key=lambda m: m["day"]),
            } for r in rows],
        }
        filename = "piano-tasks-%s-%s.json" % (user["username"], today().isoformat())
        return resp(payload, headers=[
            ("Content-Disposition", 'attachment; filename="%s"' % filename)])

    # ---- helpers ---------------------------------------------------------

    def _owned_task(self, conn, user_id, task_id):
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?",
                           (task_id, user_id)).fetchone()
        if row is None:
            raise ApiError(404, "task not found")
        return row

    @staticmethod
    def _find(tasks, task_id):
        for item in tasks:
            if item["id"] == task_id:
                return item
        raise ApiError(404, "task not found")


def count_users():
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    finally:
        conn.close()


class Server(ThreadingHTTPServer):
    """HTTPServer.server_bind() calls socket.getfqdn(), a reverse-DNS lookup.
    On a machine that is booting (or whose resolver is unreachable) that call
    can block for many seconds before the service ever accepts a connection,
    so skip it and use the bound address as the server name."""

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    init_db()
    httpd = Server((HOST, PORT), Handler)
    httpd.daemon_threads = True

    def stop(signum, frame):
        sys.stderr.write("[%s] signal %s, shutting down\n" % (now_iso(), signum))
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sys.stderr.write(
        "[%s] piano-tasks %s listening on %s:%d\n"
        "[%s] database: %s\n"
        "[%s] local date: %s   users: %d\n"
        % (now_iso(), VERSION, HOST, PORT, now_iso(), DB_PATH, now_iso(),
           today().isoformat(), count_users())
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        sys.stderr.write("[%s] stopped\n" % now_iso())


if __name__ == "__main__":
    main()