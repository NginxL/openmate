"""Run a platform's connector over stdin / stdout: stdout carries only protocol lines, logs go to stderr."""

from __future__ import annotations

import logging
import os
import sys

from .app import ConnectorApp
from .platform import Platform, Timing
from .wire import Peer


class PrefixedInput:
    """The protocol's input with a first line that was read before the program started (a launcher that looked at
    initialize to pick its interpreter, then handed the line on): readline answers it first, then reads on."""

    def __init__(self, first: bytes, rest):
        self._first = first or b""
        self._rest = rest

    def readline(self) -> bytes:
        if self._first:
            first, self._first = self._first, b""
            if first.endswith(b"\n"):
                return first
            return first + self._rest.readline()  # a line cut short: its rest is still in the stream
        return self._rest.readline()


def setup_logging(name: str) -> int:
    """OPENMATE_LOG sets OpenMate's own loggers only. Other libraries stay at INFO or above: at DEBUG websockets
    logs handshakes, headers and frames, which carry the connection's ticket. The level OpenMate's loggers got."""
    level = logging.getLevelName((name or "INFO").strip().upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=max(level, logging.INFO), stream=sys.stderr, format="%(name)s %(levelname)s %(message)s")
    logging.getLogger().setLevel(max(level, logging.INFO))
    logging.getLogger("openmate").setLevel(level)
    return level


def run(platform: Platform, timing: Timing = None, first: bytes = b"") -> int:
    """Serve the protocol until the hub closes stdin. first: the start of the input, already read (PrefixedInput)."""
    os.umask(0o077)
    sys.dont_write_bytecode = True
    setup_logging(os.environ.get("OPENMATE_LOG", ""))
    # The protocol gets its own copy of stdout; fd 1 then points at stderr, so a stray print (ours or a library's)
    # can never corrupt the protocol stream.
    proto_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_in = PrefixedInput(first, sys.stdin.buffer) if first else sys.stdin.buffer
    app = ConnectorApp(platform, timing=timing)
    peer = Peer(proto_in, proto_out, app.handle, on_event=app.on_event, after=app.after)
    app.attach(peer)
    try:
        peer.serve()  # until the hub closes stdin
    finally:
        app.shutdown()
    return 0
