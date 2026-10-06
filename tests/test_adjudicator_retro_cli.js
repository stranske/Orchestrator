'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';

// Use the Brain's real schema and the default report path. All disputes have
// aged out, so this exercises report refresh without another paid adjudication.
const initialize = `
import json
import time
import adjudicator_retro as retro
import feedback

cases = [
    ("reject_blocker", "durable", 1.5, "ccusage"),
    ("uphold_blocker", "reverted", 2.5, "ccusage"),
    ("reject_blocker", "broke_later", 0, "ledger"),
    ("uphold_blocker", "pending", 0, "ccusage"),
    ("needs_more_evidence", "reverted", 0.5, "ccusage"),
    (None, "durable", None, None),
]
now = int(time.time())
rows = []
with feedback._conn() as conn:
    for index, (decision, durability, cost, source) in enumerate(cases):
        run_id = f"original-{index}"
        conn.execute(
            "INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)",
            (run_id, now - 100 * 86400, f"owner/repo#{index + 1}"),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged,"
            "durability,durability_checked_ts) VALUES (?,'NON_PASS','PASS',1,?,?)",
            (run_id, durability, now),
        )
        row = {"case_id": run_id, "run_id": run_id, "merge_rule_verdict": "PASS"}
        if decision:
            backend = f"backend-{index}"
            conn.execute(
                "INSERT INTO costs(run_id,cost_usd,source) VALUES (?,?,?)",
                (backend, cost, source),
            )
            row.update(
                decision=decision,
                role_run_id=f"shadow-{index}",
                backend_run_id=backend,
                shadow_verdict={"uphold_blocker": "FAIL", "reject_blocker": "PASS"}.get(decision),
                later_truth=None,
                cost_usd=None,
            )
        else:
            row["error"] = "missing gate evidence"
        rows.append(row)
report = retro.report_path()
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text(json.dumps({"rows": rows, "summary": {"cases": 999}}))
print(json.dumps(rows))
`;

const snapshot = `
import json
import feedback
with feedback._conn() as conn:
    print(json.dumps({table: conn.execute(f"SELECT * FROM {table} ORDER BY run_id").fetchall()
                      for table in ("runs", "outcomes", "costs")}))
`;

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-retro-cli-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const env = {
    PATH: process.env.PATH,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: path.join(root, 'state'),
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_FEEDBACK_DB: path.join(root, 'brain.db'),
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
  };
  function run(args) {
    // File capture supports the same restricted runners as the other CLI witnesses.
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(python, args, {
        cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 10000,
      });
      assert.ifError(result.error);
      assert.equal(result.status, 0, fs.readFileSync(errors, 'utf8'));
      return JSON.parse(fs.readFileSync(output, 'utf8'));
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      fs.rmSync(output);
      fs.rmSync(errors);
    }
  }
  return {
    run,
    report: () => JSON.parse(fs.readFileSync(
      path.join(env.ORCH_STATE_DIR, 'capability-program', 'adjudicator-retro.json'), 'utf8',
    )),
    refresh: () => run([path.join(modules, 'adjudicator_retro.py'), '--dispatch', '--limit', '0']),
    brain: () => run(['-c', snapshot]),
  };
}

test('retro CLI preserves raw proposals without grading metadata and refreshes measured costs', (t) => {
  const w = world(t);
  const saved = w.run(['-c', initialize]);
  const before = w.brain();
  const summary = w.refresh();
  assert.deepEqual(summary, {
    cases: 6, adjudicated: 0, proposed_decisions: 4, metadata_only_cases: 6,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  const report = w.report();
  assert.deepEqual(report.proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 3, pending_truth: 1, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 2, disagree: 1, agreement_rate: 2 / 3,
    merge_rule_agree: 1, merge_rule_disagree: 2, merge_rule_agreement_rate: 1 / 3,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  assert.deepEqual(report.rows.map((row) => row.proposal_comparison), [
    { verdict: 'PASS', agrees: true, merge_rule_agrees: true },
    { verdict: 'FAIL', agrees: true, merge_rule_agrees: false },
    { verdict: 'PASS', agrees: false, merge_rule_agrees: false },
    { verdict: 'FAIL', agrees: null, merge_rule_agrees: null },
    { verdict: null, agrees: null, merge_rule_agrees: null },
    { verdict: null, agrees: null, merge_rule_agrees: null },
  ]);
  assert.deepEqual(report.summary, summary, 'the CLI and persisted report must agree');
  assert.equal(report.population, 0);
  assert.equal(report.shadow, true);
  assert.equal(report.source, 'retrospective');
  assert.deepEqual(report.rows.map((row) => row.role_run_id), saved.map((row) => row.role_run_id));
  assert.deepEqual(report.rows.map((row) => row.decision), saved.map((row) => row.decision));
  assert.ok(report.rows.every((row) => row.disposition === 'needs_more_evidence'));
  assert.ok(report.rows.every((row) => row.shadow_verdict === null));
  assert.equal(report.rows[0].raw_shadow_verdict, 'PASS');
  assert.equal(report.rows[1].raw_shadow_verdict, 'FAIL');
  assert.equal(report.rows[2].cost_usd, null, 'ledger placeholders are not measured costs');
  assert.equal(report.rows[3].cost_usd, 0, 'a measured zero still counts in the denominator');
  assert.equal(report.rows[3].later_truth, null, 'pending durability must stay ungraded');
  assert.deepEqual(w.brain(), before, 'refresh must not write outcomes or dispatch role runs');
  const { line, rendered } = w.run(['-c', `
import json
import adjudicator_retro as retro
import switch_review
line = retro.weekly_line()
rendered = switch_review.format_report({
    "review_days": 7, "raise_count": 0,
    "held_off": [], "on_but_idle": [], "unconditioned": [],
    "mirror_drift": {"status": "ok"}, "adjudicator_shadow": line,
})
print(json.dumps({"line": line, "rendered": rendered}))
`]);
  assert.equal(line, 'adjudicator shadow: cases 6, agree 0, disagree 0, cost 4.5 '
    + '(graded 0; costs measured 4)');
  assert.equal(rendered.split('\n').filter((text) => text === line).length, 1);
});

test('retro CLI reuses saved cases when later durability and costs arrive', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const identities = w.report().rows.map((row) => [row.case_id, row.role_run_id]);
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET durability='durable' WHERE run_id='original-3'")
    conn.execute("UPDATE costs SET cost_usd=0.75,source='ccusage' WHERE run_id='backend-2'")
print(json.dumps(True))
`]);
  const before = w.brain();
  const summary = w.refresh();
  assert.deepEqual(summary, {
    cases: 6, adjudicated: 0, proposed_decisions: 4, metadata_only_cases: 6,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: 5.25, cost_measured_cases: 5, cost_per_case: 1.05,
  });
  assert.deepEqual(w.report().summary, summary);
  assert.deepEqual(w.report().proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 4, pending_truth: 0, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 2, disagree: 2, agreement_rate: 0.5,
    merge_rule_agree: 2, merge_rule_disagree: 2, merge_rule_agreement_rate: 0.5,
    cost_usd: 5.25, cost_measured_cases: 5, cost_per_case: 1.05,
  });
  assert.deepEqual(w.report().rows.map((row) => [row.case_id, row.role_run_id]), identities);
  assert.deepEqual(w.brain(), before);
  assert.deepEqual(w.refresh(), summary, 'a second process must not duplicate saved cases');
  assert.deepEqual(w.brain(), before);

  // Refresh baseline facts too: unknown or deleted outcomes cannot keep stale comparisons.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute("UPDATE outcomes SET merged=NULL WHERE run_id='original-0'")
    conn.execute("UPDATE outcomes SET merged=0 WHERE run_id='original-1'")
    conn.execute("DELETE FROM outcomes WHERE run_id='original-2'")
print(json.dumps(True))
`]);
  const changedBrain = w.brain();
  w.refresh();
  assert.deepEqual(w.report().proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 2, pending_truth: 1, missing_merge_disposition: 1, abstained: 1, unassessed: 1,
    agree: 1, disagree: 1, agreement_rate: 0.5,
    merge_rule_agree: 2, merge_rule_disagree: 0, merge_rule_agreement_rate: 1,
    cost_usd: 5.25, cost_measured_cases: 5, cost_per_case: 1.05,
  });
  assert.equal(w.report().rows[0].merge_rule_verdict, null);
  assert.equal(w.report().rows[0].proposal_comparison.agrees, null);
  assert.equal(w.report().rows[1].merge_rule_verdict, 'FAIL');
  assert.equal(w.report().rows[2].later_truth, null);
  assert.deepEqual(w.brain(), changedBrain);
});

test('retro CLI publishes an empty report without inventing agreement or cost', (t) => {
  const w = world(t);
  w.run(['-c', 'import json, feedback; feedback._conn().close(); print(json.dumps(True))']);
  const before = w.brain();
  const summary = w.refresh();
  assert.deepEqual(summary, {
    cases: 0, adjudicated: 0, proposed_decisions: 0, metadata_only_cases: 0,
    graded: 0, agree: 0, disagree: 0,
    agreement_rate: null, merge_rule_agreement_rate: null,
    cost_usd: null, cost_measured_cases: 0, cost_per_case: null,
  });
  assert.deepEqual(w.report().summary, summary);
  assert.deepEqual(w.report().rows, []);
  assert.deepEqual(w.report().proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 0, proposed_decisions: 0,
    compared: 0, pending_truth: 0, missing_merge_disposition: 0, abstained: 0, unassessed: 0,
    agree: 0, disagree: 0, agreement_rate: null,
    merge_rule_agree: 0, merge_rule_disagree: 0, merge_rule_agreement_rate: null,
    cost_usd: null, cost_measured_cases: 0, cost_per_case: null,
  });
  assert.deepEqual(w.brain(), before);
});

test('retro CLI removes stale comparisons until durability is judged again', (t) => {
  const w = world(t);
  w.run(['-c', initialize]);
  w.refresh();
  const initial = w.report();
  const identities = initial.rows.map((row) => [row.case_id, row.role_run_id]);

  // Retain the durable/reverted labels, but withdraw their trusted observation dates.
  // A previously graded report must not keep counting these labels as later truth.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute(
        "UPDATE outcomes SET durability_checked_ts=? WHERE run_id='original-0'",
        (feedback.DURABILITY_DETECTION_SINCE - 1,),
    )
    conn.execute("UPDATE outcomes SET durability_checked_ts=NULL WHERE run_id='original-1'")
print(json.dumps(True))
`]);
  const unjudgedBrain = w.brain();
  w.refresh();
  const unjudged = w.report();
  assert.deepEqual(unjudged.proposal_comparison, {
    evidence_basis: 'raw_metadata_proposals', cases: 6, proposed_decisions: 4,
    compared: 1, pending_truth: 3, missing_merge_disposition: 0, abstained: 1, unassessed: 1,
    agree: 0, disagree: 1, agreement_rate: 0,
    merge_rule_agree: 0, merge_rule_disagree: 1, merge_rule_agreement_rate: 0,
    cost_usd: 4.5, cost_measured_cases: 4, cost_per_case: 1.125,
  });
  for (const index of [0, 1]) {
    assert.equal(unjudged.rows[index].later_truth, null);
    assert.deepEqual(unjudged.rows[index].proposal_comparison, {
      verdict: index === 0 ? 'PASS' : 'FAIL', agrees: null, merge_rule_agrees: null,
    });
  }
  assert.deepEqual(unjudged.summary, initial.summary);
  assert.deepEqual(w.brain(), unjudgedBrain, 'regrading must leave the Brain unchanged');

  // The detection boundary itself is trusted. Regrading restores the original
  // comparison without paying for another verdict or recreating any role run.
  w.run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    conn.execute(
        "UPDATE outcomes SET durability_checked_ts=? WHERE run_id IN ('original-0','original-1')",
        (feedback.DURABILITY_DETECTION_SINCE,),
    )
print(json.dumps(True))
`]);
  const rejudgedBrain = w.brain();
  w.refresh();
  const restored = w.report();
  assert.deepEqual(restored.proposal_comparison, initial.proposal_comparison);
  assert.deepEqual(restored.rows.map((row) => [row.case_id, row.role_run_id]), identities);
  assert.ok(restored.rows.every((row) => row.shadow_verdict === null));
  assert.deepEqual(w.brain(), rejudgedBrain, 'restoring truth must not redispatch saved cases');
});

test('closer lane offers the adjudicator only when both verdicts establish a dispute', (t) => {
  const w = world(t);
  const results = w.run(['-c', `
import json
from unittest.mock import patch
import capabilities
import capability_advisor as advisor

row = capabilities._blank_capability("role-adjudicator")
row["status"] = "generated"
capabilities.save({"role-adjudicator": row})
contexts = [
    {},
    {"verifier_verdict": "NON_PASS"},
    {"merge_disposition": "PASS"},
    {"verifier_verdict": "UNKNOWN", "merge_disposition": "PASS"},
    {"verifier_verdict": "NON_PASS", "merge_disposition": " UNKNOWN "},
    {"verifier_verdict": " ", "merge_disposition": "PASS"},
    {"verifier_verdict": "pass", "merge_disposition": " PASS "},
    {"verifier_verdict": "NON_PASS", "merge_disposition": "non_pass"},
    {"verifier_verdict": "NON_PASS", "merge_disposition": "PASS"},
    {"verifier_verdict": "PASS", "merge_disposition": " NON_PASS "},
]
results = []
with patch.object(advisor, "PR_FACTS_FETCH", return_value={}):
    for text in ("xyzzy plugh", "review disputed verifier results"):
        for context in contexts:
            result = advisor.advise(
                text, surface="closer-lane", repository="owner/repo",
                context=context, record=False,
            )
            results.append({
                "text": text,
                "context": context,
                "offered": any(c["capability_id"] == "role-adjudicator"
                               for c in result["capabilities"]),
                "withheld": result["precondition"]["withheld"],
                "missing": result["fact_missing"],
            })
print(json.dumps(results))
`]);
  assert.equal(results.length, 20);
  for (const [index, result] of results.entries()) {
    const offered = index % 10 >= 8;
    assert.equal(result.offered, offered, JSON.stringify(result));
    assert.equal(Boolean(result.withheld.length || result.missing.length), !offered,
      'an ineligible offer must explain whether evidence is missing or verdicts agree');
  }
});
