from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, List

from llm_browser.events.event import Event


class EventStore:
    """Append-only JSONL event storage."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.sessions_dir = self.state_dir / "sessions"
        self._lock = threading.Lock()

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def event_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def append(self, event: Event) -> Event:
        path = self.event_path(event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
        return event

    def append_many(self, events: Iterable[Event]) -> List[Event]:
        written: List[Event] = []
        for event in events:
            written.append(self.append(event))
        return written

    def read(self, session_id: str) -> List[Event]:
        path = self.event_path(session_id)
        if not path.exists():
            return []
        events: List[Event] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(Event.from_dict(json.loads(line)))
        return events
