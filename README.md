# Piano Tasks

A small self-hosted task server, plus an Android client for it.

Several people (each with their own private list), two kinds of task, reachable
from any device on the Tailscale network. No third-party packages anywhere —
Python standard library on the server, framework-only Java in the app.

* **Daily tasks** — repeat every day. Each day is ticked separately, so the
  history is kept and a streak is computed from it. You can tick a previous day
  (backfill) if you forget before midnight.
* **Long-term tasks** — one-off items with an optional due date. They stay on
  the list until you tick them off, and go amber when overdue.

## Layout

```
server.py              the whole server — stdlib only, no dependencies
static/                the web client (vanilla JS, no build step)
tests/smoke_test.py    98 API checks, run against a live server
deploy/                systemd user unit
android/               the Android client (Java, framework-only)
  app/src/main/java/   Json, ApiClient, MainActivity, Prefs
  tests/jvm_test.sh    runs the network layer against a real server, no device
```

## Running it

It runs on `piano-puter` under a systemd **user** service, with lingering
enabled so it starts at boot without anyone logging in.

```bash
ssh family@piano-puter

systemctl --user status piano-tasks      # is it up?
systemctl --user restart piano-tasks     # restart
systemctl --user stop piano-tasks        # stop
journalctl --user -u piano-tasks -f      # live log
```

Run it by hand (useful when editing):

```bash
cd ~/.local/share/piano-tasks
PIANO_TASKS_PORT=8080 python3 server.py
```

### Reach it

| From               | URL                            |
|--------------------|--------------------------------|
| This machine       | <http://127.0.0.1:8080>        |
| Any tailnet device | <http://piano-puter:8080>      |
| ...or by IP        | <http://100.69.254.55:8080>    |

On a phone, open it once and use *Add to Home Screen* — there is a web app
manifest, so it launches without browser chrome.

## Accounts

Everyone gets their own login and can only ever see their own tasks.
The first account created becomes the admin. Others register themselves from
the "Create account" tab on the site:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/register \
     -H 'Content-Type: application/json' \
     -d '{"username":"spencer","password":"something-longer","display_name":"Spencer"}'
```

Usernames are lowercase, 3–32 characters of `a-z 0-9 . _ -`.
Passwords are stored as PBKDF2-HMAC-SHA256, 200k iterations, per-user salt —
never in plain text.

## Data and backups

| What     | Where                                                  |
|----------|--------------------------------------------------------|
| Database | `~/.local/share/piano-tasks/data/tasks.db` (SQLite, WAL) |
| Code     | `~/.local/share/piano-tasks/`                            |
| Unit     | `~/.config/systemd/user/piano-tasks.service`             |

To back up, either copy the three files `tasks.db`, `tasks.db-wal`,
`tasks.db-shm` while the service is stopped, or just export from the site:

```
http://piano-puter:8080/api/export      # JSON download, cookie-authenticated
```

## HTTP API

JSON in, JSON out. Authenticate either with the `pt_session` cookie (what the
website uses) or with `Authorization: Bearer <token>` (what the phone and
desktop apps will use — the token is returned by register/login and lasts 90
days).

| Method | Path                       | Notes |
|--------|----------------------------|-------|
| GET    | `/health`                  | no auth; uptime info and server date |
| POST   | `/api/register`            | `{username, password, display_name?}` → 201, token |
| POST   | `/api/login`               | `{username, password}` → token |
| POST   | `/api/logout`              | kills this session |
| POST   | `/api/password`            | `{current_password, new_password}`; revokes other sessions |
| GET    | `/api/me`                  | who am I, plus the server's today |
| GET    | `/api/tasks?date=&archived=` | the list for a day (`date` defaults to today) |
| POST   | `/api/tasks`               | `{title, notes?, recurrence, due_date?}` |
| GET    | `/api/tasks/{id}`          | one task |
| PATCH  | `/api/tasks/{id}`          | any of `title, notes, recurrence, due_date, archived, completed` |
| DELETE | `/api/tasks/{id}`          | gone, with its history |
| POST   | `/api/tasks/{id}/toggle`   | tick/untick. `{"date":"YYYY-MM-DD"}` ticks that day instead |
| GET    | `/api/summary`             | counts for the dashboard |
| GET    | `/api/export`              | everything, as a download |

`recurrence` is `"daily"` or `"none"` (a long-term task). A daily task ignores
`due_date`.

A task looks like:

```json
{
  "id": 3,
  "title": "Take vitamins",
  "notes": "",
  "recurrence": "daily",
  "due_date": null,
  "done": true,
  "streak": 6,
  "total_done": 41,
  "history": [true, false, true, "...14 days, oldest first"],
  "archived": false,
  "created_at": "2026-10-23T16:52:15-05:00",
  "updated_at": "2026-10-23T16:52:15-05:00"
}
```

## Tests

```bash
python3 tests/smoke_test.py http://127.0.0.1:8080          # 98 checks
python3 tests/smoke_test.py http://127.0.0.1:8080 persist  # after a restart
```

`tests/repro_password.py` is a scratch harness that also verifies the password
change end to end against a throwaway database.

## The Android client

Java, framework widgets only: **no AndroidX and no third-party libraries at
all**, so the APK comes out around 46 KB. It has the 14-day history strip per
daily task, backfill by ticking past days, archive, rename, delete, and
export-to-clipboard.

### Build

Needs JDK 17 and an Android SDK with platform 34. The build pins AGP 8.5.2 and
Gradle 8.7 through the wrapper, so your own Gradle version does not matter:

```bash
cd android
echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties    # or your SDK path
JAVA_HOME=/path/to/jdk17 ./gradlew assembleDebug
# → app/build/outputs/apk/debug/app-debug.apk
```

### Verify the network layer without a device

`ApiClient` and `Json` deliberately import nothing from `android.*`, so they
compile and run on a plain JVM. This is the quickest honest check that the
client still matches the server:

```bash
cd android && ./tests/jvm_test.sh http://127.0.0.1:8080     # 40 checks
```

It exercises JSON parsing and writing, then registers a throwaway account and
walks the whole task lifecycle against the live server, including the
PATCH-override path.

### Install on a phone

Copy the APK across and open it, allowing installs from that source when
prompted, or `adb install -r app-debug.apk`.

The debug build is signed with the standard Android debug key: fine for
sideloading, but it cannot go to the Play Store. Generate your own keystore and
add a `signingConfigs` block for a release build.

Set the server address on the sign-in screen; it defaults to
`http://piano-puter:8080`. The app speaks plain HTTP, permitted explicitly in
`res/xml/network_security_config.xml`, because the link is already encrypted by
Tailscale and nothing is exposed beyond the tailnet.

## Notes

* Time is the **server's** local time. Every response carries `server_today`,
  and clients should trust that rather than their own clock — otherwise a phone
  in another timezone would disagree about what "today" means.
* The site is plain HTTP because it is only ever reachable over Tailscale,
  which already encrypts the link. Nothing listens beyond the tailnet.
* SELinux stays enforcing; the service runs unconfined as the `family` user.
* Still to come: Linux, macOS and Windows clients against this same API.