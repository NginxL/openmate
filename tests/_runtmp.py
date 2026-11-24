"""Every temporary directory a test run makes goes into one directory of the run, removed when the run ends, however
the tests were started: scripts/test.sh (which makes that directory itself and says so in OPENMATE_TEST_TMP: then
nothing here), or unittest straight (python3 -m unittest discover -s tests, one file, one test). Each test module
imports this first, the extensions' tests through their own _runtmp.py.

Without it, a run started straight left its directories in $TMPDIR: on 2026-11-24 a few such runs left 317 of them
(and 62 empty media folders, one per test data directory). Like scripts/test.sh, the directory goes under the first
of $TMPDIR ($TEMP, $TMP), /tmp and /var/tmp that is an absolute path of at most 60 bytes where it can be made (some
tests bind Unix sockets in their temporary directories, and macOS caps socket paths at 104 bytes). The environment's TMPDIR points there too, so the
programs the tests start use it as well. A run killed outright (SIGKILL) still leaves it behind."""

import atexit
import os
import shutil
import tempfile

ENV = "OPENMATE_TEST_TMP"


def _bases():
    for name in ("TMPDIR", "TEMP", "TMP"):
        yield os.environ.get(name) or ""
    yield "/tmp"
    yield "/var/tmp"


def _remove(path: str) -> None:
    # a test stopped midway may leave a directory unreadable (mode 000): made readable first, links never followed
    try:
        os.chmod(path, 0o700)  # (a umask of 0777 made it 000)
    except OSError:
        pass
    for root, dirs, _ in os.walk(path):
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


def _setup() -> None:
    if os.environ.get(ENV):
        return  # scripts/test.sh's directory, or this run's own (a program a test started)
    run = ""
    for base in _bases():
        base = base.rstrip("/")
        if not base.startswith("/") or len(os.fsencode(base)) > 60 or not os.path.isdir(base):
            continue
        try:
            run = tempfile.mkdtemp(prefix="om.", dir=base)
        except OSError:
            continue  # not writable there: the next one
        try:
            os.chmod(run, 0o700)  # (a umask of 0777 makes it 000)
        except OSError:
            pass
        break
    if not run:
        return  # nowhere to make one: as before
    os.environ[ENV] = run
    os.environ["TMPDIR"] = run
    tempfile.tempdir = run
    owner = os.getpid()
    atexit.register(lambda: os.getpid() == owner and _remove(run))  # never from a forked child


_setup()
