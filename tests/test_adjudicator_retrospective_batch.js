'use strict';

// Synthetic cohort witness, not a receipt for the unavailable historical 71.
// Only routing and provider output are stubbed. Inventory, packets, role runs,
// accounting, comparisons, persistence and report rendering use production code.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { inventory } = require('../scripts/export_adjudicator_inventory');

const repo = path.resolve(__dirname, '..');
const replay = `
import json
import sqlite3
import time
from unittest.mock import patch
import adjudicator_retro as retro
import feedback
import roles

now = int(time.time())
with feedback._conn() as conn:
    for index in range(71):
        run_id = f"synthetic-{index:02d}"
        conn.execute("INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
                     (run_id, now - index, "fixture/repo#1"))
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged,"
            "durability,durability_checked_ts) VALUES (?,'NON_PASS','PASS',?,?,?)",
            (run_id, index % 2, "durable" if index % 3 else "reverted", now))
        amount = {0: 2, 1: 4, 3: 1}.get(index % 5)
        if amount is not None:
            conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES (?,?,'ccusage')",
                         (f"backend-{index:02d}", amount))
    # Persist a prohibition that applies to every connection the role opens.
    # Snapshots alone could miss a write that restores the original value.
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER prohibit_outcome_{operation} BEFORE {operation} ON outcomes "
                     "BEGIN SELECT RAISE(ABORT, 'outcome writes prohibited'); END")

with feedback._conn() as conn:
    for statement in (
        "INSERT INTO outcomes(run_id) VALUES ('guard-probe')",
        "UPDATE outcomes SET merged=merged WHERE run_id='synthetic-00'",
        "DELETE FROM outcomes WHERE run_id='synthetic-00'",
    ):
        try:
            conn.execute(statement)
        except sqlite3.IntegrityError as exc:
            assert str(exc) == "outcome writes prohibited"
        else:
            raise AssertionError("outcome prohibition is ineffective")

def snapshot():
    with feedback._conn() as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY run_id").fetchall()
                for table in ("runs", "outcomes", "costs")}

before = snapshot()
reads = []
calls = []
current_index = None

def evidence(row):
    global current_index
    current_index = int(row["run_id"].rsplit("-", 1)[1])
    reads.append(row["run_id"])
    return {
        "disputed_finding": {"body": f"Inspect criterion for {row['run_id']}"},
        "ground_truth_evidence": {
            "diff_summary": "Fixture diff metadata; inspected source is unavailable",
            "gate_runs": [] if current_index % 5 == 4 else ["fixture-gate"],
        },
    }

def offload(backend, prompt, **kwargs):
    index = current_index
    assert kwargs["isolate"] is True
    assert f"synthetic-{index:02d}" in prompt
    calls.append(index)
    proposal = {
        "decision": ["uphold_blocker", "reject_blocker", "needs_more_evidence"][index % 5]
                    if index % 5 < 3 else None,
        "confidence": "low", "rationale": "Inspect the missing source evidence.",
        "evidence_assessment": [{"claim": "Missing source", "status": "insufficient",
            "evidence_ref": "fixture-gate", "reason": "Gate status cannot establish source"}],
        "ground_truth_refs": ["fixture-gate"],
        "recommended_next_step": "Inspect the source",
        "evidence_gaps": ["Source unavailable"],
    }
    return {"run_id": f"backend-{index:02d}", "exit": 0,
            "output": "invalid provider response" if index % 5 == 3 else json.dumps(proposal)}

checkpoints = []
with (
    patch.object(roles, "route_role", return_value={"agent": "gemini"}) as route,
    patch.object(roles, "_role_capability_event"),
    patch.object(roles.dispatcher, "offload", side_effect=offload),
):
    for _ in range(9):
        report = retro.run(dispatch=True, limit=8, evidence_reader=evidence)
        assert json.loads(retro.report_path().read_text()) == report
        assert snapshot()["outcomes"] == before["outcomes"]
        checkpoints.append({"cases": report["summary"]["cases"],
                            "run_ids": [row["run_id"] for row in report["rows"]],
                            "role_ids": [row.get("role_run_id") for row in report["rows"]]})
    resumed = retro.run(dispatch=True, limit=71, evidence_reader=evidence)
    assert resumed["rows"] == report["rows"]
    assert resumed["summary"] == report["summary"]
    assert resumed["proposal_comparison"] == report["proposal_comparison"]
    assert json.loads(retro.report_path().read_text()) == resumed

after = snapshot()
with feedback._conn() as conn:
    records = [dict(zip(("run_id", "target", "agent", "source", "metadata"), row))
               for row in conn.execute(
                   "SELECT run_id,target,agent,source,decomposition FROM runs "
                   "WHERE role_name='adjudicator' ORDER BY run_id")]
print(json.dumps({"observed_at": now, "before": before, "after": after,
                  "report": resumed, "records": records, "checkpoints": checkpoints,
                  "reads": reads, "calls": calls, "routes": route.call_count,
                  "weekly_line": retro.weekly_line()}))
`;

test('synthetic 71-dispute replay accounts for every case with outcome writes prohibited', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-batch-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const db = path.join(root, 'brain.db');
  const stdoutPath = path.join(root, 'stdout');
  const stderrPath = path.join(root, 'stderr');
  const stdout = fs.openSync(stdoutPath, 'w');
  const stderr = fs.openSync(stderrPath, 'w');
  let processResult;
  try {
    processResult = spawnSync(process.env.PYTHON || 'python3', ['-c', replay], {
      cwd: root, timeout: 60000, stdio: ['ignore', stdout, stderr],
      env: {
        PATH: process.env.PATH,
        PYTHONPATH: path.join(repo, 'src'), PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: path.join(root, 'state'),
        ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
        ORCH_FEEDBACK_DB: db,
        ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
      },
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  assert.ifError(processResult.error);
  assert.equal(processResult.status, 0, fs.readFileSync(stderrPath, 'utf8'));
  const result = JSON.parse(fs.readFileSync(stdoutPath, 'utf8'));
  const report = result.report;
  const ids = Array.from({ length: 71 }, (_, index) => `synthetic-${String(index).padStart(2, '0')}`);
  const observed = inventory({ db, observedAt: result.observed_at });
  assert.equal(observed.included_count, 71);
  assert.equal(observed.requested_cohort_verified, false);
  assert.deepEqual(observed.cases.map((row) => row.run_id), ids);
  assert.deepEqual(report.rows.map((row) => row.run_id), ids);
  assert.equal(new Set(report.rows.map((row) => row.case_id)).size, 71);
  assert.equal(report.population, 71);
  assert.equal(report.shadow, true);
  assert.deepEqual(result.reads, ids, 'resume cannot recollect or redispatch completed attempts');
  assert.deepEqual(result.checkpoints.map((item) => item.cases), [8, 16, 24, 32, 40, 48, 56, 64, 71]);
  for (const checkpoint of result.checkpoints) {
    assert.deepEqual(checkpoint.run_ids, ids.slice(0, checkpoint.cases));
    assert.deepEqual(checkpoint.role_ids,
      report.rows.slice(0, checkpoint.cases).map((row) => row.role_run_id || null));
  }

  const records = new Map(result.records.map((record) => [record.run_id, record]));
  assert.equal(records.size, 57, 'every dispatched attempt, including invalid output, needs a role run');
  assert.equal(result.routes, 57);
  assert.deepEqual(result.calls, Array.from({ length: 71 }, (_, i) => i).filter((i) => i % 5 !== 4));
  let agree = 0;
  let baselineAgree = 0;
  for (const [index, row] of report.rows.entries()) {
    const mode = index % 5;
    assert.equal(row.target, 'fixture/repo#1', 'same-PR disputes retain distinct run identities');
    assert.equal(row.shadow_verdict, null, 'metadata proposals cannot become effective verdicts');
    assert.equal(row.metadata_only, true);
    if (mode === 4) {
      assert.match(row.error, /gate/i);
      assert.equal(row.role_run_id, undefined);
      assert.equal(row.backend_run_id, undefined);
      assert.equal(row.cost_usd, null, 'unavailable evidence cannot imply a zero-dollar call');
      continue;
    }
    assert.equal(row.role_record_error, null);
    assert.equal(row.backend_run_id, `backend-${String(index).padStart(2, '0')}`);
    const record = records.get(row.role_run_id);
    assert.ok(record, 'saved attempts join to durable Brain role records by ID');
    assert.equal(record.target, row.target);
    assert.equal(record.agent, 'gemini');
    assert.equal(record.source, 'retrospective');
    const metadata = JSON.parse(record.metadata);
    assert.equal(metadata.backend_run_id, row.backend_run_id);
    assert.equal(metadata.action, 'needs_more_evidence');
    if (mode === 3) {
      assert.ok(row.errors.some((error) => error.includes('could not parse')));
      assert.equal(row.decision, undefined);
    } else {
      assert.deepEqual(metadata.proposal, row.proposal);
      assert.equal(row.decision, ['uphold_blocker', 'reject_blocker', 'needs_more_evidence'][mode]);
    }
    if (mode === 2) {
      assert.equal(row.cost_usd, null);
      assert.equal(row.cost_report.status, 'unmeasured');
      assert.equal(row.cost_report.cost_usd, null, 'missing telemetry must stay explicitly unknown');
    } else {
      assert.equal(row.cost_usd, { 0: 2, 1: 4, 3: 1 }[mode]);
      assert.equal(row.cost_report.status, 'measured');
    }
    if (mode < 2) {
      const truth = index % 3 ? 'PASS' : 'FAIL';
      const proposalAgrees = (mode === 0 ? 'FAIL' : 'PASS') === truth;
      const mergeAgrees = (index % 2 ? 'PASS' : 'FAIL') === truth;
      assert.equal(row.proposal_comparison.agrees, proposalAgrees);
      assert.equal(row.proposal_comparison.merge_rule_agrees, mergeAgrees);
      agree += Number(proposalAgrees);
      baselineAgree += Number(mergeAgrees);
    } else {
      assert.equal(row.proposal_comparison.agrees, null);
    }
  }
  for (const [key, value] of Object.entries({
    cases: 71, adjudicated: 0, graded: 0, proposed_decisions: 29,
    cost_attempted_cases: 57, cost_measured_cases: 43, cost_unmeasured_cases: 14,
    cost_imputed_cases: 0, cost_usd: 100, cost_per_case: 100 / 43,
  })) assert.equal(report.summary[key], value, key);
  for (const [key, value] of Object.entries({
    cases: 71, compared: 29, abstained: 14, unassessed: 28,
    agree, disagree: 29 - agree, agreement_rate: agree / 29,
    merge_rule_agree: baselineAgree, merge_rule_agreement_rate: baselineAgree / 29,
    cost_usd: 100, cost_measured_cases: 43, cost_per_case: 100 / 43,
  })) assert.equal(report.proposal_comparison[key], value, key);
  assert.match(result.weekly_line, /^adjudicator shadow: cases 71, agree 0, disagree 0, cost 100(?:\.0)? /);
  assert.ok(result.weekly_line.includes('cost measured 43 of 57, imputed 0, unmeasured 14'));
  assert.deepEqual(result.after.outcomes, result.before.outcomes);
  assert.deepEqual(result.after.costs, result.before.costs);
  assert.deepEqual(result.after.runs.filter((row) => row[0].startsWith('synthetic-')), result.before.runs);
  assert.equal(result.after.runs.length, result.before.runs.length + 57);
});
