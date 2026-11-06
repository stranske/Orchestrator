import base64
import json

import review_writer_gate as gate


def marker(kind, payload, provider="muse"):
    raw = base64.b64encode(json.dumps(payload).encode()).decode()
    return {
        "user": {"login": "github-actions[bot]"},
        "body": f"<!-- {kind}:{provider}:106:v1 base64:{raw} -->",
    }


def reserve(provider="muse", rid="r", attempt="stranske/Workflows:9:1"):
    return marker(
        "runner-reservation",
        {
            "provider": provider,
            "pr_number": 106,
            "reservation_id": rid,
            "workflow_attempt_id": attempt,
        },
        provider,
    )


def job(provider="muse", status="completed", conclusion="success"):
    return {
        "steps": [
            {"name": f"Run {provider.title()}", "status": status, "conclusion": conclusion},
            {"name": "Commit and push changes", "status": status, "conclusion": conclusion},
        ]
    }


def api(rows):
    return lambda endpoint: rows[endpoint]


def rows(comments, jobs=None, parent=None, repo="stranske/Workflows", run=9, attempt=1):
    base = f"repos/{repo}/actions/runs/{run}/attempts/{attempt}"
    return {
        "repos/o/r/issues/106/comments?per_page=100&page=1": comments,
        base: parent or {"id": run, "run_attempt": attempt, "status": "queued"},
        base
        + "/jobs?per_page=100&page=1": {
            "total_count": len(jobs if jobs is not None else [job()]),
            "jobs": jobs if jobs is not None else [job()],
        },
    }


def test_actual_jobs_dict_and_recorded_source_repo_are_used():
    assert gate.writer_terminality("o/r", 106, api(rows([reserve()])))["terminal"]


def test_failed_implementation_is_terminal_not_live():
    assert gate.writer_terminality("o/r", 106, api(rows([reserve()], [job(conclusion="failure")])))[
        "terminal"
    ]


def test_queued_child_blocks():
    assert not gate.writer_terminality(
        "o/r", 106, api(rows([reserve()], [job(status="queued", conclusion=None)]))
    )["terminal"]


def test_absent_child_blocks():
    assert not gate.writer_terminality("o/r", 106, api(rows([reserve()], [])))["terminal"]


def test_skipped_push_is_terminal():
    assert gate.writer_terminality("o/r", 106, api(rows([reserve()], [job(conclusion="skipped")])))[
        "terminal"
    ]


def test_skipped_implementation_child_is_terminal():
    parent = {"id": 9, "run_attempt": 1, "status": "completed"}
    skipped_job = {
        "name": "Run Muse",
        "status": "completed",
        "conclusion": "skipped",
        "steps": [],
    }
    assert gate.writer_terminality(
        "o/r", 106, api(rows([reserve()], [skipped_job], parent=parent))
    )["terminal"]
    assert gate.writer_terminality("o/r", 106, api(rows([reserve()], [], parent=parent)))[
        "terminal"
    ]
    assert not gate.writer_terminality(
        "o/r",
        106,
        api(rows([reserve()], [job(status="queued", conclusion=None)], parent=parent)),
    )["terminal"]


def test_attempt_identity_mismatch_blocks():
    data = rows([reserve()])
    data["repos/stranske/Workflows/actions/runs/9/attempts/1"] = {"id": 8, "run_attempt": 1}
    assert not gate.writer_terminality("o/r", 106, api(data))["terminal"]


def test_exact_completion_receipt_can_prove_terminal():
    completion = marker(
        "runner-completion",
        {
            "schema": "runner-completion-receipt/v1",
            "reservation_id": "r",
            "record": {
                "reservation_id": "r",
                "provider": "muse",
                "pr_number": 106,
                "status": "completed",
            },
        },
    )
    assert gate.writer_terminality("o/r", 106, api(rows([reserve(), completion], [])))["terminal"]


def test_duplicate_reservation_uses_one_attempt_read_and_malformed_blocks():
    calls = []
    source = rows([reserve(), reserve()])

    def counted(endpoint):
        calls.append(endpoint)
        return source[endpoint]

    assert gate.writer_terminality("o/r", 106, counted)["terminal"]
    assert calls.count("repos/stranske/Workflows/actions/runs/9/attempts/1") == 1
    malformed = {"repos/o/r/issues/106/comments?per_page=100&page=1": ["bad comment"]}
    assert not gate.writer_terminality("o/r", 106, api(malformed))["terminal"]


def test_all_providers_and_old_heads_on_later_comment_pages_are_checked():
    first = [reserve("codex", "old", "o/r:8:1")] * 100
    data = rows(first, [job("codex")], repo="o/r", run=8)
    data["repos/o/r/issues/106/comments?per_page=100&page=2"] = [reserve("claude", "new")]
    data.update(
        {k: v for k, v in rows([], [job("claude", "queued", None)]).items() if "/actions/" in k}
    )
    result = gate.writer_terminality("o/r", 106, api(data))
    assert not result["terminal"]
    assert result["evidence"][-1]["provider"] == "claude"


def test_receipt_cannot_override_a_queued_child_without_steps():
    completion = marker(
        "runner-completion",
        {
            "schema": "runner-completion-receipt/v1",
            "reservation_id": "r",
            "record": {
                "reservation_id": "r",
                "provider": "muse",
                "pr_number": 106,
                "status": "completed",
                "workflow_attempt_id": "stranske/Workflows:9:1",
            },
        },
    )
    children = [job(), {"name": "Run Muse autofix / Muse (autofix)", "status": "queued"}]
    assert not gate.writer_terminality("o/r", 106, api(rows([reserve(), completion], children)))[
        "terminal"
    ]


def test_same_reservation_id_does_not_hide_a_distinct_old_attempt():
    data = rows([reserve(attempt="o/r:8:1"), reserve()], [job()])
    data.update(
        {
            k: v
            for k, v in rows([], [job(status="queued", conclusion=None)], repo="o/r", run=8).items()
            if "/actions/" in k
        }
    )
    assert not gate.writer_terminality("o/r", 106, api(data))["terminal"]


def test_wrong_completion_id_and_malformed_trusted_record_are_unknown():
    wrong = marker(
        "runner-completion",
        {
            "schema": "runner-completion-receipt/v1",
            "reservation_id": "other",
            "record": {
                "reservation_id": "other",
                "provider": "muse",
                "pr_number": 106,
                "status": "completed",
            },
        },
    )
    assert not gate.writer_terminality("o/r", 106, api(rows([reserve(), wrong], [])))["terminal"]
    malformed = {
        "user": {"login": "github-actions[bot]"},
        "body": "<!-- runner-reservation:codex:106:v1 base64:broken -->",
    }
    assert not gate.writer_terminality("o/r", 106, api(rows([malformed])))["terminal"]
