#!/bin/sh
# Every test, standard library only, in isolated mode (python3 -I), then the package check.
# PYTHON=/path/to/python3 scripts/test.sh to use another interpreter (3.9 or newer).
# OPENMATE_LARKX_PYTHON=/path/to/larkx-venv/bin/python scripts/test.sh also runs the smoke test against a real
# LarkAgentX installed there (no network, no sign-in).
set -eu
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
# Every temporary directory the tests make (tempfile.mkdtemp, mktemp, the extensions' own TMPDIR use) goes into one
# run directory, removed when this script ends: passed, failed, interrupted or killed. Nothing cleans $TMPDIR on its
# own, and on 2026-11-22 19,172 leftover test directories (1.75 GB) had piled up there. The run directory goes under
# the first of $TMPDIR, /tmp and /var/tmp that is an absolute path of at most 60 bytes and where it can be made: some
# tests bind Unix sockets in their temporary directories (macOS caps socket paths at 104 bytes), and a relative path
# would point elsewhere once a test changes directory.
RUN_TMP=
cleanup() {
	[ -n "$RUN_TMP" ] || return 0
	# a test killed midway may leave a directory unreadable (mode 000); chmod -R follows no link below the root
	chmod -R u+rwx "$RUN_TMP" 2>/dev/null || :
	rm -rf "$RUN_TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for base in "${TMPDIR:-}" /tmp /var/tmp; do
	base="${base%/}"
	case "$base" in
	/?*) ;;
	*) continue ;;
	esac
	[ "$(printf %s "$base" | wc -c | tr -d ' ')" -le 60 ] || continue  # wc -c counts bytes in any locale
	RUN_TMP="$(mktemp -d "$base/om.XXXXXX" 2>/dev/null)" && break
	RUN_TMP=
done
[ -n "$RUN_TMP" ] || { echo "scripts/test.sh: cannot make a temporary directory in \$TMPDIR, /tmp or /var/tmp" >&2; exit 1; }
export TMPDIR="$RUN_TMP"
"$PY" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is needed")'
"$PY" -I -B -m unittest discover -s tests
for d in extensions/*/tests; do
	"$PY" -I -B -m unittest discover -s "$d"
done
"$PY" -I -B scripts/package.py --check
echo "all tests passed"
