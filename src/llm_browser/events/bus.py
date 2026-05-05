from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Iterator, List

from llm_browser.events.event import Event


class EventBus:
    """Small in-process pub/sub bus for live session events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue[Event]"] = []

    @contextmanager
    def subscribe(self) -> Iterator["queue.Queue[Event]"]:
        q: "queue.Queue[Event]" = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        try:
            yield q
        finally:
            with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)
