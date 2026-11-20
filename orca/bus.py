"""Small async publish/subscribe bus. Slow subscribers drop their oldest messages."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class Bus:
    def __init__(self, history: int = 500):
        self._subs: set[asyncio.Queue] = set()
        self.history: deque = deque(maxlen=history)

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, topic: str, msg: Any, keep: bool = False) -> None:
        if keep:
            self.history.append((topic, msg))
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait((topic, msg))
