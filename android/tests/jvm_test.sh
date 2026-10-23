#!/usr/bin/env bash
# Exercise the app's real networking layer against a running server.
#
# ApiClient and Json import nothing from android.*, so they compile and run on
# a plain JVM - no emulator, no device, no APK. This is the fastest way to know
# whether the client actually works against the API.
#
#   ./tests/jvm_test.sh [http://host:8080]
set -euo pipefail

SERVER="${1:-http://piano-puter:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVAC="$JAVA_HOME/bin/javac"
  JAVA="$JAVA_HOME/bin/java"
else
  JAVAC="javac"
  JAVA="java"
fi

echo "compiling the client + harness with $($JAVAC -version 2>&1)"
"$JAVAC" -Xlint:-unchecked -d "$OUT" \
  -sourcepath "$ROOT/app/src/main/java:$HERE" \
  "$HERE/com/pianotasks/app/ApiHarness.java"

echo "running against $SERVER"
"$JAVA" -cp "$OUT" com.pianotasks.app.ApiHarness "$SERVER"