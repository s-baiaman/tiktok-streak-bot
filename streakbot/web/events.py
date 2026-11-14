"""Tiny pub/sub so open browser tabs learn that something changed."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class EventHub:
    def __init__(self) -> None:
        self._flags: set[asyncio.Event] = set()

    def publish(self) -> None:
        """Tell every subscriber something changed (bursts coalesce). Call from the event loop thread."""
        for flag in self._flags:
            flag.set()

    async def subscribe(self, ping_interval: float = 25.0) -> AsyncIterator[str]:
        """Yields "changed" after a publish, or "ping" after `ping_interval` seconds of quiet."""
        flag = asyncio.Event()
        self._flags.add(flag)
        try:
            while True:
                try:
                    await asyncio.wait_for(flag.wait(), ping_interval)
                except TimeoutError:
                    yield "ping"
                else:
                    flag.clear()
                    yield "changed"
        finally:
            self._flags.discard(flag)
