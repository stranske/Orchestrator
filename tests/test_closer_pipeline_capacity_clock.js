'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const root = path.resolve(__dirname, '..');

// Exercise the real coordinator with only Python's standard library. Keep the
// short budget from the reported fixture, but model scheduling delay by moving
// wall time across the deadline instead of sleeping or depending on host load.
const scenario = `
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from closer_pipeline import run_pipeline, validate_stop

mode = sys.argv[1]
logical_now = [1_800_000_000.0]
wall_now = [logical_now[0] - 60]
capacity = {"io": 0, "low": 0, "exception": 0}
actions = []
admission_calls = []

def logical_clock():
    return logical_now[0]

def wall_clock():
    return wall_now[0]

def prepare(target, context):
    admission_calls.append("prepare")
    return {
        "revision": "rev-1",
        "identity": target,
        "semantic_cache_key": "closer-evidence-semantic/v1:"
        + hashlib.sha256(target.encode()).hexdigest(),
    }

def act(target, context):
    actions.append(target)
    return {"status": "completed", "target_complete": True}

def sample_capacity():
    admission_calls.append("capacity")
    return dict(capacity)

with tempfile.TemporaryDirectory(prefix="capacity-clock-") as directory:
    state = Path(directory) / "state.json"

    def run():
        deadline_clock = wall_clock if mode in {"wall-deadline", "wall-both"} else logical_clock
        admission_clock = wall_clock if mode in {"wall-admission", "wall-both"} else logical_clock
        deadline = deadline_clock() + 0.06
        if mode in {"logical-at-deadline", "logical-past-deadline"}:
            # Wall time still permits admission; the injected clock must govern
            # expiry as well as the capacity fixture's successful admission.
            logical_now[0] = deadline + (1 if mode == "logical-past-deadline" else 0)
        else:
            wall_now[0] = logical_now[0] + 60
        return run_pipeline(
            state,
            ["a"],
            prepare=prepare,
            interpret=lambda target, context: {"substantive_needs": []},
            exception=lambda target, context: {"reviewed": True},
            act=act,
            capacity_fn=sample_capacity,
            deadline=deadline,
            now=admission_clock,
        )

    first = run()
    if mode in {"logical-at-deadline", "logical-past-deadline"}:
        print(json.dumps({
            "receipt": first,
            "admission_calls": admission_calls,
            "actions": actions,
            "state_exists": state.exists(),
            "lock_exists": state.with_name(state.name + ".runlock").exists(),
            "wall_now": wall_now[0],
        }))
        sys.exit(0)
    # Emit admission diagnostics before the original lookup so negative controls
    # must reproduce this KeyError, rather than any unrelated subprocess failure.
    if "a" not in first["targets"]:
        print(json.dumps(first), flush=True)
    deferred = first["targets"]["a"]
    first_stop = validate_stop(state, ["a"], now=logical_clock)
    first_actions = list(actions)
    logical_now[0] += 30
    wall_now[0] = logical_now[0] - 60
    capacity.update(io=1, low=1, exception=1)
    resumed = run()
    print(json.dumps({
        "deferred": deferred,
        "first_stop": first_stop["valid"],
        "first_actions": first_actions,
        "resumed_status": resumed["targets"]["a"]["status"],
        "actions": actions,
        "resumed_stop": validate_stop(state, ["a"], now=logical_clock)["valid"],
    }))
`;

function runScenario(mode) {
  return spawnSync(process.env.PYTHON || 'python3', ['-c', scenario, mode], {
    cwd: root,
    env: {
      PATH: process.env.PATH,
      PYTHONPATH: [path.join(root, 'src'), root].join(path.delimiter),
      PYTHONDONTWRITEBYTECODE: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf8',
    timeout: 15000,
  });
}

function requireCapacityRecovery(result) {
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const observed = JSON.parse(result.stdout);
  assert.deepEqual(observed.first_actions, []);
  assert.equal(observed.deferred.status, 'deferred');
  assert.equal(observed.deferred.deferred_reason, 'worker_capacity');
  assert.equal(observed.deferred.deferred_next_trigger, 'capacity_recheck');
  assert.deepEqual(observed.deferred.resource_evidence.capacity, {
    lane: 'io', active: 0, limit: 0, available: 0, required: 1,
    sampled_at: 1800000000,
  });
  assert.equal(observed.first_stop, true);
  assert.equal(observed.resumed_status, 'completed');
  assert.deepEqual(observed.actions, ['a']);
  assert.equal(observed.resumed_stop, true);
}

test('capacity defers and resumes with a logical clock across admission delay', () => {
  requireCapacityRecovery(runScenario('logical'));
});

for (const mode of ['logical-at-deadline', 'logical-past-deadline']) {
  test(`${mode} refuses admission before callbacks or state creation`, () => {
    const result = runScenario(mode);
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr || result.stdout);
    const observed = JSON.parse(result.stdout);
    assert.equal(observed.receipt.reason, 'admission_deadline_expired');
    assert.equal(observed.receipt.authority, false);
    assert.deepEqual(observed.receipt.targets, {});
    assert.ok(observed.wall_now < observed.receipt.deadline);
    assert.ok(observed.receipt.now >= observed.receipt.deadline);
    assert.deepEqual(observed.admission_calls, []);
    assert.deepEqual(observed.actions, []);
    assert.equal(observed.state_exists, false);
    assert.equal(observed.lock_exists, false);
  });
}

for (const mode of ['wall-deadline', 'wall-admission', 'wall-both']) {
  test(`${mode} reproduces the original missing-target failure and restoring clocks passes`, () => {
    const broken = runScenario(mode);
    assert.ifError(broken.error);
    assert.equal(broken.status, 1);
    assert.match(broken.stderr, /KeyError: 'a'/);
    const receipt = JSON.parse(broken.stdout);
    assert.equal(receipt.reason, 'admission_deadline_expired');
    assert.deepEqual(receipt.targets, {});
    assert.throws(() => requireCapacityRecovery(broken));
    requireCapacityRecovery(runScenario('logical'));
  });
}
