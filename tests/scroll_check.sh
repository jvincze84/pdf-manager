#!/usr/bin/env bash
# Browser check of the incremental list loading (100 first, then 20 per scroll step):
#  - short window: exactly 100 rows rendered
#  - tall window (taller than 100 rows): the page keeps loading batches of 20 until it is filled
# (Don't use huge windows: headless Chrome rendering of e.g. 12000px hangs.)
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-18766}
NAME=pdfm-scrollcheck
TOTAL=230

docker build -q --target test -t pdf-manager:test . >/dev/null
docker build -q -t pdf-manager:latest . >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "$PORT:8000" pdf-manager:latest >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 30); do curl -sf "localhost:$PORT/api/status" >/dev/null && break; sleep 0.5; done
docker run --rm --network host -v "$PWD/tests:/srv/tests:ro" pdf-manager:test python tests/seed.py "http://localhost:$PORT" --many $TOTAL

rows() { # window height -> number of rendered rows
  timeout 120 docker run --rm --network host zenika/alpine-chrome --no-sandbox --window-size=1400,"$1" \
    --virtual-time-budget=8000 --dump-dom "http://localhost:$PORT/" 2>/dev/null | grep -o '<tr data-id=' | wc -l
}
fail=0
short=$(rows 700);   echo "window 700px:   $short rows (expected 100)";        [ "$short" -eq 100 ] || fail=1
tall=$(rows 6500);   echo "window 6500px:  $tall rows (expected >100, step 20)"
{ [ "$tall" -gt 100 ] && [ $(( (tall - 100) % 20 )) -eq 0 ] || [ "$tall" -eq $TOTAL ]; } || fail=1
[ $fail -eq 0 ] && echo "OK" || { echo "FAILED"; exit 1; }
