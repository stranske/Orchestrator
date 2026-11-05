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
    [['fixed_member_row_missing'], ['fixed_member_row_missing'], ['ambiguous_live_rows']]);
  assert.ok(result.cases.slice(0, 3).every((item) => item.retained_report_row === null));
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
