from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from llm_browser.auth import (
    CodexAuth,
    PermanentCodexAuthError,
    auth_status,
    import_codex_cli_auth,
    load_codex_auth,
    load_harness_codex_auth,
    refresh_codex_auth,
)


class CodexAuthTest(unittest.TestCase):
    def test_loads_codex_auth_without_exposing_tokens_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": "access-secret",
                            "refresh_token": "refresh-secret",
                            "id_token": "id-secret",
                            "account_id": "acct_123",
                        },
                        "last_refresh": "2026-05-06T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            auth = load_codex_auth(home)

            self.assertIsNotNone(auth)
            assert auth is not None
            self.assertEqual(auth.access_token, "access-secret")
            summary = auth.redacted_summary()
            self.assertEqual(summary["account_id"], "acct_123")
            self.assertTrue(summary["has_access_token"])
            self.assertNotIn("access-secret", json.dumps(summary))
            self.assertNotIn("refresh-secret", json.dumps(summary))

    def test_missing_auth_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_codex_auth(Path(tmp)))

    def test_import_codex_cli_auth_writes_harness_store_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            auth_home = root / "harness"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": "access-secret",
                            "refresh_token": "refresh-secret",
                            "account_id": "acct_123",
                        },
                    }
                ),
                encoding="utf-8",
            )

            auth = import_codex_cli_auth(codex_home=codex_home, auth_home=auth_home)
            loaded = load_harness_codex_auth(auth_home)

            self.assertEqual(auth.source, "codex-cli-import")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.access_token, "access-secret")
            mode = stat.S_IMODE(os.stat(auth_home / "auth.json").st_mode)
            self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_refresh_codex_auth_updates_tokens_under_harness_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_home = Path(tmp)
            original = CodexAuth(
                access_token="old-access",
                account_id="acct_123",
                refresh_token="old-refresh",
                id_token=None,
                source_path=auth_home / "auth.json",
                source="harness",
            )
            from llm_browser.auth import save_codex_auth

            save_codex_auth(original, auth_home=auth_home)
            response = Mock(status_code=200)
            response.json.return_value = {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
            session = Mock()
            session.post.return_value = response

            refreshed = refresh_codex_auth(auth_home=auth_home, session=session)

            self.assertEqual(refreshed.access_token, "new-access")
            self.assertEqual(refreshed.refresh_token, "new-refresh")
            self.assertEqual(load_harness_codex_auth(auth_home).access_token, "new-access")  # type: ignore[union-attr]
            self.assertEqual(session.post.call_args.kwargs["json"]["grant_type"], "refresh_token")

    def test_refresh_codex_auth_marks_permanent_refresh_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_home = Path(tmp)
            original = CodexAuth(
                access_token="old-access",
                account_id="acct_123",
                refresh_token="old-refresh",
                id_token=None,
                source_path=auth_home / "auth.json",
                source="harness",
            )
            from llm_browser.auth import save_codex_auth

            save_codex_auth(original, auth_home=auth_home)
            response = Mock(status_code=401, text='{"error":"refresh_token_reused"}')
            session = Mock()
            session.post.return_value = response

            with self.assertRaises(PermanentCodexAuthError):
                refresh_codex_auth(auth_home=auth_home, session=session)

            loaded = load_harness_codex_auth(auth_home)
            assert loaded is not None
            self.assertEqual(loaded.refresh_failed, "reused")

    def test_auth_status_reports_harness_and_cli_without_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_home = root / "harness"
            codex_home = root / "codex"
            auth_home.mkdir()
            codex_home.mkdir()
            payload = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "account_id": "acct_123",
                },
            }
            (auth_home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
            (codex_home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")

            status_payload = auth_status(auth_home=auth_home, codex_home=codex_home)

            rendered = json.dumps(status_payload)
            self.assertIn("acct_123", rendered)
            self.assertNotIn("access-secret", rendered)
            self.assertNotIn("refresh-secret", rendered)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
