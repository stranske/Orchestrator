"""Accepted repeat dispatch remains a truthful wait through the real staged queue."""

import copy
import json
import time
from types import SimpleNamespace

from test_native_runtime_repeat_composition import (
    TARGET,
    run,
)
from test_native_runtime_repeat_composition import (
    capacity_host as capacity_host,
)
from test_native_runtime_repeat_composition import (
    host as host,
)
from test_native_runtime_repeat_composition import (
    native_world as native_world,
)
from test_native_runtime_repeat_composition import (
    public_merge_world as public_merge_world,
)
from test_native_runtime_repeat_composition import (
    repeat as repeat,
)
from test_native_runtime_repeat_composition import (
    runtime_request as runtime_request,
)
from test_repeat_verification_events import event

import closer_dispatcher as dispatcher
import closer_pipeline as pipeline
import closer_queue as queue
import verifier_evidence


def test_acknowledged_repeat_dispatch_staged_restart_is_stop_valid(repeat):
    assert dispatcher._pipeline_capacity()["low"] == 0
    assert not repeat.world.runtime_workspace.exists()
    raw = repeat.world.root / "repeat-staged.json"
    staged = dispatcher._pipeline_state_path(raw)
    inner = staged.parent / f"{staged.stem}.actions" / f"{pipeline._digest(TARGET)}.json"
    repeat.backend.state_path = inner
    accepted = run(repeat)
    assert repeat.dispatches == 1, accepted
    operation = queue._read_state(inner)["items"][TARGET]["operation"]
    assert operation["status"] == "completed"
    assert operation["result"]["verification_post_accepted"]["mutation_kind"] == "workflow_dispatch"
    acknowledgment = operation["result"]["verification_post_accepted"]
    assert acknowledgment["correlation_input"] == operation["result"]["verification_request_basis"]
    assert (
        repeat.dispatch_fields[0]["inputs[verification_request_basis]"]
        == acknowledgment["correlation_input"]
    )
    accepted_core = copy.deepcopy(repeat.contents.rows)
    repeat.backend.state_path = raw
    args = SimpleNamespace(state=str(raw), shadow=False, max_workers=1)
    result = dispatcher._run_staged_pipeline(args, [TARGET], repeat.backend, time.time() + 330)
    assert pipeline.validate_stop(staged, assignment_targets=[TARGET])["valid"], result
    state = json.loads(staged.read_text())
    row = state["runs"][result["run_id"]]["targets"][TARGET]
    assert (
        row["results"]["interpret"]["classification"] == "deterministic_verifier_request_wait"
    ), row
    assert row["status"] == "pending" and row["stage"] == "act", row
    assert row["pending_kind"] == "external_event"
    assert row["results"]["act"]["queue_receipt"]["stop_valid"] is True
    assert row["results"]["act"]["pending_evidence"]["operation_id"] == operation["id"]
    assert repeat.dispatches == 1 and repeat.world.events["provider"] == 0

    # Repeated staged entry must consume the same accepted queue operation.
    restarted = dispatcher._run_staged_pipeline(args, [TARGET], repeat.backend, time.time() + 330)
    assert pipeline.validate_stop(staged, assignment_targets=[TARGET])["valid"], restarted
    restarted_row = json.loads(staged.read_text())["runs"][restarted["run_id"]]["targets"][TARGET]
    assert restarted_row["status"] == "pending"
    assert restarted_row["pending_kind"] == "external_event"
    assert queue._read_state(inner)["items"][TARGET]["operation"] == operation
    assert repeat.contents.rows == accepted_core
    assert repeat.dispatches == 1 and repeat.world.events["provider"] == 0

    # Only a fresh authenticated merge-bound result supersedes that wait.
    prior = copy.deepcopy(repeat.world.snapshot["verifier"])
    terminal = copy.deepcopy(prior)
    terminal.update(
        run_id="100",
        verdict="PASS",
        provider_verdicts=["PASS"],
        verification_event=event(
            material="e",
            repo="owner/repo",
            pr="12",
            head_sha=repeat.world.snapshot["head"],
            evaluated_sha=repeat.world.snapshot["merge"],
        ),
        predecessor_event_id=prior["verification_event"]["event_id"],
    )
    pr = {
        "number": 12,
        "headRefOid": repeat.world.snapshot["head"],
        "mergeCommit": {"oid": repeat.world.snapshot["merge"]},
        "comments": {
            "nodes": [
                {
                    "author": {"login": "github-actions[bot]"},
                    "url": "https://github.com/owner/repo/pull/12#issuecomment-" + value["run_id"],
                    "body": "<!-- verifier-corpus-decision/v1 " + json.dumps(value) + " -->",
                }
                for value in (prior, terminal)
            ]
        },
    }
    parsed = verifier_evidence.decision_from_pr("owner/repo", pr)
    assert parsed["verdict"] == "PASS" and parsed["run_id"] == "100"
    repeat.world.snapshot.update(
        verifier=parsed, verifier_runs=[], revision="authenticated-repeat-terminal"
    )
    assert (
        repeat.backend._accepted_verification_request(repeat.world.snapshot, state_path=inner)
        is None
    )
    expected = repeat.backend.select(repeat.world.snapshot)[0]
    assert expected == "collect_evidence"
    resumed = dispatcher._run_staged_pipeline(args, [TARGET], repeat.backend, time.time() + 330)
    row = json.loads(staged.read_text())["runs"][resumed["run_id"]]["targets"][TARGET]
    assert row["results"]["prepare"]["proposal"]["action"] == expected, row
    assert (
        row["results"]["prepare"]["proposal"]["reason"]
        != "same verification request already accepted"
    )
    assert repeat.dispatches == 1 and repeat.world.events["provider"] == 0
