'use strict';

// Fixture evidence only. This exercises the production closer hook and role
// recorder; it is not an observation of a real contested closer disposition.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const exercise = `
import json
import sqlite3
import sys
import time
from unittest.mock import patch
import feedback
import merge_guard
import roles
import router

scenario = json.loads(sys.argv[1])
target = "fixture/repo#41"
head = "a" * 40
with feedback._conn() as conn:
    conn.execute("INSERT INTO runs(run_id,ts,target) VALUES ('original',?,?)",
                 (int(time.time()), target))
    conn.execute("INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                 "VALUES ('original','NON_PASS','PASS',0)")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER prohibit_outcome_{operation} BEFORE {operation} ON outcomes "
                     "BEGIN SELECT RAISE(ABORT, 'outcome writes prohibited'); END")
    # Check the prohibition, including no-op writes invisible to a snapshot.
    for statement in (
        "INSERT INTO outcomes(run_id) VALUES ('probe')",
        "UPDATE outcomes SET merged=merged WHERE run_id='original'",
        "DELETE FROM outcomes WHERE run_id='original'",
    ):
        try:
            conn.execute(statement)
        except sqlite3.IntegrityError as exc:
            assert str(exc) == "outcome writes prohibited"
        else:
            raise AssertionError("ineffective outcome prohibition")

def snapshot():
    with feedback._conn() as conn:
        return conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()

before = snapshot()
with feedback._conn() as conn:
    original = conn.execute("SELECT * FROM runs WHERE run_id='original'").fetchone()
preflights = []

def preflight(pr, *, expected_head):
    assert (pr, expected_head) == (target, head)
    preflights.append(expected_head)
    # Allow the advisory checkpoint, then refuse before any merge command.
    # This proves the shadow result cannot waive the final deterministic gate.
    return {"blocked": len(preflights) > 1, "reason": "fixture final gate refusal"}

def forbidden(*args, **kwargs):
    raise AssertionError("merge or outcome publication was attempted")

def offload(backend, prompt, **kwargs):
    assert backend == "gemini"
    assert target in prompt and "runtime gate PASS disagrees with review panel BLOCKED" in prompt
    assert "fixture-runtime-evidence" in prompt and "persisted-review" in prompt
    if scenario.get("provider") == "raise":
        raise RuntimeError("fixture provider unavailable")
    proposal = {
        "decision": "uphold_blocker", "confidence": "low",
        "rationale": "The review evidence supports inspecting the blocker.",
        "evidence_assessment": [{"claim": "Review blocker", "status": "supported",
            "evidence_ref": "persisted-review", "reason": "Review finding is supplied"}],
        "ground_truth_refs": ["persisted-review"],
        "recommended_next_step": "Inspect the disputed source",
        "evidence_gaps": [],
    }
    return {"run_id": "fixture-provider", "exit": 0,
            "output": "invalid response" if scenario.get("provider") == "invalid"
                      else json.dumps(proposal)}

roles.reset_role_invocation_counts()
with (
    patch.object(router, "load_capacity", return_value={}),
    patch.object(roles, "route_role", return_value=None if scenario.get("no_capacity")
                 else {"agent": "gemini"}) as route,
    patch.object(roles.dispatcher, "offload", side_effect=offload) as provider,
):
    result = merge_guard.guarded_merge(
        target, expected_head=head, confirm_merge=not scenario.get("dry_run", False),
        env={"ORCH_ROLE_SHADOW": "0" if scenario.get("disabled") else "1"},
        metadata_fn=lambda pr: {"target": pr, "state": "OPEN", "is_draft": False,
                               "labels": [], "closing_issues": [], "title": "Fixture"},
        gate_fn=lambda *a, **kw: {"status": "executed", "verdict": "PASS", "blocks": False,
                                  "evidence_ref": "fixture-runtime-evidence"},
        panel_fn=lambda *a, **kw: {"status": "reused", "verdict": scenario["panel"]},
        preflight_fn=preflight,
        review_loop_fn=lambda *a, **kw: {"required": False, "eligible": True},
        merge_fn=forbidden, record_outcome_fn=forbidden, spawn_fn=forbidden,
    )

with feedback._conn() as conn:
    records = [dict(zip(("run_id", "target", "agent", "metadata"), row))
               for row in conn.execute("SELECT run_id,target,agent,decomposition FROM runs "
                                       "WHERE role_name='adjudicator' ORDER BY run_id")]
    events = [json.loads(row[0]) for row in conn.execute(
        "SELECT payload_json FROM completion_events WHERE producer='roles' ORDER BY event_id")]
    unchanged = conn.execute("SELECT * FROM runs WHERE run_id='original'").fetchone() == original
print(json.dumps({"result": result, "records": records, "events": events,
                  "routes": route.call_count, "calls": provider.call_count,
                  "before": before, "after": snapshot(), "original_run_unchanged": unchanged,
                  "preflights": preflights}))
`;

function run(t, scenario) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-closer-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const stdoutPath = path.join(root, 'stdout');
  const stderrPath = path.join(root, 'stderr');
  const stdout = fs.openSync(stdoutPath, 'w');
  const stderr = fs.openSync(stderrPath, 'w');
  let child;
  try {
    child = spawnSync(process.env.PYTHON || 'python3', ['-c', exercise, JSON.stringify(scenario)], {
      cwd: root, timeout: 60000, stdio: ['ignore', stdout, stderr],
      env: {
        PATH: process.env.PATH,
        PYTHONPATH: path.join(repo, 'src'), PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: path.join(root, 'state'),
        ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
        ORCH_FEEDBACK_DB: path.join(root, 'brain.db'),
        ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
      },
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  assert.ifError(child.error);
  assert.equal(child.status, 0, fs.readFileSync(stderrPath, 'utf8'));
  const result = JSON.parse(fs.readFileSync(stdoutPath, 'utf8'));
  assert.deepEqual(result.after, result.before);
  assert.equal(result.original_run_unchanged, true);
  assert.equal(result.result.merge_executed, false);
  assert.equal(result.result.blocked, !scenario.dry_run);
  assert.equal(result.preflights.length, scenario.dry_run ? 1 : 2);
  if (!scenario.dry_run) assert.equal(result.result.reason, 'fixture final gate refusal');
  return result;
}

test('closer contested hook persists a role record without outcome writes', (t) => {
  const result = run(t, { panel: 'BLOCKED' });
  const shadow = result.result.adjudication;
  assert.equal(result.calls, 1);
  assert.equal(result.routes, 1);
  assert.equal(shadow.selector.selector_status, 'invoked');
  assert.equal(shadow.selector.accepted, true);
  assert.equal(result.records.length, 1);
  const record = result.records[0];
  assert.equal(record.run_id, shadow.role_run_id);
  assert.equal(record.target, 'fixture/repo#41');
  assert.equal(record.agent, 'gemini');
  const metadata = JSON.parse(record.metadata);
  assert.equal(metadata.backend_run_id, 'fixture-provider');
  assert.equal(metadata.proposal.decision, 'uphold_blocker');
  assert.equal(metadata.action, 'uphold_blocker');
  assert.equal(metadata.decision_source, 'adjudicator_agent');
  assert.ok(result.events.some((event) => event.result?.accepted === true
    && event.result?.disagreement === true));
});

for (const panel of ['PASS', 'INCONCLUSIVE', null]) {
  test(`closer ${panel} panel creates no shadow invocation or role record`, (t) => {
    const result = run(t, { panel });
    assert.equal(result.result.adjudication, undefined);
    assert.equal(result.calls, 0);
    assert.equal(result.routes, 0);
    assert.deepEqual(result.records, []);
    assert.deepEqual(result.events, []);
  });
}

for (const condition of ['disabled', 'dry_run', 'no_capacity']) {
  test(`closer contested hook respects ${condition} without a provider call`, (t) => {
    const result = run(t, { panel: 'BLOCKED', [condition]: true });
    assert.equal(result.calls, 0);
    assert.equal(result.routes, condition === 'no_capacity' ? 1 : 0);
    assert.deepEqual(result.records, []);
    assert.equal(result.result.adjudication.selector.selector_status, 'matched_not_invoked');
    assert.equal(result.result.adjudication.role_run_id, null);
  });
}

for (const provider of ['invalid', 'raise']) {
  test(`closer ${provider} shadow response preserves the final gate and outcomes`, (t) => {
    const result = run(t, { panel: 'BLOCKED', provider });
    assert.equal(result.calls, 1);
    assert.equal(result.routes, 1);
    if (provider === 'invalid') {
      assert.equal(result.result.adjudication.selector.accepted, false);
      assert.equal(result.records.length, 1);
      const metadata = JSON.parse(result.records[0].metadata);
      assert.equal(metadata.action, 'needs_more_evidence');
      assert.equal(metadata.proposal, undefined);
      assert.equal(metadata.decision_source, 'baseline_needs_more_evidence');
    } else {
      assert.deepEqual(result.result.adjudication, { error: 'fixture provider unavailable' });
      assert.deepEqual(result.records, []);
    }
  });
}
