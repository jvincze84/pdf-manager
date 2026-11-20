#!/usr/bin/env bash
# Visual smoke test: start the app with sample data in a throw-away container and
# take light + dark screenshots with headless Chrome into tests/output/.
#   tests/screenshot.sh            -> tests/output/light.png, dark.png, search.png, duplicates.png, viewer.png, viewer-mobile.png
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-18765}
NAME=pdfm-screenshot
OUT="$PWD/tests/output"
mkdir -p "$OUT" && chmod 777 "$OUT"

docker build -q --target test -t pdf-manager:test . >/dev/null
docker build -q -t pdf-manager:latest . >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "$PORT:8000" pdf-manager:latest >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 30); do curl -sf "localhost:$PORT/api/status" >/dev/null && break; sleep 0.5; done
docker run --rm --network host -v "$PWD/tests:/srv/tests:ro" pdf-manager:test python tests/seed.py "http://localhost:$PORT"
sleep 6  # let text extraction + the one OCR job finish

shot() { # file url [extra chrome flags]
  docker run --rm --network host -v "$OUT:/out" zenika/alpine-chrome --no-sandbox --hide-scrollbars \
    --window-size=1400,600 --virtual-time-budget=4000 "${@:3}" --screenshot="/out/$1" "$2" >/dev/null 2>&1
}
shot light.png  "http://localhost:$PORT/" --blink-settings=preferredColorScheme=1
shot dark.png   "http://localhost:$PORT/?folder=Scans" --blink-settings=preferredColorScheme=0
shot search.png "http://localhost:$PORT/?q=%22pontos%20keres%C3%A9s%22" --blink-settings=preferredColorScheme=1
shot duplicates.png "http://localhost:$PORT/#duplicates" --blink-settings=preferredColorScheme=0
# built-in PDF viewer: needs a current Chrome (PDF.js 6 uses CSS round()) -> puppeteer image;
# also checks list -> viewer -> Back -> list. Writes viewer.png + viewer-mobile.png.
rm -f "$OUT/viewer.png" "$OUT/viewer-mobile.png"
docker run --rm --network host -v "$PWD/tests/viewer_check.js:/tmp/viewer_check.js:ro" -v "$OUT:/out" \
  ghcr.io/puppeteer/puppeteer:latest node /tmp/viewer_check.js "http://localhost:$PORT"
echo "Screenshots in $OUT:"; ls "$OUT"
