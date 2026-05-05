from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Event:
    type: str
    session_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts_ms: int = field(default_factory=now_ms)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "ts_ms": self.ts_ms,
            "type": self.type,
            "session_id": self.session_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        return cls(
            version=int(data.get("version", 1)),
            id=str(data["id"]),
            ts_ms=int(data["ts_ms"]),
            type=str(data["type"]),
            session_id=str(data["session_id"]),
            payload=dict(data.get("payload") or {}),
        )
