"""Offline batch continuation diagnostics; no live providers or production state."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_dispatcher import FakeGitHub, closer
from test_closer_disposition import TARGET, fixture

import claims
import closer_dispatcher as adapter
import closer_disposition
import closer_pipeline as pipeline


class InjectedProcessDeath(BaseException):
    """Private test only: abrupt loss before the outer callback result persists."""


@pytest.mark.parametrize(
    "barrier",
    [
        None,
        "head",
        "review_revision",
        "source_digest",
        "evidence_hash",
        "owner_hold",
        "native_nonpass",
        "claim_held",
        "closure_crash",
    ],
)
def test_new_valid_acceptance_supersedes_a_durable_nonaccepting_refusal(
    tmp_path, monkeypatch, barrier
):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    for name in ("claim", "release", "update_metadata"):
        monkeypatch.setattr(claims, name, lambda *a, **k: True)
    monkeypatch.setattr(claims, "holder", lambda *a, **k: None)
    monkeypatch.setattr(adapter.gh_capacity, "throttle", lambda *a, **k: {"action": "allow"})
    snap, _packet, ref, prep, interpretation = fixture(tmp_path / "packet")
    snap.update(
        state="MERGED",
        merge="d" * 40,
        labels=[],
        active_threads=0,
        verifier_history_complete=True,
        verifier={
            "schema": "verifier-corpus-decision/v1",
            "verdict": "PASS",
            "head_sha": snap["head"],
            "evaluated_sha": "d" * 40,
        },
    )
    accepted = closer_disposition.integrate_ordinary(TARGET, snap, ref, prep, interpretation)
    assert accepted["status"] == "accepted"
    decision = accepted["disposition"]
    assert adapter.acceptance_ok(snap, decision)
    refused = copy.deepcopy(interpretation)
    refused["proposal"].update(
        classification="uncertain", substantive_needs=["Original proof missing"]
    )
    prep = {
        **prep,
        "identity": TARGET,
        "snapshot": snap,
        "packet_ref": ref,
        "proposal": {"action": "collect_evidence", "eligible": True},
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "5" * 64,
    }
    monkeypatch.setattr(adapter, "GitHub", lambda **kw: FakeGitHub(snap))
    real_run = pipeline.run_pipeline
    path = adapter._pipeline_state_path(tmp_path / "state.json")

    def capture(_path, _targets, **callbacks):
        return callbacks

    closure_calls = []

    def closure_transport(command, *, timeout):
        assert command[1] == "-c" and command[-1] == TARGET
        closure_calls.append(command)
        snap["sources"][0]["state"] = "closed"
        if barrier == "closure_crash":
            raise InjectedProcessDeath("simulated process loss after external closure")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"closed": 1, "errors": 0}))

    def callbacks_for(dispositions):
        backend = closer(
            tmp_path,
            snap,
            apply=True,
            workers=True,
            dispositions=dispositions,
            run_fn=closure_transport,
        )
        backend.campaign_admission = lambda *a, **k: {"allowed": True}
        with monkeypatch.context() as scope:
            scope.setattr(pipeline, "run_pipeline", capture)
            result = adapter._run_staged_pipeline(
                SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1),
                [TARGET],
                backend,
                time.time() + 300,
            )
        return result, backend

    first_callbacks, _ = callbacks_for({})
    first = real_run(
        path,
        [TARGET],
        prepare=lambda *_: copy.deepcopy(prep),
        interpret=lambda *_: {**refused, "substantive_needs": []},
        exception=lambda *_: pytest.fail("No provider is allowed"),
        act=first_callbacks["act"],
        reconcile=first_callbacks["reconcile"],
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 10,
    )
    assert first["targets"][TARGET]["deferred_reason"] == "validated_inner_wait"
    before = path.read_bytes()
    prep["proposal"] = {"action": "close_source", "eligible": True}
    prep["semantic_cache_key"] = "closer-evidence-semantic/v1:" + "6" * 64
    resumed, backend = callbacks_for({TARGET: decision})
    assert backend.select(snap)[0] == "close_source"
    result = resumed["interpret"](
        TARGET,
        {
            "prepare": prep,
            "revision": snap["revision"],
            "identity": TARGET,
            "deadline": time.time() + 60,
            "state_path": str(path),
            "action_state_path": str(
                path.parent / f"{path.stem}.actions" / f"{pipeline._digest(TARGET)}.json"
            ),
        },
    )
    assert path.read_bytes() == before
    assert result.get("classification") == "deterministic_close_source", result
    monkeypatch.setenv("ORCH_SUPERVISOR_CLOSE", "1")
    if barrier and barrier != "closure_crash":
        backend.dispositions[TARGET] = copy.deepcopy(decision)
        if barrier in {"head", "review_revision", "source_digest"}:
            backend.dispositions[TARGET][barrier] = "changed-after-interpretation"
        elif barrier == "evidence_hash":
            backend.dispositions[TARGET]["criteria"][0]["evidence"][0]["sha256"] = "0" * 64
        elif barrier == "owner_hold":
            backend.blockers[TARGET] = {"reason": "explicit owner hold"}
        elif barrier == "native_nonpass":
            snap["verifier"]["verdict"] = "NON_PASS"
        else:
            monkeypatch.setattr(claims, "claim", lambda *a, **k: False)
        action_context = {
            "prepare": prep,
            "interpret": result,
            "revision": snap["revision"],
            "identity": TARGET,
            "state_path": str(path),
            "action_state_path": str(
                path.parent / f"{path.stem}.actions" / f"{pipeline._digest(TARGET)}.json"
            ),
            # claim_held must keep a full action budget so the inner queue reaches
            # the refused claim (unverified_claim_conflict → RuntimeError). A
            # sub-second cutoff races observe/setup under xdist and can stop as a
            # valid receipt_reserve deferral instead, which is a flake not a proof.
            "deadline": time.time() + 30,
        }
        if barrier == "claim_held":
            with pytest.raises(RuntimeError, match="valid continuation boundary"):
                resumed["act"](TARGET, action_context)
        else:
            blocked = resumed["act"](TARGET, action_context)
            assert blocked.get("status") != "completed", blocked
        assert not closure_calls
        assert snap["sources"][0]["state"] == "open"
        return

    # Real pipeline/native queue execute a simulated external closure transport.
    # The accepted disposition, fresh selection and receipt machinery stay real.
    def run_delivery():
        return real_run(
            path,
            [TARGET],
            prepare=lambda *_: copy.deepcopy(prep),
            interpret=resumed["interpret"],
            exception=lambda *_: pytest.fail("No provider replay"),
            act=resumed["act"],
            reconcile=resumed["reconcile"],
            capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            deadline=time.time() + 10,
        )

    if barrier == "closure_crash":
        with pytest.raises(InjectedProcessDeath):
            run_delivery()
        crashed = next(iter(pipeline._load(path)["runs"].values()))["targets"][TARGET]
        assert crashed["status"] in {"running", "uncertain"}
    else:
        completed = run_delivery()
        assert completed["targets"][TARGET]["status"] == "completed", completed
    assert len(closure_calls) == 1
    assert snap["sources"][0]["state"] == "closed"
    if barrier != "closure_crash":
        assert pipeline.validate_stop(path, [TARGET])["valid"]
    # A fresh adapter and a further cadence must retain the accepted decision and
    # finished outcome without repeating the external closure after restart.
    prep["proposal"] = {"action": "complete", "eligible": False}
    restarted, _ = callbacks_for({TARGET: decision})
    settled = real_run(
        path,
        [TARGET],
        prepare=lambda *_: copy.deepcopy(prep),
        interpret=restarted["interpret"],
        exception=lambda *_: pytest.fail("No provider replay"),
        act=restarted["act"],
        reconcile=restarted["reconcile"],
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 10,
    )
    assert settled["targets"][TARGET]["status"] == "completed", settled
    assert len(closure_calls) == 1


@pytest.mark.parametrize("validation_exit", [0, 65])
def test_cleanup_is_once_and_releases_only_private_mutex_on_valid_or_rejected_stop(
    tmp_path, validation_exit
):
    from test_install_closer_dispatcher import fake_home, installer

    home, _, postrun, _ = fake_home(tmp_path, label_repair=True)
    assert home.is_relative_to(tmp_path)
    installer.install(home, apply=True, enforce=True)
    validator = home / ".codex/orchestrator-mirror/closer_dispatcher.py"
    validator.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "h=Path(os.environ['HOME'])\n"
        "assert 'pytest-' in str(h)\n"
        "with (h/'validation-count').open('a') as f:f.write('validate\\n')\n"
        "sys.exit(int(os.environ['FAKE_VALIDATION_EXIT']))\n"
    )
    (home / "mutex").mkdir()
    result = subprocess.run(
        ["/bin/bash", str(postrun), "closer"],
        env={
            **os.environ,
            "HOME": str(home),
            "HANDOFF_DIR": str(home / "handoff"),
            "CLOSER_ROUND_STATE": str(home / "round.json"),
            "FAKE_VALIDATION_EXIT": str(validation_exit),
        },
        capture_output=True,
    )
    assert result.returncode == (0 if validation_exit == 0 else 65)
    assert (home / "validation-count").read_text() == "validate\n"
    assert (home / "repair-actions").read_text() == "repaired\n"
    assert not (home / "mutex").exists()
    assert (home / "actions").exists() is (validation_exit == 0)
    if validation_exit == 0:
        assert (home / "actions").read_text() == "end-round\nrelay\n"


def test_audit_growth_retains_chain_and_tamper_fails_closed(tmp_path):
    path = tmp_path / "pipeline.json"
    original = {
        "schema": "closer-expired-invalid-round-recovery/v1",
        "source_run_id": "origin",
        "targets": {"owner/repo#1": {"recovery": "quarantine"}},
    }
    previous = {"invalid_stop_history": [original]}
    expected = [original]
    for i in range(6):
        entry = {
            "schema": original["schema"],
            "source_run_id": f"read-cadence-{i}",
            "targets": {},
            "diagnostic_storage_padding": "x" * 110_000,
        }
        local, reference = pipeline._stage_audit_history(path, previous, entry)
        previous.update(invalid_stop_history=local, invalid_stop_archive=reference)
        expected.append(entry)
        assert pipeline._resolved_audit_history(path, previous) == expected
    assert previous["invalid_stop_archive"]
    chunk = pipeline._audit_archive_dir(path) / (
        previous["invalid_stop_archive"]["sha256"] + ".json"
    )
    original_bytes = chunk.read_bytes()
    chunk.write_bytes(original_bytes + b" ")
    with pytest.raises(ValueError, match="tampered"):
        pipeline._resolved_audit_history(path, previous)
    chunk.write_bytes(original_bytes)
    assert pipeline._resolved_audit_history(path, previous) == expected


def test_publication_lock_serializes_but_does_not_fence_an_older_verified_snapshot(tmp_path):
    from scripts import install_verified_snapshot as installer
    from test_install_verified_snapshot import _snapshot

    old = _snapshot(tmp_path / "old")
    new = _snapshot(tmp_path / "new")
    (old / "module.py").write_text("VALUE = 'older verified payload'\n")
    (new / "module.py").write_text("VALUE = 'newer verified payload'\n")
    mirror = tmp_path / "private-runtime"
    installer.install(new, mirror, expected_digest=installer.snapshot_digest(new))
    first_generation = mirror.resolve()
    assert "newer" in (mirror / "module.py").read_text()
    # This explicitly documents current installer scope, not a safe deployment.
    # It verifies payload identity and serializes writers, but has no source-age fence.
    installer.install(old, mirror, expected_digest=installer.snapshot_digest(old))
    assert mirror.resolve() != first_generation
    assert "older" in (mirror / "module.py").read_text()
    assert (first_generation / "module.py").read_text().find("newer") >= 0


@pytest.mark.parametrize(
    "reason", ["substantive_or_ambiguous_finding", "source_coverage_incomplete"]
)
@pytest.mark.parametrize(
    "resource_kind", ["worker_capacity", "provider_failure", "github_capacity", "deadline"]
)
def test_restored_refusal_survives_three_measured_read_deferral_cadences(
    tmp_path, monkeypatch, reason, resource_kind
):
    from test_closer_pipeline import FakeRoundCapacityDeferred
    from test_closer_refusal_continuation import compacted_legacy_refusal

    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(claims, "holder", lambda *a, **k: None)
    for name in ("claim", "release", "update_metadata"):
        monkeypatch.setattr(claims, name, lambda *a, **k: True)
    target, path, _snapshot, proof, doc, old, prepared = compacted_legacy_refusal(tmp_path, reason)
    origin = copy.deepcopy(next(iter(doc["runs"].values()))["invalid_stop_history"])
    inner = Path(proof["inner_path"])
    original_native = inner.read_bytes()
    clock = [max(time.time(), next(iter(doc["runs"].values()))["deadline"] + 1)]
    counts = {"prepare": 0, "interpret": 0}
    for _ in range(3):
        cutoff = clock[0] + 5

        def prepare(*_args):
            counts["prepare"] += 1
            if resource_kind == "github_capacity":
                raise FakeRoundCapacityDeferred(
                    resource="core",
                    throttle={
                        "resource": "core",
                        "state": "shed",
                        "action": "defer",
                        "remaining": 50,
                        "limit": 5000,
                        "reset_in_s": 4200,
                        "source": "call",
                        "reason": "offline measured SHED",
                    },
                    observed_at=clock[0],
                    reset_at_epoch=clock[0] + 4200,
                )
            if resource_kind == "deadline":
                clock[0] = cutoff + 1
                raise adapter.RoundDeadlineExhausted(deadline=cutoff, remaining_seconds=0)
            return copy.deepcopy(prepared)

        def interpret(*_args):
            counts["interpret"] += 1
            assert resource_kind == "provider_failure"
            return {
                "status": "deferred",
                "reason": "provider_failure",
                "next_trigger": "retry_profile_later",
                "resource_evidence": {
                    "provider_failure": {
                        "profile_id": "offline-low",
                        "agent": "offline",
                        "run_id": "offline-failed-admission",
                        "exit": 70,
                        "category": "transport",
                        "evidence_hash": "e" * 64,
                    }
                },
            }

        result = pipeline.run_pipeline(
            path,
            [target],
            prepare=prepare,
            interpret=interpret,
            exception=lambda *_: pytest.fail("No provider"),
            act=lambda *_: pytest.fail("No guarded action"),
            capacity_fn=lambda: {
                "io": 1,
                "low": 0 if resource_kind == "worker_capacity" else 1,
                "exception": 0,
            },
            deadline=cutoff,
            now=lambda: clock[0],
            round_deadline_exception=adapter.RoundDeadlineExhausted,
            round_capacity_exception=FakeRoundCapacityDeferred,
        )
        assert result.get("reason") != "prior_run_has_no_valid_stop", result
        current = next(iter(pipeline._load(path)["runs"].values()))
        assert origin[0] in pipeline._resolved_audit_history(path, current)
        assert current["targets"][target]["results"]["act"] == old["results"]["act"]
        assert not current["targets"][target].get("known_completed_action")
        assert inner.read_bytes() == original_native
        clock[0] = cutoff + 10
    assert counts["prepare"] == 3


@pytest.mark.parametrize(
    "tamper",
    [
        "reason",
        "trigger",
        "act",
        "receipt",
        "identity",
        "revision",
        "anchor_hash",
        "anchor_source",
        "anchor_head",
        "anchor_path",
        "audit_act",
        "provider_uncertain",
        "known_action",
        "native_head",
        "native_operation",
        "capacity",
    ],
)
def test_retained_refusal_read_retry_keeps_every_native_and_audit_barrier(
    tmp_path, monkeypatch, tamper
):
    from test_closer_refusal_continuation import compacted_legacy_refusal

    import closer_queue

    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(claims, "holder", lambda *a, **k: None)
    target, path, _snapshot, proof, doc, _old, prepared = compacted_legacy_refusal(
        tmp_path, "substantive_or_ambiguous_finding"
    )
    at = max(time.time(), next(iter(doc["runs"].values()))["deadline"] + 1)
    pipeline.run_pipeline(
        path,
        [target],
        prepare=lambda *_: copy.deepcopy(prepared),
        interpret=lambda *_: pytest.fail("No provider"),
        act=lambda *_: pytest.fail("No action"),
        exception=lambda *_: pytest.fail("No exception provider"),
        capacity_fn=lambda: {"io": 1, "low": 0, "exception": 0},
        deadline=at + 5,
        now=lambda: at,
    )
    current = next(iter(pipeline._load(path)["runs"].values()))
    row = current["targets"][target]
    history = pipeline._resolved_audit_history(path, current)
    assert pipeline._audited_retained_wait(path, current, target, row, history, at, "unused")
    anchor = current["recovered_invalid_stop"]["targets"][target]
    if tamper == "reason":
        row["results"]["act"]["reason"] = "arbitrary_blocked_reason"
    elif tamper == "trigger":
        row["results"]["act"]["next_trigger"] = "retry_provider"
    elif tamper == "act":
        row["results"]["act"]["status"] = "completed"
    elif tamper == "receipt":
        row["results"]["act"]["queue_receipt"]["valid"] = False
    elif tamper in {"identity", "revision"}:
        row[tamper] = "changed"
    elif tamper == "anchor_hash":
        anchor["previous_item_sha256"] = "0" * 64
    elif tamper == "anchor_source":
        anchor["previous_run_id"] = "unrelated-source"
    elif tamper == "anchor_head":
        anchor["head"] = "e" * 40
    elif tamper == "anchor_path":
        anchor["inner_path"] = str(tmp_path / "unrelated.json")
    elif tamper == "audit_act":
        history[-1]["targets"][target]["act_result"] = {"status": "completed"}
    elif tamper == "provider_uncertain":
        row["provider_outcome_uncertain"] = {"run_id": "unknown"}
    elif tamper == "known_action":
        row["known_completed_action"] = True
    elif tamper == "capacity":
        row["resource_evidence"]["capacity"]["available"] = 1
    else:
        native_path = Path(proof["inner_path"])
        native = closer_queue._read_state(native_path)
        if tamper == "native_head":
            native["items"][target]["evidence"]["head"] = "e" * 40
        else:
            native["items"][target]["operation"] = {"id": "unknown", "status": "uncertain"}
        closer_queue._atomic_write(native_path, native)
    if tamper == "capacity":
        run_id = next(iter(pipeline._load(path)["runs"]))
        prior = pipeline.validate_stop(path, [target], now=lambda: at + 6)
        assert not pipeline._expired_invalid_round_recovery(prior, current, path, run_id, at + 6)[0]
    else:
        assert not pipeline._audited_retained_wait(
            path, current, target, row, history, at, "unused"
        )
