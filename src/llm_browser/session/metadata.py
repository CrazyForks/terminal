from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from llm_browser.events.event import now_ms


@dataclass(frozen=True)
class SessionMetadata:
    id: str
    state_dir: Path
    artifact_dir: Path
    cwd: Path
    status: str
    created_ms: int
    updated_ms: int
    parent_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "state_dir": str(self.state_dir),
            "artifact_dir": str(self.artifact_dir),
            "cwd": str(self.cwd),
            "status": self.status,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def create(
        cls,
        session_id: str,
        state_dir: Path,
        cwd: Path,
        parent_id: Optional[str] = None,
    ) -> "SessionMetadata":
        session_dir = state_dir / "sessions" / session_id
        created_ms = now_ms()
        return cls(
            id=session_id,
            parent_id=parent_id,
            state_dir=state_dir,
            artifact_dir=session_dir / "artifacts",
            cwd=cwd,
            status="created",
            created_ms=created_ms,
            updated_ms=created_ms,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionMetadata":
        return cls(
            id=str(data["id"]),
            parent_id=data.get("parent_id"),
            state_dir=Path(str(data["state_dir"])),
            artifact_dir=Path(str(data["artifact_dir"])),
            cwd=Path(str(data["cwd"])),
            status=str(data["status"]),
            created_ms=int(data["created_ms"]),
            updated_ms=int(data["updated_ms"]),
        )

    def with_status(self, status: str) -> "SessionMetadata":
        return SessionMetadata(
            id=self.id,
            parent_id=self.parent_id,
            state_dir=self.state_dir,
            artifact_dir=self.artifact_dir,
            cwd=self.cwd,
            status=status,
            created_ms=self.created_ms,
            updated_ms=now_ms(),
        )
