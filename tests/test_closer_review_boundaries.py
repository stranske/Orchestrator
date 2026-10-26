"""Offline regressions for the grouped post-push collector/admission review."""

import contextlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_evidence import base_api, collect, snapshot
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_capacity import publish
from test_closer_native_observer import host as host
from test_keepalive_verifier_verdict import _decision, _evidence_pr

import closer_dispatcher as dispatcher
import closer_native_observer as native
import closer_operational_capacity as scoped
import durability_sweep as sweep
import feedback
import verifier_evidence


def _bound_worker_adjudication(tmp_path):
    from test_closer_execution_contracts import context, request

    bound = context.__wrapped__(tmp_path / "named-adjudication")
    return bound["snapshot"], {
        "adjudication_request": request(bound),
        "packet_ref": bound["packet_ref"],
        "interpretation": bound["original"],
    }


def test_not_requested_placeholder_is_recollected_when_action_needs_the_section(tmp_path):
    snap = snapshot(revision="same-revision")
    first = collect(tmp_path / "first", snap, action="post_merge_verify")
    assert first["sections"]["diff_files"]["provenance"]["request_status"] == "not_requested"
    reads = []

    def api(endpoint, **kwargs):
        reads.append(endpoint)
        return base_api(endpoint, **kwargs)

    second = collect(
        tmp_path / "second",
        snap,
        api=api,
        action="merge",
        previous=first,
        refresh_sections={"source"},
    )
    assert any("/compare/" in endpoint for endpoint in reads)
    assert second["sections"]["diff_files"]["status"] == "complete"
    assert (
        second["sections"]["diff_files"].get("provenance", {}).get("request_status")
        != "not_requested"
    )


@pytest.mark.parametrize("damage", ["wrong-origin", "missing", "nested"])
def test_legacy_worker_validates_actual_checkout_identity_before_capacity(
    tmp_path, monkeypatch, damage
):
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "origin",
            (
                "https://github.com/owner/other.git"
                if damage == "wrong-origin"
                else "https://github.com/owner/repo.git"
            ),
        ],
        check=True,
    )
    if damage == "missing":
        selected = checkout / "absent"
    elif damage == "nested":
        selected = checkout / "nested"
        selected.mkdir()
    else:
        selected = checkout
    config = tmp_path / "workspaces.json"
    config.write_text(json.dumps({"owner/repo": str(selected)}))
    workspace, bindings = dispatcher.load_workspace_config(config, validate_execution=False)
    backend = dispatcher.Closer(
        SimpleNamespace(),
        state_path=tmp_path / "state",
        workers=True,
        workspaces=workspace,
        workspace_bindings=bindings,
        workspace_validator=lambda target: dispatcher.load_workspace_config(
            config, targets=[target]
        ),
    )
    monkeypatch.setattr(
        dispatcher,
        "_exception_route",
        lambda **_kw: pytest.fail("workspace validation must precede capacity selection"),
    )
    monkeypatch.setattr(
        dispatcher,
        "_fresh_capacity_report",
        lambda: pytest.fail("workspace validation must precede capacity reads"),
    )
    snap, advisory = _bound_worker_adjudication(tmp_path)
    result = backend.worker(snap["target"], "disposition", snap, {}, advisory_context=advisory)
    assert result["status"] == "blocked"
    assert "workspace validation failed" in result["reason"]


def test_legacy_target_validation_does_not_validate_unrelated_missing_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/o/r.git"],
        check=True,
    )
    config = tmp_path / "map.json"
    config.write_text(json.dumps({"o/r": str(checkout), "o/other": str(tmp_path / "missing")}))
    result, _ = dispatcher.load_workspace_config(config, targets=["o/r#7"])
    assert result["o/r"] == str(checkout)


def test_malformed_optional_usage_is_control_only_with_recorded_cause(capacity_host):
    telemetry = Path.home() / ".codex/orchestrator/usage-poll/claude-usage.json"
    telemetry.parent.mkdir(parents=True, exist_ok=True)
    telemetry.write_text('{"broken":')
    context = publish(capacity_host)
    report = native._read(Path(context["report"]["path"]))
    assert report["control_plane_only"] is True and report["profile_ids"] == []
    assert any("JSONDecodeError" in reason for reason in context["control_plane_reasons"])
    assert scoped.validate_report(report, now=time.time())


def test_control_only_profile_fallback_retains_reason(capacity_host, monkeypatch):
    import execution_profiles

    monkeypatch.setattr(execution_profiles, "get_profile", lambda *_: None)
    context = publish(capacity_host)
    assert any("active low" in reason for reason in context["control_plane_reasons"])
    assert native._read(Path(context["report"]["path"]))["profiles"] == {}


def test_lifecycle_refresh_is_bounded_and_fair_including_previously_passed_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    rows = [(f"run-{n:03d}", "o/r", n) for n in range(105)]
    monkeypatch.setattr(sweep, "_missing_verifier_runs", lambda: rows)
    calls = []

    def fetch(repo, numbers):
        calls.append(numbers)
        return {}

    for _ in range(3):
        result = sweep.refresh_verifier_verdicts(_fetch_fn=fetch)
        assert result["verifier_checked"] <= sweep.VERIFIER_REFRESH_BATCH
    assert set().union(*(set(numbers) for numbers in calls)) == set(range(105))
    cursor = tmp_path / "brain.db.verifier-refresh.json"
    original = cursor.read_bytes()
    sweep.refresh_verifier_verdicts(dry_run=True, _fetch_fn=fetch)
    assert cursor.read_bytes() == original


def test_refresh_cursor_uses_actual_connection_not_cached_database_path(tmp_path, monkeypatch):
    actual = tmp_path / "actual.db"
    unrelated = tmp_path / "unrelated.db"

    @contextlib.contextmanager
    def connection():
        with sqlite3.connect(actual) as database:
            yield database

    monkeypatch.setattr(feedback, "_conn", connection)
    monkeypatch.setattr(feedback, "DB_PATH", unrelated)
    monkeypatch.setattr(sweep, "_missing_verifier_runs", lambda: [("run", "o/r", 7)])
    sweep.refresh_verifier_verdicts(_fetch_fn=lambda *_: {})
    assert Path(str(actual) + ".verifier-refresh.json").is_file()
    assert not Path(str(unrelated) + ".verifier-refresh.json").exists()


def test_refresh_discovers_later_nonpass_after_a_stored_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("run-7", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("run-7", merged=True, durability="durable")
    with feedback._conn() as connection:
        connection.execute("UPDATE outcomes SET verifier_verdict='PASS'")
    marker = verifier_evidence.decision_from_pr(
        "o/r", _evidence_pr(_decision("NON_PASS", providers=["CONCERNS"]))
    )
    assert (
        sweep.refresh_verifier_verdicts(_fetch_fn=lambda *_: {7: marker})["verifier_recorded"] == 1
    )
    with feedback._conn() as connection:
        assert connection.execute("SELECT verifier_verdict FROM outcomes").fetchone() == (
            "NON_PASS",
        )


def test_cursor_corruption_or_write_refusal_does_not_discard_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    cursor = tmp_path / "brain.db.verifier-refresh.json"
    cursor.write_text("not-json")
    monkeypatch.setattr(sweep, "_missing_verifier_runs", lambda: [("run", "o/r", 7)])
    monkeypatch.setattr(sweep, "_save_verifier_refresh_cursor", lambda *_: "PermissionError")
    marker = verifier_evidence.decision_from_pr("o/r", _evidence_pr(_decision()))
    recorded = []
    monkeypatch.setattr(
        verifier_evidence, "record_decision", lambda *args: recorded.append(args) or True
    )
    result = sweep.refresh_verifier_verdicts(_fetch_fn=lambda *_: {7: marker})
    assert result["verifier_recorded"] == 1 and len(recorded) == 1
    assert result["verifier_cursor_error"] == "PermissionError"


def test_failed_repository_fetch_does_not_starve_later_refresh_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    rows = [(f"run-{n:03d}", "o/broken" if n < 50 else "o/healthy", n) for n in range(51)]
    monkeypatch.setattr(sweep, "_missing_verifier_runs", lambda: rows)
    calls = []

    def fetch(repo, numbers):
        calls.append(repo)
        if repo == "o/broken":
            raise OSError("temporary unavailable")
        return {}

    first = sweep.refresh_verifier_verdicts(_fetch_fn=fetch)
    assert first["verifier_recorded"] == 0
    assert first["verifier_refresh_errors"] == {"o/broken": "OSError"}
    second = sweep.refresh_verifier_verdicts(_fetch_fn=fetch)
    assert second["verifier_checked"] == 50
    assert "o/healthy" in calls
    assert rows == [(f"run-{n:03d}", "o/broken" if n < 50 else "o/healthy", n) for n in range(51)]


def test_global_control_exception_preserves_backoff_without_fetching_other_repos(
    tmp_path, monkeypatch
):
    import closer_evidence

    class ControlStop(Exception, closer_evidence.RoundControlException):
        pass

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(
        sweep,
        "_missing_verifier_runs",
        lambda: [
            ("a", "o/rate-limited", 1),
            ("b", "o/other", 2),
        ],
    )
    calls = []

    def fetch(repo, _numbers):
        calls.append(repo)
        raise ControlStop("authoritative capacity wait")

    result = sweep.refresh_verifier_verdicts(_fetch_fn=fetch)
    assert calls == ["o/rate-limited"]
    assert result["verifier_checked"] == 1 and result["verifier_remaining"] == 1
    assert result["verifier_recorded"] == 0


def test_legacy_mapping_change_cannot_validate_a_different_checkout(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    for directory in (old, new):
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "remote",
                "add",
                "origin",
                "https://github.com/owner/repo.git",
            ],
            check=True,
        )
    config = tmp_path / "workspaces.json"
    config.write_text(json.dumps({"owner/repo": str(old)}))
    original, bindings = dispatcher.load_workspace_config(config, validate_execution=False)
    backend = dispatcher.Closer(
        SimpleNamespace(),
        state_path=tmp_path / "state",
        workers=True,
        workspaces=original,
        workspace_bindings=bindings,
        workspace_validator=lambda target: dispatcher.validate_bound_workspace_config(
            config, original, target
        ),
    )
    config.write_text(json.dumps({"owner/repo": str(new)}))
    subprocess.run(
        [
            "git",
            "-C",
            str(old),
            "remote",
            "set-url",
            "origin",
            "https://github.com/owner/unrelated.git",
        ],
        check=True,
    )
    monkeypatch.setattr(
        dispatcher,
        "_exception_route",
        lambda **_kw: pytest.fail("workspace identity must precede capacity selection"),
    )
    monkeypatch.setattr(
        dispatcher,
        "_fresh_capacity_report",
        lambda: pytest.fail("workspace validation must precede capacity reads"),
    )
    snap, advisory = _bound_worker_adjudication(tmp_path)
    result = backend.worker(snap["target"], "disposition", snap, {}, advisory_context=advisory)
    assert result["status"] == "blocked"
    assert "mapping changed" in result["reason"]
