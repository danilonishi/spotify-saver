"""Live notification fan-out for connected browser clients."""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import uuid4


class NotificationHub:
    """Broadcast notification events and replay a short reconnect window."""

    def __init__(self, history_limit: int = 100):
        self._history: deque[dict] = deque(maxlen=history_limit)
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._lock = asyncio.Lock()
        self._instance_id = uuid4().hex
        self._next_id = 1

    async def publish(self, category: str, message: str, **details: str) -> None:
        async with self._lock:
            event = {
                "id": f"{self._instance_id}:{self._next_id}",
                "category": category,
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **details,
            }
            self._next_id += 1
            self._history.append(event)
            for subscriber in self._subscribers:
                subscriber.put_nowait(event)

    async def subscribe(self, last_event_id: str = "") -> AsyncIterator[dict]:
        subscriber: asyncio.Queue[dict] = asyncio.Queue()
        prefix, separator, sequence = last_event_id.rpartition(":")
        replay_after = (
            int(sequence)
            if separator and prefix == self._instance_id and sequence.isdigit()
            else 0
        )
        async with self._lock:
            replay = [
                event
                for index, event in enumerate(
                    self._history, start=max(1, self._next_id - len(self._history))
                )
                if index > replay_after
            ]
            self._subscribers.add(subscriber)
        try:
            for event in replay:
                yield event
            while True:
                try:
                    yield await asyncio.wait_for(subscriber.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield {"comment": "keep-alive"}
        finally:
            async with self._lock:
                self._subscribers.discard(subscriber)


def encode_sse_event(event: dict) -> str:
    """Serialize a notification event as a Server-Sent Event."""
    if "comment" in event:
        return f": {event['comment']}\n\n"
    return f"id: {event['id']}\nevent: notification\ndata: {json.dumps(event)}\n\n"
