'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { caseGaps } = require('../scripts/export_adjudicator_case_gaps');
const { reconcileSummary } = require('../scripts/reconcile_adjudicator_summary');

const source = path.resolve(__dirname, '../src');
const script = path.resolve(__dirname, '../scripts/reconcile_adjudicator_summary.js');
const digest = (value) => createHash('sha256').update(value).digest('hex');
const write = (file, value) => fs.writeFileSync(file, JSON.stringify(value));
const read = (file) => JSON.parse(fs.readFileSync(file));

function python(root, code, args = []) {
  const files = ['stdout', 'stderr'].map((name) => path.join(root, name));
  const fds = files.map((name) => fs.openSync(name, 'w'));
  let result;
  try {
    result = spawnSync(process.env.PYTHON || 'python3', ['-I', '-B', '-c',
      `import sys\nsys.path.insert(0, sys.argv[1])\n${code}`, source, ...args], {
      timeout: 30000, stdio: ['ignore', ...fds],
      env: { ...process.env, ORCH_STATE_DIR: path.join(root, 'state'),
        ORCH_FEEDBACK_DB: path.join(root, 'unused.db'),
        ORCH_LOCAL_RUNTIME: path.join(root, 'runtime') },
    });
  } finally { fds.forEach((fd) => fs.closeSync(fd)); }
  assert.ifError(result.error);
  assert.equal(result.status, 0, fs.readFileSync(files[1], 'utf8'));
  return fs.readFileSync(files[0], 'utf8');
}

function world(t, { shared = false, zero = false, retry = false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-summary-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const report = path.join(root, 'report.json');
  const db = path.join(root, 'brain.db');
  const manifest = path.join(root, 'manifest.json');
  const caseFile = path.join(root, 'cases.json');
  const switchReview = path.join(root, 'switch-review.txt');
  const rows = ['one', 'two'].map((name, index) => ({
    run_id: name, target: 'fixture/repo#1', verifier_verdict: 'NON_PASS',
    adjudicated_verdict: 'PASS', role_run_id: `role:${name}`,
    backend_run_id: shared ? 'backend:shared' : `backend:${name}`,
    decision: 'uphold_blocker', cost_usd: zero ? null : index === 0 || shared ? 1.5 : null,
    later_truth: 'PASS', merge_rule_verdict: 'PASS',
    packet: { target: 'fixture/repo#1', ground_truth_evidence: { head_sha: 'a'.repeat(40) } },
  }));
  if (retry) rows[0].retry_attempts = [{ backend_run_id: 'backend:prior', cost_usd: 2 }];
  write(report, { rows });
  python(root, `
import json
from pathlib import Path
import adjudicator_retro as retro
import switch_review
file = Path(sys.argv[2])
report = json.loads(file.read_text())
report["summary"] = retro.summarize(report["rows"])
file.write_text(json.dumps(report))
rendered = switch_review.format_report({"review_days": 7, "raise_count": 0,
    "held_off": [], "on_but_idle": [], "unconditioned": [],
    "mirror_drift": {"status": "ok"}, "adjudicator_shadow": retro.weekly_line(file)})
Path(sys.argv[3]).write_text(rendered)
`, [report, switchReview]);
  write(manifest, { schema_version: 2, cases: rows.map(({ run_id, target,
    verifier_verdict, adjudicated_verdict }) => ({ run_id, target, verifier_verdict, adjudicated_verdict })) });
  const conn = new DatabaseSync(db);
  conn.exec(`CREATE TABLE runs (run_id TEXT PRIMARY KEY,target TEXT,ts INTEGER,
    role_name TEXT,agent TEXT,decomposition TEXT);
    CREATE TABLE outcomes (run_id TEXT PRIMARY KEY,verifier_verdict TEXT,
      adjudicated_verdict TEXT,merged INTEGER,durability TEXT,durability_checked_ts INTEGER);
    CREATE TABLE costs (run_id TEXT PRIMARY KEY,cost_usd REAL,source TEXT);`);
  for (const row of rows) {
    conn.prepare('INSERT INTO runs VALUES (?,?,2000000000,NULL,NULL,NULL)').run(row.run_id, row.target);
    conn.prepare("INSERT INTO outcomes VALUES (?,'NON_PASS','PASS',1,'durable',2000000000)").run(row.run_id);
    conn.prepare("INSERT INTO runs VALUES (?,?,0,'adjudicator','gemini',?)").run(row.role_run_id,
      row.target, JSON.stringify({ role: 'adjudicator', backend_run_id: row.backend_run_id,
        action: row.decision }));
    conn.prepare("INSERT OR IGNORE INTO costs VALUES (?,?,'ccusage')").run(row.backend_run_id,
      zero ? 0 : row.cost_usd);
  }
  conn.close();
  const refresh = () => write(caseFile, caseGaps({ report, db, manifest,
    observedAt: 2000000000, expectedCount: 2, persistedRoles: true }));
  refresh();
  return { root, report, db, caseGaps: caseFile, switchReview, refresh,
    python: process.env.PYTHON || 'python3' };
}

test('production summaries, rendered output and read-only persisted exports reconcile with explicit limits', (t) => {
  const options = world(t);
  const inputs = [options.report, options.db, options.caseGaps, options.switchReview];
  const before = inputs.map((file) => fs.readFileSync(file));
  const files = fs.readdirSync(options.root);
  const result = reconcileSummary(options);
  assert.equal(result.switch_review_comparison.status, 'matches_saved_renderer');
  assert.ok(result.summary_comparison.length > 15);
  assert.ok(result.summary_comparison.every((item) => item.status === 'match'));
  assert.equal(result.saved_summary.graded, 0, 'metadata proposals cannot become accepted grades');
  assert.equal(result.exported_record_totals.matched_role_records, 2);
  assert.equal(result.exported_record_totals.known_backend_cost_subtotal_usd, 1.5);
  assert.equal(result.exported_record_totals.unknown_current_case_costs, 1);
  assert.equal(result.exported_record_totals.complete_cost_usd, 'UNKNOWN');
  assert.equal(result.reconciliation_complete, false);
  assert.equal(result.operational_acceptance_complete, false);
  assert.equal(result.requested_cohort_verified, false);
  assert.ok(path.isAbsolute(result.recount_provenance.interpreter));
  for (const row of result.cases) assert.ok(row.evidence_gaps.includes('original_dispute_revision_binding_unverified'));
  assert.equal(result.sources.saved_report.sha256, digest(before[0]));
  assert.equal(result.sources.reconciler_sha256, digest(fs.readFileSync(script)));
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
  assert.deepEqual(fs.readdirSync(options.root), files, 'no report, Brain or sidecar writes');
});

test('saved aggregate corruption is detected even when captured rendering repeats it', (t) => {
  const options = world(t);
  const report = read(options.report);
  report.summary.cases = 99;
  report.summary.cost_usd = 42;
  delete report.summary.advice_graded;
  write(options.report, report);
  fs.writeFileSync(options.switchReview, fs.readFileSync(options.switchReview, 'utf8')
    .replace('cases 2,', 'cases 99,').replace('cost 1.5 ', 'cost 42 '));
  options.refresh();
  const result = reconcileSummary(options);
  assert.equal(result.switch_review_comparison.status, 'matches_saved_renderer');
  for (const field of ['cases', 'cost_usd']) assert.equal(
    result.summary_comparison.find((item) => item.field === field).status, 'mismatch');
  assert.equal(result.summary_comparison.find((item) => item.field === 'advice_graded').status, 'missing_saved_field');
  assert.equal(result.switch_review_comparison.persisted_totals_verified, false);
});

test('shared whole-run receipts count once; zero telemetry and retained attempts stay unresolved', (t) => {
  const shared = reconcileSummary(world(t, { shared: true }));
  assert.equal(shared.saved_summary.cost_attempted_cases, 1);
  assert.equal(shared.exported_record_totals.distinct_measured_backends, 1);
  assert.equal(shared.exported_record_totals.known_backend_cost_subtotal_usd, 1.5);
  const zero = reconcileSummary(world(t, { zero: true }));
  assert.equal(zero.exported_record_totals.known_backend_cost_subtotal_usd, null);
  assert.equal(zero.exported_record_totals.unknown_current_case_costs, 2);
  assert.equal(zero.exported_record_totals.complete_cost_usd, 'UNKNOWN');
  const retry = reconcileSummary(world(t, { retry: true }));
  assert.equal(retry.saved_summary.cost_usd, 3.5);
  assert.equal(retry.exported_record_totals.known_backend_cost_subtotal_usd, 1.5);
  assert.ok(retry.cases[0].evidence_gaps.includes('retained_attempt_records_not_reconciled'));
});

test('stale report digests, changed row digests, duplicate and missing exports cannot supply costs', (t) => {
  const options = world(t);
  const original = read(options.caseGaps);
  for (const mutate of [
    (ledger) => { ledger.saved_report_snapshot.sha256 = '0'.repeat(64); },
    (ledger) => { ledger.cases[0].saved_report_evidence.row_sha256 = '0'.repeat(64); },
    (ledger) => { ledger.cases.push(ledger.cases[0]); },
    (ledger) => { ledger.cases.shift(); },
    (ledger) => { ledger.cases[0].persisted_role_evidence.backend_run_id = 'other'; },
    (ledger) => { ledger.cases[0].persisted_role_evidence.backend_cost.source = 'ledger'; },
  ]) {
    const ledger = structuredClone(original);
    mutate(ledger);
    write(options.caseGaps, ledger);
    const result = reconcileSummary(options);
    assert.equal(result.cases[0].exported_backend_cost_usd, 'UNKNOWN');
    assert.equal(result.exported_record_totals.known_backend_cost_subtotal_usd, null);
    assert.equal(result.reconciliation_complete, false);
  }
});

test('unexpected export cases and ambiguous or mismatched switch captures remain gaps', (t) => {
  const options = world(t);
  const ledger = read(options.caseGaps);
  ledger.cases.push({ ...ledger.cases[0], run_id: 'extra' });
  write(options.caseGaps, ledger);
  const capture = fs.readFileSync(options.switchReview, 'utf8');
  for (const text of [capture + capture, 'no adjudicator line', capture.replace('cases 2,', 'cases 3,')]) {
    fs.writeFileSync(options.switchReview, text);
    const result = reconcileSummary(options);
    assert.ok(result.evidence_gaps.some((item) => item.kind === 'export_cases_absent_from_saved_report'));
    assert.ok(result.evidence_gaps.some((item) => item.kind === 'switch_review_not_reconciled'));
  }
});

test('aggregate-only historical artifact records missing inputs without substituting a cohort', () => {
  const result = reconcileSummary({ report: path.resolve(__dirname,
    '../docs/evidence/issue-428-metadata-floor-20261006/private-replay-summary.json') });
  assert.equal(result.saved_summary.cases, 37);
  assert.equal(result.saved_population, 122);
  assert.equal(result.saved_case_row_count, null);
  assert.equal(result.exported_record_totals.unknown_current_case_costs, null);
  assert.equal(result.summary_comparison, null);
  assert.equal(result.recount_provenance, null);
  assert.equal(result.switch_review_comparison.status, 'unavailable');
  assert.deepEqual(result.cases, []);
  assert.deepEqual(result.evidence_gaps.map((item) => item.kind), [
    'persisted_case_export_unavailable', 'actual_switch_review_output_unavailable', 'saved_case_rows_unavailable',
  ]);
  assert.equal(result.reconciliation_complete, false);
});

test('owner exclusions suppress execution claims and recount, including an excluded inventory identity', (t) => {
  const options = world(t);
  const ledger = read(options.caseGaps);
  ledger.cases[0].inventory_reasons = ['owner_excluded_repository'];
  write(options.caseGaps, ledger);
  // A nonexistent interpreter proves that excluded reports are not recounted.
  let result = reconcileSummary({ ...options, python: '/missing/interpreter' });
  assert.deepEqual(result.cases[0].evidence_gaps, ['owner_excluded_repository']);
  assert.equal(result.cases[0].exported_backend_cost_usd, undefined);
  assert.equal(result.summary_comparison, null);
  const report = read(options.report);
  report.rows[0].target = ' STRANSKE/collab-admin\t#1';
  report.rows[0].cost_usd = 'excluded-execution-evidence';
  write(options.report, report);
  result = reconcileSummary({ ...options, python: '/missing/interpreter' });
  assert.equal(JSON.stringify(result).includes('excluded-execution-evidence'), false);
  assert.equal(result.summary_comparison, null);
});

test('excluded retry identities withhold recount and conflicting shared receipts stay unknown', (t) => {
  const options = world(t, { retry: true });
  const report = read(options.report);
  report.rows[0].retry_attempts[0].target = 'stranske/Collab-Admin#1';
  write(options.report, report);
  const excluded = reconcileSummary({ ...options, python: '/missing/python' });
  assert.equal(excluded.summary_comparison, null);
  assert.deepEqual(excluded.cases[0].evidence_gaps, ['owner_excluded_repository']);
  const shared = world(t, { shared: true });
  const ledger = read(shared.caseGaps);
  ledger.cases[1].persisted_role_evidence.backend_cost.cost_usd = 9;
  write(shared.caseGaps, ledger);
  const result = reconcileSummary(shared);
  assert.equal(result.exported_record_totals.known_backend_cost_subtotal_usd, null);
  assert.equal(result.exported_record_totals.distinct_measured_backends, 0);
  assert.equal(result.exported_record_totals.unknown_current_case_costs, 2);
  assert.ok(result.cases.every((row) => row.exported_backend_cost_usd === 'UNKNOWN'));
  assert.ok(result.evidence_gaps.some((gap) => gap.kind === 'conflicting_backend_cost_receipts'));
});

test('CLI emits reviewable incomplete JSON; invalid input and failed interpreter emit none', (t) => {
  const options = world(t);
  const output = path.join(options.root, 'cli-output');
  const error = path.join(options.root, 'cli-error');
  function invoke(args) {
    const fds = [fs.openSync(output, 'w'), fs.openSync(error, 'w')];
    try {
      const result = spawnSync(process.execPath, [script, ...args], {
        timeout: 30000, stdio: ['ignore', ...fds],
      });
      assert.ifError(result.error);
      return result.status;
    } finally { fds.forEach((fd) => fs.closeSync(fd)); }
  }
  assert.equal(invoke(['--report', options.report, '--case-gaps', options.caseGaps,
    '--switch-review', options.switchReview, '--python', options.python]), 2);
  assert.equal(read(output).reconciliation_complete, false);
  for (const args of [[], ['--report', options.report, '--python', '/missing/python']]) {
    assert.equal(invoke(args), 1);
    assert.equal(fs.readFileSync(output, 'utf8'), '');
  }
  for (const value of [null, {}, { summary: [] }, { summary: {}, rows: [null] }]) {
    write(options.report, value);
    assert.equal(invoke(['--report', options.report]), 1);
    assert.equal(fs.readFileSync(output, 'utf8'), '');
  }
});
