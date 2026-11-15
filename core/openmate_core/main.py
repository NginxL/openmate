"""Run a platform's connector over stdin / stdout: stdout carries only protocol lines, logs go to stderr."""

from __future__ import annotations

import logging
import os
import sys

from .app import ConnectorApp
from .platform import Platform, Timing
from .wire import Peer


def run(platform: Platform, timing: Timing = None) -> int:
    os.umask(0o077)
    sys.dont_write_bytecode = True
    logging.basicConfig(level=os.environ.get("OPENMATE_LOG", "INFO").upper(), stream=sys.stderr,
                        format="%(name)s %(levelname)s %(message)s")
    # The protocol gets its own copy of stdout; fd 1 then points at stderr, so a stray print (ours or a library's)
    # can never corrupt the protocol stream.
    proto_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_in = sys.stdin.buffer
    app = ConnectorApp(platform, timing=timing)
    peer = Peer(proto_in, proto_out, app.handle, on_event=app.on_event, after=app.after)
    app.attach(peer)
    try:
        peer.serve()  # until the hub closes stdin
    finally:
        app.shutdown()
    return 0
