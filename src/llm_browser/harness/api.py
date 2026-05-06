from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from llm_browser.browser.helpers import ensure_agent_helpers_file
from llm_browser.tool.result import ToolImage

if TYPE_CHECKING:
    from llm_browser.browser import BrowserRuntime
    from llm_browser.tool.context import ToolContext


@dataclass
class HelperAPI:
    """Small capability object shared by core helpers and skill installers."""

    ctx: "ToolContext"
    runtime: "BrowserRuntime"
    images: List[ToolImage]
    namespace: Dict[str, Any]
    check_cancel: Callable[[], None]
    cancel_requested: Callable[[], bool]
    sleep: Callable[[float], None]

    @property
    def cwd(self) -> Path:
        return self.ctx.session.cwd

    @property
    def artifact_dir(self) -> Path:
        return self.ctx.session.artifact_dir

    @property
    def download_dir(self) -> Path:
        return Path(getattr(self.runtime, "downloads_dir", self.runtime.root_dir / "downloads"))

    @property
    def output_dir(self) -> Path:
        return self.cwd / "outputs"

    def emit_image(self, image: ToolImage) -> ToolImage:
        self.images.append(image)
        self.ctx.emit_image(image)
        return image

    def resolve_path(self, path: Any, *, base: Optional[Path] = None) -> Path:
        value = Path(str(path)).expanduser()
        if value.is_absolute():
            return value
        return (base or self.cwd) / value

    def output_path(self, path: str = "") -> str:
        requested = Path(path).expanduser()
        if not path:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            return str(self.output_dir)
        if requested.is_absolute():
            try:
                relative = requested.relative_to("/home/user/outputs")
            except ValueError:
                requested.parent.mkdir(parents=True, exist_ok=True)
                return str(requested)
            target = self.output_dir / relative
        else:
            target = self.cwd / requested
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def copy_to_artifact(self, source: Path, name: Optional[str] = None) -> Path:
        source = source.expanduser()
        if not source.is_absolute():
            source = (self.cwd / source).resolve()
        safe_name = safe_artifact_name(name or source.name)
        target = self.artifact_dir / "python-artifacts" / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def agent_helpers_path(self) -> Path:
        return ensure_agent_helpers_file(self.cwd)


def safe_artifact_name(name: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in Path(name).name)
    return safe_name or "artifact.bin"
