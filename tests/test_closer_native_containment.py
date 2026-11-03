"""Offline containment of real named unknown outcomes, never acceptance authority."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import test_closer_bounded_native_hooks as native_fixture
import test_closer_quarantine_integration as fixture

import closer_pipeline as pipeline
import closer_resources as resources
import closer_runtime_binding as binding

UNKNOWN = "owner/unknown#7"
HEALTHY = "owner/healthy#8"


def mixed_round(tmp_path):
    raw, state, held = fixture.original_state(tmp_path)
    retained_ad = fixture.advertisement(state, "branch:owner/held@original-unknown")
    protected = {p: p.read_bytes() for p in (raw, state, retained_ad)}
    events = Counter()
    callbacks = fixture.callbacks(events)

    def act(name, _context):
        events[(name, "simulated-attempt")] += 1
        if name == UNKNOWN:
            raise RuntimeError("simulated effect transport lost its completion receipt")
        return {"status": "completed", "target_complete": True}

    callbacks["act"] = act
    candidate = fixture.run(
        state, [UNKNOWN, HEALTHY, held[0]], callbacks, lambda t, _c: fixture.inventory(t)
    )
    assert candidate["targets"][UNKNOWN]["status"] == "uncertain", candidate
    assert candidate["targets"][HEALTHY]["status"] == "completed", candidate
    shard = resources.shard_dir(resources.scoped_root(state), UNKNOWN) / "state.json"
    doc = json.loads(shard.read_bytes())
    row = doc["runs"][candidate["run_ids"][UNKNOWN]]["targets"][UNKNOWN]
    assert row == candidate["targets"][UNKNOWN]
    assert not pipeline._validate_stop_legacy(state, held)["valid"]
    assert {p: p.read_bytes() for p in protected} == protected
    return raw, state, held, protected, events, candidate, shard, copy.deepcopy(row)


def config(tmp_path, raw, candidate):
    assignment = tmp_path / "assignment.json"
    fixture.write(assignment, {"targets": [{"target": t} for t in candidate["assignment"]]})
    return {
        "mechanism": "native-handoff/v1",
        "dispatch_state": str(raw),
        "assignment": str(assignment),
        "handoff_dir": str(tmp_path / "handoff"),
        "native_hooks": {
            "postrun_dependencies": {
                "assignment": {
                    "path": str(assignment),
                    "sha256": hashlib.sha256(assignment.read_bytes()).hexdigest(),
                }
            }
        },
    }


def test_actual_new_unknown_is_contained_and_only_disjoint_work_reenters(tmp_path):
    raw, state, held, protected, events, candidate, shard, original_row = mixed_round(tmp_path)
    verdict = resources.validate_stop(state, candidate["round_id"], candidate["assignment"])
    assert verdict["global_stop_valid"] is False
    assert verdict["admitted_domain_valid"] is False
    assert verdict["scoped_containment_valid"] is True
    matching = [r for r in verdict["quarantined_admitted_items"] if r["target"] == UNKNOWN]
    assert len(matching) == 1
    item = matching[0]
    assert item["source_path"] == str(shard)
    assert item["run_id"] == candidate["run_ids"][UNKNOWN]
    assert item["item_sha256"] == pipeline._digest(original_row)
    assert set(fixture.inventory(UNKNOWN)) <= set(item["keys"])
    cfg = config(tmp_path, raw, candidate)
    proof = binding.scoped_continuation_proof(cfg, candidate)
    assert proof is not None
    assert proof["domain_outcome"] == "quarantined"
    assert proof["accepted"] is False and proof["global_stop_valid"] is False
    assert binding.scoped_continuation_proof(cfg, proof) == proof
    unknown_ads = resources.unknown_reservations(resources.scoped_root(state))
    assert unknown_ads["pr:" + UNKNOWN] == UNKNOWN
    for cadence in range(2):
        other = f"owner/disjoint#{cadence + 10}"
        resumed = fixture.run(
            state, [UNKNOWN, other], fixture.callbacks(events), lambda t, _c: fixture.inventory(t)
        )
        assert resumed["targets"][UNKNOWN]["status"] == "deferred"
        assert resumed["targets"][UNKNOWN]["scope_proof"]
        assert resumed["targets"][other]["status"] == "completed"
        assert events[(UNKNOWN, "prepare")] == events[(UNKNOWN, "interpret")] == 1
        assert events[(UNKNOWN, "simulated-attempt")] == 1
        assert events[(UNKNOWN, "simulated-effect")] == 0
        current = json.loads(shard.read_bytes())["runs"][candidate["run_ids"][UNKNOWN]]["targets"][
            UNKNOWN
        ]
        assert current == original_row
        assert resources.unknown_reservations(resources.scoped_root(state)) == unknown_ads
        assert {p: p.read_bytes() for p in protected} == protected
        assert not pipeline._validate_stop_legacy(state, held)["valid"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing-row",
        "changed-row",
        "unbounded",
        "initial-refusal",
        "run-id",
        "receipt",
        "assignment",
        "lane",
    ],
)
def test_containment_never_admits_missing_unbound_or_changed_authority(tmp_path, damage):
    raw, state, _held, protected, _events, candidate, shard, _row = mixed_round(tmp_path)
    cfg = config(tmp_path, raw, candidate)
    if damage in {"missing-row", "changed-row"}:
        doc = json.loads(shard.read_bytes())
        rows = doc["runs"][candidate["run_ids"][UNKNOWN]]["targets"]
        if damage == "missing-row":
            del rows[UNKNOWN]
        else:
            rows[UNKNOWN]["error"] = "changed after sealed aggregate receipt"
        fixture.write(shard, doc)
    elif damage == "unbounded":
        # An unrelated unanchored identity cannot be reduced to a repository fence.
        doc = json.loads(state.read_bytes())
        doc["carry"]["unbounded-identity"] = {"status": "uncertain", "stage": "act", "results": {}}
        fixture.write(state, doc)
    elif damage == "initial-refusal":
        held_target = next(t for t in candidate["assignment"] if t not in candidate["run_ids"])
        Path(candidate["targets"][held_target]["scope_proof"]["path"]).unlink()
    elif damage == "run-id":
        recorded = json.loads(Path(candidate["receipt_path"]).read_bytes())
        recorded["run_ids"][UNKNOWN] = candidate["run_ids"][HEALTHY]
        fixture.write(Path(candidate["receipt_path"]), recorded)
        candidate["run_ids"] = recorded["run_ids"]
        candidate["receipt_sha256"] = hashlib.sha256(
            Path(candidate["receipt_path"]).read_bytes()
        ).hexdigest()
    elif damage == "receipt":
        with Path(candidate["receipt_path"]).open("ab") as stream:
            stream.write(b" ")
    elif damage == "assignment":
        Path(cfg["assignment"]).write_text("{}")
    else:
        (Path(cfg["handoff_dir"]) / ".lane-closer.lock").mkdir(parents=True)
    verdict = resources.validate_stop(state, candidate["round_id"], candidate["assignment"])
    if damage not in {"receipt", "assignment", "lane"}:
        assert verdict["scoped_containment_valid"] is False
    assert binding.scoped_continuation_proof(cfg, candidate) is None
    for path, before in protected.items():
        if not (damage == "unbounded" and path == state):
            assert path.read_bytes() == before


@pytest.mark.parametrize("failure", [None, "b-unknown", "c-other", "c-denied"])
def test_native_containment_requires_terminal_b_and_exact_normal_c_refusal(
    tmp_path, monkeypatch, failure
):
    raw, _state, _held, protected, _events, candidate, _shard, _row = mixed_round(
        tmp_path / "queue"
    )
    host = tmp_path / "host"
    host.mkdir()
    cfg, scripts, source, snapshot, lane, events, original = native_fixture.hooks.__wrapped__(
        host, monkeypatch
    )
    assignment_bytes = json.dumps(
        {"targets": [{"target": t} for t in candidate["assignment"]]}
    ).encode()
    source.write_bytes(assignment_bytes)
    snapshot = raw.parent / "native-assignment-snapshot.json"
    snapshot.write_bytes(assignment_bytes)
    scripts["postrun_dependencies"]["assignment"] = {
        "path": str(snapshot),
        "sha256": binding._hash(snapshot),
    }
    cfg.update(mechanism="native-handoff/v1", dispatch_state=str(raw), assignment=str(snapshot))
    interpreter = scripts["postrun_dependencies"]["interpreter"]["path"]

    def runner(argv, **kwargs):
        if argv[0] == interpreter:
            if failure == "b-unknown":
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(
                argv, 78, stdout=json.dumps(candidate).encode(), stderr=b""
            )
        if argv[0] == scripts["postrun"]["path"] and failure == "c-denied":
            events.append(argv[0])
            raise PermissionError("simulated owned cleanup denied")
        result = original(argv, **kwargs)
        if argv[0] == scripts["postrun"]["path"]:
            result.returncode = 65
            result.stderr = (
                b"unrelated stop failure"
                if failure == "c-other"
                else b"handoff-postrun: closer stop rejected; preserve ledger and resume actionable work"
            )
        return result

    outcome, detail = binding.run_native_round(
        [interpreter, "offline-dispatcher.py", "--assignment", str(snapshot)], cfg, runner=runner
    )
    assert events.count(scripts["postrun"]["path"]) == 1, (outcome, detail)
    assert lane.exists() is (failure == "c-denied")
    if failure == "c-denied":
        assert "PermissionError" in detail
    if failure is None:
        assert outcome == "scoped_continuation", detail
        proof = json.loads(detail)["proof"]
        assert proof["domain_outcome"] == "quarantined" and proof["accepted"] is False
    else:
        assert outcome != "scoped_continuation"
    assert {p: p.read_bytes() for p in protected} == protected
