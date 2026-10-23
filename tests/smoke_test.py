#!/usr/bin/env python3
"""End-to-end checks against a running piano-tasks server.

Usage:  python3 tests/smoke_test.py <base-url>            full suite (fresh DB)
        python3 tests/smoke_test.py <base-url> persist    just the persistence proof
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
MODE = sys.argv[2] if len(sys.argv) > 2 else "full"

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   <- " + str(extra)) if (extra and not cond) else ""))
    return cond


class Client(object):
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, token=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with self.opener.open(req, timeout=15) as res:
                raw = res.read().decode("utf-8")
                return res.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw[:200]
            return err.code, payload

    def raw_get(self, path, full=False):
        req = urllib.request.Request(BASE + path)
        try:
            with self.opener.open(req, timeout=15) as res:
                body = res.read()
                return res.status, (body if full else body[:200])
        except urllib.error.HTTPError as err:
            return err.code, err.read()[:200]


def today_shift(days):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def keepalive_section():
    """A browser reuses one socket for many requests. Every request body must
    be consumed even when the handler bails out early, or the next request on
    that socket starts mid-body and everything after it is garbage."""
    from urllib.parse import urlparse
    import http.client

    parsed = urlparse(BASE)
    host, port = parsed.hostname, parsed.port or 80

    print("\n-- keep-alive: one socket, several requests")
    conn = http.client.HTTPConnection(host, port, timeout=15)

    def go(method, path, body=None):
        headers = {}
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        raw = res.read()
        try:
            decoded = json.loads(raw) if raw else None
        except ValueError:
            decoded = raw[:120]
        return res.status, decoded, res.will_close

    status, payload, will_close = go("POST", "/api/tasks", {"title": "no session here"})
    check("unauthenticated POST carrying a body -> 401", status == 401, (status, payload))
    check("the server left the connection open", not will_close, will_close)

    status, payload, _ = go("GET", "/health")
    check("next request on that same socket -> 200, not byte soup",
          status == 200 and (payload or {}).get("service") == "piano-tasks", (status, payload))

    status, payload, _ = go("POST", "/api/login", {"username": "nobody", "password": "whatever"})
    check("a login carrying a body on that socket -> 401", status == 401, (status, payload))

    status, payload, _ = go("GET", "/health")
    check("the socket is still in sync afterwards",
          status == 200 and (payload or {}).get("service") == "piano-tasks", (status, payload))

    # an oversized body cannot be resynced, so the server must hang up
    try:
        conn.request("POST", "/api/tasks", body=b"x" * 300000,
                     headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        res.read()
        check("oversized body -> 413", res.status == 413, res.status)
    except Exception as exc:  # noqa: BLE001
        check("oversized body -> 413", False, exc)
    conn.close()

    fresh = http.client.HTTPConnection(host, port, timeout=15)
    try:
        fresh.request("GET", "/health")
        res = fresh.getresponse()
        res.read()
        check("a new connection still works afterwards", res.status == 200, res.status)
    finally:
        fresh.close()


def full_suite():
    print("\n-- health (no auth)")
    anon = Client()
    status, health = anon.call("GET", "/health")
    check("GET /health -> 200", status == 200, status)
    check("health reports ok + today", bool(health.get("ok")) and health.get("server_today"),
          health)

    print("\n-- static assets")
    for path, want in (("/", 200), ("/app.js", 200), ("/styles.css", 200),
                       ("/manifest.webmanifest", 200), ("/icon.svg", 200),
                       ("/nope.js", 404), ("/..%2Fserver.py", 403)):
        status, _ = anon.raw_get(path)
        check("GET %s -> %d" % (path, want), status == want, status)

    print("\n-- unauthenticated access is refused")
    status, _ = anon.call("GET", "/api/tasks")
    check("GET /api/tasks without a session -> 401", status == 401, status)
    status, _ = anon.call("POST", "/api/tasks", {"title": "sneaky"})
    check("POST /api/tasks without a session -> 401", status == 401, status)

    keepalive_section()

    print("\n-- registration")
    alice = Client()
    status, reg = alice.call("POST", "/api/register",
                             {"username": "alice", "password": "hunter2", "display_name": "Alice"})
    check("register alice -> 201", status == 201, (status, reg))
    check("register response describes the account just created",
          (reg or {}).get("user", {}).get("username") == "alice", reg)
    token_a = (reg or {}).get("token")
    check("register returns a session token", isinstance(token_a, str) and len(token_a) > 20)
    check("first account is admin", (reg or {}).get("user", {}).get("is_admin") is True, reg)
    status, me_who = alice.call("GET", "/api/me")
    check("the registering client is signed in as alice",
          me_who.get("authenticated") and me_who["user"]["username"] == "alice", me_who)

    # Each negative case gets its own client: a 201 the test did not expect
    # would otherwise replace that client's session cookie and silently
    # re-point every later request at the wrong account.
    probe = Client()
    status, dup = probe.call("POST", "/api/register", {"username": "Alice", "password": "hunter2"})
    check("duplicate username (different case) -> 409", status == 409, (status, dup))
    status, bad = probe.call("POST", "/api/register", {"username": "bo", "password": "hunter2"})
    check("2-character username -> 400", status == 400, (status, bad))
    status, bad = probe.call("POST", "/api/register", {"username": "x" * 33, "password": "hunter2"})
    check("33-character username -> 400", status == 400, (status, bad))
    status, bad = probe.call("POST", "/api/register", {"username": "b-ob!", "password": "hunter2"})
    check("username with illegal characters -> 400", status == 400, (status, bad))
    status, bad = probe.call("POST", "/api/register", {"username": "bob", "password": "123"})
    check("too-short password -> 400", status == 400, (status, bad))

    bob = Client()
    status, reg_b = bob.call("POST", "/api/register", {"username": "bob", "password": "password1"})
    check("register bob -> 201", status == 201, (status, reg_b))
    check("second account is not admin",
          (reg_b or {}).get("user", {}).get("is_admin") is False, reg_b)
    status, who = bob.call("GET", "/api/me")
    check("bob's client is signed in as bob",
          who.get("authenticated") and who["user"]["username"] == "bob", who)
    status, who = alice.call("GET", "/api/me")
    check("alice's client is still alice",
          who.get("authenticated") and who["user"]["username"] == "alice", who)

    print("\n-- login")
    stranger = Client()
    status, _ = stranger.call("POST", "/api/login", {"username": "alice", "password": "wrong-pass"})
    check("wrong password -> 401", status == 401, status)
    status, login = stranger.call("POST", "/api/login", {"username": "Alice", "password": "hunter2"})
    check("login with different case -> 200", status == 200, (status, login))
    check("login returns a fresh token",
          (login or {}).get("token") not in (None, token_a), login)
    status, me = stranger.call("GET", "/api/me")
    check("GET /api/me authenticated via cookie",
          status == 200 and me.get("authenticated") and me["user"]["username"] == "alice", me)

    status, me2 = anon.call("GET", "/api/me")
    check("GET /api/me anonymous reports authenticated=false",
          status == 200 and me2.get("authenticated") is False, me2)

    status, bearer = anon.call("GET", "/api/me", token=(login or {}).get("token"))
    check("Bearer token auth works (for the native apps)",
          status == 200 and bearer.get("authenticated") is True, bearer)

    print("\n-- creating tasks")
    status, daily = alice.call("POST", "/api/tasks",
                               {"title": "  Take   vitamins  ", "recurrence": "daily"})
    check("create daily task -> 201", status == 201, (status, daily))
    check("title whitespace is normalised", daily.get("title") == "Take vitamins", daily)
    check("new daily task starts unchecked and due_date is dropped",
          daily.get("done") is False and daily.get("due_date") is None, daily)

    status, oneoff = alice.call("POST", "/api/tasks", {
        "title": "Renew passport", "recurrence": "none",
        "due_date": today_shift(-2), "notes": "bring photos"})
    check("create long-term task with a past due date -> 201", status == 201, (status, oneoff))
    check("past-due task is flagged overdue", oneoff.get("overdue") is True, oneoff)
    check("notes are stored", oneoff.get("notes") == "bring photos", oneoff)

    status, plain = alice.call("POST", "/api/tasks", {"title": "Buy a ladder"})
    check("omitting recurrence defaults to long-term", plain.get("recurrence") == "none", plain)

    status, bad = alice.call("POST", "/api/tasks", {"title": "  "})
    check("blank title -> 400", status == 400, (status, bad))
    status, bad = alice.call("POST", "/api/tasks", {"title": "x", "recurrence": "hourly"})
    check("unknown recurrence -> 400", status == 400, (status, bad))
    status, bad = alice.call("POST", "/api/tasks", {"title": "x", "due_date": "2026-02-30"})
    check("impossible calendar date -> 400", status == 400, (status, bad))
    status, bad = alice.call("POST", "/api/tasks", {"title": "x" * 501})
    check("over-long title -> 400", status == 400, (status, bad))

    print("\n-- daily completion, history and streaks")
    did = daily["id"]
    status, toggled = alice.call("POST", "/api/tasks/%d/toggle" % did, {})
    check("toggle today -> done", toggled.get("done") is True, toggled)
    check("streak is 1 after one day", toggled.get("streak") == 1, toggled)
    check("history strip has 14 slots ending 'on'",
          len(toggled.get("history")) == 14 and toggled["history"][-1] is True, toggled)

    status, again = alice.call("POST", "/api/tasks/%d/toggle" % did, {})
    check("toggle again -> not done (undo works)", again.get("done") is False, again)

    # backfill two earlier days, then re-check the streak
    for offset in (-2, -1):
        status, _ = alice.call("POST", "/api/tasks/%d/toggle" % did, {"date": today_shift(offset)})
        check("backfill %s -> 200" % today_shift(offset), status == 200, status)
    status, today_view = alice.call("GET", "/api/tasks")
    row = [t for t in today_view["tasks"] if t["id"] == did][0]
    check("today is still not ticked after backfilling earlier days",
          row["done"] is False, row)
    check("streak of 2 counts back from yesterday when today is not done",
          row["streak"] == 2, row)
    status, _ = alice.call("POST", "/api/tasks/%d/toggle" % did, {})
    status, today_view = alice.call("GET", "/api/tasks")
    row = [t for t in today_view["tasks"] if t["id"] == did][0]
    check("streak of 3 once today is ticked", row["streak"] == 3, row)
    check("total_done counts every ticked day", row["total_done"] == 3, row)

    status, past = alice.call("GET", "/api/tasks?date=" + today_shift(-2))
    past_row = [t for t in past["tasks"] if t["id"] == did][0]
    check("viewing a past date shows that day's tick", past_row["done"] is True, past_row)
    check("past view counts 1 day of streak from that date",
          past_row["streak"] == 1, past_row)

    status, bad = alice.call("POST", "/api/tasks/%d/toggle" % did, {"date": "not-a-day"})
    check("toggle with a malformed date -> 400", status == 400, (status, bad))

    print("\n-- long-term completion")
    status, done_l = alice.call("POST", "/api/tasks/%d/toggle" % oneoff["id"], {})
    check("toggling a long-term task marks it done",
          done_l.get("done") is True and done_l.get("completed_at"), done_l)
    check("overdue clears once done", done_l.get("overdue") is False, done_l)
    status, undone_l = alice.call("POST", "/api/tasks/%d/toggle" % oneoff["id"], {})
    check("long-term task can be reopened",
          undone_l.get("done") is False and undone_l.get("completed_at") is None, undone_l)

    print("\n-- editing")
    status, edited = alice.call("PATCH", "/api/tasks/%d" % plain["id"],
                                {"title": "Buy a big ladder", "notes": "step ladder"})
    check("rename + notes via PATCH", edited.get("title") == "Buy a big ladder"
          and edited.get("notes") == "step ladder", edited)
    status, edited = alice.call("PATCH", "/api/tasks/%d" % plain["id"],
                                {"recurrence": "daily"})
    check("switching a long-term task to daily clears due_date",
          edited.get("recurrence") == "daily" and edited.get("due_date") is None, edited)
    status, edited = alice.call("PATCH", "/api/tasks/%d" % plain["id"],
                                {"recurrence": "none", "due_date": today_shift(3)})
    check("switching back to long-term keeps a due date",
          edited.get("recurrence") == "none" and edited.get("due_date") == today_shift(3), edited)
    status, empty = alice.call("PATCH", "/api/tasks/%d" % plain["id"], {})
    check("empty PATCH -> 400", status == 400, (status, empty))

    print("\n-- archive / summary")
    status, _ = alice.call("PATCH", "/api/tasks/%d" % plain["id"], {"archived": True})
    listing = alice.call("GET", "/api/tasks")[1]
    check("archived task disappears from the default list",
          plain["id"] not in [t["id"] for t in listing["tasks"]], listing["tasks"])
    with_arch = alice.call("GET", "/api/tasks?archived=1")[1]
    check("archived task returns with ?archived=1",
          plain["id"] in [t["id"] for t in with_arch["tasks"]], [t["id"] for t in with_arch["tasks"]])
    status, _ = alice.call("PATCH", "/api/tasks/%d" % plain["id"], {"archived": False})

    listing = alice.call("GET", "/api/tasks")[1]
    summary = listing["summary"]
    check("summary counts one daily task", summary["daily_total"] == 1, summary)
    check("summary counts the daily task as done", summary["daily_done"] == 1, summary)
    check("summary counts 2 open long-term tasks", summary["longterm_open"] == 2, summary)
    check("summary reports 1 overdue", summary["overdue"] == 1, summary)
    check("summary best_streak is 3", summary["best_streak"] == 3, summary)
    check("summary surfaces server_today", bool(summary["server_today"]), summary)

    print("\n-- isolation between users")
    bob_list = bob.call("GET", "/api/tasks")[1]
    check("bob sees an empty list", bob_list["tasks"] == [], bob_list["tasks"])
    status, _ = bob.call("GET", "/api/tasks/%d" % did)
    check("bob cannot read alice's task -> 404", status == 404, status)
    status, _ = bob.call("PATCH", "/api/tasks/%d" % did, {"title": "hijacked"})
    check("bob cannot edit alice's task -> 404", status == 404, status)
    status, _ = bob.call("POST", "/api/tasks/%d/toggle" % did, {})
    check("bob cannot toggle alice's task -> 404", status == 404, status)
    status, _ = bob.call("DELETE", "/api/tasks/%d" % did)
    check("bob cannot delete alice's task -> 404", status == 404, status)
    status, bob_task = bob.call("POST", "/api/tasks", {"title": "Walk the dog", "recurrence": "daily"})
    check("bob can keep his own list", status == 201, (status, bob_task))
    alice_ids = [t["id"] for t in alice.call("GET", "/api/tasks")[1]["tasks"]]
    check("alice's list is unaffected by bob", bob_task["id"] not in alice_ids, alice_ids)

    print("\n-- password change")
    status, _ = alice.call("POST", "/api/password",
                           {"current_password": "nope", "new_password": "brand-new-pw"})
    check("wrong current password -> 403", status == 403, status)
    status, _ = alice.call("POST", "/api/password",
                           {"current_password": "hunter2", "new_password": "brand-new-pw"})
    check("password change -> 200", status == 200, status)
    fresh = Client()
    status, _ = fresh.call("POST", "/api/login", {"username": "alice", "password": "hunter2"})
    check("old password no longer works -> 401", status == 401, status)
    status, relog = fresh.call("POST", "/api/login",
                               {"username": "alice", "password": "brand-new-pw"})
    check("new password works -> 200", status == 200, status)
    status, _ = stranger.call("GET", "/api/tasks")
    check("other sessions were revoked by the change -> 401", status == 401, status)
    check("the session doing the change survived", alice.call("GET", "/api/me")[0] == 200)

    print("\n-- export")
    status, blob = alice.raw_get("/api/export", full=True)
    check("GET /api/export -> 200", status == 200, status)
    try:
        dumped = json.loads(blob.decode("utf-8"))
        check("export contains the user's tasks",
              len(dumped["tasks"]) == 3 and dumped["user"]["username"] == "alice", dumped.get("user"))
        daily_json = [t for t in dumped["tasks"] if t["id"] == did][0]
        check("export carries the per-day history",
              len(daily_json["completed_days"]) == 3, daily_json.get("completed_days"))
    except Exception as exc:  # noqa: BLE001
        check("export is valid JSON", False, exc)

    print("\n-- deletion")
    status, _ = alice.call("DELETE", "/api/tasks/%d" % plain["id"])
    check("DELETE -> 200", status == 200, status)
    check("deleted task is gone from the list",
          plain["id"] not in [t["id"] for t in alice.call("GET", "/api/tasks")[1]["tasks"]])
    check("deleted task is gone from the export",
          plain["id"] not in [t["id"] for t in
                              json.loads(alice.raw_get("/api/export", full=True)[1].decode())["tasks"]])

    print("\n-- logout")
    status, _ = bob.call("POST", "/api/logout", {})
    check("logout -> 200", status == 200, status)
    status, _ = bob.call("GET", "/api/tasks")
    check("session is dead after logout -> 401", status == 401, status)


def persistence_check():
    """Run after a restart: the accounts, the daily task and every ticked day
    must still be there, and the streak must still read 3."""
    print("\n-- after a server restart")
    alice = Client()
    status, login = alice.call("POST", "/api/login",
                               {"username": "alice", "password": "brand-new-pw"})
    check("alice can still log in (accounts survived)", status == 200, (status, login))
    if status != 200:
        return
    status, listing = alice.call("GET", "/api/tasks")
    tasks = listing["tasks"]
    check("tasks survived the restart", len(tasks) == 2, [t["title"] for t in tasks])
    daily = [t for t in tasks if t["recurrence"] == "daily"][0]
    check("daily task still reads as done today", daily["done"] is True, daily)
    check("streak of 3 survived the restart", daily["streak"] == 3, daily)
    check("per-day history survived (3 ticked days)",
          daily["total_done"] == 3, daily.get("total_done"))
    check("long-term task kept its notes and due date",
          any(t["notes"] == "bring photos" and t["due_date"] for t in tasks), tasks)


if __name__ == "__main__":
    if MODE == "persist":
        persistence_check()
    else:
        full_suite()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + "; ".join(FAIL))
    sys.exit(1 if FAIL else 0)