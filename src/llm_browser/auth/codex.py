from __future__ import annotations

import base64
import contextlib
import json
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import requests

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_AUTH_ISSUER = "https://auth.openai.com"
OPENAI_TOKEN_URL = f"{OPENAI_AUTH_ISSUER}/oauth/token"
OPENAI_DEVICE_BASE_URL = f"{OPENAI_AUTH_ISSUER}/api/accounts"
OPENAI_DEVICE_VERIFICATION_URL = f"{OPENAI_AUTH_ISSUER}/codex/device"
AUTH_FILE_NAME = "auth.json"


@dataclass(frozen=True)
class CodexAuth:
    access_token: str
    account_id: str
    refresh_token: Optional[str]
    id_token: Optional[str]
    source_path: Path
    auth_mode: Optional[str] = None
    last_refresh: Optional[str] = None
    source: str = "unknown"
    expires_at: Optional[int] = None
    refresh_failed: Optional[str] = None

    def redacted_summary(self) -> Dict[str, Any]:
        return {
            "available": True,
            "source": self.source,
            "source_path": str(self.source_path),
            "auth_mode": self.auth_mode,
            "account_id": self.account_id,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "has_id_token": bool(self.id_token),
            "last_refresh": self.last_refresh,
            "expires_at": self.expires_at,
            "refresh_failed": self.refresh_failed,
        }

    def with_tokens(
        self,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        id_token: Optional[str] = None,
        account_id: Optional[str] = None,
        expires_at: Optional[int] = None,
        last_refresh: Optional[str] = None,
        source_path: Optional[Path] = None,
        source: Optional[str] = None,
        refresh_failed: Optional[str] = None,
    ) -> "CodexAuth":
        next_id_token = id_token if id_token is not None else self.id_token
        next_account_id = account_id or _account_id_from_id_token(next_id_token) or self.account_id
        return CodexAuth(
            access_token=access_token or self.access_token,
            account_id=next_account_id,
            refresh_token=refresh_token if refresh_token is not None else self.refresh_token,
            id_token=next_id_token,
            source_path=source_path or self.source_path,
            auth_mode=self.auth_mode or "chatgpt",
            last_refresh=last_refresh if last_refresh is not None else self.last_refresh,
            source=source or self.source,
            expires_at=expires_at if expires_at is not None else self.expires_at,
            refresh_failed=refresh_failed,
        )


@dataclass(frozen=True)
class DeviceCode:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval: int
    expires_in: int = 900

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "device_auth_id": self.device_auth_id,
            "interval": self.interval,
            "expires_in": self.expires_in,
        }


class CodexAuthError(RuntimeError):
    pass


class PermanentCodexAuthError(CodexAuthError):
    pass


def default_auth_home() -> Path:
    env = os.environ.get("LLM_BROWSER_AUTH_HOME") or os.environ.get("BROWSER_USE_TERMINAL_AUTH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".browser-use-terminal"


def harness_auth_path(auth_home: Optional[Path] = None) -> Path:
    return (auth_home or default_auth_home()).expanduser() / AUTH_FILE_NAME


def load_codex_auth(auth_home: Optional[Path] = None) -> Optional[CodexAuth]:
    """Load the harness-owned Codex auth, falling back to read-only Codex CLI auth.

    Passing `auth_home` keeps compatibility with older tests and loads
    `<auth_home>/auth.json` directly. With no argument, the harness store is
    preferred. The Codex CLI fallback is read-only and can be disabled with
    `LLM_BROWSER_DISABLE_CODEX_CLI_FALLBACK=1`.
    """

    if auth_home is not None:
        return _load_auth_file(auth_home.expanduser() / AUTH_FILE_NAME, default_source="provided")

    auth = load_harness_codex_auth()
    if auth is not None:
        return auth
    if _env_bool("LLM_BROWSER_DISABLE_CODEX_CLI_FALLBACK", False):
        return None
    return load_codex_cli_auth()


def load_harness_codex_auth(auth_home: Optional[Path] = None) -> Optional[CodexAuth]:
    return _load_auth_file(harness_auth_path(auth_home), default_source="harness")


def load_codex_cli_auth(codex_home: Optional[Path] = None) -> Optional[CodexAuth]:
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return _load_auth_file(home / AUTH_FILE_NAME, default_source="codex-cli-readonly")


def save_codex_auth(auth: CodexAuth, auth_home: Optional[Path] = None, source: Optional[str] = None) -> CodexAuth:
    path = harness_auth_path(auth_home)
    saved = auth.with_tokens(source_path=path, source=source or auth.source or "harness")
    with _auth_file_lock(path):
        _write_auth_file_locked(path, saved)
    return saved


def import_codex_cli_auth(
    codex_home: Optional[Path] = None,
    auth_home: Optional[Path] = None,
) -> CodexAuth:
    auth = load_codex_cli_auth(codex_home)
    if auth is None:
        home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        raise CodexAuthError(f"Codex CLI auth not found at {home / AUTH_FILE_NAME}")
    return save_codex_auth(auth.with_tokens(source="codex-cli-import"), auth_home=auth_home, source="codex-cli-import")


def logout_codex_auth(auth_home: Optional[Path] = None) -> bool:
    path = harness_auth_path(auth_home)
    with _auth_file_lock(path):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def request_device_code(session: Optional[requests.Session] = None) -> DeviceCode:
    http = session or requests.Session()
    response = http.post(
        f"{OPENAI_DEVICE_BASE_URL}/deviceauth/usercode",
        json={"client_id": CODEX_CLIENT_ID},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return DeviceCode(
        verification_url=OPENAI_DEVICE_VERIFICATION_URL,
        user_code=str(payload["user_code"]),
        device_auth_id=str(payload["device_auth_id"]),
        interval=int(payload.get("interval") or 5),
        expires_in=int(payload.get("expires_in") or 900),
    )


def complete_device_code_login(
    device_code: DeviceCode,
    auth_home: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    timeout_s: float = 900.0,
) -> CodexAuth:
    http = session or requests.Session()
    deadline = time.monotonic() + timeout_s
    code_payload: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        response = http.post(
            f"{OPENAI_DEVICE_BASE_URL}/deviceauth/token",
            json={"device_auth_id": device_code.device_auth_id, "user_code": device_code.user_code},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if response.status_code == 200:
            code_payload = response.json()
            break
        if response.status_code not in {403, 404}:
            raise CodexAuthError(f"device code polling failed: HTTP {response.status_code}: {response.text[:500]}")
        time.sleep(max(1, device_code.interval))
    if code_payload is None:
        raise TimeoutError("device code login timed out")

    token_payload = exchange_authorization_code(
        authorization_code=str(code_payload["authorization_code"]),
        code_verifier=str(code_payload["code_verifier"]),
        session=http,
        redirect_uri=f"{OPENAI_AUTH_ISSUER}/deviceauth/callback",
    )
    auth = _auth_from_token_response(
        token_payload,
        path=harness_auth_path(auth_home),
        source="device-code",
        previous=None,
    )
    return save_codex_auth(auth, auth_home=auth_home, source="device-code")


def exchange_authorization_code(
    authorization_code: str,
    code_verifier: str,
    session: Optional[requests.Session] = None,
    redirect_uri: str = f"{OPENAI_AUTH_ISSUER}/deviceauth/callback",
) -> Dict[str, Any]:
    http = session or requests.Session()
    response = http.post(
        OPENAI_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": authorization_code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def refresh_codex_auth(
    auth_home: Optional[Path] = None,
    auth: Optional[CodexAuth] = None,
    session: Optional[requests.Session] = None,
) -> CodexAuth:
    path = harness_auth_path(auth_home)
    with _auth_file_lock(path):
        current = auth or load_harness_codex_auth(auth_home)
        if current is None:
            raise CodexAuthError(f"harness Codex auth not found at {path}; run auth codex login or auth codex import")
        if not current.refresh_token:
            raise PermanentCodexAuthError("Codex auth has no refresh token; login again")
        http = session or requests.Session()
        response = http.post(
            OPENAI_TOKEN_URL,
            json={
                "client_id": CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if response.status_code == 401:
            failed = _classify_refresh_failure(response.text)
            failed_auth = current.with_tokens(refresh_failed=failed, last_refresh=_now_iso())
            _write_auth_file_locked(path, failed_auth)
            raise PermanentCodexAuthError(f"Codex refresh token is no longer usable: {failed}")
        if response.status_code >= 400:
            raise CodexAuthError(f"Codex token refresh failed: HTTP {response.status_code}: {response.text[:500]}")
        refreshed = _auth_from_token_response(response.json(), path=path, source=current.source, previous=current)
        _write_auth_file_locked(path, refreshed)
        return refreshed


def auth_status(auth_home: Optional[Path] = None, codex_home: Optional[Path] = None) -> Dict[str, Any]:
    harness = load_harness_codex_auth(auth_home)
    cli = load_codex_cli_auth(codex_home)
    return {
        "harness": harness.redacted_summary() if harness else {"available": False, "path": str(harness_auth_path(auth_home))},
        "codex_cli": cli.redacted_summary() if cli else {"available": False},
    }


def _load_auth_file(path: Path, default_source: str) -> Optional[CodexAuth]:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token")
    id_token_raw = tokens.get("id_token")
    id_token = id_token_raw if isinstance(id_token_raw, str) else None
    account_id = tokens.get("account_id") or tokens.get("chatgpt_account_id") or _account_id_from_id_token(id_token)
    if not access_token or not account_id:
        return None
    return CodexAuth(
        access_token=str(access_token),
        account_id=str(account_id),
        refresh_token=_optional_str(tokens.get("refresh_token")),
        id_token=id_token,
        source_path=path,
        auth_mode=_optional_str(data.get("auth_mode")) or "chatgpt",
        last_refresh=_optional_str(data.get("last_refresh")),
        source=_optional_str(data.get("source")) or default_source,
        expires_at=_optional_int(data.get("expires_at") or tokens.get("expires_at")),
        refresh_failed=_optional_str(data.get("refresh_failed")),
    )


def _auth_from_token_response(
    payload: Dict[str, Any],
    path: Path,
    source: str,
    previous: Optional[CodexAuth],
) -> CodexAuth:
    access_token = str(payload.get("access_token") or (previous.access_token if previous else ""))
    refresh_token = _optional_str(payload.get("refresh_token")) or (previous.refresh_token if previous else None)
    id_token = _optional_str(payload.get("id_token")) or (previous.id_token if previous else None)
    if not access_token:
        raise CodexAuthError("token response did not include an access token")
    account_id = _optional_str(payload.get("account_id") or payload.get("chatgpt_account_id"))
    account_id = account_id or _account_id_from_id_token(id_token) or (previous.account_id if previous else None)
    if not account_id:
        raise CodexAuthError("token response did not include an account id and id_token had none")
    expires_in = _optional_int(payload.get("expires_in"))
    expires_at = int(time.time()) + expires_in if expires_in else (previous.expires_at if previous else None)
    return CodexAuth(
        access_token=access_token,
        account_id=account_id,
        refresh_token=refresh_token,
        id_token=id_token,
        source_path=path,
        auth_mode="chatgpt",
        last_refresh=_now_iso(),
        source=source,
        expires_at=expires_at,
        refresh_failed=None,
    )


def _auth_to_payload(auth: CodexAuth) -> Dict[str, Any]:
    return {
        "provider": "openai-codex",
        "auth_mode": auth.auth_mode or "chatgpt",
        "source": auth.source,
        "last_refresh": auth.last_refresh or _now_iso(),
        "expires_at": auth.expires_at,
        "refresh_failed": auth.refresh_failed,
        "tokens": {
            "access_token": auth.access_token,
            "refresh_token": auth.refresh_token,
            "id_token": auth.id_token,
            "account_id": auth.account_id,
        },
    }


def _write_auth_file_locked(path: Path, auth: CodexAuth) -> None:
    payload = _auth_to_payload(auth)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _account_id_from_id_token(id_token: Optional[str]) -> Optional[str]:
    if not id_token or id_token.count(".") < 2:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    value = claims.get("https://api.openai.com/auth") or {}
    if isinstance(value, dict) and value.get("chatgpt_account_id"):
        return str(value["chatgpt_account_id"])
    if claims.get("chatgpt_account_id"):
        return str(claims["chatgpt_account_id"])
    return None


def _classify_refresh_failure(body: str) -> str:
    text = body.lower()
    if "refresh_token_expired" in text:
        return "expired"
    if "refresh_token_reused" in text:
        return "reused"
    if "refresh_token_invalidated" in text:
        return "revoked"
    return "unknown"


@contextlib.contextmanager
def _auth_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}
