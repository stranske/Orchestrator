'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { liveCohort } = require('../scripts/export_adjudicator_live_cohort');

const repo = path.resolve(__dirname, '..');
const script = path.join(repo, 'scripts/export_adjudicator_live_cohort.js');
const now = 1793880000;
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const identity = (n) => ({ run_id: `fixture-${n}`, target: 'fixture/repo#1',
  verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' });
const row = (n) => ({ ...identity(n), case_id: `fixture-case-${n}`, source: 'live',
  attempted_at: now - 20, first_attempted_at: now - 30,
  certification: { status: 'certified', evaluated_sha: 'a'.repeat(40) },
  proposal: { decision: 'reject_blocker', rationale: 'private proposal contents' },
  decision: 'reject_blocker', disposition: 'reject_blocker',
  role_run_id: `role-${n}`, backend_run_id: `backend-${n}`, errors: [],
  packet: { private: 'private evidence contents' } });

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-live-cohort-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const manifest = path.join(root, 'fixed.json');
  const report = path.join(root, 'live.json');
  const cases = Array.from({ length: 20 }, (_, n) => identity(n));
  const saved = { schema: 'adjudicator-live/v1', generated_at: now - 1,
    summary: { advised: 999, graded: 999 }, rows: cases.map((_, n) => row(n)) };
  fs.writeFileSync(manifest, JSON.stringify({ schema_version: 2, cases }));
  const options = { manifest, report, expectedManifestHash: digest(fs.readFileSync(manifest)),
    manifestReference: 'fixture retained membership reference', observedAt: now,
    evidenceClass: 'current_snapshot', evidenceReference: 'fixture native live report reference' };
  const save = () => fs.writeFileSync(report, JSON.stringify(saved));
  const pin = () => { options.expectedManifestHash = digest(fs.readFileSync(manifest)); };
  save();
  return { root, options, cases, saved, save, pin };
}

function execute(root, command, args) {
  const files = ['stdout', 'stderr'].map((name) => path.join(root, name));
  const descriptors = files.map((file) => fs.openSync(file, 'w'));
  let result;
  try {
    result = spawnSync(command, args, { cwd: root, timeout: 30000,
      env: { ...process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: path.join(root, 'state'), ORCH_LOCAL_RUNTIME: path.join(root, 'runtime') },
      stdio: ['ignore', ...descriptors] });
  } finally { descriptors.forEach((fd) => fs.closeSync(fd)); }
  assert.ifError(result.error);
  return { status: result.status, stdout: fs.readFileSync(files[0], 'utf8'),
    stderr: fs.readFileSync(files[1], 'utf8') };
}

function cli(w, extra = []) {
  return execute(w.root, process.execPath, [script, '--manifest', w.options.manifest,
    '--expected-manifest-hash', w.options.expectedManifestHash,
    '--manifest-reference', w.options.manifestReference, '--report', w.options.report,
    '--observed-at', String(w.options.observedAt),
    '--evidence-class', w.options.evidenceClass,
    '--evidence-reference', w.options.evidenceReference, ...extra]);
}

test('real native live producer supplies dated locators for 15 fixed members without acceptance claims', (t) => {
  const w = world(t);
  const generated = execute(w.root, process.env.PYTHON || 'python3', ['-c', `
import json, sys
from contextlib import contextmanager
from pathlib import Path
import adjudicator_live as live
import feedback
root, now = Path(sys.argv[1]), int(sys.argv[2])
(root / 'live.json').unlink()
feedback.DB_PATH = root / 'brain.db'
sha = live._toy_repository(root)
with feedback._conn() as connection:
    for n in range(20):
        connection.execute('INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)',
            (f'fixture-{n}', now - n, 'fixture/repo#1'))
        connection.execute('INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) '
            'VALUES (?,?,?,1)', (f'fixture-{n}', 'NON_PASS', 'PASS'))
@contextmanager
def provider(target, head):
    assert head == sha
    yield root / 'repo'
calls = []
def runner(**kwargs):
    assert kwargs['source'] == 'live'
    n = len(calls)
    calls.append(n)
    decision = 'reject_blocker' if n < 15 else 'needs_more_evidence'
    return {'backend_run_id': f'backend-{n}', 'role_run_id': f'role-{n}',
        'proposal': {'decision': decision, 'rationale': 'private fixture response'},
        'advisory_plan': {'decision': decision}, 'errors': []}
options = dict(path=root / 'live.json', db=feedback.DB_PATH, now=now + 1, env={},
    evidence_reader=lambda _: live._evidence_for(sha), runner=runner, provider=provider,
    snapshot_reader=lambda _: {'number': 1, 'headRefOid': sha, 'body': live.AC_BODY},
    github_reader=lambda _: {'total_count': 0, 'check_runs': []})
first = live.run_live(limit=20, **options)
assert first['attempted'] == 20 and len(calls) == 20
assert live.run_live(limit=20, **options)['attempted'] == 0
assert len(calls) == 20
with feedback._conn() as connection:
    assert connection.execute('SELECT DISTINCT verifier_verdict,adjudicated_verdict FROM outcomes').fetchall() == [('NON_PASS', 'PASS')]
`, w.root, String(now - 2)]);
  assert.equal(generated.status, 0, generated.stderr);
  const before = [w.options.manifest, w.options.report, path.join(w.root, 'brain.db')]
    .map((file) => digest(fs.readFileSync(file)));
  const out = cli(w);
  assert.equal(out.status, 0, out.stderr);
  const result = JSON.parse(out.stdout);
  assert.equal(result.retained_binary_decisions, 15, JSON.stringify(result.cases[0]));
  assert.deepEqual(result.cases.map((item) => item.run_id), w.cases.map((item) => item.run_id));
  assert.equal(result.cases[0].checked_at, new Date(now * 1000).toISOString());
  assert.equal(result.cases[0].retained_report_row.attempted_at,
    new Date((now - 1) * 1000).toISOString());
  assert.match(result.cases[0].retained_report_row.sha256, /^[a-f0-9]{64}$/);
  assert.equal(result.report.sha256, before[1]);
  assert.equal(result.manifest.sha256, before[0]);
  assert.equal(result.cases.filter((item) => item.gaps.includes('retained_abstention')).length, 5);
  for (const flag of ['original_cohort_provenance_verified', 'dispatch_receipts_authenticated',
    'historical_proof_verified', 'operational_acceptance_complete', 'dispatch_authorized']) {
    assert.equal(result[flag], false);
  }
  assert.deepEqual([w.options.manifest, w.options.report, path.join(w.root, 'brain.db')]
    .map((file) => digest(fs.readFileSync(file))), before);
  assert.ok(!out.stdout.includes('private fixture response'));
});

test('later report changes preserve fixed membership, unavailable members and report references', (t) => {
  const w = world(t);
  w.saved.rows = [row(3), row(1), row(2), row(0), row(99)];
  w.save();
  const first = liveCohort(w.options);
  assert.equal(first.retained_binary_decisions, 4);
  assert.equal(first.cases[4].status, 'unavailable');
  assert.deepEqual(first.cases[4].gaps, ['fixed_member_row_missing']);
  assert.equal(first.cases[0].retained_report_row.location, 'rows[3]');
  w.saved.rows.reverse();
  w.saved.rows.push(row(4));
  w.save();
  const second = liveCohort({ ...w.options, observedAt: now + 60 });
  assert.deepEqual(second.cases.map((item) => item.run_id), w.cases.map((item) => item.run_id));
  assert.equal(second.retained_binary_decisions, 5);
  assert.equal(second.cases[0].retained_report_row.location, 'rows[1]');
  assert.notEqual(first.report.sha256, second.report.sha256);
  assert.equal(first.manifest.sha256, second.manifest.sha256);
  assert.ok(!JSON.stringify(second).includes('private'));
});

test('native retries retain fixed membership and date the latest receipt separately from the first attempt', (t) => {
  const w = world(t);
  const generated = execute(w.root, process.env.PYTHON || 'python3', ['-c', `
import json, sys
from contextlib import contextmanager
from pathlib import Path
import adjudicator_live as live
import feedback
root, now = Path(sys.argv[1]), int(sys.argv[2])
(root / 'live.json').unlink()
feedback.DB_PATH = root / 'brain.db'
sha = live._toy_repository(root)
with feedback._conn() as connection:
    for n in range(20):
        connection.execute('INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)',
            (f'fixture-{n}', now - n, 'fixture/repo#1'))
        connection.execute('INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) '
            'VALUES (?,?,?,1)', (f'fixture-{n}', 'NON_PASS', 'PASS'))
@contextmanager
def provider(target, head):
    assert head == sha
    yield root / 'repo'
calls = []
def runner(**kwargs):
    assert kwargs['source'] == 'live'
    n = len(calls)
    calls.append(n)
    result = {'backend_run_id': f'backend-{n}', 'role_run_id': f'role-{n}', 'errors': []}
    if 15 <= n < 20:
        result['errors'] = ['backend exit=1 fixture failure']
    else:
        result['proposal'] = {'decision': 'reject_blocker', 'rationale': 'private fixture response'}
        result['advisory_plan'] = {'decision': 'reject_blocker'}
    return result
options = dict(path=root / 'live.json', db=feedback.DB_PATH, env={},
    evidence_reader=lambda _: live._evidence_for(sha), runner=runner, provider=provider,
    snapshot_reader=lambda _: {'number': 1, 'headRefOid': sha, 'body': live.AC_BODY},
    github_reader=lambda _: {'total_count': 0, 'check_runs': []})
first = live.run_live(limit=20, now=now - 60, **options)
assert first['attempted'] == 20 and first['summary']['advised'] == 15
(root / 'before-retry.json').write_bytes((root / 'live.json').read_bytes())
second = live.run_live(limit=20, now=now - 1, **options)
assert second['attempted'] == 5 and len(calls) == 25
assert live.run_live(limit=20, now=now, **options)['attempted'] == 0
assert len(calls) == 25
assert first['rows'][:15] == second['rows'][:15]
assert all(item['later_truth'] is None for item in second['rows'])
`, w.root, String(now - 1)]);
  assert.equal(generated.status, 0, generated.stderr);
  const before = liveCohort({ ...w.options, report: path.join(w.root, 'before-retry.json') });
  const reportBytes = fs.readFileSync(w.options.report);
  const manifestBytes = fs.readFileSync(w.options.manifest);
  const result = JSON.parse(cli(w).stdout);
  assert.equal(before.retained_binary_decisions, 15);
  assert.equal(result.retained_binary_decisions, 20);
  assert.equal(result.manifest.sha256, before.manifest.sha256);
  assert.deepEqual(result.cases.map((item) => item.run_id), w.cases.map((item) => item.run_id));
  for (const [index, item] of result.cases.entries()) {
    const receipt = item.retained_report_row;
    assert.equal(receipt.attempts, index < 15 ? 1 : 2);
    assert.equal(receipt.first_attempted_at, new Date((now - 61) * 1000).toISOString());
    assert.equal(receipt.attempted_at,
      new Date((index < 15 ? now - 61 : now - 2) * 1000).toISOString());
    assert.match(receipt.evaluated_sha, /^[a-f0-9]{40}$/);
    if (index < 15) assert.deepEqual(receipt, before.cases[index].retained_report_row);
    else {
      assert.notEqual(receipt.role_run_id, before.cases[index].retained_report_row.role_run_id);
      assert.notEqual(receipt.backend_run_id, before.cases[index].retained_report_row.backend_run_id);
    }
  }
  assert.equal(result.dispatch_receipts_authenticated, false);
  assert.equal(result.operational_acceptance_complete, false);
  assert.deepEqual(fs.readFileSync(w.options.report), reportBytes);
  assert.deepEqual(fs.readFileSync(w.options.manifest), manifestBytes);
  assert.ok(!JSON.stringify(result).includes('private'));
});

test('malformed saved retry counts block decisions without inventing attempt provenance', (t) => {
  const w = world(t);
  const invalid = [null, 0, -1, 1.5, '2', true, Number.MAX_SAFE_INTEGER + 1];
  invalid.forEach((value, index) => { w.saved.rows[index].attempts = value; });
  w.saved.rows[7].attempts = 2;
  delete w.saved.rows[7].first_attempted_at;
  w.save();
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 12);
  for (const item of result.cases.slice(0, 8)) {
    assert.ok(item.gaps.includes('attempt_provenance_invalid'));
    assert.equal(item.retained_report_row.attempts, null);
  }
  assert.equal(result.cases[7].retained_report_row.first_attempted_at, null);
  // Older reports without a saved counter do not acquire an invented count.
  assert.equal(result.cases[8].retained_report_row.attempts, null);
  assert.equal(result.cases[8].status, 'retained_binary_decision');
});

test('a retained role-write failure cannot supply the fifteenth dated decision', (t) => {
  const w = world(t);
  for (const item of w.saved.rows.slice(15)) {
    item.decision = item.proposal.decision = item.disposition = 'needs_more_evidence';
  }
  w.save();
  assert.equal(liveCohort(w.options).retained_binary_decisions, 15);
  // Even a saved locator cannot contradict an explicit failed Brain write.
  w.saved.rows[14].role_record_error = 'private database write failure';
  w.save();
  const before = [w.options.manifest, w.options.report].map((file) => fs.readFileSync(file));
  const out = cli(w);
  assert.equal(out.status, 0, out.stderr);
  const result = JSON.parse(out.stdout);
  assert.equal(result.retained_binary_decisions, 14);
  assert.deepEqual(result.cases[14].gaps, ['role_record_write_failed']);
  assert.equal(result.cases[14].status, 'blocked');
  assert.equal(result.cases[14].retained_report_row.decision, 'reject_blocker');
  assert.equal(result.cases[14].retained_report_row.role_run_id, 'role-14');
  assert.equal(result.dispatch_receipts_authenticated, false);
  assert.equal(result.operational_acceptance_complete, false);
  assert.ok(!out.stdout.includes('private'));
  assert.deepEqual([w.options.manifest, w.options.report].map((file) => fs.readFileSync(file)), before);
  // A later retained report can clear the failure without changing the paid
  // decision, its attempt date or backend identity. This observer only reads it.
  w.saved.rows[14].role_record_error = null;
  w.save();
  const recovered = liveCohort(w.options);
  assert.equal(recovered.retained_binary_decisions, 15);
  assert.deepEqual(recovered.cases[14].gaps, []);
  for (const field of ['decision', 'attempted_at', 'role_run_id', 'backend_run_id']) {
    assert.equal(recovered.cases[14].retained_report_row[field],
      result.cases[14].retained_report_row[field]);
  }
});

test('native paid decisions retain the role persistence break without redispatch or fabricated receipts', (t) => {
  const w = world(t);
  const generated = execute(w.root, process.env.PYTHON || 'python3', ['-c', `
import sys
from contextlib import contextmanager
from pathlib import Path
import adjudicator_live as live
import feedback
root, now = Path(sys.argv[1]), int(sys.argv[2])
(root / 'live.json').unlink()
feedback.DB_PATH = root / 'brain.db'
sha = live._toy_repository(root)
with feedback._conn() as connection:
    connection.execute('INSERT INTO runs(run_id,ts,target) VALUES (?,?,?)',
        ('fixture-0', now, 'fixture/repo#1'))
    connection.execute('INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) '
        'VALUES (?,?,?,1)', ('fixture-0', 'NON_PASS', 'PASS'))
@contextmanager
def provider(target, head):
    assert head == sha
    yield root / 'repo'
calls = []
def runner(**kwargs):
    calls.append(kwargs['source'])
    return {'backend_run_id': 'paid-backend', 'role_run_id': None,
        'role_record_error': 'private database write failure',
        'role_record': {'run_id': 'unwritten-role', 'proposal': 'private role payload'},
        'proposal': {'decision': 'reject_blocker', 'rationale': 'private paid response'},
        'advisory_plan': {'decision': 'reject_blocker'}, 'errors': []}
options = dict(path=root / 'live.json', db=feedback.DB_PATH, now=now + 1, env={},
    evidence_reader=lambda _: live._evidence_for(sha), runner=runner, provider=provider,
    snapshot_reader=lambda _: {'number': 1, 'headRefOid': sha, 'body': live.AC_BODY},
    github_reader=lambda _: {'total_count': 0, 'check_runs': []})
first = live.run_live(**options)
assert first['attempted'] == 1 and first['rows'][0]['decision'] == 'reject_blocker'
assert first['rows'][0]['role_record_error'] == 'private database write failure'
assert live.run_live(**options)['attempted'] == 0 and calls == ['live']
`, w.root, String(now - 2)]);
  assert.equal(generated.status, 0, generated.stderr);
  const inputs = [w.options.manifest, w.options.report, path.join(w.root, 'brain.db')];
  const before = inputs.map((file) => fs.readFileSync(file));
  const out = cli(w);
  assert.equal(out.status, 0, out.stderr);
  const result = JSON.parse(out.stdout);
  const item = result.cases[0];
  assert.equal(result.retained_binary_decisions, 0);
  assert.equal(item.status, 'blocked');
  assert.deepEqual(item.gaps, ['role_record_write_failed', 'role_receipt_locator_missing']);
  assert.equal(item.retained_report_row.decision, 'reject_blocker');
  assert.equal(item.retained_report_row.role_run_id, null);
  assert.equal(item.retained_report_row.backend_run_id, 'paid-backend');
  assert.equal(item.retained_report_row.attempts, 1);
  assert.equal(item.retained_report_row.attempted_at, new Date((now - 1) * 1000).toISOString());
  assert.ok(result.cases.slice(1).every((entry) => entry.gaps[0] === 'fixed_member_row_missing'));
  assert.equal(result.operational_acceptance_complete, false);
  assert.ok(!out.stdout.includes('private'));
  assert.ok(!out.stdout.includes('unwritten-role'));
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
});

test('historical and current report sources stay distinct from the date of a new read', (t) => {
  const w = world(t);
  const historicalAt = Date.parse('2026-10-18T12:00:00Z') / 1000;
  w.saved.generated_at = historicalAt;
  for (const item of w.saved.rows) {
    item.attempted_at = historicalAt - 1;
    item.first_attempted_at = historicalAt - 2;
  }
  w.save();
  w.options.evidenceClass = 'historical_snapshot';
  w.options.evidenceReference = 'fixture retained October 18 report revision';
  const historical = JSON.parse(cli(w).stdout);
  w.options.evidenceClass = 'current_snapshot';
  w.options.evidenceReference = 'fixture current live writer report';
  w.options.observedAt += 60;
  const current = JSON.parse(cli(w).stdout);
  assert.equal(historical.report.evidence_class, 'historical_snapshot');
  assert.equal(historical.report.evidence_reference, 'fixture retained October 18 report revision');
  assert.equal(current.report.evidence_class, 'current_snapshot');
  assert.equal(current.report.evidence_reference, 'fixture current live writer report');
  assert.equal(historical.report.sha256, current.report.sha256);
  assert.equal(historical.report.byte_length, fs.statSync(w.options.report).size);
  assert.equal(current.report.byte_length, historical.report.byte_length);
  assert.equal(historical.manifest.sha256, current.manifest.sha256);
  assert.equal(historical.report.generated_at, new Date(historicalAt * 1000).toISOString());
  assert.equal(current.report.generated_at, historical.report.generated_at);
  assert.notEqual(current.checked_at, historical.checked_at);
  for (const result of [historical, current]) {
    assert.equal(result.retained_binary_decisions, 20);
    assert.ok(result.cases.every((item) => item.checked_at === result.checked_at));
    assert.equal(result.historical_proof_verified, false);
    assert.equal(result.dispatch_receipts_authenticated, false);
    assert.equal(result.original_cohort_provenance_verified, false);
    assert.equal(result.operational_acceptance_complete, false);
  }
});

test('missing historical report retains its source reference without supplying October 18 proof', (t) => {
  const w = world(t);
  fs.unlinkSync(w.options.report);
  w.options.evidenceClass = 'historical_snapshot';
  w.options.evidenceReference = 'fixture unavailable October 18 report';
  const out = cli(w);
  assert.equal(out.status, 2, out.stderr);
  const result = JSON.parse(out.stdout);
  assert.equal(result.report.evidence_class, 'historical_snapshot');
  assert.equal(result.report.evidence_reference, w.options.evidenceReference);
  assert.equal(result.report.status, 'unavailable');
  assert.equal(result.report.byte_length, null);
  assert.equal(result.report.generated_at, null);
  assert.equal(result.retained_binary_decisions, 0);
  assert.equal(result.historical_proof_verified, false);
  assert.ok(result.cases.every((item) => item.gaps[0] === 'live_report_unavailable'));
  assert.equal(fs.existsSync(w.options.report), false);
});

test('unattributed or unsupported report sources fail before reading observation inputs', (t) => {
  const w = world(t);
  const read = fs.readFileSync;
  fs.readFileSync = () => { throw new Error('unexpected input read'); };
  try {
    for (const evidenceClass of [undefined, '', 'mirror_sync', 'dispatch_receipt',
      'historical_snapshot private payload']) {
      assert.throws(() => liveCohort({ ...w.options, evidenceClass }), /evidenceClass/);
    }
    for (const evidenceReference of [undefined, null, '', '  ', 123]) {
      assert.throws(() => liveCohort({ ...w.options, evidenceReference }), /evidenceReference/);
    }
  } finally { fs.readFileSync = read; }
  const omitted = execute(w.root, process.execPath, [script,
    '--manifest', w.options.manifest,
    '--expected-manifest-hash', w.options.expectedManifestHash,
    '--manifest-reference', w.options.manifestReference,
    '--report', w.options.report, '--observed-at', String(now)]);
  assert.equal(omitted.status, 1);
  assert.equal(omitted.stdout, '');
  w.options.evidenceClass = 'historical_snapshot private payload';
  const invalid = cli(w);
  assert.equal(invalid.status, 1);
  assert.equal(invalid.stdout, '');
  assert.ok(!invalid.stderr.includes('private payload'));
});

test('changed identities and ambiguous duplicates cannot lend a decision to a fixed member', (t) => {
  const w = world(t);
  w.saved.rows[0].target = 'fixture/repo#2';
  w.saved.rows[1].verifier_verdict = 'FAIL';
  w.saved.rows.push(row(2));
  w.save();
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 17);
  assert.deepEqual(result.cases.slice(0, 3).map((item) => item.gaps),
    [['live_target_mismatch'], ['fixed_member_row_missing'], ['ambiguous_live_rows']]);
  assert.ok(result.cases.slice(0, 3).every((item) => item.retained_report_row === null));
});

test('conflicting targets for one native dispute cannot supply the fifteenth decision', (t) => {
  const w = world(t);
  for (const item of w.saved.rows.slice(15)) {
    item.decision = item.proposal.decision = item.disposition = 'needs_more_evidence';
  }
  w.save();
  assert.equal(liveCohort(w.options).retained_binary_decisions, 15);
  // run_live indexes cases by run + verdict pair, independently of target.
  // Distinct receipt locators must not hide a second target for that same case.
  w.saved.rows.push({ ...row(0), target: 'fixture/other#2',
    role_run_id: 'conflicting-role', backend_run_id: 'conflicting-backend' });
  for (const reverse of [false, true]) {
    if (reverse) w.saved.rows.reverse();
    w.save();
    const before = [w.options.manifest, w.options.report].map((file) => fs.readFileSync(file));
    const out = cli(w);
    assert.equal(out.status, 0, out.stderr);
    const result = JSON.parse(out.stdout);
    assert.equal(result.retained_binary_decisions, 14);
    assert.equal(result.cases[0].status, 'blocked');
    assert.deepEqual(result.cases[0].gaps, ['live_target_mismatch']);
    assert.equal(result.cases[0].retained_report_row, null);
    assert.deepEqual(result.cases.map((item) => item.run_id), w.cases.map((item) => item.run_id));
    assert.equal(result.operational_acceptance_complete, false);
    assert.ok(!out.stdout.includes('conflicting-role'));
    assert.ok(!out.stdout.includes('conflicting-backend'));
    assert.deepEqual([w.options.manifest, w.options.report].map((file) => fs.readFileSync(file)), before);
  }
});

test('owner-excluded target aliases do not expose execution fields or affect allowed members', (t) => {
  const w = world(t);
  const excluded = { ...identity(0), target: 'STRANSKE/Collab-Admin#2' };
  w.saved.rows.push(excluded);
  w.save();
  const read = fs.readFileSync;
  const parse = JSON.parse;
  // This read trap exercises the in-memory report after JSON parsing. Excluded
  // rows must be discarded before accessing their dispute or receipt fields.
  JSON.parse = function (bytes, ...args) {
    const parsed = parse(bytes, ...args);
    if (parsed?.schema === 'adjudicator-live/v1') {
      for (const field of ['run_id', 'role_run_id', 'backend_run_id']) {
        Object.defineProperty(parsed.rows.at(-1), field, {
          get() { throw new Error('excluded execution evidence accessed'); },
        });
      }
    }
    return parsed;
  };
  try {
    const before = read(w.options.report);
    const result = liveCohort(w.options);
    assert.equal(result.retained_binary_decisions, 20);
    assert.deepEqual(read(w.options.report), before);
  } finally { JSON.parse = parse; }
});

test('saved summary, retrospective decisions and incomplete or conflicting receipts never count', (t) => {
  const w = world(t);
  const changes = [
    (item) => { item.source = 'retrospective'; },
    (item) => { item.decision = item.proposal.decision = 'needs_more_evidence'; },
    (item) => { item.proposal.decision = 'uphold_blocker'; },
    (item) => { item.disposition = 'needs_more_evidence'; },
    (item) => { item.certification.status = 'uncertified'; },
    (item) => { item.certification.evaluated_sha = 'branch-name'; },
    (item) => { delete item.attempted_at; },
    (item) => { item.attempted_at = now + 1; },
    (item) => { item.first_attempted_at = now; },
    (item) => { delete item.role_run_id; },
    (item) => { item.backend_run_id = ''; },
    (item) => { item.errors = ['private provider error']; },
    (item) => { item.errors = 'private malformed error'; },
    (item) => { item.error = 'private error'; },
    (item) => { item.backend_run_id = 'backend-15'; },
  ];
  changes.forEach((change, index) => change(w.saved.rows[index]));
  w.save();
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 4);
  assert.ok(result.cases.slice(0, 16).every((item) => item.status === 'blocked' && item.gaps.length));
  assert.deepEqual(result.cases[15].gaps, ['backend_receipt_locator_reused']);
  assert.ok(!JSON.stringify(result).includes('private'));
});

test('missing report preserves all 20 obligations without creating a report or database', (t) => {
  const w = world(t);
  fs.unlinkSync(w.options.report);
  const before = fs.readdirSync(w.root);
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 0);
  assert.equal(result.report.status, 'unavailable');
  assert.equal(result.report.sha256, null);
  assert.equal(result.cases.length, 20);
  assert.ok(result.cases.every((item) => item.gaps[0] === 'live_report_unavailable'));
  assert.deepEqual(fs.readdirSync(w.root), before);
  assert.equal(cli(w).status, 2);
  assert.equal(fs.existsSync(w.options.report), false);
});

test('a shared role locator cannot create two observed decisions', (t) => {
  const w = world(t);
  w.saved.rows[0].role_run_id = w.saved.rows[1].role_run_id;
  w.save();
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 18);
  assert.deepEqual(result.cases.slice(0, 2).map((item) => item.gaps),
    [['role_receipt_locator_reused'], ['role_receipt_locator_reused']]);
});

test('a second dispute on the same run remains a separate fixed obligation', (t) => {
  const w = world(t);
  w.cases[1] = { ...w.cases[0], adjudicated_verdict: 'FAIL' };
  w.saved.rows[1] = { ...w.saved.rows[1], ...w.cases[1] };
  fs.writeFileSync(w.options.manifest, JSON.stringify({ schema_version: 2, cases: w.cases }));
  w.pin();
  w.save();
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 20);
  assert.equal(result.cases[0].adjudicated_verdict, 'PASS');
  assert.equal(result.cases[1].adjudicated_verdict, 'FAIL');
  assert.notEqual(result.cases[0].retained_report_row.location, result.cases[1].retained_report_row.location);
});

test('input changes during observation abort rather than publish mixed dates or membership', (t) => {
  const w = world(t);
  const read = fs.readFileSync;
  const exists = fs.existsSync;
  for (const file of [w.options.manifest, w.options.report]) {
    const original = read(file);
    let reads = 0;
    fs.readFileSync = function (selected, ...args) {
      if (selected === file && ++reads === 2) fs.appendFileSync(file, '\n');
      return read(selected, ...args);
    };
    try { assert.throws(() => liveCohort(w.options), /changed during observation/); }
    finally { fs.readFileSync = read; fs.writeFileSync(file, original); }
  }
  fs.unlinkSync(w.options.report);
  fs.existsSync = function (file) {
    if (file === w.options.report) w.save();
    return exists(file);
  };
  try { assert.throws(() => liveCohort(w.options), /changed during observation/); }
  finally { fs.existsSync = exists; }
});

test('manifest pin, size, dispute identities and report schema fail closed', (t) => {
  const w = world(t);
  assert.throws(() => liveCohort({ ...w.options, expectedManifestHash: 'a'.repeat(64) }), /hash mismatch/);
  assert.throws(() => liveCohort({ ...w.options, manifestReference: '' }), /reference/);
  for (const cases of [w.cases.slice(1), [...w.cases, identity(99)],
    [...w.cases.slice(1), w.cases[1]], [{ ...w.cases[0], verifier_verdict: 'PASS' }, ...w.cases.slice(1)],
    [{ ...w.cases[0], run_id: '' }, ...w.cases.slice(1)]]) {
    fs.writeFileSync(w.options.manifest, JSON.stringify({ schema_version: 2, cases }));
    w.pin();
    assert.throws(() => liveCohort(w.options));
  }
  fs.writeFileSync(w.options.manifest, JSON.stringify({ schema_version: 1, cases: w.cases }));
  w.pin();
  assert.throws(() => liveCohort(w.options), /schema_version 2/);
  fs.writeFileSync(w.options.manifest, JSON.stringify({ schema_version: 2, cases: w.cases }));
  w.pin();
  for (const saved of [{ rows: w.saved.rows }, { schema: 'adjudicator-live/v1', rows: [null] }]) {
    fs.writeFileSync(w.options.report, JSON.stringify(saved));
    assert.throws(() => liveCohort(w.options), /native adjudicator-live/);
  }
});

test('invalid dates cannot convert current observations to historical proof', (t) => {
  const w = world(t);
  for (const value of [undefined, NaN, -1, true, '1793880000', 1.5, Number.MAX_SAFE_INTEGER]) {
    assert.throws(() => liveCohort({ ...w.options, observedAt: value }), /timestamp/);
  }
  for (const value of [now + 1, '2026-10-18', null]) {
    w.saved.generated_at = value;
    w.save();
    const result = liveCohort(w.options);
    assert.equal(result.retained_binary_decisions, 0);
    assert.ok(result.cases.every((item) => item.gaps.includes('attempt_or_report_date_invalid')));
    assert.equal(result.historical_proof_verified, false);
  }
});

test('owner exclusions retain identity obligations without execution evidence', (t) => {
  const w = world(t);
  w.cases[0].target = 'STRANSKE/Collab-Admin#1';
  fs.writeFileSync(w.options.manifest, JSON.stringify({ schema_version: 2, cases: w.cases }));
  w.pin();
  w.saved.rows[0].target = w.cases[0].target;
  w.saved.rows[0].role_run_id = 'private excluded role';
  w.save();
  const result = liveCohort(w.options);
  assert.equal(result.retained_binary_decisions, 19);
  assert.deepEqual(result.cases[0].gaps, ['owner_excluded_repository']);
  assert.equal(result.cases[0].retained_report_row, null);
  assert.ok(!JSON.stringify(result).includes('private excluded role'));
});

test('CLI rejects dispatch and malformed input without printing private payloads', (t) => {
  const w = world(t);
  assert.equal(cli(w, ['--dispatch']).status, 1);
  fs.writeFileSync(w.options.report, '{private broken JSON');
  const result = cli(w);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.ok(!result.stderr.includes('private'));
});
