#!/bin/sh
# Every test, standard library only, in isolated mode (python3 -I), then the package check.
# PYTHON=/path/to/python3 scripts/test.sh to use another interpreter (3.9 or newer).
# OPENMATE_LARKX_PYTHON=/path/to/larkx-venv/bin/python scripts/test.sh also runs the smoke test against a real
# LarkAgentX installed there (no network, no sign-in).
set -eu
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
# Every temporary directory the tests make (tempfile.mkdtemp, mktemp, the extensions' own TMPDIR use) goes into one
# run directory, removed when this script ends, pass or fail. Nothing cleans $TMPDIR on its own, and on 2026-11-22
# 19,172 leftover test directories (1.75 GB) had piled up there. Keep the name short: some tests bind Unix sockets in
# their temporary directories, and macOS caps socket paths at 104 bytes.
base="${TMPDIR:-/tmp}"
RUN_TMP="$(mktemp -d "${base%/}/om-run.XXXXXX")"
trap 'rm -rf "$RUN_TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$RUN_TMP"
"$PY" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is needed")'
"$PY" -I -B -m unittest discover -s tests
for d in extensions/*/tests; do
	"$PY" -I -B -m unittest discover -s "$d"
done
"$PY" -I -B scripts/package.py --check
echo "all tests passed"
