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


def test_worker_captures_browser_harness_startup_stdout(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_load_browser_harness(ns):
        print("cloud startup chatter")
        ns["browser_harness_available"] = True
        ns["browser_harness_error"] = None

    monkeypatch.setattr(worker, "_load_browser_harness", fake_load_browser_harness)

    response = worker._run(
        {
            "id": "startup-chatter",
            "session_id": "task-startup-chatter",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "result = {'ok': True}",
        }
    )

    assert response["ok"] is True
    assert "cloud startup chatter" in response["text"]
    assert response["data"] == {"ok": True}


def test_worker_capture_screenshot_attaches_image_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_load_browser_harness(ns):
        def fake_capture_screenshot(path=None, full=False, max_dim=None):
            target = Path(path or "shot.png").expanduser()
            if not target.is_absolute():
                target = tmp_path / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"png")
            return str(target)

        ns["capture_screenshot"] = fake_capture_screenshot
        ns["browser_harness_available"] = True
        ns["browser_harness_error"] = None

    monkeypatch.setattr(worker, "_load_browser_harness", fake_load_browser_harness)

    response = worker._run(
        {
            "id": "attached-screenshot",
            "session_id": "task-attached-screenshot",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "path = capture_screenshot('after-click.png')\nresult = {'path': path}",
        }
    )

    assert response["ok"] is True
    assert response["data"]["path"].endswith("after-click.png")
    assert response["images"][0]["label"] == "after-click"
    assert Path(response["images"][0]["path"]).exists()


def test_worker_screenshot_shorthand_emits_labeled_image(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_load_browser_harness(ns):
        def fake_capture_screenshot(path=None, full=False, max_dim=None):
            target = tmp_path / "shot.png"
            target.write_bytes(b"png")
            return str(target)

        ns["capture_screenshot"] = fake_capture_screenshot
        ns["browser_harness_available"] = True
        ns["browser_harness_error"] = None

    monkeypatch.setattr(worker, "_load_browser_harness", fake_load_browser_harness)

    response = worker._run(
        {
            "id": "screenshot-shorthand",
            "session_id": "task-screenshot-shorthand",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "image = screenshot('verified-state')\nresult = image",
        }
    )

    assert response["ok"] is True
    assert response["data"]["label"] == "verified-state"
    assert response["images"][0]["label"] == "verified-state"
    assert Path(response["images"][0]["path"]).exists()


def test_worker_screenshot_clip_uses_cdp_clip_and_attaches_image(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []

    def fake_load_browser_harness(ns):
        def fake_cdp(method, **kwargs):
            calls.append((method, kwargs))
            return {"data": "cG5n"}

        ns["cdp"] = fake_cdp
        ns["browser_harness_available"] = True
        ns["browser_harness_error"] = None

    monkeypatch.setattr(worker, "_load_browser_harness", fake_load_browser_harness)

    response = worker._run(
        {
            "id": "screenshot-clip",
            "session_id": "task-screenshot-clip",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "image = screenshot_clip('table', 10, 20, 300, 120)\nresult = image",
        }
    )

    assert response["ok"] is True
    assert calls[0][0] == "Page.captureScreenshot"
    assert calls[0][1]["clip"] == {
        "x": 10.0,
        "y": 20.0,
        "width": 300.0,
        "height": 120.0,
        "scale": 1.0,
    }
    assert response["images"][0]["label"] == "table"
    assert len(response["images"]) == 1
    assert Path(response["images"][0]["path"]).exists()


def test_worker_raw_cdp_capture_screenshot_attaches_image(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_load_browser_harness(ns):
        def fake_cdp(method, session_id=None, **kwargs):
            assert session_id is None
            if method == "Page.captureScreenshot":
                return {"data": "cG5n"}
            return {}

        ns["cdp"] = fake_cdp
        ns["browser_harness_available"] = True
        ns["browser_harness_error"] = None

    monkeypatch.setattr(worker, "_load_browser_harness", fake_load_browser_harness)

    response = worker._run(
        {
            "id": "raw-cdp-screenshot",
            "session_id": "task-raw-cdp-screenshot",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "result = cdp('Page.captureScreenshot', format='png')",
        }
    )

    assert response["ok"] is True
    assert response["data"] == {"data": "cG5n"}
    assert len(response["images"]) == 1
    assert response["images"][0]["label"] == "cdp_screenshot_1"
    assert Path(response["images"][0]["path"]).exists()


def test_worker_page_info_fallback_reads_target_url_and_title(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeHelpers:
        def current_tab(self):
            return {"targetId": "target-2"}

        def cdp(self, method, **kwargs):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "targetId": "target-1",
                            "type": "page",
                            "attached": True,
                            "url": "https://old.example/",
                            "title": "Old",
                        },
                        {
                            "targetId": "target-2",
                            "type": "page",
                            "attached": True,
                            "url": "https://example.com/",
                            "title": "Example",
                        },
                    ]
                }
            if method == "Page.getLayoutMetrics":
                return {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600}}
            raise AssertionError(method)

    def fake_load_browser_harness(ns):
        ns["page_info"] = lambda: (_ for _ in ()).throw(RuntimeError("page JS wedged"))
        ns["__browser_harness_helpers__"] = FakeHelpers()
        ns["browser_harness_available"] = True
        ns["browser_harness_error"] = None

    monkeypatch.setattr(worker, "_load_browser_harness", fake_load_browser_harness)

    response = worker._run(
        {
            "id": "page-info-fallback",
            "session_id": "task-page-info-fallback",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "result = page_info()",
        }
    )

    assert response["ok"] is True
    assert response["data"]["url"] == "https://example.com/"
    assert response["data"]["title"] == "Example"
    assert response["data"]["w"] == 800
    assert response["data"]["h"] == 600
    assert response["data"]["fallback"] == "cdp"


def test_worker_autoloads_agent_workspace_helpers(tmp_path: Path) -> None:
    workspace = tmp_path / ".browser-use" / "agent-workspace"
    workspace.mkdir(parents=True)
    (workspace / "agent_helpers.py").write_text(
        "def helper_value():\n    return 42\n",
        encoding="utf-8",
    )

    response = worker._run(
        {
            "id": "agent-helpers",
            "session_id": "task-agent-helpers",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "result = {'workspace': agent_workspace(create=False), 'value': helper_value()}",
        }
    )

    assert response["ok"] is True
    assert response["data"]["workspace"] == str(workspace)
    assert response["data"]["value"] == 42


def test_worker_error_hints_are_appended(tmp_path: Path) -> None:
    cases = [
        (
            "raise RuntimeError(\"':contains' is not a valid CSS selector\")",
            "':contains' is jQuery, not CSS.",
        ),
        (
            "raise RuntimeError(\"Identifier 'buttons' has already been declared\")",
            "execution contexts persist",
        ),
        (
            "raise RuntimeError('Blocked a frame with origin https://a from accessing a cross-origin frame')",
            "Cross-origin iframe DOM access",
        ),
        (
            "raise RuntimeError('-32602 No target with given id found')",
            "target closed or was replaced",
        ),
        (
            "raise RuntimeError(\"Runtime.getExecutionContexts wasn't found\")",
            "Runtime.getExecutionContexts is not a CDP method",
        ),
    ]

    for idx, (code, expected_hint) in enumerate(cases):
        response = worker._run(
            {
                "id": f"hint-{idx}",
                "session_id": f"task-hint-{idx}",
                "cwd": str(tmp_path),
                "artifact_dir": str(tmp_path / "artifacts"),
                "code": code,
            }
        )
        assert response["ok"] is False
        assert "Hint:" in response["error"]
        assert expected_hint in response["error"]


def test_worker_set_final_answer_persists_metadata_and_compact_result(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "final-answer",
            "session_id": "task-final-answer",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": "summary = set_final_answer({'stores': [{'name': 'A', 'address': 'B'}]}, artifact_name='stores.json')\nresult = summary",
        }
    )

    assert response["ok"] is True
    assert response["data"]["count"] == 1
    assert response["outputs"][0]["text"].startswith("final answer ready:")
    assert Path(response["data"]["artifact"]["path"]).exists()
    metadata = tmp_path / "artifacts" / ".final_answer.json"
    assert metadata.exists()
    assert '"stores"' in metadata.read_text()


def test_worker_audit_artifact_reports_general_quality_checks(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "artifact-audit",
            "session_id": "task-artifact-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
rows = [
    {'name': 'Big Mac', 'price': '$1', 'category': 'Burgers'},
    {'name': 'Big Mac', 'price': '$1', 'category': 'Popular'},
    {'name': '', 'price': '$2', 'category': 'Chicken'},
]
audit = audit_artifact(
    records=rows,
    required_fields=['name', 'price'],
    dedupe_fields=['name', 'price'],
    bucket_field='category',
    bucket_targets={'Burgers': 2, 'Chicken': 1},
)
result = audit
""",
        }
    )

    assert response["ok"] is True
    audit = response["data"]
    assert audit["ready_for_done"] is False
    assert audit["generated_by"] == "audit_artifact"
    assert audit["record_count"] == 3
    assert audit["checks"]["missing_fields"]["name"]["count"] == 1
    assert audit["checks"]["dedupe"]["duplicate_count"] == 1
    assert audit["checks"]["buckets"]["unmet_targets"] == {
        "Burgers": {"count": 1, "target": 2}
    }
    assert Path(audit["audit_path"]).exists()
    assert response["artifacts"][0]["source_path"] == audit["audit_path"]


def test_worker_audit_artifact_treats_unavailable_placeholders_as_missing(
    tmp_path: Path,
) -> None:
    response = worker._run(
        {
            "id": "artifact-placeholder-audit",
            "session_id": "task-artifact-placeholder-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
rows = [
    {'duration': 'Not visible in captured result/detail card; ad status was Active.'},
    {'duration': 'unknown_from_topplastics_source'},
    {'duration': '12 days'},
]
audit = audit_artifact(records=rows, required_fields=['duration'])
result = audit
""",
        }
    )

    assert response["ok"] is True
    audit = response["data"]
    assert audit["ready_for_done"] is False
    assert audit["checks"]["missing_fields"]["duration"]["count"] == 2


def test_worker_audit_artifact_zero_records_requires_explicit_empty_proof(
    tmp_path: Path,
) -> None:
    blocked = worker._run(
        {
            "id": "artifact-zero-record-audit",
            "session_id": "task-artifact-zero-record-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
audit = audit_artifact(records=[], required_fields=['name', 'url'])
result = audit
""",
        }
    )

    assert blocked["ok"] is True
    audit = blocked["data"]
    assert audit["ready_for_done"] is False
    assert audit["record_count"] == 0
    assert audit["checks"]["record_count"]["violation"] == "zero_records"
    assert audit["checks"]["missing_fields"]["name"]["count"] == 0

    allowed = worker._run(
        {
            "id": "artifact-zero-record-audit-allowed",
            "session_id": "task-artifact-zero-record-audit-allowed",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts2"),
            "code": """
audit = audit_artifact(records=[], required_fields=['name', 'url'], allow_empty=True)
result = audit
""",
        }
    )

    assert allowed["ok"] is True
    assert allowed["data"]["ready_for_done"] is True
    assert "record_count" not in allowed["data"]["checks"]


def test_worker_audit_artifact_reports_missing_source_evidence(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "artifact-source-audit",
            "session_id": "task-artifact-source-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
rows = [{'item_name': 'Big Mac', 'item_price': '$1', 'currency': 'USD'}]
audit = audit_artifact(
    records=rows,
    required_fields=['item_name', 'item_price', 'currency'],
    dedupe_fields=['item_name', 'item_price'],
    source_evidence={
        'requested_store_address': '302 Potrero Ave',
        'source_url': 'https://example.invalid/store/123',
        'source_page_title': 'Store menu',
    },
    required_source_fields=[
        'requested_store_address',
        'source_url',
        'source_page_title',
        'selected_source_entity_name',
        'selected_source_entity_address',
    ],
)
result = audit
""",
        }
    )

    assert response["ok"] is True
    audit = response["data"]
    assert audit["ready_for_done"] is False
    source_check = audit["checks"]["source_evidence"]
    assert "selected_source_entity_name" in source_check["missing_fields"]
    assert "selected_source_entity_address" in source_check["missing_fields"]


def test_worker_audit_artifact_reports_unapproved_source_scope_broadening(
    tmp_path: Path,
) -> None:
    response = worker._run(
        {
            "id": "artifact-source-scope-audit",
            "session_id": "task-artifact-source-scope-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
rows = [{'name': 'A', 'specialty': 'Rhinoplasty'}]
audit = audit_artifact(
    records=rows,
    source_scope_evidence={
        'requested_scope': 'Beverly Hills, CA',
        'requested_source_status': 'No results found in selected location',
        'actual_scope': 'showing results for all locations',
        'scope_match': 'broadened',
        'fallback_used': True,
        'fallback_allowed': False,
        'out_of_scope_record_count': 40,
    },
    required_scope_fields=[
        'requested_scope',
        'requested_source_status',
        'actual_scope',
        'scope_match',
    ],
)
result = audit
""",
        }
    )

    assert response["ok"] is True
    audit = response["data"]
    assert audit["ready_for_done"] is False
    scope = audit["checks"]["source_scope"]
    assert scope["missing_fields"] == {}
    assert "fallback_used_without_user_permission" in scope["violations"]
    assert "out_of_scope_record_count=40" in scope["violations"]


def test_worker_audit_artifact_can_require_unique_visual_files(tmp_path: Path) -> None:
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    image_a.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
            "de0000000c4944415408d763f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
        )
    )
    image_b.write_bytes(image_a.read_bytes())
    response = worker._run(
        {
            "id": "artifact-unique-visual-audit",
            "session_id": "task-artifact-unique-visual-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": f"""
audit = audit_artifact(visual_files={[str(image_a), str(image_b)]!r}, unique_visual_files=True)
result = audit
""",
        }
    )

    assert response["ok"] is True
    audit = response["data"]
    assert audit["ready_for_done"] is False
    uniqueness = audit["checks"]["visual_file_uniqueness"]
    assert uniqueness["duplicate_hash_group_count"] == 1
    assert len(uniqueness["duplicate_hash_groups"][0]["paths"]) == 2


def test_worker_audit_artifact_reports_selection_metric_gaps(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "artifact-selection-audit",
            "session_id": "task-artifact-selection-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
selected = [
    {'id': 'b', 'duration_days': 804},
    {'id': 'a', 'duration_days': 315},
    {'id': 'd', 'duration_days': 35},
]
pool = [
    {'id': 'a', 'duration_days': 315},
    {'id': 'b', 'duration_days': 804},
    {'id': 'c', 'duration_days': 500},
    {'id': 'd', 'duration_days': 35},
]
audit = audit_artifact(
    records=selected,
    selection_metric_field='duration_days',
    selection_order='desc',
    selection_limit=3,
    selection_pool_records=pool,
    selection_key_fields=['id'],
)
result = audit
""",
        }
    )

    assert response["ok"] is True
    audit = response["data"]
    assert audit["ready_for_done"] is False
    selection = audit["checks"]["selection"]
    assert selection["missing_metric_count"] == 0
    assert selection["order_violation_count"] == 0
    assert selection["missing_top_candidate_count"] == 1
    assert selection["selected_outside_top_count"] == 1
    assert selection["missing_top_candidate_examples"][0]["record"]["id"] == "c"


def test_worker_set_final_answer_embeds_last_artifact_audit(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "final-answer-audit",
            "session_id": "task-final-answer-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
rows = [{'name': 'A'}, {'name': ''}]
audit = audit_artifact(records=rows, required_fields=['name'])
summary = set_final_answer({'rows': rows}, artifact_name='rows.json')
result = summary
""",
        }
    )

    assert response["ok"] is True
    assert response["data"]["ready_for_done"] is False
    assert response["data"]["audit"]["checks"]["missing_fields"]["name"]["count"] == 1
    assert "audit_ready_for_done=False" in response["outputs"][-1]["text"]
    metadata = tmp_path / "artifacts" / ".final_answer.json"
    assert '"ready_for_done": false' in metadata.read_text()


def test_worker_set_final_answer_recommends_audit_for_nested_records(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "final-answer-nested-audit",
            "session_id": "task-final-answer-nested-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
menu = {
    'categories': [
        {'category_name': 'A', 'items': [{'item_name': f'item-{idx}', 'price': '$1'} for idx in range(15)]},
        {'category_name': 'B', 'items': [{'item_name': f'item-{idx}', 'price': '$1'} for idx in range(15, 30)]},
    ]
}
summary = set_final_answer(menu, artifact_name='menu.json')
result = summary
""",
        }
    )

    assert response["ok"] is True
    recommendation = response["data"]["audit_recommendation"]
    assert recommendation["recommended"] is True
    assert recommendation["nested_record_count"] >= 30
    assert response["data"]["ready_for_done"] is False
    assert "audit=missing" in response["outputs"][-1]["text"]
    assert "ready_for_done=False" in response["outputs"][-1]["text"]
    metadata = tmp_path / "artifacts" / ".final_answer.json"
    assert "large_structured_result_estimate" in metadata.read_text()


def test_worker_set_final_answer_requires_source_evidence_for_source_identity_claims(
    tmp_path: Path,
) -> None:
    response = worker._run(
        {
            "id": "final-answer-source-evidence",
            "session_id": "task-final-answer-source-evidence",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
menu = {
    'store_address': '302 Potrero Ave, San Francisco, CA 94103',
    'categories': [
        {'category_name': 'A', 'items': [{'item_name': f'item-{idx}', 'item_price': '$1'} for idx in range(6)]},
    ],
}
rows = [item for category in menu['categories'] for item in category['items']]
audit = audit_artifact(records=rows, required_fields=['item_name', 'item_price'], dedupe_fields=['item_name', 'item_price'])
summary = set_final_answer(menu, artifact_name='menu.json', audit=audit)
result = summary
""",
        }
    )

    assert response["ok"] is True
    assert response["data"]["ready_for_done"] is False
    assert "source evidence" in response["data"]["audit_note"]
    assert response["data"]["audit_recommendation"]["source_identity_claim_paths"] == [
        "store_address"
    ]


def test_worker_set_final_answer_requires_source_scope_audit_for_broadened_claims(
    tmp_path: Path,
) -> None:
    response = worker._run(
        {
            "id": "final-answer-source-scope",
            "session_id": "task-final-answer-source-scope",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
rows = [{'name': f'Dr {idx}', 'specialty': 'Rhinoplasty'} for idx in range(40)]
audit = audit_artifact(records=rows, required_fields=['name'])
summary = set_final_answer(
    {
        'metadata': {
            'note': 'Requested source showed no results found in selected location; used all locations to meet the target.'
        },
        'records': rows,
    },
    artifact_name='surgeons.json',
    audit=audit,
)
result = summary
""",
        }
    )

    assert response["ok"] is True
    assert response["data"]["ready_for_done"] is False
    assert "source_scope" in response["data"]["audit_note"]
    assert response["data"]["audit_recommendation"]["source_scope_claim_paths"] == [
        "metadata.note"
    ]


def test_worker_set_final_answer_requires_selection_audit_for_ranking_claims(
    tmp_path: Path,
) -> None:
    response = worker._run(
        {
            "id": "final-answer-selection-audit",
            "session_id": "task-final-answer-selection-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
ads = [{'library_id': str(idx), 'active_duration_days': idx} for idx in range(5)]
audit = audit_artifact(records=ads, required_fields=['library_id'])
summary = set_final_answer(
    {'selection_method': 'top ads by longest active duration', 'ads': ads},
    artifact_name='ads.json',
    audit=audit,
)
result = summary
""",
        }
    )

    assert response["ok"] is True
    assert response["data"]["ready_for_done"] is False
    assert "selection check" in response["data"]["audit_note"]
    assert response["data"]["audit_recommendation"]["selection_claim_paths"] == [
        "selection_method"
    ]


def test_worker_set_final_answer_rejects_ad_hoc_audit_for_audit_worthy_output(
    tmp_path: Path,
) -> None:
    response = worker._run(
        {
            "id": "final-answer-ad-hoc-audit",
            "session_id": "task-final-answer-ad-hoc-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
ads = [
    {
        'advertiser_name': f'Ad {idx}',
        'deployment_duration': 'Not visible in captured result/detail card',
        'creative_screenshot': f'/tmp/ad-{idx}.png',
    }
    for idx in range(5)
]
audit = {
    'ready_for_done': True,
    'record_count': 5,
    'missing_required_counts': {'advertiser_name': 0, 'deployment_duration': 0},
}
summary = set_final_answer({'ads': ads}, artifact_name='ads.json', audit=audit)
result = summary
""",
        }
    )

    assert response["ok"] is True
    assert response["data"]["ready_for_done"] is False
    assert "does not match audit_artifact" in response["data"]["audit_note"]
    assert response["data"]["audit_recommendation"]["visual_artifact_path_count"] == 5
    assert "audit_ready_for_done=False" in response["outputs"][-1]["text"]


def test_worker_set_final_answer_recommends_audit_for_summary_artifacts_and_images(tmp_path: Path) -> None:
    response = worker._run(
        {
            "id": "final-answer-artifact-audit",
            "session_id": "task-final-answer-artifact-audit",
            "cwd": str(tmp_path),
            "artifact_dir": str(tmp_path / "artifacts"),
            "code": """
summary = set_final_answer({
    'output_path': '/tmp/result.json',
    'record_count': 410,
    'screenshots': {
        'one': '/tmp/one.png',
        'two': '/tmp/two.jpg',
        'three': '/tmp/three.webp',
    },
})
result = summary
""",
        }
    )

    assert response["ok"] is True
    recommendation = response["data"]["audit_recommendation"]
    assert recommendation["recommended"] is True
    assert recommendation["explicit_record_count"] == 410
    assert recommendation["visual_artifact_path_count"] == 3
    assert recommendation["structured_artifact_path_count"] == 1
    assert response["data"]["ready_for_done"] is False
    assert "audit=missing" in response["outputs"][-1]["text"]


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


def test_visible_managed_browser_prefers_google_chrome(monkeypatch) -> None:
    monkeypatch.delenv("CHROME_PATH", raising=False)

    class FakePath:
        def __init__(self, value: str) -> None:
            self.value = value

        def exists(self) -> bool:
            return self.value == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

        def __str__(self) -> str:
            return self.value

    monkeypatch.setattr(worker, "Path", FakePath)

    assert (
        worker._pick_managed_chrome_path(visible=True)
        == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )


def test_managed_chrome_args_visible_vs_headless(tmp_path: Path) -> None:
    visible = worker._managed_chrome_args("/chrome", 9333, tmp_path / "profile", True)
    headless = worker._managed_chrome_args("/chrome", 9334, tmp_path / "profile", False)

    assert "--new-window" in visible
    assert "--window-size=1512,900" in visible
    assert "--headless=new" not in visible
    assert "--headless=new" in headless
    assert "--new-window" not in headless
