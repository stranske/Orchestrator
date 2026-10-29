import fcntl
import json
import subprocess
from pathlib import Path

import pytest

import closer_launch_operator as operator
from closer_frontier_policy import action_identity, make_frontier


def test_ledger_keeps_unobserved_business_stages_unknown(tmp_path):
    campaign = tmp_path / "campaign.json"
    result = tmp_path / "result.json"
    terminal = tmp_path / "terminal.json"
    campaign.write_text(json.dumps({"schema_version": 1, "campaign_id": "goal-1"}))
    result.write_text(
        json.dumps({"detail": json.dumps({"native_execution": {"phases": {"A": {}}}})})
    )
    terminal.write_text(json.dumps({"passed": True, "kind": "native-wrapper-invocation"}))
    ledger = operator.acceptance_ledger(campaign, result, terminal)
    assert ledger["last_proven_boundary"] == {"stage": "ACTION A", "state": "observed"}
    assert [row["state"] for row in ledger["rows"][3:]] == ["UNKNOWN"] * 4


def test_launch_needs_matching_explicit_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(operator, "plan", lambda *_a, **_k: pytest.fail("plan must not run"))
    try:
        operator.main(
            [
                "launch",
                "--manifest",
                "/x",
                "--campaign",
                "/campaign",
                "--run-id",
                "r",
                "--confirm-run-id",
                "wrong",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("mismatched confirmation unexpectedly launched")
    assert "requires --confirm-run-id" in capsys.readouterr().err


def test_blocked_diagnostic_returns_75_without_subprocess(monkeypatch, capsys):
    monkeypatch.setattr(
        operator, "plan", lambda *_a, **_k: {"launch_permitted_by_diagnostic": False}
    )
    monkeypatch.setattr(
        operator.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch")
    )
    assert (
        operator.main(
            [
                "launch",
                "--manifest",
                "/x",
                "--campaign",
                "/campaign",
                "--run-id",
                "r",
                "--confirm-run-id",
                "r",
            ]
        )
        == 75
    )
    assert json.loads(capsys.readouterr().out)["launch_permitted_by_diagnostic"] is False


def test_launch_crosses_the_canonical_bash_wrapper_boundary(monkeypatch, capsys):
    wrapper = "/private/tmp/canonical-cursor-lane.sh"
    prepared = {
        "launch_permitted_by_diagnostic": True,
        "canonical_wrapper_argv": ["/bin/bash", wrapper, "closer"],
    }
    calls = []
    monkeypatch.setattr(operator, "plan", lambda *_a, **_k: prepared)
    monkeypatch.setattr(
        operator.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs))
        or subprocess.CompletedProcess(argv, 17),
    )
    assert (
        operator.main(
            [
                "launch",
                "--manifest",
                "/x",
                "--campaign",
                "/campaign",
                "--run-id",
                "r",
                "--confirm-run-id",
                "r",
            ]
        )
        == 17
    )
    assert calls == [(["/bin/bash", wrapper, "closer"], {"check": False})]
    assert json.loads(capsys.readouterr().out)["native_entry_exit"] == 17


def test_canonical_wrapper_argv_rejects_relative_or_aliased_wrapper(tmp_path):
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("#!/bin/bash\n")
    assert operator._canonical_wrapper_argv(
        {"mechanism_contract": {"wrapper": {"path": str(wrapper)}}}
    ) == [
        "/bin/bash",
        str(wrapper),
        "closer",
    ]
    alias = tmp_path / "alias.sh"
    alias.symlink_to(wrapper)
    with pytest.raises(ValueError, match="canonical"):
        operator._canonical_wrapper_argv({"mechanism_contract": {"wrapper": {"path": str(alias)}}})


def test_plan_holds_when_actual_native_wrapper_entry_validation_fails(monkeypatch, tmp_path):
    """Use the real entry checker, with a physically wrong wrapper source."""
    import closer_native_observer as native

    manifest_path = tmp_path / "host-manifest.json"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text("#!/bin/bash\necho legacy\n")
    manifest_path.write_text("{}")
    manifest = {
        "launcher": {"path": "/bin/true"},
        "mechanism_contract": {"wrapper": {"path": str(wrapper)}},
    }
    value = {"campaign_root": str(tmp_path / "campaigns")}
    monkeypatch.setattr(operator, "_load_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        native, "contract", lambda _manifest: (value, wrapper, Path("/bin/true"), tmp_path)
    )
    monkeypatch.setattr(
        operator,
        "campaign_gate",
        lambda *_args: {"stage": "ready", "kind": "finite-current-authority"},
    )
    import closer_launch_preflight as readiness

    monkeypatch.setattr(
        readiness, "inspect", lambda *_args, **_kwargs: pytest.fail("must not refresh")
    )
    prepared = operator.plan(manifest_path, tmp_path / "campaign.json", "run-1")
    assert prepared["launch_permitted_by_diagnostic"] is False
    assert prepared["business_capacity"]["stage"] == "held"
    assert "wrapper entry" in prepared["business_capacity"]["reason"]
    assert prepared["canonical_wrapper_argv"] is None


def test_finite_unavailable_requires_controller_reentry_not_native_relaunch(tmp_path):
    campaign = tmp_path / "campaign.json"
    frontier = make_frontier(
        ["acme/one#1"],
        owner="runner-a",
        frontier_id="one-attempt",
        bindings={
            "acme/one#1": {
                "revision": "head-1",
                "action_id": action_identity("acme/one#1", "head-1", "review", {}),
            }
        },
    )
    campaign.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "work",
                "assignment_targets": ["acme/one#1"],
                "worker_config": {"runtime_manifest": "/manifest", "finite_frontier": frontier},
                "targets": {"*round*": "reconcile"},
                "workers": [{"worker_id": "w", "state": "terminal", "outcome": "unavailable"}],
            }
        )
    )
    campaign.with_name(campaign.name + ".lock").touch()
    gate = operator.campaign_gate(campaign, Path("/manifest"))
    assert gate["stage"] == "held"
    assert gate["accepted_dispositions"] == [
        "native-pre-dispatch-no-start/v2",
        "native-retained-no-b-reconciliation/v1",
    ]


def test_campaign_gate_holds_missing_or_malformed_state(tmp_path):
    missing = tmp_path / "missing.json"
    assert operator.campaign_gate(missing, Path("/manifest"))["stage"] == "held"
    malformed = tmp_path / "bad.json"
    malformed.write_text("not-json")
    malformed.with_name(malformed.name + ".lock").touch()
    assert operator.campaign_gate(malformed, Path("/manifest"))["stage"] == "held"


def test_campaign_gate_holds_manifest_mismatch_and_active_writer_lock(tmp_path):
    campaign = tmp_path / "campaign.json"
    campaign.write_text(
        json.dumps({"schema_version": 1, "worker_config": {"runtime_manifest": "/other"}})
    )
    lock = campaign.with_name(campaign.name + ".lock")
    lock.touch()
    assert operator.campaign_gate(campaign, Path("/manifest"))["stage"] == "held"
    campaign.write_text(
        json.dumps({"schema_version": 1, "worker_config": {"runtime_manifest": "/manifest"}})
    )
    with lock.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        assert operator.campaign_gate(campaign, Path("/manifest"))["stage"] == "held"


def test_campaign_gate_reports_ready_generic_and_finite_current(tmp_path):
    def write(name, worker_config):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "phase": "work",
                    "worker_config": worker_config,
                    "assignment_targets": ["acme/one#1"],
                    "targets": {"*round*": "pending"},
                    "workers": [],
                }
            )
        )
        path.with_name(path.name + ".lock").touch()
        return path

    generic = write("generic.json", {"runtime_manifest": "/manifest"})
    assert operator.campaign_gate(generic, Path("/manifest")) == {
        "stage": "ready",
        "kind": "non-finite",
    }
    frontier = make_frontier(
        ["acme/one#1"],
        owner="runner-a",
        frontier_id="fresh",
        bindings={
            "acme/one#1": {
                "revision": "head-1",
                "action_id": action_identity("acme/one#1", "head-1", "review", {}),
            }
        },
    )
    finite = write("finite.json", {"runtime_manifest": "/manifest", "finite_frontier": frontier})
    assert operator.campaign_gate(finite, Path("/manifest")) == {
        "stage": "ready",
        "kind": "finite-current-authority",
    }


def test_campaign_gate_reports_source_initialization_hold_and_nonwork_phase(tmp_path):
    from closer_campaign import Campaign

    path = tmp_path / "held.campaign.json"
    campaign = Campaign(path)
    initial = {
        "hold": {
            "kind": "source_initialization",
            "reason": "await typed demand",
            "bootstrap_anchor_sha256": "a" * 64,
        },
        "correction_controller": {"config": {"state_path": str(path)}},
    }
    campaign.begin(
        "new",
        ["acme/one#1"],
        owner="owner",
        release_pin="b" * 64,
        worker_config={"runtime_manifest": "/manifest"},
        _source_initialization=initial,
    )
    before = path.read_bytes()
    held = operator.campaign_gate(path, Path("/manifest"))
    assert held["stage"] == "held" and held["hold"] == initial["hold"]
    assert path.read_bytes() == before
    campaign.update(lambda doc: doc.update(hold=None, phase="release"))
    assert operator.campaign_gate(path, Path("/manifest"))["kind"] == "campaign-phase"
