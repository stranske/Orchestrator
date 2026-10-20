"""Real CLI entry gates; all external transport is replaced with explicit offline spies."""

import json
import time
from types import SimpleNamespace

import pytest

import closer_dispatcher as d
import closer_pipeline as p
import closer_queue as q
import closer_resources as r


def setup_entry(tmp_path, monkeypatch, target="o/r#2", snapshot=None):
    state = tmp_path / "state"
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": target}]}))
    observed = snapshot or {
        "target": target,
        "revision": "r",
        "kind": "pr",
        "head": "a" * 40,
        "head_branch": "feature",
        "head_repository": "o/r",
        "sources": [{"target": "o/r#9"}],
    }
    calls = []
    backend = SimpleNamespace(
        workspaces={target: str(tmp_path / "tree")},
        observe=lambda _: observed,
        execute=lambda *a: calls.append(a) or {"status": "completed"},
        reconcile=lambda *a: {"status": "unknown"},
    )
    monkeypatch.setattr(d, "GitHub", lambda **kw: None)
    monkeypatch.setattr(d, "Closer", lambda *a, **kw: backend)
    monkeypatch.setattr(d, "known_workspaces", lambda _: {})
    args = [
        "run",
        "--assignment",
        str(assignment),
        "--state",
        str(state),
        "--apply",
        "--workers",
        "--budget-seconds",
        "10",
    ]
    return state, target, observed, calls, args


@pytest.mark.parametrize("mode", ["raw", "legacy", "resource_scoped"])
def test_cli_gate_precedes_legacy_state_read(tmp_path, monkeypatch, capsys, mode):
    state, target, _, calls, args = setup_entry(tmp_path, monkeypatch)
    state.write_text("{}")
    monkeypatch.setattr(q, "_read_state", lambda _: pytest.fail("contested entry read state"))
    if mode != "raw":
        args += ["--pipeline", "--coordinator-mode", mode]
    if mode == "resource_scoped":
        args += [
            "--coordinator-contract-pin",
            json.dumps(
                {
                    "schema": r.CONTRACT_SCHEMA,
                    "layout": r.LAYOUT,
                    "pipeline_schema": p.SCHEMA,
                    "coordination_gates": 1,
                }
            ),
        ]
    # Do not enter actual backend construction for staged target work.
    if mode != "raw":

        def staged(args, targets, backend, deadline):
            with pytest.raises(r.ResourceBlocked):
                with r.admission_gate(
                    args._coordination_admission, d._pipeline_state_path(state), target
                ):
                    pytest.fail("contested target admitted")
            return {"round_id": "a" * 32}

        monkeypatch.setattr(d, "_run_staged_pipeline", staged)
        monkeypatch.setattr(p, "validate_stop", lambda *a, **kw: {"valid": False})
        monkeypatch.setattr(p, "validate_stop_scoped", lambda *a, **kw: {"valid": False})
    with r.coordination_gate(d._pipeline_state_path(state), target):
        assert d.main(args) == 78
    assert not calls
    capsys.readouterr()


@pytest.mark.parametrize("alias", ["pr", "issue", "branch", "worktree"])
def test_raw_legacy_exact_alias_unknown_blocks_effect_disjoint_progresses(
    tmp_path, monkeypatch, capsys, alias
):
    state, target, observed, calls, args = setup_entry(tmp_path, monkeypatch)
    root = r.scoped_root(d._pipeline_state_path(state))
    key = {
        "pr": "pr:o/r#2",
        "issue": "issue:o/r#9",
        "branch": "branch:o/r@feature",
        "worktree": "worktree:" + str((tmp_path / "tree").resolve()),
    }[alias]

    def native(_state, targets, observe, execute, **kw):
        try:
            execute(target, "merge", observe(target), {"id": "offline"})
        except r.ResourceBlocked:
            return {"stop_valid": False, "targets": {target: {"status": "uncertain"}}}
        return {"stop_valid": True, "targets": {target: {"status": "completed"}}}

    monkeypatch.setattr(q, "run_round", native)
    # Leave a durable unknown from another owner, without creating any scoped root.
    with r.legacy_reserve(root, [key], target="o/r#1", owner="prior", deadline=time.time() + 2):
        pass
    assert not root.exists()
    before = r.unknown_reservations(root)
    assert d.main(args) == 78
    assert not calls and r.unknown_reservations(root) == before
    capsys.readouterr()
    observed.update(head_branch="disjoint", sources=[{"target": "o/r#99"}])
    observed["target"] = "o/r#3"
    target = "o/r#3"
    # The fake native transport uses the fresh target and distinct workspace mapping.
    state2, _, _, calls2, args2 = setup_entry(
        tmp_path, monkeypatch, target=target, snapshot=observed
    )
    # PR collision uses #2, other three alias dimensions must also be disjoint.
    monkeypatch.setattr(
        d,
        "Closer",
        lambda *a, **kw: SimpleNamespace(
            workspaces={target: str(tmp_path / "different-tree")},
            observe=lambda _: observed,
            execute=lambda *a: calls2.append(a) or {"status": "completed"},
            reconcile=lambda *a: {},
        ),
    )
    assert d.main(args2) == 78  # old unknown remains a stop barrier, not a global execution hold
    assert len(calls2) == 1 and r.unknown_reservations(root) == before
    capsys.readouterr()


def test_raw_validate_stop_includes_common_unknown_intents(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    root = r.scoped_root(d._pipeline_state_path(state))
    with r.legacy_reserve(
        root, ["branch:o/r@feature"], target="o/r#1", owner="old", deadline=time.time() + 2
    ):
        pass
    monkeypatch.setattr(q, "validate_stop", lambda *a, **kw: {"valid": True, "stop_valid": True})
    assert d.main(["validate-stop", "--state", str(state)]) == 78
    receipt = json.loads(capsys.readouterr().out)
    assert "unresolved_resource_reservations" in receipt["reasons"]


def test_preheld_capability_is_exact_and_expires(tmp_path):
    with r.coordination_admission(tmp_path / "s", ["o/r#1"]) as admission:
        with r.admission_gate(admission, tmp_path / "s", "O/R#1"):
            pass
        for path, target in [(tmp_path / "other", "o/r#1"), (tmp_path / "s", "o/r#2")]:
            with pytest.raises(r.ResourceBlocked):
                with r.admission_gate(admission, path, target):
                    pass
    with pytest.raises(r.ResourceBlocked):
        with r.admission_gate(admission, tmp_path / "s", "o/r#1"):
            pass


@pytest.mark.parametrize("status", ["completed", "unknown", "blocked"])
@pytest.mark.parametrize("action", ["merge", "initiate_verify", "post_merge_verify"])
def test_runtime_reconcile_never_falls_through_native_readback(tmp_path, status, action):
    result = {"status": status, "reason": "offline Core receipt"}
    operation = {
        "id": "original",
        "owner": "original-owner",
        "action": action,
        "binding": {"head": "a" * 40},
    }
    seen = []
    binding = SimpleNamespace(reconcile=lambda t, op: seen.append((t, op)) or result)
    backend = d.Closer(
        SimpleNamespace(snapshot=lambda _: pytest.fail("native readback bypassed Core")),
        state_path=tmp_path / "s",
        runtime_binding=binding,
    )
    assert backend.reconcile("o/r#1", operation) is result
    assert seen == [("o/r#1", operation)] and seen[0][1] is operation
