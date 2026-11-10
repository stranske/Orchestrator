"""Hermetic tests for the #1025 lines Gate still leaves uncovered.

The baseline is CI run 41982570224 (`gate-coverage` on `b69ff47`): statement
coverage, `branch = false`. Tests that landed after that run already execute
most of this chunk's missing statements. What this module adds is the other
arm of a few selftest guards, two migration row-loss refusals, the read-cache
json proxy, and the docs-drift caller check in both directions.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

import capabilities
import capability_advisor as advisor
import capability_propensity as propensity
import capability_recurrence_check as recurrence
import feedback


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep ledger, Brain, and state in the temp dir and refuse process spawns."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")
    monkeypatch.setattr(propensity, "_capability_heartbeat", lambda *_a, **_k: None)
    spawned: list[object] = []

    def _blocked(*_args, **_kwargs):
        spawned.append((_args, _kwargs))
        raise AssertionError("a subprocess was started")

    monkeypatch.setattr(subprocess, "Popen", _blocked)
    monkeypatch.setattr(subprocess, "run", _blocked)
    yield spawned
    assert spawned == []


def _runs_table(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts INTEGER)")


def _drop_copied_rows(connection: sqlite3.Connection, table: str) -> None:
    """Delete every copied row. The trace runs before each statement, so the trigger is
    installed on the statement after CREATE, once the rebuild table exists and before the copy."""
    state = {"arm": False, "done": False}

    def _trace(sql: str) -> None:
        if state["done"]:
            return
        if f"CREATE TABLE {table}" in sql:
            state["arm"] = True
            return
        if not state["arm"]:
            return
        state["arm"] = False
        state["done"] = True
        connection.execute(
            f"CREATE TRIGGER drop_copied_{table} AFTER INSERT ON {table} "
            f"BEGIN DELETE FROM {table}; END"
        )

    connection.set_trace_callback(_trace)


def test_evaluations_migration_refuses_a_copy_that_drops_rows() -> None:
    """A rebuild that keeps fewer rows than it read must stop before the swap."""
    connection = sqlite3.connect(":memory:")
    _runs_table(connection)
    connection.execute(
        "CREATE TABLE evaluations_v2 (experiment_id TEXT, implementer_arm_id TEXT, score REAL)"
    )
    connection.execute("INSERT INTO evaluations_v2 VALUES ('exp-1', 'arm-1', 1.0)")
    connection.commit()
    _drop_copied_rows(connection, "evaluations_v2_rebuild")

    with pytest.raises(RuntimeError, match="evaluations_v2 migration refused row loss: before=1"):
        feedback._migrate_schema(connection)

    assert connection.execute("SELECT COUNT(*) FROM evaluations_v2").fetchone()[0] == 1
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "evaluations_v2" in names


def test_agent_switches_migration_refuses_a_copy_that_drops_rows() -> None:
    """The same refusal rolls the agent_switches rebuild back, key and rows intact."""
    connection = sqlite3.connect(":memory:")
    _runs_table(connection)
    connection.execute(
        "CREATE TABLE agent_switches ("
        "pr_ref TEXT, from_agent TEXT, to_agent TEXT, switched_ts INTEGER, "
        "auto_label INTEGER, commits_before INTEGER, commits_after INTEGER, "
        "merged INTEGER, durability TEXT, recorded_ts INTEGER)"
    )
    connection.execute("INSERT INTO agent_switches VALUES ('o/r#1','a','b',1,0,1,1,1,'durable',2)")
    connection.commit()
    assert not connection.in_transaction
    _drop_copied_rows(connection, "agent_switches_rebuild")

    with pytest.raises(RuntimeError, match="agent_switches migration refused row loss: before=1"):
        feedback._migrate_schema(connection)

    assert not connection.in_transaction
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_switches)")}
    assert "source" not in columns
    assert connection.execute("SELECT COUNT(*) FROM agent_switches").fetchone()[0] == 1


def test_bindings_selftest_restores_a_preseeded_empty_surface() -> None:
    """t-empty is restored when the selftest did not create it."""
    saved = advisor.SURFACE_BINDINGS.get("t-empty")
    sentinel = {"preexisting": "kept"}
    advisor.SURFACE_BINDINGS["t-empty"] = sentinel
    try:
        advisor._selftest_bindings()
        assert advisor.SURFACE_BINDINGS.get("t-empty") == sentinel
    finally:
        if saved is None:
            advisor.SURFACE_BINDINGS.pop("t-empty", None)
        else:
            advisor.SURFACE_BINDINGS["t-empty"] = saved


def test_tick_evidence_selftest_drops_a_binding_it_installed() -> None:
    """The pop arm runs when the tick surface was not already in the mutable table.

    The declared binding has to stay visible to ``binding_for`` or the selftest stops
    before cleanup. Only the read that captures ``real_binding`` is answered with None.
    """
    original = advisor.SURFACE_BINDINGS

    class _HideOneRead(dict):
        def get(self, key, default=None):
            caller = inspect.currentframe()
            caller = caller.f_back if caller is not None else None
            if (
                key == propensity.TICK_SURFACE
                and caller is not None
                and caller.f_code.co_name == "_selftest_tick_evidence"
                and caller.f_lineno == 3621
            ):
                return None
            return super().get(key, default)

    proxy = _HideOneRead(original)
    advisor.SURFACE_BINDINGS = proxy
    try:
        propensity._selftest_tick_evidence()
        assert propensity.TICK_SURFACE not in proxy
    finally:
        advisor.SURFACE_BINDINGS = original


def _swallow(monkeypatch: pytest.MonkeyPatch, owner: object, name: str, when, value=True):
    real = getattr(owner, name)

    def _wrapper(*args, **kwargs):
        if when(*args, **kwargs):
            return value
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, _wrapper)


def test_repair_selftest_names_an_unevidenced_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repair call that accepts a blank fix or artifact must still be refused here."""
    _swallow(
        monkeypatch,
        propensity,
        "record_repair",
        lambda *_a, **k: not str(k.get("fix") or "").strip()
        or not str(k.get("artifact") or "").strip(),
    )
    with pytest.raises(AssertionError, match="an unevidenced repair must be refused"):
        propensity._selftest_repair()


def test_find_selftest_names_a_blank_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_find",
        lambda **k: not str(k.get("defect") or "").strip()
        or not str(k.get("artifact") or "").strip(),
        value={"find_id": "swallowed"},
    )
    with pytest.raises(AssertionError, match="an unevidenced find must be refused"):
        propensity._selftest_finds()


def test_find_selftest_names_a_missing_finder(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_find",
        lambda **k: bool(str(k.get("defect") or "").strip())
        and bool(str(k.get("artifact") or "").strip())
        and not k.get("surface")
        and not k.get("capability_id"),
        value={"find_id": "swallowed"},
    )
    with pytest.raises(AssertionError, match="a find with no finder must be refused"):
        propensity._selftest_finds()


def test_find_selftest_names_a_capability_find_without_an_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_find",
        lambda **k: bool(k.get("capability_id")) and not k.get("experiment_id"),
        value={"find_id": "swallowed"},
    )
    with pytest.raises(
        AssertionError, match="a capability find with no experiment id must be refused"
    ):
        propensity._selftest_finds()


def test_decline_selftest_names_a_blank_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_decline",
        lambda *_a, **k: not str(k.get("reason") or "").strip(),
    )
    with pytest.raises(AssertionError, match="an unexplained decline must be refused"):
        propensity._selftest_declines()


def test_decline_selftest_names_a_non_advisory_experiment(monkeypatch: pytest.MonkeyPatch) -> None:
    def _when(capability_id: str, experiment_id: str, **_k) -> bool:
        del capability_id
        return not str(experiment_id).startswith("advice:")

    _swallow(monkeypatch, propensity, "record_decline", _when)
    with pytest.raises(AssertionError, match="a non-advisory experiment id must be refused"):
        propensity._selftest_declines()


def test_decline_selftest_names_an_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_decline",
        lambda *_a, **k: k.get("kind") == "wrong-match",
    )
    with pytest.raises(AssertionError, match="an unknown decline kind must be refused"):
        propensity._selftest_declines()


def test_provenance_selftest_names_an_unknown_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_usefulness",
        lambda *_a, **k: k.get("provenance") == "great",
    )
    with pytest.raises(AssertionError, match="an unknown verdict provenance must be refused"):
        propensity._selftest_provenance()


def test_provenance_selftest_names_a_blank_corroboration(monkeypatch: pytest.MonkeyPatch) -> None:
    def _when(*_a, **k) -> bool:
        return (
            k.get("provenance") in {"outcome_corroborated", "defect_found"}
            and not str(k.get("corroboration") or "").strip()
        )

    _swallow(monkeypatch, propensity, "record_usefulness", _when)
    with pytest.raises(AssertionError, match="without a named corroboration must be refused"):
        propensity._selftest_provenance()


def test_provenance_selftest_names_an_omitted_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_usefulness",
        lambda *_a, **k: "provenance" not in k,
    )
    with pytest.raises(AssertionError, match="no stated provenance must be REFUSED"):
        propensity._selftest_provenance()


def test_provenance_cli_names_a_missing_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    real = propensity.main

    def _main(argv: list[str]) -> int:
        if "useful" in argv and "--provenance" not in argv:
            return 0
        return real(argv)

    monkeypatch.setattr(propensity, "main", _main)
    with pytest.raises(AssertionError, match="must refuse a verdict with no --provenance"):
        propensity._selftest_provenance()


def test_late_outcome_selftest_skips_another_experiments_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auditor event from a different trial is not part of this amendment."""
    real = propensity._events

    def _events(cap: dict, *args, **kwargs) -> list:
        events = list(real(cap, *args, **kwargs))
        called_from_the_log_check = any(
            info.function == "_selftest_late_outcome" and info.lineno == 6566
            for info in inspect.stack()
        )
        if called_from_the_log_check:
            events.append(
                {
                    "type": "outcome",
                    "ref": "not-an-advice-ref",
                    "metadata": {propensity.VERDICT_PROVENANCE_KEY: "should-have-been-skipped"},
                    "timestamp": 1,
                }
            )
        return events

    monkeypatch.setattr(propensity, "_events", _events)
    propensity._selftest_late_outcome()


def test_late_outcome_selftest_names_a_refused_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    real = propensity.record_late_outcome

    def _wrapper(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        except ValueError:
            return {"attached": True, "reason": "swallowed"}

    monkeypatch.setattr(propensity, "record_late_outcome", _wrapper)
    with pytest.raises(AssertionError, match="must not attach"):
        propensity._selftest_late_outcome()


def test_late_outcome_selftest_names_a_bad_deliverable_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = propensity.record_late_outcome

    def _wrapper(*args, **kwargs):
        if kwargs.get("deliverable") == "Bad Slug!":
            return {"attached": True}
        return real(*args, **kwargs)

    monkeypatch.setattr(propensity, "record_late_outcome", _wrapper)
    with pytest.raises(AssertionError, match="a non-kebab deliverable must be refused"):
        propensity._selftest_late_outcome()


def test_detection_selftest_names_a_blank_promotion_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_promotion",
        lambda *_a, **_k: not str(_a[2] if len(_a) > 2 else "").strip(),
    )
    with pytest.raises(AssertionError, match="an unexplained binding promotion must be refused"):
        propensity._selftest_detection()


def test_root_selftest_names_an_unevidenced_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    _swallow(
        monkeypatch,
        propensity,
        "record_usefulness",
        lambda *_a, **k: not str(k.get("evidence") or "").strip(),
    )
    with pytest.raises(AssertionError, match="an unevidenced usefulness verdict must be refused"):
        propensity._selftest()


def test_root_selftest_names_a_non_advisory_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    def _when(_capability_id: str, experiment_id: str, **_k) -> bool:
        return experiment_id == "not-an-advice-ref"

    _swallow(monkeypatch, propensity, "record_trigger", _when)
    with pytest.raises(AssertionError, match="a non-advisory experiment id must be refused"):
        propensity._selftest()


def test_unlinked_edge_guard_names_a_default_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """The selftest's raise is how a default insert of an orphan edge stays refused."""
    real = feedback._record_influence_edge_in_conn

    def _wrapper(*args, **kwargs):
        if kwargs.get("target_run_id") == "orphan-probe" and not kwargs.get("allow_unlinked"):
            return {"edge_id": "edge:should-have-raised"}
        return real(*args, **kwargs)

    monkeypatch.setattr(feedback, "_record_influence_edge_in_conn", _wrapper)
    with pytest.raises(AssertionError, match="an unlinked edge must not be insertable by default"):
        feedback._selftest()


def test_renewal_selftest_names_a_revived_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    real = capabilities.transition

    def _transition(capability_id: str, new_state: str, **kwargs):
        if new_state == "observed" and kwargs.get("reason") == "by hand":
            return None
        return real(capability_id, new_state, **kwargs)

    monkeypatch.setattr(capabilities, "transition", _transition)
    with pytest.raises(AssertionError, match="a transition revived an expiry-retired gate row"):
        capabilities._selftest_renewal()


def test_renewal_selftest_names_a_blank_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    real = capabilities.renew

    def _renew(capability_id: str, **kwargs):
        refs = kwargs.get("evidence_refs") or []
        if not str(kwargs.get("reason") or "").strip() or not any(str(ref).strip() for ref in refs):
            return {"revived_from": "retired"}
        return real(capability_id, **kwargs)

    monkeypatch.setattr(capabilities, "renew", _renew)
    with pytest.raises(AssertionError, match="renewed without a reason and evidence"):
        capabilities._selftest_renewal()


def test_renewal_selftest_names_a_stacked_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    real = capabilities.renew

    def _renew(capability_id: str, **kwargs):
        if kwargs.get("reason") == "again" and kwargs.get("timestamp") == 2_000:
            return {"revived_from": "retired"}
        return real(capability_id, **kwargs)

    monkeypatch.setattr(capabilities, "renew", _renew)
    with pytest.raises(AssertionError, match="a second renewal at the same moment stacked"):
        capabilities._selftest_renewal()


def test_renewal_selftest_names_an_undone_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    real = capabilities.renew

    def _renew(capability_id: str, **kwargs):
        if kwargs.get("reason") == "undo":
            return {"revived_from": "retired"}
        return real(capability_id, **kwargs)

    monkeypatch.setattr(capabilities, "renew", _renew)
    with pytest.raises(AssertionError, match="a renewal undid a deliberate retirement"):
        capabilities._selftest_renewal()


def test_active_capability_missing_an_entrypoint_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The selftest catches its own AssertionError, so the message is the guard."""
    real = capabilities.validate_capability

    def _validate(cap: dict, *args, **kwargs):
        if cap.get("status") == "active" and not cap.get("entrypoint"):
            return None
        return real(cap, *args, **kwargs)

    monkeypatch.setattr(capabilities, "validate_capability", _validate)
    capabilities._selftest()


class _SecondThresholdRead(dict):
    """The formatted report still lists a prose gate; the later branch sees none."""

    reads = 0

    def get(self, key, default=None):
        if key != "threshold_undefined":
            return super().get(key, default)
        type(self).reads += 1
        if type(self).reads >= 4:
            return []
        return super().get(key, default)


def test_usage_selftest_states_the_all_encoded_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 4469 runs only when the same report later reports no prose thresholds."""
    real = capabilities.usage_report
    _SecondThresholdRead.reads = 0

    def _usage(payload, *args, **kwargs):
        report = real(payload, *args, **kwargs)
        names = set((payload.get("capabilities") or {}))
        if names == {"g", "r", "u", "s", "p", "e"}:
            return _SecondThresholdRead(report)
        return report

    monkeypatch.setattr(capabilities, "usage_report", _usage)
    capabilities._selftest()
    assert _SecondThresholdRead.reads >= 4


def test_read_cache_counter_delegates_other_json_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counting stand-in only overrides loads. Every other json attribute stays the stdlib one."""
    real_load = capabilities.load
    delegated: dict[str, str] = {}

    def _load(path, *args, **kwargs):
        proxy = capabilities.json
        if type(proxy).__name__ == "_Counting":
            delegated["dumps"] = proxy.dumps({"k": 1})
        return real_load(path, *args, **kwargs)

    monkeypatch.setattr(capabilities, "load", _load)
    capabilities._selftest_read_cache()
    assert delegated["dumps"] == json.dumps({"k": 1})


def test_docs_drift_selftest_tracks_the_caller_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The both-directions check runs when the ledger row is present and no fixture errors.

    Rail-exercise and the prologue's bash are answered locally, so nothing is spawned. The block
    still calls the real caller-state helper, then the caller it saved, and the live row must
    agree with that caller.
    """
    import capability_activation_audit as audit

    capabilities.seed_declared(capabilities.REG)
    # The row is fleet history, not a declaration this tree seeds. The both-directions
    # block is skipped until the ledger actually holds it, so the test registers one.
    rows = capabilities.load(capabilities.REG, create=False)
    row = capabilities._blank_capability("docs-drift-fix-agent")
    row["status"] = "observed"
    row["notes"] = "hermetic row so the caller check runs in both directions"
    rows["docs-drift-fix-agent"] = row
    capabilities.save(rows, capabilities.REG)
    assert recurrence.env_prereq.ledger_rows_absent("docs-drift-fix-agent") is None
    monkeypatch.delenv("ORCH_RANGE_LANE_ROLLOUT", raising=False)
    calls: list[dict] = []

    def _live_caller(cap: dict) -> dict:
        calls.append(cap)
        return {"exists": False, "repo": "R", "workflow": "w", "path": "/p"}

    monkeypatch.setattr(audit, "external_caller", _live_caller)

    def _replay(*, offline: bool = False) -> dict:
        del offline
        rows = [
            {
                "capability": None,
                "kind": "classify",
                "fires": True,
                "actual": "implement",
                "source": "child",
                "provenance": "frozen fixture",
                "labels": [],
                "title": "child",
            },
            {
                "capability": None,
                "kind": "high_stakes",
                "fires": True,
                "source": "routine",
                "provenance": "frozen fixture",
                "labels": [],
                "title": "routine",
                "reason": "none",
            },
            {
                "capability": "docs-drift-fix-agent",
                "kind": "predicate_note",
                "fires": False,
                "source": "caller",
                "provenance": "frozen fixture",
                "detail": {"exists": False},
                "labels": [],
                "title": "",
            },
        ]
        firing = sum(1 for row in rows if row["fires"])
        return {
            "total": len(recurrence.FIXTURES) + len(recurrence.PREDICATE_FIXTURES),
            "would_fire": firing,
            "would_miss": len(rows) - firing,
            "rows": rows,
        }

    monkeypatch.setattr(recurrence, "replay", _replay)
    real_script = recurrence.ORCHESTRATE
    saved = (
        recurrence._tick_env_attempt,
        recurrence._TICK_ENV,
        recurrence._TICK_ENV_DIAG,
        recurrence.TICK_ENV_BACKOFF,
        recurrence.ORCHESTRATE,
    )

    def _attempt() -> tuple[dict, dict]:
        path = recurrence.ORCHESTRATE
        if path == real_script:
            return (
                {"ORCH_RUN_ADVERSARIAL_REVIEW": "1", "ORCH_RANGE_LANE_ROLLOUT": "0"},
                {"reason": "ok", "keys": 2},
            )
        if not path.is_file():
            return {}, {"reason": "script_missing", "detail": str(path)}
        text = path.read_text(encoding="utf-8")
        if "cat /no/such/credential" in text:
            return {}, {"reason": "nonzero_exit", "returncode": 1}
        if "prologue with no ORCH_ exports" in text:
            return {}, {"reason": "no_orch_keys", "returncode": 0, "stdout_lines": 1, "keys": 0}
        env: dict[str, str] = {}
        head = text.split("_gh_gate()", 1)[0]
        for line in head.splitlines():
            match = re.search(
                r'export (ORCH_[A-Z0-9_]+)="\$\{ORCH_[A-Z0-9_]+:-([^}]*)\}"',
                line,
            )
            if not match:
                continue
            env[match.group(1)] = os.environ.get(match.group(1)) or match.group(2)
        if not env:
            return {}, {"reason": "no_orch_keys", "returncode": 0, "stdout_lines": 1, "keys": 0}
        return env, {"reason": "ok", "keys": len(env)}

    recurrence._tick_env_attempt = _attempt
    try:
        recurrence._selftest()
    finally:
        (
            recurrence._tick_env_attempt,
            recurrence._TICK_ENV,
            recurrence._TICK_ENV_DIAG,
            recurrence.TICK_ENV_BACKOFF,
            recurrence.ORCHESTRATE,
        ) = saved
    assert len(calls) == 1
    assert calls[0].get("capability_id") == "docs-drift-fix-agent"
    assert audit.external_caller is _live_caller
