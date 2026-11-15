"""Starting under LarkAgentX's Python.

The core needs only the standard library and runs on the hub machine's own python3, but LarkAgentX and its
dependencies live in the interpreter the user installed it into (the setting larkx_python). The hub starts
bin/openmate-feishu with python3 and gives the settings only in initialize, the first line on stdin. So before the
core starts, handoff() reads that one line (a byte at a time: nothing after it is taken from the stream) and, when
the backend is LarkAgentX, replaces the process with `larkx_python -I -B bin/openmate-feishu`. The new program gets
the line through an inherited pipe, never through the environment, the command line or a file (the line holds the
hub's token); stdin, stdout and stderr stay the hub's. When the interpreter cannot be used, the program goes on in
python3 with the line it read, and the backend says why (problem()), so the connector's status shows it. Requests
that come before initialize (a conformance check's ping, a manifest) are answered on the spot by early(), as the core
would answer them, so the choice is still made on initialize.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Dict, MutableMapping, Optional, Tuple

CHILD = "OPENMATE_LARKX_CHILD"  # in the re-executed program: the interpreter it was started with
FIRST_FD = "OPENMATE_LARKX_FIRST_FD"  # the inherited pipe that holds the protocol's first line
LINE_MOST = 4 << 20  # a first line longer than this is not looked at; it goes on to the core as it is
EARLY_MOST = 32  # requests answered before initialize; after that the program goes on in this interpreter
PROTOCOL = 1
PIPE_DIRECT = 8 << 10  # a line this short fits in any pipe's buffer; a longer one is written by a helper process
PROBE_TIMEOUT = 20.0
PYTHON_LEAST = (3, 9)

_problem = ""


def problem() -> str:
    """Why the configured interpreter is not used ("" when it is, or when none is configured)."""
    return _problem


def read_line(fd: int, most: int = LINE_MOST) -> bytes:
    """One line from fd, without reading past its newline (so the rest stays in the stream for the next reader)."""
    buf = bytearray()
    while len(buf) < most:
        b = os.read(fd, 1)
        if not b:
            break
        buf += b
        if b == b"\n":
            break
    return bytes(buf)


def read_all(fd: int, most: int = LINE_MOST + 1) -> bytes:
    buf = bytearray()
    while len(buf) < most:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _message(line: bytes) -> Optional[dict]:
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def initialize_config(line: bytes) -> Optional[dict]:
    """The settings an initialize line of this protocol carries; None when the line is not that."""
    msg = _message(line)
    if msg is None or msg.get("method") != "initialize":
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if params.get("protocol") != PROTOCOL:
        return None
    config = params.get("config")
    return config if isinstance(config, dict) else {}


def _answer(fd: int, msg: dict, early: Callable) -> None:
    """Answer a request that came before initialize, the way the core does (early raises RPCError for an error)."""
    try:
        reply = {"id": msg.get("id"), "result": early(str(msg.get("method")), msg.get("params")) or {}}
    except Exception as e:
        to_wire = getattr(e, "to_wire", None)
        reply = {"id": msg.get("id"), "error": to_wire() if to_wire else {"message": "内部错误，详情见扩展日志"}}
    data = memoryview((json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    while data:
        data = data[os.write(fd, data):]


def first_initialize(stdin_fd: int, stdout_fd: int, early: Optional[Callable]) -> Tuple[bytes, Optional[dict]]:
    """Read up to initialize: (its line, its settings). Requests before it are answered with early (and dropped
    without it), notifications are dropped, as the core does with both. (line, None) when the input ends, a line is
    unreadable as a request, or too many came first: that line goes on to the core as it is."""
    for _ in range(EARLY_MOST):
        line = read_line(stdin_fd)
        config = initialize_config(line)
        if config is not None:
            return line, config
        msg = _message(line)
        if early is None or msg is None or "method" not in msg:
            return line, None
        if "id" in msg:
            _answer(stdout_fd, msg, early)
    return b"", None


def check_python(path: str, run: Callable = subprocess.run) -> str:
    """"" when path is an interpreter this program can be run with, else the reason in a sentence."""
    if not os.path.isabs(path):
        return "LarkAgentX 的 Python（larkx_python）要写绝对路径，例如某个 venv 里的 /…/bin/python"
    if not os.path.isfile(path):
        return "找不到 LarkAgentX 的 Python：%s 不存在" % path
    if not os.access(path, os.X_OK):
        return "LarkAgentX 的 Python 不能执行：%s 没有执行权限" % path
    try:
        res = run([path, "-I", "-S", "-c", "import sys; sys.stdout.write('%d.%d' % sys.version_info[:2])"],
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return "LarkAgentX 的 Python 运行不了：%s" % path
    out = (res.stdout or b"").decode("ascii", "replace").strip()
    try:
        version = tuple(int(x) for x in out.split("."))
    except ValueError:
        version = ()
    if res.returncode != 0 or len(version) != 2:
        return "LarkAgentX 的 Python 运行不了：%s" % path
    if version < PYTHON_LEAST:
        return "LarkAgentX 的 Python 是 %d.%d，需要 3.9 或更新" % version
    return ""


def decide(env: Dict[str, str], config: Optional[dict], check: Callable[[str], str] = check_python) -> Tuple[str, str]:
    """What to do with the settings of initialize: ("exec", interpreter) or ("stay", the reason, "" for none)."""
    backend = (env.get("OPENMATE_BACKEND") or "larkagentx").strip().lower()
    if backend != "larkagentx" or config is None:
        return "stay", ""
    python = str(config.get("larkx_python") or "").strip()
    if not python:
        return "stay", ""  # the backend says the setting is missing (not_configured)
    why = check(python)
    if why:
        return "stay", why
    return "exec", python


def _pipe_holding(data: bytes) -> int:
    """The read end of a pipe that yields data, then end of file; inheritable by an exec'd program."""
    r, w = os.pipe()
    if len(data) <= PIPE_DIRECT:
        view = memoryview(data)
        while view:
            view = view[os.write(w, view):]
        os.close(w)
    else:
        # More than a pipe may buffer: a helper writes it while the new program reads. Forked twice, so the helper
        # is not the new program's child (nobody has to reap it).
        pid = os.fork()
        if pid == 0:
            try:
                os.close(r)
                if os.fork() == 0:
                    try:
                        view = memoryview(data)
                        while view:
                            view = view[os.write(w, view):]
                    finally:
                        os._exit(0)
            finally:
                os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)
    os.set_inheritable(r, True)
    return r


def handoff(launcher: str, env: Optional[MutableMapping[str, str]] = None, stdin_fd: int = 0, stdout_fd: int = 1,
            execve: Callable = os.execve, check: Callable[[str], str] = check_python,
            early: Optional[Callable] = None) -> bytes:
    """Before the core runs: what it must read first (the line taken from stdin, or "" when nothing was read).
    May replace the process with larkx_python (then it does not return). early(method, params) answers a request
    that comes before initialize."""
    global _problem
    env = os.environ if env is None else env
    if env.get(CHILD):
        # The re-executed program: the first line is in the pipe the launcher left open.
        fd_text = env.pop(FIRST_FD, "")
        if not fd_text.isdigit():
            return b""
        fd = int(fd_text)
        try:
            return read_all(fd)
        finally:
            os.close(fd)
    backend = (env.get("OPENMATE_BACKEND") or "larkagentx").strip().lower()
    if backend != "larkagentx":
        return b""
    line, config = first_initialize(stdin_fd, stdout_fd, early)
    what, arg = decide(dict(env), config, check)
    if what != "exec":
        _problem = arg
        return line
    r = _pipe_holding(line)
    child = dict(env)
    child[CHILD] = arg
    child[FIRST_FD] = str(r)
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except (OSError, ValueError):
            pass
    try:
        execve(arg, [arg, "-I", "-B", launcher], child)
    except OSError:
        os.close(r)
        _problem = "没能用 LarkAgentX 的 Python 启动扩展：%s" % arg
        return line
    # execve only returns in tests (a fake that records the call)
    os.close(r)
    return line
