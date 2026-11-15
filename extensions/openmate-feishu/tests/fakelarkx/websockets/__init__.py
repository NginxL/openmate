"""A stand-in for websockets: connect() opens a fake connection that yields larkx._fake.STATE.frames."""

import asyncio
import queue

from . import exceptions  # noqa: F401


class _Connection:
    def __init__(self, state):
        self.frames = state.frames  # its own: a connection left over from an earlier test reads nothing of a later one
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.closed:
            try:
                item = self.frames.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            if item == "close":
                self.closed = True
                break
            if item == "drop":
                self.closed = True
                raise exceptions.ConnectionClosed("dropped")
            return item
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


async def _open(url, kwargs):
    from larkx._fake import STATE

    STATE.ws_kwargs.append(dict(kwargs, url=url))
    what = STATE.ws_connects.pop(0) if STATE.ws_connects else "open"
    if isinstance(what, BaseException):
        raise what
    STATE.ws = _Connection(STATE)
    return STATE.ws


def connect(url, **kwargs):
    return _open(url, kwargs)
