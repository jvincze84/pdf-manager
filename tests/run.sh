#!/usr/bin/env bash
# Run the automated tests inside Docker (same image/deps as production + pytest).
#
#   tests/run.sh                         # everything
#   tests/run.sh tests/test_api.py       # one file
#   tests/run.sh -k name_search          # pytest filters work
#   SKIP_JS=1 tests/run.sh               # skip the JavaScript syntax check
#
# app/ and tests/ are mounted, so code changes don't need an image rebuild
# (only requirements / Dockerfile changes do - the build below is cached anyway).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> building test image"
docker build -q --target test -t pdf-manager:test . >/dev/null

if [ "${SKIP_JS:-0}" != "1" ]; then
  echo "==> JS syntax check"
  docker run --rm -v "$PWD/app/static:/s:ro" node:22-alpine sh -c "for f in /s/*.js; do node --check \$f || exit 1; done"
fi

[ $# -eq 0 ] && set -- tests
echo "==> pytest $*"
docker run --rm \
  -v "$PWD/app:/srv/app:ro" \
  -v "$PWD/tests:/srv/tests:ro" \
  pdf-manager:test pytest -q -p no:cacheprovider "$@"
