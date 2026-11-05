"""Hermetic tests for Gate-missing statements still open on the #1025 head.

Authority is CI run 41982991645 (`gate-coverage`), not a single-module pytest
coverage table. These tests target production branches that run still lists as
missing: tick consult/record failures, a non-exempt observer verdict, an unknown
late-outcome direction kept beside a real one, the cadence-registry import
fallback, the tick alarm backstop, the role-lineage fallback when no aggregate
row exists, the offline switch-review tripwire, and the feedback selftest sections that CI
skips when role capabilities have no version lineage. Selftest ``raise AssertionError`` arms stay out — they
run only when the check they guard is already wrong.
"""

from __future__ import annotations

import builtins
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

import capabilities
import capability_advisor
import capability_propensity as propensity
import feedback

NOW = 1_700_000_000
CAP = "obs-tick"
EXP = "advice:mixeddir0001"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep ledger, Brain, and state in the temp dir and refuse process spawns."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.delenv("ORCH_TICK_EVIDENCE_DISABLED", raising=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")
    monkeypatch.setattr(propensity, "_capability_heartbeat", lambda *_a, **_k: None)
    spawned: list[str] = []

    def _blocked(kind: str):
        def _inner(*_args, **_kwargs):
            spawned.append(kind)
            raise AssertionError(f"{kind} was started")

        return _inner

    monkeypatch.setattr(subprocess, "Popen", _blocked("Popen"))
    monkeypatch.setattr(subprocess, "run", _blocked("run"))
    yield spawned
    assert spawned == []


def _quiet_advice(*_args, **_kwargs) -> dict:
    return {"recorded_matches": 0, "capabilities": []}


def _observer(path: Path, name: str = CAP) -> None:
    row = capabilities._blank_capability(name)
    row.update(status="wired", matcher={"kind": "tick_phase", "name": name})
    capabilities.save({name: row}, path)


def _bind(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setitem(
        capability_advisor.SURFACE_BINDINGS,
        propensity.TICK_SURFACE,
        {name: "synthetic observer" for name in names},
    )


def _artifact(state: Path, payload: dict, mtime: int, name: str = "obs.json") -> None:
    artifact = state / name
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(artifact, (mtime, mtime))


def test_consult_names_an_advisor_failure_whether_or_not_it_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A down advisor is an error field. Recording and inspecting both say so."""
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)

    def _down(*_args, **_kwargs):
        raise RuntimeError("advisor down")

    monkeypatch.setattr(capability_advisor, "advise", _down)
    recorded = propensity.tick_evidence(
        now=NOW, state_dir=tmp_path / "state", path=ledger, steps={}
    )
    inspected = propensity.tick_evidence(
        now=NOW, state_dir=tmp_path / "inspect", path=ledger, steps={}, record=False
    )
    for report in (recorded, inspected):
        assert report["consulted"] is False
        assert report["advice_error"] == "RuntimeError: advisor down"
        assert report["matches_recorded"] == 0
    assert (tmp_path / "state" / propensity.TICK_EVIDENCE_STATE).is_file()
    assert not (tmp_path / "inspect" / propensity.TICK_EVIDENCE_STATE).exists()


def test_bound_name_missing_from_the_ledger_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A surface binding with no ledger row is skipped, not graded against nothing."""
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)
    state = tmp_path / "state"
    _bind(monkeypatch, "ghost")
    _artifact(state, {}, NOW, name="ghost.json")
    monkeypatch.setattr(capability_advisor, "advise", _quiet_advice)
    report = propensity.tick_evidence(
        now=NOW,
        state_dir=state,
        path=ledger,
        steps={"ghost": {"artifact": "ghost.json", "cadence_days": 1}},
    )
    assert report["skipped"] == [
        {
            "capability_id": "ghost",
            "reason": "not_in_ledger",
            "detail": propensity.TICK_SKIP_REASONS["not_in_ledger"],
        }
    ]
    assert report["baselined"] == []
    assert report["evaluated"] == []


def test_trigger_write_failure_skips_the_capability_before_a_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trigger that cannot be stored does not become a baseline or a verdict."""
    ledger = tmp_path / "capabilities.json"
    _observer(ledger)
    state = tmp_path / "state"
    _bind(monkeypatch, CAP)
    _artifact(state, {"a": [{"id": "one"}]}, NOW)
    monkeypatch.setattr(capability_advisor, "advise", _quiet_advice)
    monkeypatch.setattr(propensity, "TICK_FINDING_FIELDS", {CAP: {"a": ("id",)}})

    def _trigger(*_args, **_kwargs):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(propensity, "record_trigger", _trigger)
    report = propensity.tick_evidence(
        now=NOW,
        state_dir=state,
        path=ledger,
        steps={CAP: {"artifact": "obs.json", "cadence_days": 1}},
    )
    assert report["triggers_recorded"] == 0
    assert report["baselined"] == []
    assert report["evaluated"] == []
    assert report["skipped"][0]["reason"] == "trigger_failed"
    assert report["skipped"][0]["detail"] == "RuntimeError: ledger locked"


def test_nonexempt_observer_records_one_usefulness_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the exemption is absent, a changed report records one machine verdict."""
    ledger = tmp_path / "capabilities.json"
    _observer(ledger)
    state = tmp_path / "state"
    _bind(monkeypatch, CAP)
    monkeypatch.setattr(propensity, "TICK_FINDING_FIELDS", {CAP: {"a": ("id",)}})
    monkeypatch.setattr(propensity, "verdict_exempt_reason", lambda _cap: None)
    monkeypatch.setattr(capability_advisor, "advise", _quiet_advice)
    steps = {CAP: {"artifact": "obs.json", "cadence_days": 1}}
    _artifact(state, {"a": [{"id": "old"}]}, NOW)
    first = propensity.tick_evidence(now=NOW, state_dir=state, path=ledger, steps=steps)
    assert first["verdicts_recorded"] == 0
    assert first["baselined"][0]["capability_id"] == CAP
    _artifact(state, {"a": [{"id": "new"}]}, NOW + 30)
    second = propensity.tick_evidence(now=NOW + 30, state_dir=state, path=ledger, steps=steps)
    assert second["verdicts_recorded"] == 1
    assert second["evaluated"][0]["useful"] is True
    assert second["evaluated"][0]["recorded"] is True
    stored = capabilities.load(ledger, create=False)[CAP]["event_history"]
    outcomes = [event for event in stored if event["type"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["metadata"]["useful"] is True


def test_usefulness_write_failure_is_named_and_does_not_advance_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed verdict is reported and the previous baseline stays the state."""
    ledger = tmp_path / "capabilities.json"
    _observer(ledger)
    state = tmp_path / "state"
    _bind(monkeypatch, CAP)
    monkeypatch.setattr(propensity, "TICK_FINDING_FIELDS", {CAP: {"a": ("id",)}})
    monkeypatch.setattr(propensity, "verdict_exempt_reason", lambda _cap: None)
    monkeypatch.setattr(capability_advisor, "advise", _quiet_advice)
    steps = {CAP: {"artifact": "obs.json", "cadence_days": 1}}
    _artifact(state, {"a": [{"id": "old"}]}, NOW)
    propensity.tick_evidence(now=NOW, state_dir=state, path=ledger, steps=steps)

    def _full(*_args, **_kwargs):
        raise RuntimeError("store full")

    monkeypatch.setattr(propensity, "record_usefulness", _full)
    _artifact(state, {"a": [{"id": "new"}]}, NOW + 30)
    report = propensity.tick_evidence(now=NOW + 30, state_dir=state, path=ledger, steps=steps)
    assert report["verdicts_recorded"] == 0
    assert report["evaluated"][0]["reason"] == "verdict_failed"
    assert report["evaluated"][0]["detail"] == "RuntimeError: store full"
    blob = json.loads((state / propensity.TICK_EVIDENCE_STATE).read_text(encoding="utf-8"))
    assert blob["capabilities"][CAP]["artifact_mtime"] == NOW


def test_state_write_failure_is_a_named_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tick report survives a state-file failure and names it."""
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)
    monkeypatch.setattr(capability_advisor, "advise", _quiet_advice)

    def _disk(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(propensity, "_write_json_atomic", _disk)
    report = propensity.tick_evidence(now=NOW, state_dir=tmp_path / "state", path=ledger, steps={})
    assert report["state_write_error"] == "OSError: disk full"
    assert report["consulted"] is True


def test_missing_cadence_registry_bounds_the_ceiling_as_daily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unimportable cadence registry is an empty table, not a crashed tick."""
    ledger = tmp_path / "capabilities.json"
    name = "rail-exercise"
    _observer(ledger, name)
    _bind(monkeypatch, name)
    monkeypatch.setattr(propensity, "TICK_FINDING_FIELDS", {name: {"a": ("id",)}})
    monkeypatch.setattr(propensity, "verdict_exempt_reason", lambda _cap: None)
    monkeypatch.setattr(capability_advisor, "advise", _quiet_advice)
    real_import = builtins.__import__

    def _import(module_name, *args, **kwargs):
        if module_name == "cadence_registry" or module_name.startswith("cadence_registry."):
            raise ImportError("registry unavailable")
        return real_import(module_name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    report = propensity.tick_evidence(now=NOW, state_dir=tmp_path / "state", path=ledger)
    assert report["skipped"][0]["reason"] == "no_cadence_artifact"
    # cadence_days 6 on the real rail-exercise step would ceiling at 0.14.
    assert report["ceiling"]["graded_verdicts_per_day_ceiling"] == 1.0


def test_alarm_backstop_is_reported_and_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SIGALRM handler becomes a tick error dict, not an uncaught timeout."""
    monkeypatch.setattr(signal, "alarm", lambda *_args, **_kwargs: 0)

    def _trip(**_kwargs):
        handler = signal.getsignal(signal.SIGALRM)
        handler(signal.SIGALRM, None)

    monkeypatch.setattr(propensity, "tick_evidence", _trip)
    report = propensity.tick_evidence_guarded(now=NOW)
    assert report["verdicts_recorded"] == 0
    assert report["error"].startswith("TimeoutError:")
    assert "tick capability evidence exceeded" in report["error"]
    assert report["reason"] == "tick capability evidence failed; the tick is unaffected"


def test_signal_install_failure_still_runs_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SIGALRM (non-main thread) runs the tick unbounded instead of failing it."""

    def _no_signals(*_args, **_kwargs):
        raise ValueError("not the main thread")

    monkeypatch.setattr(signal, "signal", _no_signals)
    monkeypatch.setattr(
        propensity, "tick_evidence", lambda **_kwargs: {"surface": "tick", "verdicts_recorded": 3}
    )
    assert propensity.tick_evidence_guarded() == {"surface": "tick", "verdicts_recorded": 3}


def test_disarm_failure_keeps_the_tick_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed alarm teardown is swallowed. The tick result still returns."""

    def _alarm(seconds, *_args, **_kwargs):
        if seconds == 0:
            raise OSError("disarm failed")
        return 0

    monkeypatch.setattr(signal, "alarm", _alarm)
    monkeypatch.setattr(
        propensity, "tick_evidence", lambda **_kwargs: {"kept": True, "verdicts_recorded": 1}
    )
    assert propensity.tick_evidence_guarded() == {"kept": True, "verdicts_recorded": 1}


def test_unknown_late_direction_is_orphaned_beside_a_real_amendment(tmp_path: Path) -> None:
    """A direction the writer would refuse, already stored, must not drop the real one."""
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("helper")
    row.update(
        status="wired",
        matcher={"kind": "task_type", "name": "helper"},
        trigger_cadence="per dispatch",
    )
    capabilities.save({"helper": row}, ledger)
    assert propensity.record_usefulness(
        "helper",
        EXP,
        useful=True,
        evidence="the retry helper stopped failing closed",
        provenance="machine_observed",
        path=ledger,
        timestamp=NOW,
    )
    for key, direction, stamp in (
        ("late:good", propensity.LATE_OUTCOME_CORROBORATES, NOW + 1),
        ("late:bad", "sideways", NOW + 2),
    ):
        assert capabilities.heartbeat(
            "helper",
            propensity.LATE_OUTCOME_EVENT_TYPE,
            ref=EXP,
            path=ledger,
            timestamp=stamp,
            idempotency_key=key,
            metadata={
                propensity.LATE_OUTCOME_DIRECTION_KEY: direction,
                "evidence": direction,
                "deliverable": "",
            },
        )
    trial = next(
        item
        for item in propensity.experiments(path=ledger, now=NOW + 3)
        if item["experiment_id"] == EXP
    )
    assert "helper" in trial["useful"]
    assert trial["late_outcomes"]["helper"]["direction"] == "corroborates"
    orphan = trial["late_outcome_orphans"]["helper"]
    assert orphan["why"] == "unknown direction 'sideways'"
    assert [row["direction"] for row in orphan["amendments"]] == ["sideways"]


def test_role_lineage_without_an_aggregate_copies_the_acting_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No aggregate row: the role inherits this acting run, not another run's verdict."""
    conn = feedback._conn()
    feedback._record_run_in_conn(conn, "tgt-fallback", "o/r#9", "implement", "codex")
    feedback._record_outcome_in_conn(
        conn,
        "tgt-fallback",
        adjudicated_verdict="PASS",
        merged=True,
        durability="durable",
        notes="acting notes",
    )
    event = feedback._record_completion_event_in_conn(
        conn,
        "role-fallback",
        event_type="role",
        phase="execution",
        producer="feedback.record_role_run",
        payload={},
    )
    feedback._record_run_in_conn(
        conn, "role-fallback", "o/r#9", "role:triage", "codex", role_name="triage"
    )
    feedback._record_influence_edge_in_conn(
        conn,
        target_run_id="tgt-fallback",
        influence_type="role",
        influence_id="role-fallback",
        source_run_id="role-fallback",
        source_event_id=event["event_id"],
        accepted=True,
    )
    monkeypatch.setattr(feedback, "_role_outcome_aggregate", lambda *_args, **_kwargs: None)
    assert feedback._propagate_outcome_lineage_in_conn(conn, "tgt-fallback") == 1
    role = conn.execute(
        "SELECT adjudicated_verdict, durability, notes, failure_class FROM outcomes "
        "WHERE run_id='role-fallback'"
    ).fetchone()
    conn.close()
    assert role[0] == "PASS"
    assert role[1] == "durable"
    assert role[2] == "automatically influenced tgt-fallback; downstream notes: acting notes"
    assert role[3] is None


def _versioned(name: str, version: str) -> dict:
    row = capabilities._blank_capability(name)
    row["capability_version_id"] = version
    return row


def test_offline_switch_review_fails_closed_when_a_tripwire_fires() -> None:
    """The isolation context records a sweep, a gh call, and a tick-env read, then refuses them."""
    import switch_review

    with pytest.raises(AssertionError, match="live switch sweep"):
        with propensity._offline_switch_review():
            assert switch_review.review(window=1) == {"held_off": [], "on_but_idle": []}
            reached_gh = switch_review._GH_CALL_RUNNER(["issue", "view", "1"])
            assert reached_gh[0] is False
            assert "may not reach gh" in reached_gh[2]
            assert switch_review.env_as_the_tick_sees_it() == ({}, {})


def test_feedback_selftest_runs_role_lineage_when_versions_exist(tmp_path: Path) -> None:
    """CI skips these sections because a bootstrap ledger has no version ids. A seeded one must not."""
    import env_prereq

    ledger = tmp_path / "capabilities.json"
    capabilities.save(
        {
            "role-redirect": _versioned("role-redirect", "cv:redirect"),
            "offload": _versioned("offload", "cv:offload"),
            "role-triage": _versioned("role-triage", "cv:triage"),
        },
        ledger,
    )
    capabilities.REG = ledger
    assert env_prereq.ledger_rows_absent("role-redirect", "offload", "role-triage") is None
    assert env_prereq.ledger_version_lineage_absent("role-redirect", "offload") is None
    assert env_prereq.ledger_version_lineage_absent("role-triage") is None
    previous = feedback.DB_PATH
    try:
        feedback._selftest()
    finally:
        feedback.DB_PATH = previous


def test_selftest_restores_a_binding_that_was_already_there() -> None:
    """The cleanup else-arm runs only when the surface was bound before the selftest started.

    The tick surface is declared in the table, so its selftest always takes the restore arm.
    These three names are not, so Gate only executes the pop. Seeding them first takes the other arm.
    """
    import capability_advisor

    sentinel = {"preexisting": "kept"}
    surfaces = {
        "f-surf": propensity._selftest_finds,
        "t-dec": propensity._selftest_declines,
        "t-surf": propensity._selftest_detection,
    }
    saved = {name: capability_advisor.SURFACE_BINDINGS.get(name) for name in surfaces}
    try:
        for name, fn in surfaces.items():
            capability_advisor.SURFACE_BINDINGS[name] = dict(sentinel)
            fn()
            assert capability_advisor.SURFACE_BINDINGS[name] == sentinel, name
    finally:
        for name, previous in saved.items():
            if previous is None:
                capability_advisor.SURFACE_BINDINGS.pop(name, None)
            else:
                capability_advisor.SURFACE_BINDINGS[name] = previous
