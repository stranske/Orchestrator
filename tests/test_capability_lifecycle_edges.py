"""Hermetic refusal and persistence contracts for capability lifecycle boundaries."""

import copy
import json
import socket
import subprocess

import pytest

import capabilities as caps


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for name in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        monkeypatch.setenv(name, str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("lifecycle boundary test attempted a process or network call")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _ledger(tmp_path, **records):
    path = tmp_path / "capabilities.json"
    caps._write_ledger_unlocked(path, records)
    return path


def _record(name, status="wired"):
    record = caps._blank_capability(name)
    record["status"] = status
    return record


def test_compiled_registration_is_idempotent_and_rejects_identity_changes(tmp_path):
    path = tmp_path / "capabilities.json"
    args = dict(
        target_kind="role",
        artifact={"schema": "test", "body": "original"},
        lifecycle_policy={},
        record={"status": "observed"},
        path=path,
    )
    first = caps.register_compiled_version("candidate", **args)
    original = path.read_bytes()
    assert caps.register_compiled_version("candidate", **args) == first
    assert path.read_bytes() == original
    args["artifact"] = {"schema": "test", "body": "changed"}
    with pytest.raises(ValueError, match="immutable compiled capability changed"):
        caps.register_compiled_version("candidate", **args)
    assert path.read_bytes() == original
    args["target_kind"] = "unsupported"
    with pytest.raises(ValueError, match="unsupported capability target_kind"):
        caps.register_compiled_version("candidate", **args)
    assert path.read_bytes() == original


def test_verified_rollback_persists_proof_without_rewriting_prior_outcomes(tmp_path):
    candidate = _record("candidate", "canary")
    candidate.update(
        predecessor="baseline",
        rollback_pending={"predecessor": "baseline", "reason": "regression"},
        next_transition="active",
        outcome_links=["original:failure"],
    )
    baseline = _record("baseline", "active")
    path = _ledger(tmp_path, candidate=candidate, baseline=baseline)
    before_baseline = copy.deepcopy(baseline)
    result = caps.complete_verified_rollback(
        "candidate",
        rollback_proof="sha256:receipt",
        predecessor="baseline",
        path=path,
        timestamp=50,
    )
    stored = json.loads(path.read_text())["capabilities"]
    assert result == stored["candidate"]
    assert result["status"] == "retired"
    assert result["rollback_pending"] is None and result["next_transition"] is None
    assert result["outcome_links"] == ["original:failure"]
    assert result["rollback_result"] == {
        "predecessor": "baseline",
        "reason": "regression",
        "phase": "verified",
        "rollback_proof": "sha256:receipt",
        "verified_at": 50,
    }
    assert stored["baseline"] == before_baseline
    assert result["event_history"][-1]["to"] == "retired"


@pytest.mark.parametrize(
    "fault,message",
    [
        ("proof", "sha256 proof"),
        ("missing", "unknown capability"),
        ("pending", "no pending rollback"),
        ("predecessor", "predecessor drifted"),
        ("inactive", "not routable"),
    ],
)
def test_rollback_refusals_preserve_ledger_bytes(tmp_path, fault, message):
    candidate = _record("candidate", "canary")
    candidate.update(predecessor="baseline", rollback_pending={"predecessor": "baseline"})
    records = {"candidate": candidate, "baseline": _record("baseline", "active")}
    if fault == "missing":
        records.pop("candidate")
    elif fault == "pending":
        candidate["rollback_pending"] = None
    elif fault == "predecessor":
        candidate["rollback_pending"]["predecessor"] = "other"
    elif fault == "inactive":
        records["baseline"]["status"] = "retired"
    path = _ledger(tmp_path, **records)
    before = path.read_bytes()
    with pytest.raises((ValueError, AssertionError), match=message):
        caps.complete_verified_rollback(
            "candidate",
            rollback_proof="bad" if fault == "proof" else "sha256:proof",
            predecessor="baseline",
            path=path,
            timestamp=50,
        )
    assert path.read_bytes() == before


def test_successor_link_preserves_original_outcomes_and_refuses_replacement(tmp_path):
    retired = _record("retired", "retired")
    retired["outcome_links"] = ["original:failure", "original:success"]
    path = _ledger(
        tmp_path, retired=retired, successor=_record("successor"), other=_record("other")
    )
    result = caps.link_successor("retired", "successor", path=path, timestamp=60)
    stored = json.loads(path.read_text())["capabilities"]
    assert result["preserved_outcome_links"] == retired["outcome_links"]
    assert stored["retired"]["successor"] == "successor"
    assert stored["successor"]["supersedes"] == "retired"
    assert (
        stored["retired"]["event_history"][-1]["preserved_outcome_links"]
        == retired["outcome_links"]
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="different successor"):
        caps.link_successor("retired", "other", path=path, timestamp=61)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "fault,message", [("predecessor", "retired predecessor"), ("successor", "live successor")]
)
def test_successor_refusals_do_not_write(tmp_path, fault, message):
    path = _ledger(
        tmp_path,
        old=_record("old", "wired" if fault == "predecessor" else "retired"),
        new=_record("new", "retired" if fault == "successor" else "wired"),
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        caps.link_successor("old", "new", path=path, timestamp=70)
    assert path.read_bytes() == before


def test_owner_question_records_authority_without_activating_target(tmp_path):
    cap = _record("candidate", "retired")
    cap["outcome_links"] = ["original:outcome"]
    path = _ledger(tmp_path, candidate=cap)
    result = caps.attach_owner_question(
        "candidate", {"question_id": "question:one", "status": "open"}, path=path, timestamp=80
    )
    assert result == json.loads(path.read_text())["capabilities"]["candidate"]
    assert result["status"] == "retired"
    assert result["outcome_links"] == ["original:outcome"]
    assert result["owner_question"] == {
        "question_id": "question:one",
        "status": "open",
        "recorded_at": 80,
    }
    assert result["gate_evidence"] == "question:one"
    assert result["event_history"][-1]["type"] == "owner_question"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid owner-question"):
        caps.attach_owner_question("candidate", {"status": "open"}, path=path)
    with pytest.raises(ValueError, match="unknown capability"):
        caps.attach_owner_question("missing", {"question_id": "q", "status": "open"}, path=path)
    assert path.read_bytes() == before


def test_reconcile_continues_after_one_compiled_version_fails(tmp_path, monkeypatch):
    rows = {
        "a": {"capability_version_id": "v:a"},
        "b": {"capability_version_id": "v:b"},
        "legacy": {},
    }
    monkeypatch.setattr(caps, "load", lambda path, create: rows)
    calls = []

    def reconcile(name, **kwargs):
        calls.append((name, kwargs))
        if name == "a":
            raise OSError("temporary proof unavailable")
        return {"capability_id": name, "status": "shadow"}

    monkeypatch.setattr(caps, "reconcile_causal_lifecycle", reconcile)
    path = tmp_path / "ledger.json"
    result = caps.reconcile_all(path, now=90)
    assert calls == [("a", {"path": path, "timestamp": 90}), ("b", {"path": path, "timestamp": 90})]
    assert result == {
        "reconciled": [{"capability_id": "b", "status": "shadow"}],
        "errors": [{"capability_id": "a", "error": "temporary proof unavailable"}],
        "valid": False,
    }


@pytest.mark.parametrize(
    "command,arguments,function,result,expected_rc",
    [
        ("summary", [], "summary", {"count": 2}, 0),
        ("inventory", [], "summary", {"count": 2}, 0),
        ("usage", [], "summary", {"count": 2}, 0),
        ("sweep", [], "sweep", ["old"], 0),
        ("reconcile", [], "reconcile_all", {"valid": False}, 1),
        ("seed-declared", [], "seed_declared", {"missing": ["one"]}, 1),
        ("register", ["--name", "one", "--record-json", "RECORD"], "register", None, 0),
        (
            "transition",
            ["--name", "one", "--to", "retired", "--reason", "decision", "--evidence-ref", "proof"],
            "transition",
            None,
            0,
        ),
        (
            "renew",
            ["--name", "one", "--reason", "proof", "--evidence-ref", "receipt"],
            "renew",
            {"renewed": True},
            0,
        ),
        (
            "probe",
            [
                "--name",
                "one",
                "--probe",
                "producer_probe",
                "--failed",
                "--ref",
                "proof",
                "--detail",
                "failed",
            ],
            "record_probe",
            None,
            0,
        ),
        (
            "heartbeat",
            ["--name", "one", "--type", "invocation", "--ref", "proof"],
            "heartbeat",
            None,
            0,
        ),
    ],
)
def test_cli_dispatch_preserves_arguments_and_failure_status(
    tmp_path, monkeypatch, capsys, command, arguments, function, result, expected_rc
):
    path = tmp_path / "record.json"
    path.write_text('{"status":"observed"}')
    args = [str(path) if x == "RECORD" else x for x in arguments]
    calls = []

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(caps, function, operation)
    monkeypatch.setattr(caps, "usage_report", lambda report: {"usage": report["count"]})
    assert caps.main(["--json", command, *args]) == expected_rc
    assert len(calls) == 1
    call_args, kwargs = calls[0]
    if command == "register":
        assert call_args == ("one", {"status": "observed"}) and kwargs == {}
    elif command == "transition":
        assert call_args == ("one", "retired") and kwargs == {
            "reason": "decision",
            "evidence_refs": ["proof"],
        }
    elif command == "renew":
        assert call_args == ("one",) and kwargs == {"reason": "proof", "evidence_refs": ["receipt"]}
    elif command == "probe":
        assert call_args == ("one", "producer_probe") and kwargs == {
            "passed": False,
            "ref": "proof",
            "detail": "failed",
        }
    elif command == "heartbeat":
        assert call_args == ("one", "invocation") and kwargs == {"ref": "proof"}
    else:
        assert call_args == () and kwargs == {}
    output = capsys.readouterr().out
    if command in {"summary", "inventory", "usage", "sweep", "reconcile", "seed-declared", "renew"}:
        assert isinstance(json.loads(output), dict)


def test_cli_renew_refusal_is_structured_and_nonzero(monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise ValueError("evidence is missing")

    monkeypatch.setattr(caps, "renew", refuse)
    assert (
        caps.main(["renew", "--name", "one", "--reason", "reason", "--evidence-ref", "proof"]) == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "capability_id": "one",
        "renewed": False,
        "refused": "evidence is missing",
    }


def test_cli_without_command_shows_help_and_fails(capsys):
    assert caps.main([]) == 2
    assert "usage:" in capsys.readouterr().out


def _active_record():
    record = _record("active", "active")
    record.update(
        owner="test",
        matcher={"kind": "task_type", "equals": "test"},
        entrypoint="test.py",
        trigger_cadence="test",
        output_artifact="result",
        downstream_consumer="test",
        learning_sink="test",
        last_match=1,
        last_invocation=1,
        last_success=1,
        expiry=10**12,
        kill_switch="TEST=0",
        rollback={"transition": "retired"},
        outcome_links=["test:outcome"],
        activation_evidence={
            p: {"passed": True, "checked_at": 1, "ref": "test:proof"} for p in caps.ACTIVE_PROBES
        },
    )
    return record


@pytest.mark.parametrize(
    "fault,message",
    [
        ("outcome", "outcome links"),
        ("passed", "passing producer_probe"),
        ("checked_at", "checked_at"),
        ("ref", "evidence ref"),
        ("expiry", "expired"),
    ],
)
def test_active_validation_refuses_incomplete_or_expired_authority(fault, message):
    record = _active_record()
    caps.validate_capability(record, now=100)
    if fault == "outcome":
        record["outcome_links"] = []
    elif fault == "expiry":
        record["expiry"] = 99
    else:
        record["activation_evidence"]["producer_probe"][fault] = (
            False if fault == "passed" else None
        )
    with pytest.raises(AssertionError, match=message):
        caps.validate_capability(record, now=100)


def test_initial_load_migrates_features_without_claiming_activation(tmp_path, monkeypatch):
    features = tmp_path / "features.json"
    features.write_text(
        json.dumps(
            {
                "old-hardened": {"maturity": "hardened", "module": "old.py"},
                "old-observed": {"maturity": "new", "module": "new.py"},
            }
        )
    )
    monkeypatch.setattr(caps, "FEATURES_REG", features)
    monkeypatch.setattr(caps, "KNOWN_GATES", {})
    monkeypatch.setattr(caps, "declared_row_ids", lambda: set())
    path = tmp_path / "new-ledger.json"
    result = caps.load(path)
    assert result["old-hardened"]["status"] == "generated"
    assert result["old-observed"]["status"] == "observed"
    assert result["old-hardened"]["entrypoint"] == "old.py"
    for name in ["old-hardened", "old-observed"]:
        assert result[name]["event_history"][0]["activation_inferred"] is False
        assert result[name]["last_invocation"] is None
    assert set(json.loads(path.read_text())["capabilities"]) == set(result)


def test_refused_registry_operations_preserve_existing_bytes(tmp_path):
    path = _ledger(tmp_path, candidate=_record("candidate"))
    before = path.read_bytes()
    with pytest.raises(AssertionError, match="key/id mismatch"):
        caps.save({"wrong": _record("candidate")}, path)
    with pytest.raises(ValueError, match="already registered"):
        caps.register("candidate", {}, path=path)
    with pytest.raises(ValueError, match="unknown capability"):
        caps.transition("missing", "retired", reason="test", path=path)
    with pytest.raises(ValueError, match="invalid capability event type"):
        caps.heartbeat("candidate", "not-an-event", path=path)
    with pytest.raises(ValueError, match="heartbeat requires ref"):
        caps.heartbeat("candidate", "output", path=path)
    with pytest.raises(ValueError, match="unknown activation probe"):
        caps.record_probe("candidate", "not-a-probe", passed=True, ref="proof", path=path)
    with pytest.raises(ValueError, match="unknown capability"):
        caps.record_probe("missing", "producer_probe", passed=True, ref="proof", path=path)
    assert path.read_bytes() == before


def test_state_transition_refuses_illegal_move_and_noop_preserves_history():
    record = _record("candidate", "retired")
    before = copy.deepcopy(record)
    caps._transition_in_place(record, "retired", reason="noop", timestamp=1, evidence_ref=None)
    assert record == before
    with pytest.raises(ValueError, match="illegal capability transition"):
        caps._transition_in_place(record, "active", reason="bad", timestamp=2, evidence_ref=None)
    assert record == before


@pytest.mark.parametrize(
    "fault,message",
    [
        ("field", "missing required fields"),
        ("state", "invalid status"),
        ("schema", "unsupported schema_version"),
        ("criteria", "machine-checkable bounds"),
    ],
)
def test_ledger_validation_rejects_malformed_records(fault, message):
    record = _record("candidate")
    if fault == "field":
        record.pop("owner")
    elif fault == "state":
        record["status"] = "invented"
    elif fault == "schema":
        record["schema_version"] = -1
    else:
        record["gate_criteria"] = "approve someday"
    with pytest.raises(AssertionError, match=message):
        caps.validate_capability(record)


def test_ledger_audit_collects_invalid_record_without_hiding_valid_record(tmp_path, monkeypatch):
    bad = _record("bad")
    bad["status"] = "invented"
    monkeypatch.setattr(caps, "load", lambda path, create: {"bad": bad, "good": _record("good")})
    path = tmp_path / "audit.json"
    result = caps.validate_ledger(path, create=False)
    assert result == {
        "path": str(path),
        "count": 2,
        "errors": [{"capability_id": "bad", "error": "capability has invalid status: invented"}],
        "valid": False,
    }


def test_missing_lineage_and_existing_migration_are_refused(tmp_path):
    path = _ledger(tmp_path, candidate=_record("candidate"))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown capability"):
        caps.adopt_module_version("missing", path=path, root=tmp_path)
    with pytest.raises(ValueError, match="unknown capability"):
        caps.reconcile_causal_lifecycle("missing", path=path, timestamp=100)
    with pytest.raises(ValueError, match="immutable version lineage"):
        caps.reconcile_causal_lifecycle("candidate", path=path, timestamp=100)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        caps.migrate_features_to_capabilities(tmp_path / "absent.json", path, now=100)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "liveness,blocker",
    [
        ("retired", "retired"),
        ("wired_but_dry", "wired but nothing flows through it"),
        ("stale_active", "marked active but no recent success"),
        ("healthy", "none"),
    ],
)
def test_unblock_does_not_feed_retired_stale_or_healthy_records(liveness, blocker):
    result = caps.unblock(_record("candidate"), liveness=liveness, now=100)
    assert result["blocker"] == blocker
    assert result["feed"] is False
    assert result["retire_candidate"] is False


def test_evidence_debt_does_not_hide_excess_rework_behind_volume():
    record = _record("candidate")
    record["lifecycle_policy"] = {"max_rework": 0}
    record["causal_evidence"] = {
        "readiness": {"durable_subjects": ["one", "two", "three"], "rework": 1}
    }
    result = caps.evidence_debt(record)
    assert result["remaining"] == 0
    assert result["blocked_by"] == ["1 rework over the max of 0"]


def test_entrypoint_resolution_uses_only_existing_unique_source_files(tmp_path):
    source = tmp_path / "dispatcher.py"
    source.write_text("def offload(): pass\n")
    assert caps.resolve_entrypoint_sources(
        "/dispatcher.offload/dispatcher.py/missing.py/", root=tmp_path
    ) == [source]


def test_malformed_ledger_and_missing_sweep_are_explicit(tmp_path):
    with pytest.raises(ValueError, match="missing capabilities object"):
        caps._checked_ledger({"schema_version": caps.SCHEMA_VERSION, "capabilities": []})
    assert caps.sweep(tmp_path / "missing.json", now=100) == []


def test_failed_atomic_replace_cleans_temporary_file_and_preserves_ledger(tmp_path, monkeypatch):
    path = _ledger(tmp_path, candidate=_record("candidate"))
    before = path.read_bytes()
    files = set(tmp_path.iterdir())

    def fail_replace(*args, **kwargs):
        raise OSError("replacement refused")

    monkeypatch.setattr(caps.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement refused"):
        caps._write_ledger_unlocked(path, {"new": _record("new")})
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == files


def test_recorded_probe_detail_and_expiry_history_remain_bound(tmp_path):
    path = _ledger(tmp_path, candidate=_record("candidate"))
    result = caps.record_probe(
        "candidate",
        "producer_probe",
        passed=False,
        ref="proof:one",
        detail="failed control",
        path=path,
        timestamp=100,
    )
    assert result is None
    stored = json.loads(path.read_text())["capabilities"]["candidate"]
    assert stored["activation_evidence"]["producer_probe"] == {
        "passed": False,
        "checked_at": 100,
        "ref": "proof:one",
        "detail": "failed control",
    }
    event = {"type": "transition", "to": "retired", "cause": caps.EXPIRY_CAUSE}
    record = {"status": "retired", "event_history": [event, {"type": "heartbeat"}]}
    assert caps.expiry_retirement(record) == event
    assert caps._is_expiry_timeout({"to": "active", "cause": caps.EXPIRY_CAUSE}) is False


def test_readonly_summary_exposes_invalid_active_claim(tmp_path, monkeypatch):
    cap = _active_record()
    cap["capability_id"] = "candidate"
    cap["outcome_links"] = []
    monkeypatch.setattr(caps, "load_declared", lambda path: {"candidate": cap})
    report = caps.summary(tmp_path / "summary.json", create=False)
    assert report["active_without_edges"] == [
        {"capability_id": "candidate", "error": "active capability missing outcome links"}
    ]


def test_healthy_liveness_requires_real_match_invocation_and_outcome():
    cap = _record("candidate", "exercised")
    cap.update(last_match=1, last_invocation=1, outcome_links=["real:outcome"])
    assert caps.classify_liveness(cap, now=100) == "healthy"


@pytest.mark.parametrize("reason", ["decision", "drought"])
def test_unaccepted_usage_does_not_manufacture_outcome_linkage(reason):
    cap = _record("candidate")
    if reason == "decision":
        cap["notes"] = "REVISIT TRIGGER: approved source evidence arrives"
    else:
        cap.update(last_invocation=1, gate_criteria={"min_reuse": 3})
    result = caps.unblock(cap, liveness="invoked_without_outcomes", now=30 * 86400)
    assert result["feed"] is False
    assert result["needs_trigger"] is False
    assert (
        "deferred by an explicit decision" in result["blocker"]
        if reason == "decision"
        else "no ACCEPTED use yet" in result["blocker"]
    )
