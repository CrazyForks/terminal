from pathlib import Path

from llm_browser_worker import worker


def test_worker_run_executes_in_persistent_session_namespace(tmp_path: Path) -> None:
    first = worker._run(
        {
            "id": "one",
            "session_id": "task-1",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "counter = globals().get('counter', 0) + 1\nresult = counter\nemit_output(f'counter={counter}')",
        }
    )
    second = worker._run(
        {
            "id": "two",
            "session_id": "task-1",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "counter = globals().get('counter', 0) + 1\nresult = counter",
        }
    )

    assert first["ok"] is True
    assert first["data"] == 1
    assert first["outputs"] == [{"text": "counter=1"}]
    assert second["ok"] is True
    assert second["data"] == 2


def test_worker_records_artifacts_and_images(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"png")

    response = worker._run(
        {
            "id": "image",
            "session_id": "task-2",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": f"emit_image({str(source)!r}, label='shot', mime_type='image/png')",
        }
    )

    assert response["ok"] is True
    assert response["images"][0]["label"] == "shot"
    assert response["images"][0]["mime_type"] == "image/png"
    assert Path(response["images"][0]["path"]).exists()


def test_worker_records_browser_state_details(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "browser-state",
            "session_id": "task-3",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "emit_browser_state(url='https://example.com', title='Example', status='connected', tabs=2, viewport={'w': 1440, 'h': 900})",
        }
    )

    assert response["ok"] is True
    assert response["browser_events"] == [
        {
            "type": "browser.state",
            "payload": {
                "url": "https://example.com",
                "title": "Example",
                "status": "connected",
                "tabs": 2,
                "viewport": {"w": 1440, "h": 900},
            },
        }
    ]


def test_managed_browser_does_not_use_system_chromium_without_opt_in(
    tmp_path: Path, monkeypatch
) -> None:
    system_chromium = tmp_path / "chromium"
    system_chromium.write_text("#!/bin/sh\n")
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.delenv("LLM_BROWSER_ALLOW_SYSTEM_CHROMIUM", raising=False)
    monkeypatch.delenv("LLM_BROWSER_ALLOW_GOOGLE_CHROME", raising=False)
    monkeypatch.setattr(worker, "_playwright_chromium_candidates", lambda: [])
    monkeypatch.setattr(worker.shutil, "which", lambda name: str(system_chromium))

    try:
        worker._pick_chromium_path()
    except RuntimeError as exc:
        assert "Playwright Chromium not found" in str(exc)
    else:
        raise AssertionError("system Chromium should require explicit opt-in")

    monkeypatch.setenv("LLM_BROWSER_ALLOW_SYSTEM_CHROMIUM", "1")
    assert worker._pick_chromium_path()
