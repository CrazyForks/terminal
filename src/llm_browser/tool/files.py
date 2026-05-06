from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from llm_browser.tool.context import ToolContext
from llm_browser.tool.result import ToolResult


MAX_READ_CHARS = 20000
MAX_DIFF_CHARS = 20000
MAX_SEARCH_OUTPUT = 30000
DEFAULT_REPO_MAP_SCAN = 2000
MAX_REPO_MAP_SCAN = 20000
DEFAULT_REPO_MAP_LIMIT = 30
MAX_REPO_MAP_LIMIT = 80
_FILE_LOCKS: Dict[str, threading.RLock] = {}
_FILE_LOCKS_LOCK = threading.Lock()
_REPO_MAP_IGNORED_DIRS = {
    ".browser-use-terminal",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
_REPO_MAP_MANIFEST_NAMES = {
    "cargo.toml",
    "composer.json",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tsconfig.json",
    "uv.lock",
}
_REPO_MAP_DOC_NAMES = {
    "agents.md",
    "changelog.md",
    "contributing.md",
    "readme.md",
}
_REPO_MAP_ENTRYPOINT_NAMES = {
    "__main__.py",
    "app.py",
    "asgi.py",
    "cli.py",
    "index.js",
    "index.jsx",
    "index.ts",
    "index.tsx",
    "lib.rs",
    "main.go",
    "main.py",
    "main.rs",
    "main.ts",
    "main.tsx",
    "manage.py",
    "mod.rs",
    "server.py",
    "wsgi.py",
}
_REPO_MAP_SOURCE_ROOT_NAMES = {
    "app",
    "cmd",
    "docs",
    "examples",
    "internal",
    "lib",
    "pkg",
    "scripts",
    "src",
    "test",
    "tests",
}
_REPO_MAP_WORKSPACE_ROOT_NAMES = {"apps", "crates", "packages", "services"}


def read_file(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(arguments["path"]))
    if not path.exists():
        raise FileNotFoundError(_missing_path_message(path))
    if path.is_dir():
        limit = int(arguments.get("limit", 200))
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:limit]
        text = "\n".join(("/" if entry.is_dir() else "") + entry.name for entry in entries)
        return ToolResult(text=text, data={"path": str(path), "kind": "directory", "count": len(entries)})

    raw = path.read_bytes()
    if _looks_binary(raw):
        return ToolResult(
            text=f"{path} appears to be binary; use shell/file-specific tooling to inspect it.",
            data={"path": str(path), "kind": "binary", "bytes": len(raw), "binary": True},
        )

    text, _ = _decode_text(raw)
    line_offset = arguments.get("line_offset")
    line_limit = arguments.get("line_limit")
    if line_offset is not None or line_limit is not None:
        start = max(0, int(line_offset or 0))
        limit = int(line_limit or 200)
        lines = text.splitlines(keepends=True)
        chunk = "".join(lines[start : start + limit])
        return ToolResult(
            text=chunk,
            data={"path": str(path), "kind": "text", "line_offset": start, "line_count": len(lines), "returned_lines": min(limit, len(lines) - start)},
        )

    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", MAX_READ_CHARS))
    chunk = text[offset : offset + limit]
    return ToolResult(text=chunk, data={"path": str(path), "kind": "text", "offset": offset, "total_chars": len(text)})


def write_file(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(arguments["path"]))
    content = str(arguments.get("content", ""))
    with _file_lock(path):
        old_text = ""
        old_meta = _TextMeta()
        existed = path.exists()
        if existed:
            old_text, old_meta = _read_text_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        newline = old_meta.newline if existed else _detect_newline(content)
        next_text = _normalize_newlines(content, newline)
        _write_text_file(path, next_text, old_meta if existed else _TextMeta(newline=newline))
    diff = _unified_diff(old_text, next_text, fromfile=str(path) if existed else "/dev/null", tofile=str(path))
    return ToolResult(
        text=f"wrote {path}\n{_cap(diff, MAX_DIFF_CHARS)}",
        data={"path": str(path), "chars": len(next_text), "created": not existed, "diff": _cap(diff, MAX_DIFF_CHARS)},
    )


def edit_file(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(arguments["path"]))
    old = str(arguments["old"])
    new = str(arguments["new"])
    replace_all = bool(arguments.get("replace_all", False))
    with _file_lock(path):
        text, meta = _read_text_file(path)
        count = text.count(old)
        if count == 0:
            raise ValueError(f"old text not found in {path}\n{_near_miss_message(text, old)}")
        if count > 1 and not replace_all:
            raise ValueError(f"old text appears {count} times in {path}; set replace_all=true or make old text unique")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        _write_text_file(path, updated, meta)
    changed = count if replace_all else 1
    diff = _unified_diff(text, updated, fromfile=str(path), tofile=str(path))
    return ToolResult(
        text=f"edited {path}\n{_cap(diff, MAX_DIFF_CHARS)}",
        data={"path": str(path), "replacements": changed, "diff": _cap(diff, MAX_DIFF_CHARS)},
    )


def apply_patch_file(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    patch = str(arguments["patch"])
    check = bool(arguments.get("check", False))
    command = ["git", "apply", "--recount", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    result = subprocess.run(
        command,
        cwd=str(ctx.session.cwd),
        input=patch,
        text=True,
        capture_output=True,
        timeout=30,
    )
    output = (result.stdout or "") + (("\n" if result.stdout and result.stderr else "") + result.stderr if result.stderr else "")
    if result.returncode != 0:
        raise ValueError(f"patch failed with status {result.returncode}: {output.strip()}")
    text = "patch check passed" if check else "patch applied"
    changed_files = _patch_paths(patch)
    return ToolResult(
        text=(text + (f"\nfiles: {', '.join(changed_files)}" if changed_files else "") + (f"\n{output}" if output else "")),
        data={"returncode": result.returncode, "check": check, "files": changed_files},
    )


def glob_files(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    pattern = str(arguments["pattern"])
    root = _resolve(ctx, str(arguments.get("root", ".")))
    if not root.exists():
        raise FileNotFoundError(_missing_path_message(root))
    limit = int(arguments.get("limit", 200))
    recursive = bool(arguments.get("recursive", True))
    if root.is_file():
        matches = [root] if fnmatch.fnmatch(root.name, pattern) else []
    else:
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        matches = sorted(
            [path for path in iterator if path.exists()],
            key=lambda path: (path.stat().st_mtime if path.exists() else 0),
            reverse=True,
        )[:limit]
    text = "\n".join(str(path) for path in matches)
    return ToolResult(text=text, data={"root": str(root), "pattern": pattern, "count": len(matches), "recursive": recursive})


def grep_files(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    pattern = str(arguments["pattern"])
    root = _resolve(ctx, str(arguments.get("root", ".")))
    include = str(arguments.get("include", "*"))
    limit = int(arguments.get("limit", 200))
    if not root.exists():
        raise FileNotFoundError(_missing_path_message(root))
    rg_result = _grep_with_rg(pattern=pattern, root=root, include=include, limit=limit)
    if rg_result is not None:
        return rg_result
    lines: List[str] = []
    for path in _iter_files(root, include):
        if len(lines) >= limit:
            break
        try:
            raw = path.read_bytes()
            if _looks_binary(raw):
                continue
            text, _ = _decode_text(raw)
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern in line:
                lines.append(f"{path}:{lineno}:{line}")
                if len(lines) >= limit:
                    break
    return ToolResult(text="\n".join(lines), data={"root": str(root), "pattern": pattern, "count": len(lines), "engine": "python"})


def repo_map(ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    root = _resolve(ctx, str(arguments.get("root", ".")))
    if not root.exists():
        raise FileNotFoundError(_missing_path_message(root))
    if root.is_file():
        root = root.parent

    scan_limit = _bounded_int(arguments.get("scan_limit"), DEFAULT_REPO_MAP_SCAN, 1, MAX_REPO_MAP_SCAN)
    section_limit = _bounded_int(arguments.get("limit"), DEFAULT_REPO_MAP_LIMIT, 1, MAX_REPO_MAP_LIMIT)
    files, truncated = _repo_file_list(root, scan_limit)
    git_root = _git_root(root)
    top_level = _top_level_entries(root, section_limit)
    manifests = _select_known_files(files, _REPO_MAP_MANIFEST_NAMES, section_limit)
    docs = _select_known_files(files, _REPO_MAP_DOC_NAMES, section_limit)
    source_roots = _source_roots(files, section_limit)
    entrypoints = _entrypoint_files(files, section_limit)
    tests = _test_files(files, section_limit)
    suggested_next_reads = _suggested_next_reads(docs, manifests, entrypoints, tests, source_roots, section_limit)

    lines = [
        f"Repository map for {root}",
        f"Git root: {git_root or 'not detected'}",
        f"Scanned files: {len(files)}" + (" (truncated)" if truncated else ""),
    ]
    lines.extend(_format_repo_map_section("Top-level entries", top_level))
    lines.extend(_format_repo_map_section("Docs", docs))
    lines.extend(_format_repo_map_section("Manifests", manifests))
    lines.extend(_format_repo_map_section("Source roots", [f"{item['path']} ({item['files']} files)" for item in source_roots]))
    lines.extend(_format_repo_map_section("Likely entrypoints", entrypoints))
    lines.extend(_format_repo_map_section("Likely tests", tests))
    lines.extend(_format_repo_map_section("Suggested next reads", suggested_next_reads))
    if truncated:
        lines.append("Scan truncated; pass a larger scan_limit or use glob/grep for targeted follow-up.")

    return ToolResult(
        text="\n".join(lines),
        data={
            "root": str(root),
            "git_root": git_root,
            "scanned_files": len(files),
            "scan_limit": scan_limit,
            "truncated": truncated,
            "top_level": top_level,
            "docs": docs,
            "manifests": manifests,
            "source_roots": source_roots,
            "entrypoints": entrypoints,
            "tests": tests,
            "suggested_next_reads": suggested_next_reads,
        },
    )


class _TextMeta:
    def __init__(self, newline: str = "\n", bom: bool = False) -> None:
        self.newline = newline
        self.bom = bom


def _resolve(ctx: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = ctx.session.cwd / candidate
    return candidate.resolve()


def _file_lock(path: Path):
    key = str(path)
    with _FILE_LOCKS_LOCK:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
    return lock


def _read_text_file(path: Path) -> Tuple[str, _TextMeta]:
    if not path.exists():
        raise FileNotFoundError(_missing_path_message(path))
    raw = path.read_bytes()
    if _looks_binary(raw):
        raise ValueError(f"{path} appears to be binary; refusing text edit")
    text, bom = _decode_text(raw)
    return text, _TextMeta(newline=_detect_newline(text), bom=bom)


def _write_text_file(path: Path, text: str, meta: _TextMeta) -> None:
    data = text.encode("utf-8")
    if meta.bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def _decode_text(raw: bytes) -> Tuple[str, bool]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace"), bom


def _looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    if b"\x00" in raw[:4096]:
        return True
    sample = raw[:4096]
    textish = sum(1 for byte in sample if byte in b"\n\r\t\b\f" or 32 <= byte <= 126 or byte >= 128)
    return (textish / max(1, len(sample))) < 0.85


def _detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "\n":
        return normalized
    return normalized.replace("\n", newline)


def _unified_diff(old: str, new: str, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def _grep_with_rg(pattern: str, root: Path, include: str, limit: int) -> Optional[ToolResult]:
    if shutil.which("rg") is None:
        return None
    command = [
        "rg",
        "--line-number",
        "--fixed-strings",
        "--color=never",
        "--glob",
        include,
        "--",
        pattern,
        str(root),
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if result.returncode not in {0, 1}:
        return None
    lines = result.stdout.splitlines()[:limit]
    text = "\n".join(lines)
    if len(text) > MAX_SEARCH_OUTPUT:
        text = _cap(text, MAX_SEARCH_OUTPUT)
    return ToolResult(text=text, data={"root": str(root), "pattern": pattern, "count": len(lines), "engine": "rg"})


def _iter_files(root: Path, include: str) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and fnmatch.fnmatch(path.name, include):
            yield path


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _repo_file_list(root: Path, limit: int) -> Tuple[List[str], bool]:
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = sorted((name for name in dirnames if not _repo_map_ignored_dir(name)), key=str.lower)
        for filename in sorted(filenames, key=str.lower):
            if len(files) >= limit:
                return files, True
            path = Path(dirpath) / filename
            try:
                rel_path = path.relative_to(root).as_posix()
            except ValueError:
                continue
            files.append(rel_path)
    return files, False


def _repo_map_ignored_dir(name: str) -> bool:
    return name in _REPO_MAP_IGNORED_DIRS or name.endswith(".egg-info")


def _git_root(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _top_level_entries(root: Path, limit: int) -> List[str]:
    entries: List[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError:
        return entries
    for child in children:
        if child.is_dir() and _repo_map_ignored_dir(child.name):
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) >= limit:
            break
    return entries


def _select_known_files(files: List[str], names: set[str], limit: int) -> List[str]:
    matches = [path for path in files if _basename(path).lower() in names]
    return sorted(matches, key=_repo_map_path_sort_key)[:limit]


def _source_roots(files: List[str], limit: int) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for path in files:
        parts = path.split("/")
        if not parts:
            continue
        first = parts[0]
        first_lower = first.lower()
        root_name: Optional[str] = None
        if first_lower in _REPO_MAP_WORKSPACE_ROOT_NAMES and len(parts) > 1:
            root_name = f"{first}/{parts[1]}"
        elif first_lower in _REPO_MAP_SOURCE_ROOT_NAMES:
            root_name = first
        if root_name is not None:
            counts[root_name] = counts.get(root_name, 0) + 1
    return [
        {"path": path, "files": counts[path]}
        for path in sorted(counts, key=lambda item: (-counts[item], item.lower()))[:limit]
    ]


def _entrypoint_files(files: List[str], limit: int) -> List[str]:
    matches = [path for path in files if _basename(path).lower() in _REPO_MAP_ENTRYPOINT_NAMES]
    return sorted(matches, key=_entrypoint_sort_key)[:limit]


def _test_files(files: List[str], limit: int) -> List[str]:
    matches = []
    for path in files:
        basename = _basename(path).lower()
        parts = [part.lower() for part in path.split("/")]
        if (
            "tests" in parts
            or "test" in parts
            or basename.startswith("test_")
            or basename.endswith("_test.py")
            or ".spec." in basename
            or ".test." in basename
        ):
            matches.append(path)
    return sorted(matches, key=_repo_map_path_sort_key)[:limit]


def _suggested_next_reads(
    docs: List[str],
    manifests: List[str],
    entrypoints: List[str],
    tests: List[str],
    source_roots: List[Dict[str, Any]],
    limit: int,
) -> List[str]:
    candidates: List[str] = []
    candidates.extend(docs)
    candidates.extend(manifests)
    candidates.extend(entrypoints[:8])
    candidates.extend(item["path"] for item in source_roots[:8])
    candidates.extend(tests[:5])
    return _dedupe(candidates)[:limit]


def _format_repo_map_section(title: str, values: List[str]) -> List[str]:
    if not values:
        return [f"{title}: none"]
    return [f"{title}:"] + [f"  - {value}" for value in values]


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _repo_map_path_sort_key(path: str) -> Tuple[int, str]:
    return (path.count("/"), path.lower())


def _entrypoint_sort_key(path: str) -> Tuple[int, int, str]:
    basename = _basename(path).lower()
    priority = 0 if basename in {"main.py", "cli.py", "app.py", "server.py", "index.ts", "index.tsx"} else 1
    return (priority, path.count("/"), path.lower())


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _missing_path_message(path: Path) -> str:
    parent = path.parent if path.parent.exists() else _nearest_existing_parent(path)
    suggestions: List[str] = []
    if parent and parent.exists():
        needle = path.name.lower()
        for child in sorted(parent.iterdir(), key=lambda item: item.name.lower()):
            if needle in child.name.lower() or child.name.lower() in needle:
                suggestions.append(str(child))
            if len(suggestions) >= 8:
                break
    message = f"path not found: {path}"
    if suggestions:
        message += "\ndid you mean:\n" + "\n".join(suggestions)
    return message


def _nearest_existing_parent(path: Path) -> Optional[Path]:
    for parent in path.parents:
        if parent.exists():
            return parent
    return None


def _near_miss_message(text: str, needle: str) -> str:
    if not needle:
        return "old text is empty"
    lines = text.splitlines()
    needle_head = needle.strip().splitlines()[0][:80] if needle.strip() else needle[:80]
    candidates = difflib.get_close_matches(needle_head, [line[:120] for line in lines], n=5, cutoff=0.45)
    if not candidates:
        return "no close line matches found"
    return "closest lines:\n" + "\n".join(candidates)


def _patch_paths(patch: str) -> List[str]:
    paths: List[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            value = line[4:].strip()
            if value == "/dev/null":
                continue
            if value.startswith("a/") or value.startswith("b/"):
                value = value[2:]
            if value not in paths:
                paths.append(value)
    return paths


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}\n\n[... omitted {len(text) - max_chars} chars ...]\n\n{text[-tail:]}"
