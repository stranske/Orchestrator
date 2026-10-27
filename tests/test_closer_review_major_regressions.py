"""Independent bounded local causal controls; no service/provider/native actions.

Campaign control isolates actual same-process flock reuse while stubbing only
post-lock descendant content validation. Queue control uses real finite rounds
and the existing synthetic CI observation fixture. Neither grants authority.
"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_closer_ci_followup import TARGET, world

import closer_frontier_policy as policy
import closer_queue as queue


def test_campaign_completed_descendant_never_reopens_held_campaign(tmp_path):
    state = tmp_path.resolve() / "campaign.json"
    script = r"""
import inspect,json,sys
from pathlib import Path
import closer_campaign as campaign
import closer_native_descendants as descendants
state=Path(sys.argv[1]).resolve()
doc={'schema_version':1,'fixture':'independent local only'}
state.write_text(json.dumps(doc))
def observed(cfg,ref,*,require_finalized,_history_budget):
    assert require_finalized is True
    assert _history_budget.campaign_doc is doc
    assert _history_budget.campaign_path == state
    return []
descendants.observed_leaves=observed
campaign._complete_rows_from_leaves=lambda *a: {'observed':'local-only'}
store=campaign.Campaign(state)
stream=store._locked()
try:
    fn=campaign._completed_cohort_rows
    args=[{}, {'native_worker':{'state':str(state)}}, {'descendant_bundle':{'fixture':True}}]
    if len(inspect.signature(fn).parameters)==4:args.append(doc)
    assert fn(*args)=={'observed':'local-only'}
    print('reused-original-doc-with-held-flock',flush=True)
finally:stream.close()
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(state)],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("same-process held campaign flock was reopened and blocked") from None
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reused-original-doc-with-held-flock"


def test_unrelated_completed_real_round_preserves_original_ci_no_effect_proof(
    tmp_path, monkeypatch
):
    w = world(tmp_path, monkeypatch)
    path = w.retained["action_path"]
    args = dict(frontier=w.frontier, receipt=w.retained["receipt"], evidence=w.retained["evidence"])
    original = queue.ci_wait_no_effect_proof(path, TARGET, **args)
    assert original["confirmed_no_effect"] is True
    target = "offline/other#22"
    revision = "other-r1"
    binding = {}
    frontier = policy.make_frontier(
        [target],
        owner="independent-local",
        frontier_id="unrelated-completed",
        bindings={
            target: {
                "revision": revision,
                "action_id": policy.action_identity(target, revision, "review", binding),
            }
        },
    )
    queue.run_round(
        path,
        [target],
        lambda *_: {
            "revision": revision,
            "eligible": True,
            "action": "review",
            "operation_binding": binding,
        },
        lambda *_: {"status": "completed", "advanced": True},
        owner="independent-local",
        deadline=None,
        finite_frontier=frontier,
        frontier_target=target,
        request_timeout_seconds=10,
        receipt_reserve_seconds=0,
        max_operation_retries=0,
        max_passes=1,
        claim_fn=lambda *_: True,
        release_fn=lambda *_: True,
        holder_fn=lambda *_: None,
    )
    document = queue._read_state(path)
    other = document["frontiers"][frontier["frontier_id"]]["items"][target]
    assert other["operation"]["status"] == "completed", other
    before = path.read_bytes()
    proof = queue.ci_wait_no_effect_proof(path, TARGET, **args)
    assert proof["confirmed_no_effect"] is True and proof["authority"] is False
    assert path.read_bytes() == before

    # Unrelated work may be completed, but live or explicitly uncertain work
    # remains a global barrier. Each read must preserve the original bytes.
    for status in ["submitted", "running", "uncertain", "unknown"]:
        changed = copy.deepcopy(document)
        changed["frontiers"][frontier["frontier_id"]]["items"][target]["operation"][
            "status"
        ] = status
        path.write_text(json.dumps(changed))
        blocked_bytes = path.read_bytes()
        with pytest.raises(queue.QueueStateError, match="unresolved outcome"):
            queue.ci_wait_no_effect_proof(path, TARGET, **args)
        assert path.read_bytes() == blocked_bytes
    for key in ["provider_outcome_uncertain", "recovery_barrier", "outcome_uncertain"]:
        changed = copy.deepcopy(document)
        changed["frontiers"][frontier["frontier_id"]]["items"][target][key] = True
        path.write_text(json.dumps(changed))
        blocked_bytes = path.read_bytes()
        with pytest.raises(queue.QueueStateError, match="uncertain outcome"):
            queue.ci_wait_no_effect_proof(path, TARGET, **args)
        assert path.read_bytes() == blocked_bytes
