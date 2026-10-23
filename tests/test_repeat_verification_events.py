"""Repeat outcomes are causal, exact-subject events, not newest run wins."""

import copy

import feedback
import verification_events as events
import verifier_evidence as verifier

HEAD, MERGE = "a" * 40, "b" * 40


def event(material="c", **changes):
    value = dict(
        schema=events.SCHEMA,
        repo="o/r",
        pr="7",
        head_sha=HEAD,
        evaluated_sha=MERGE,
        request_sha256="d" * 64,
        material_sha256=material * 64,
    )
    value.update(changes)
    value["event_id"] = events.event_id(value)
    return value


def decision(run=1, verdict="NON_PASS", **changes):
    result = dict(
        schema=verifier.MARKER,
        repo="o/r",
        pr="7",
        head_sha=HEAD,
        evaluated_sha=MERGE,
        run_id=str(run),
        run_attempt="1",
        verdict=verdict,
        provider_verdicts=["PASS" if verdict == "PASS" else "CONCERNS"],
        ci_failed=False,
    )
    result.update(changes)
    return result


def test_event_identity_excludes_transport_and_causal_metadata_but_binds_material():
    current = event()
    assert events.valid_event(
        {**current, "observed_at": "tomorrow", "predecessor_event_id": "f" * 64},
        "o/r",
        7,
        HEAD,
        MERGE,
    )
    assert event("e")["event_id"] != current["event_id"]
    assert not events.valid_event({**current, "material_sha256": "e" * 64}, "o/r", 7, HEAD, MERGE)
    assert not events.valid_event(current, "o/other", 7, HEAD, MERGE)


def test_nonpass_new_required_evidence_same_sha_can_pass_without_rewriting_original():
    original = decision()
    repaired = decision(
        2, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(original)
    )
    assert verifier.applicable_decision([repaired, original]) is repaired
    assert original["verdict"] == "NON_PASS"
    assert verifier.decision_matches_identity(repaired, "o/r", 7, HEAD, MERGE)
    assert not verifier.decision_matches_identity(repaired, "o/r", 8, HEAD, MERGE)


def test_pending_and_failed_retry_do_not_reuse_prior_pass():
    original = decision(1, "PASS")
    state = decision(2, verification_event=event(), predecessor_event_id=events.legacy_id(original))
    state.pop("verdict")
    state.update(schema=verifier.STATE_MARKER, status="pending")
    assert verifier.applicable_decision([original], [state]) is None
    retry = decision(
        3, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(original)
    )
    assert verifier.applicable_decision([original, retry], [state]) is retry


def test_concurrent_successors_and_orphans_are_unknown_even_if_one_passes():
    original = decision()
    one = decision(
        2, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(original)
    )
    two = decision(
        99, verification_event=event("e"), predecessor_event_id=events.legacy_id(original)
    )
    assert verifier.applicable_decision([original, one, two]) is None
    assert verifier.applicable_decision([one]) is None
    assert verifier.applicable_decision([original, one, copy.deepcopy(one)]) is one


def test_history_ingestion_retains_original_and_does_not_regress_out_of_order(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("r", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("r", merged=True, durability="durable")
    old = decision()
    new = decision(
        2, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(old)
    )
    assert verifier.record_decision("r", "o/r", 7, old)
    assert verifier.record_decision("r", "o/r", 7, new)
    assert not verifier.record_decision("r", "o/r", 7, old)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict,durability FROM outcomes WHERE run_id='r'"
        ).fetchone() == ("PASS", "durable")
        assert conn.execute(
            "SELECT COUNT(*) FROM completion_events WHERE producer='verifier_evidence'"
        ).fetchone() == (2,)
    assert not verifier.record_decision("r", "o/other", 7, new)


def test_shared_collector_fixture_has_identical_python_hashes():
    import json
    from pathlib import Path

    sample = json.loads(
        (Path(__file__).parent / "fixtures/verification_event/canonical.json").read_text()
    )
    actual = sample["verification_event"]
    assert events.content_hash(sample["request"]) == actual["request_sha256"]
    assert events.content_hash(sample["material"]) == actual["material_sha256"]
    assert events.valid_event(actual, "stranske/example", 7, HEAD, MERGE)


def test_pending_projection_clears_old_pass_and_terminal_restores_it(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("r", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("r", merged=True)
    old = decision(1, "PASS")
    new = decision(
        3, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(old)
    )
    pending = {
        k: v for k, v in new.items() if k not in {"verdict", "ci_failed", "provider_verdicts"}
    }
    pending.update(schema=verifier.STATE_MARKER, status="pending", run_id="2")
    assert verifier.record_decision("r", "o/r", 7, old)
    assert not verifier.record_decision("r", "o/r", 7, pending)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
        ).fetchone() == (None,)
    assert verifier.record_decision("r", "o/r", 7, new)
    assert not verifier.record_decision("r", "o/r", 7, pending)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
        ).fetchone() == ("PASS",)


def test_corrupt_authenticated_current_event_does_not_resurrect_old_pass():
    import json

    old = decision(1, "PASS")
    new = decision(
        2, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(old)
    )
    new["verification_event"]["material_sha256"] = "f" * 64
    pr = dict(
        number=7,
        headRefOid=HEAD,
        mergeCommit={"oid": MERGE},
        comments={
            "nodes": [
                dict(
                    author={"login": "github-actions[bot]"},
                    url="https://github.com/o/r/pull/7#issuecomment-2",
                    body="\n".join(
                        f"<!-- {verifier.MARKER} {json.dumps(d)} -->" for d in (old, new)
                    ),
                )
            ]
        },
    )
    assert verifier.decision_from_pr("o/r", pr) is None


def test_upgrade_imports_authenticated_remote_ancestry_not_scalar_guess(monkeypatch, tmp_path):
    import json

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("r", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("r", merged=True, verifier_verdict="NON_PASS")
    old = decision()
    terminal = decision(
        3, "PASS", verification_event=event(), predecessor_event_id=events.legacy_id(old)
    )
    state = {
        key: value
        for key, value in terminal.items()
        if key not in {"verdict", "ci_failed", "provider_verdicts"}
    }
    state.update(schema=verifier.STATE_MARKER, status="pending", run_id="2")

    def parse(records):
        pr = dict(
            number=7,
            headRefOid=HEAD,
            mergeCommit={"oid": MERGE},
            comments={
                "nodes": [
                    dict(
                        author={"login": "github-actions[bot]"},
                        url=f"https://github.com/o/r/pull/7#issuecomment-{i+1}",
                        body=f"<!-- {row['schema']} {json.dumps(row)} -->",
                    )
                    for i, row in enumerate(records)
                ]
            },
        )
        return verifier.decision_from_pr("o/r", pr, include_pending=True)

    pending = parse([old, state])
    assert pending and pending["status"] == "pending"
    verifier.record_decision("r", "o/r", 7, pending)
    completed = parse([old, state, terminal])
    assert completed and completed["verdict"] == "PASS"
    assert verifier.record_decision("r", "o/r", 7, completed)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
        ).fetchone() == ("PASS",)
        assert conn.execute(
            "SELECT COUNT(*) FROM completion_events WHERE producer='verifier_evidence'"
        ).fetchone() == (3,)


def test_authenticated_ancestry_import_is_atomic_before_current_projection(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("r", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("r", merged=True, verifier_verdict="NON_PASS")
    old = decision(verdict="PASS")
    pending = {
        key: value
        for key, value in decision(
            2, verification_event=event(), predecessor_event_id=events.legacy_id(old)
        ).items()
        if key not in {"verdict", "ci_failed", "provider_verdicts"}
    }
    pending.update(schema=verifier.STATE_MARKER, status="pending", _lifecycle_history=[old])
    original = feedback._record_completion_event_in_conn
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(kwargs["payload"])
        if len(calls) == 2:
            raise RuntimeError("interrupted ancestry import")
        return original(*args, **kwargs)

    monkeypatch.setattr(feedback, "_record_completion_event_in_conn", fail_second)
    with pytest.raises(RuntimeError, match="interrupted"):
        verifier.record_decision("r", "o/r", 7, pending)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
        ).fetchone() == ("NON_PASS",)
        assert conn.execute(
            "SELECT COUNT(*) FROM completion_events WHERE producer='verifier_evidence'"
        ).fetchone() == (0,)
    monkeypatch.setattr(feedback, "_record_completion_event_in_conn", original)
    verifier.record_decision("r", "o/r", 7, pending)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
        ).fetchone() == (None,)
