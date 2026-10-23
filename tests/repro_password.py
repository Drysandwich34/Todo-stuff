#!/usr/bin/env python3
"""Isolate why the password change reports 200 but does not persist.

Part A: end-to-end over HTTP against a brand-new database.
Part B: in-process, with `tx` monkeypatched so every BEGIN/COMMIT/ROLLBACK and
        every statement's rowcount is printed.
"""
import http.cookiejar
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = "/Users/potato/.hermes/cache/scratch/repro-pw"
PORT = 8098


def load_server(db_path):
    os.environ["PIANO_TASKS_DB"] = db_path
    os.environ["PIANO_TASKS_HOST"] = "127.0.0.1"
    os.environ["PIANO_TASKS_PORT"] = str(PORT)
    spec = importlib.util.spec_from_file_location("ptsrv", os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def matches(db_path, username, password):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if row is None:
        return None
    import hashlib
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                             bytes.fromhex(row["pw_salt"]), row["iterations"])
    return dk.hex() == row["pw_hash"]


# --------------------------------------------------------------- part A

def part_a():
    print("=== PART A: end to end over HTTP, fresh database ===")
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    db = os.path.join(WORK, "tasks.db")
    env = dict(os.environ, PIANO_TASKS_DB=db, PIANO_TASKS_PORT=str(PORT),
               PIANO_TASKS_HOST="127.0.0.1", PYTHONUNBUFFERED="1")
    log = open(os.path.join(WORK, "server.log"), "wb")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=2).read()
                break
            except Exception:
                time.sleep(0.25)
        else:
            print("  server never became healthy"); return

        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def call(method, path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                         data=data, method=method)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with op.open(req, timeout=10) as res:
                    return res.status, json.loads(res.read().decode())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read().decode() or "{}")

        print("  register alice ->", call("POST", "/api/register",
                                          {"username": "alice", "password": "hunter2"})[0])
        print("  hash matches hunter2 before change:", matches(db, "alice", "hunter2"))
        status, payload = call("POST", "/api/password",
                               {"current_password": "hunter2", "new_password": "brand-new-pw"})
        print("  password change ->", status, payload)
        print("  sessions in db after change:",
              sqlite3.connect(db).execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        print("  hash matches hunter2       after change:", matches(db, "alice", "hunter2"))
        print("  hash matches brand-new-pw  after change:", matches(db, "alice", "brand-new-pw"))
        print("  login with hunter2      ->", call("POST", "/api/login",
                                                  {"username": "alice", "password": "hunter2"})[0])
        print("  login with brand-new-pw ->", call("POST", "/api/login",
                                                  {"username": "alice", "password": "brand-new-pw"})[0])
        conn = sqlite3.connect(db)
        print("  journal_mode:", conn.execute("PRAGMA journal_mode").fetchone()[0])
        conn.close()
        print("  wal file exists:", os.path.exists(db + "-wal"),
              os.path.getsize(db + "-wal") if os.path.exists(db + "-wal") else "")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()
    print("  --- server stderr ---")
    with open(os.path.join(WORK, "server.log")) as fh:
        sys.stdout.write("".join("    " + ln for ln in fh.readlines()[-25:]))


# --------------------------------------------------------------- part B

def part_b():
    print("\n=== PART B: in-process, instrumented tx ===")
    db = os.path.join(WORK, "iso.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    srv = load_server(db)
    srv.init_db()

    # create alice directly
    pw_hash, pw_salt, iters = srv.hash_password("hunter2")
    conn = srv.connect()
    conn.execute("INSERT INTO users(username, display_name, pw_hash, pw_salt, iterations,"
                 " is_admin, created_at) VALUES ('alice','Alice',?,?,?,0,?)",
                 (pw_hash, pw_salt, iters, srv.now_iso()))
    conn.execute("INSERT INTO sessions(token, user_id, created_at, expires_at, label)"
                 " VALUES ('tok-alice', 1, ?, '2099-01-01T00:00:00-05:00', '')",
                 (srv.now_iso(),))
    conn.execute("INSERT INTO sessions(token, user_id, created_at, expires_at, label)"
                 " VALUES ('tok-old', 1, ?, '2099-01-01T00:00:00-05:00', '')",
                 (srv.now_iso(),))
    conn.close()
    print("  before: sessions =",
          sqlite3.connect(db).execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    # instrument the module's tx class
    real_tx = srv.tx

    class SpyTx(object):
        def __init__(self, c):
            self.inner = real_tx(c)
            self.conn = c

        def __enter__(self):
            print("    -> BEGIN IMMEDIATE")
            out = self.inner.__enter__()
            print("       in_transaction =", self.conn.in_transaction)
            return out

        def __exit__(self, et, ev, tb):
            print("    <- exit(tb=%r %r)" % (et, ev))
            result = self.inner.__exit__(et, ev, tb)
            print("       in_transaction now =", self.conn.in_transaction)
            return result

    srv.tx = SpyTx

    # a fake handler exposing just what api_password uses
    alice = sqlite3.connect(db)
    alice.row_factory = sqlite3.Row
    user_row = alice.execute("SELECT * FROM users WHERE username='alice'").fetchone()
    alice.close()

    handler = srv.Handler.__new__(srv.Handler)
    handler._read_json = lambda: {"current_password": "hunter2",
                                  "new_password": "brand-new-pw"}
    handler._user = lambda required=True: user_row
    handler._bearer_or_cookie_token = lambda: "tok-alice"

    status, payload, headers = handler.api_password()
    print("  api_password returned:", status, payload)

    print("  after:  sessions =",
          sqlite3.connect(db).execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    print("  hash matches hunter2      :", matches(db, "alice", "hunter2"))
    print("  hash matches brand-new-pw :", matches(db, "alice", "brand-new-pw"))


if __name__ == "__main__":
    part_a()
    part_b()