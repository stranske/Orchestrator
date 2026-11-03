'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { inventory, reportInventory } = require('../scripts/export_adjudicator_inventory');

const repo = path.resolve(__dirname, '..');
const script = path.join(repo, 'scripts/export_adjudicator_inventory.js');
const now = 2000000000;
const cutoff = now - 90 * 86400;

function retainedReport(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-retained-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const report = path.join(root, 'historical.json');
  const saved = { generated_at: now - 86400, population: 40,
    summary: { cases: 58, graded: 1, advice_graded: 2 },
    native_publication: { source_report_sha256: 'retained-claim' },
    rows: [
      { case_id: 'case-a', target: 'owner/repo#1', decision: 'needs_more_evidence',
        backend_run_id: 'primary', raw_output: 'private backend response',
        error: 'private error detail', native_projection: { graded: 29 },
        retry_attempts: [{ backend_run_id: 'prior',
          retry_attempts: [{ backend_run_id: 'oldest' }] }] },
      { case_id: 'case-b', target: 'owner/repo#2', decision: 'reject_blocker',
        later_truth: 'PASS', backend_run_id: 'prior' },
    ] };
  fs.writeFileSync(report, JSON.stringify(saved));
  return { root, saved, options: { report, observedAt: now,
    evidenceClass: 'historical_snapshot', evidenceReference: 'retained-source-reference' } };
}

test('report recovery preserves ordered rows, recursive paid history and conflicting counts', (t) => {
  const { root, options } = retainedReport(t);
  const before = fs.readFileSync(options.report);
  const files = fs.readdirSync(root);
  const result = reportInventory(options);
  assert.deepEqual(result.cases.map(({ case_id }) => case_id), ['case-a', 'case-b']);
  assert.equal(result.retained_population, 40);
  assert.equal(result.case_count, 2, 'derive rows from evidence, never retained population/summary');
  assert.deepEqual(result.retained_summary, { cases: 58, graded: 1, advice_graded: 2 });
  assert.equal(result.report_sha256, createHash('sha256').update(before).digest('hex'));
  assert.equal(result.report_byte_length, before.length);
  assert.equal(result.retained_generated_at, now - 86400);
  assert.equal(result.observed_at, now, 'recovery time is separate from the retained observation');
  assert.deepEqual(result.paid_attempts.map((item) => item.backend_run_id),
    ['primary', 'prior', 'oldest', 'prior']);
  assert.deepEqual(result.paid_identity_summary, { records: 4, unique: 3, repeated: [
    { backend_run_id: 'prior', locations: ['rows[0].retry_attempts[0]', 'rows[1]'] },
  ] });
  assert.equal(result.cases[0].retained_error_present, true);
  assert.equal(result.cases[1].retained_error_present, false);
  assert.equal(result.cases[1].retained_later_truth, 'PASS');
  assert.equal(result.native_publication_present, true);
  assert.equal(result.cases[0].native_projection_present, true);
  assert.equal(result.receipt_authority, 'not_authenticated_by_inventory');
  assert.equal(result.requested_cohort_verified, false);
  assert.equal(result.dispatch_authorized, false);
  assert.equal(Object.hasOwn(result, 'graded'), false, 'saved claims cannot create fresh grades');
  assert.ok(result.evidence_gaps.includes('native_journal_not_observed'));
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.deepEqual(fs.readFileSync(options.report), before);
  assert.deepEqual(fs.readdirSync(root), files, 'inventory creates no runtime files');
});

test('report recovery rejects missing input without initializing runtime state', (t) => {
  const { root, options } = retainedReport(t);
  const missing = path.join(root, 'missing', 'report.json');
  assert.throws(() => reportInventory({ ...options, report: missing }), /ENOENT/);
  assert.equal(fs.existsSync(path.dirname(missing)), false);
});

test('report recovery refuses malformed and ambiguous retained identities', (t) => {
  const { saved, options } = retainedReport(t);
  for (const change of [
    (value) => { value.rows = {}; },
    (value) => { value.rows[0] = null; },
    (value) => { value.rows[1].case_id = 'case-a'; },
    (value) => { value.rows[0].target = ''; },
    (value) => { value.rows[0].retry_attempts = {}; },
    (value) => { value.rows[0].retry_attempts[0].retry_attempts = [null]; },
    (value) => { value.rows[0].backend_run_id = {}; },
  ]) {
    const malformed = structuredClone(saved);
    change(malformed);
    fs.writeFileSync(options.report, JSON.stringify(malformed));
    assert.throws(() => reportInventory(options), /rows|case_id|retry_attempts|attempt|backend/);
  }
});

test('report recovery requires explicit attribution and a valid check time', (t) => {
  const { options } = retainedReport(t);
  for (const overrides of [{ observedAt: NaN }, { observedAt: -1 },
    { evidenceClass: 'authenticated' }, { evidenceReference: '' }]) {
    assert.throws(() => reportInventory({ ...options, ...overrides }));
  }
});

test('report recovery aborts if the input changes during observation', (t) => {
  const { options } = retainedReport(t);
  const read = fs.readFileSync;
  let observations = 0;
  fs.readFileSync = function observed(file, ...args) {
    if (file === options.report && ++observations === 2) {
      fs.writeFileSync(file, '{"rows":[]}');
    }
    return read(file, ...args);
  };
  try {
    assert.throws(() => reportInventory(options), /changed during inventory/);
  } finally {
    fs.readFileSync = read;
  }
});

test('report CLI never combines historical recovery with dispatch or current Brain selection', (t) => {
  const { root, options } = retainedReport(t);
  const args = [script, '--report', options.report, '--observed-at', String(now),
    '--evidence-class', options.evidenceClass, '--evidence-reference', options.evidenceReference];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), reportInventory(options));
  for (const extra of [['--dispatch'], ['--db', 'absent.db'], ['--manifest', 'absent.json'],
    ['--days', '90'], ['--expected-count', '58']]) {
    const refused = execute(root, process.execPath, [...args, ...extra]);
    assert.equal(refused.status, 1, refused.stderr);
    assert.equal(refused.stdout, '', 'refusal must not publish a partial inventory');
  }
});

function nativeJournal(t) {
  const fixture = retainedReport(t);
  const journal = path.join(fixture.root, 'native.sqlite3');
  const connection = new DatabaseSync(journal);
  connection.exec(`PRAGMA application_id=1313097550;
    CREATE TABLE native_attempts (attempt_id TEXT PRIMARY KEY, case_id TEXT,
      material_revision TEXT, case_digest TEXT, state TEXT, backend_run_id TEXT,
      report_sha256 TEXT, created_at INTEGER, updated_at INTEGER,
      raw_response TEXT, validated_json TEXT);`);
  const insert = connection.prepare('INSERT INTO native_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)');
  for (const [id, state, backend, raw, validation] of [
    ['a', 'reserved', null, null, null],
    ['b', 'in_flight', null, null, null],
    ['c', 'in_flight', 'started-provider', null, null],
    ['d', 'raw_persisted', 'primary', 'private raw response', null],
    ['e', 'validated', 'native-provider', 'private raw response', '{"private":"proposal"}'],
    ['f', 'unrecognized', null, null, null],
  ]) insert.run(id, 'case-a', `revision-${id}`, 'digest', state, backend, 'source-hash',
    now - 1, now, raw, validation);
  connection.close();
  return { ...fixture, journal, options: { ...fixture.options, journal } };
}

test('pre-dispatch inventory reconciles report and native locators without exposing payloads', (t) => {
  const { root, journal, options } = nativeJournal(t);
  const before = fs.readFileSync(journal);
  const files = fs.readdirSync(root);
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.generic_retry_route, 'refused_native_canonical_publication');
  assert.equal(result.native_journal.status, 'available');
  assert.equal(result.native_journal.receipt_authority, 'not_authenticated_by_inventory');
  assert.deepEqual(result.paid_identity_reconciliation, {
    report_unique: 3, journal_unique: 3, union_unique: 5,
    report_only: ['prior', 'oldest'], journal_only: ['started-provider', 'native-provider'],
    unresolved_without_backend_id: ['b'],
  });
  const attempts = result.native_journal.attempts;
  assert.deepEqual(attempts.map((item) => item.attempt_id), ['a', 'b', 'c', 'd', 'e', 'f']);
  assert.equal(attempts[0].recovery_step, 'producer_must_prove_invocation_never_started');
  assert.equal(attempts[1].recovery_step, 'UNKNOWN_recover_provider_receipt_before_any_retry');
  assert.equal(attempts[3].recovery_step, 'resume_native_validation_without_redispatch');
  assert.equal(attempts[4].recovery_step, 'reauthenticate_with_native_projection_before_grading');
  assert.equal(attempts[5].recovery_step, 'unrecognized_state_requires_native_review');
  assert.equal(attempts[4].raw_receipt_present, true);
  assert.equal(attempts[4].validation_present, true);
  assert.equal(attempts[1].raw_receipt_present, false);
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.equal(result.live_writer_ownership, 'unverified');
  assert.equal(result.dispatch_authorized, false);
  assert.ok(result.stop_reasons.includes('unresolved_native_attempts_require_recovery'));
  assert.deepEqual(fs.readFileSync(journal), before);
  assert.deepEqual(fs.readdirSync(root), files, 'read-only observation creates no lock or runtime');
  assert.ok(!reportInventory(options).evidence_gaps.includes('native_journal_not_observed'));
  assert.ok(reportInventory(options).evidence_gaps.includes('native_journal_receipts_not_authenticated'));
});

test('all validated native rows still require authentication, independent truth and live writer proof', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec("DELETE FROM native_attempts WHERE state != 'validated'");
  connection.close();
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.dispatch_authorized, false);
  assert.ok(!result.stop_reasons.includes('unresolved_native_attempts_require_recovery'));
  assert.ok(result.stop_reasons.includes('live_writer_ownership_unverified'));
  assert.ok(result.stop_reasons.includes('native_receipt_authentication_and_independent_truth_required'));
  assert.equal(Object.hasOwn(result, 'graded'), false);
});

test('missing native evidence remains unavailable and never means zero attempts', (t) => {
  const { root, options } = retainedReport(t);
  for (const journal of [undefined, path.join(root, 'absent', 'native.sqlite3')]) {
    const result = reportInventory({ ...options, journal }).pre_dispatch;
    assert.equal(result.native_journal.status, 'unavailable');
    assert.equal(result.native_journal.attempts, null);
    assert.equal(result.paid_identity_reconciliation.journal_unique, null);
    assert.equal(result.paid_identity_reconciliation.union_unique, null);
    assert.equal(result.dispatch_authorized, false);
  }
  assert.equal(fs.existsSync(path.join(root, 'absent')), false);
});

test('an observed empty journal cannot establish writer ownership or authorize spending', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec('DELETE FROM native_attempts');
  connection.close();
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.native_journal.status, 'available');
  assert.deepEqual(result.native_journal.attempts, []);
  assert.equal(result.paid_identity_reconciliation.journal_unique, 0);
  assert.equal(result.paid_identity_reconciliation.union_unique, 3);
  assert.equal(result.live_writer_ownership, 'unverified');
  assert.equal(result.dispatch_authorized, false);
});

test('an active journal writer does not make a read-only snapshot claim ownership', (t) => {
  const { journal, options } = nativeJournal(t);
  const connection = new DatabaseSync(journal);
  connection.exec("BEGIN IMMEDIATE; DELETE FROM native_attempts");
  try {
    const result = reportInventory(options).pre_dispatch;
    assert.equal(result.native_journal.status, 'available');
    assert.equal(result.native_journal.attempts.length, 6, 'uncommitted changes are invisible');
    assert.equal(result.live_writer_ownership, 'unverified');
    assert.equal(result.dispatch_authorized, false);
  } finally {
    connection.exec('ROLLBACK');
    connection.close();
  }
});

test('foreign and incomplete journals are refused without schema repair', (t) => {
  const { root, options } = retainedReport(t);
  const journal = path.join(root, 'foreign.sqlite3');
  const connection = new DatabaseSync(journal);
  connection.exec('CREATE TABLE unrelated (id TEXT)');
  connection.close();
  for (const reason of ['non_native_journal', 'native_journal_unreadable_or_schema_unavailable']) {
    const before = fs.readFileSync(journal);
    const result = reportInventory({ ...options, journal }).pre_dispatch.native_journal;
    assert.equal(result.status, 'unavailable');
    assert.equal(result.reason, reason);
    assert.equal(result.attempts, null);
    assert.deepEqual(fs.readFileSync(journal), before);
    const db = new DatabaseSync(journal);
    db.exec('PRAGMA application_id=1313097550');
    db.close();
  }
});

test('report without native marker still cannot authorize generic retry', (t) => {
  const { saved, options } = retainedReport(t);
  delete saved.native_publication;
  fs.writeFileSync(options.report, JSON.stringify(saved));
  const result = reportInventory(options).pre_dispatch;
  assert.equal(result.generic_retry_route, 'not_authorized_by_inventory');
  assert.equal(result.dispatch_authorized, false);
});

test('journal CLI stays read-only and requires explicit report attribution', (t) => {
  const { root, journal, options } = nativeJournal(t);
  const args = [script, '--report', options.report, '--journal', journal,
    '--observed-at', String(now), '--evidence-class', options.evidenceClass,
    '--evidence-reference', options.evidenceReference];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), reportInventory(options));
  const refused = execute(root, process.execPath, [script, '--db', 'absent.db',
    '--journal', journal, '--observed-at', String(now)]);
  assert.equal(refused.status, 1);
  assert.equal(refused.stdout, '');
  assert.match(refused.stderr, /require --report/);
});

function world(t, schema = true) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-inventory-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const db = path.join(root, 'brain.db');
  const connection = new DatabaseSync(db);
  if (schema) connection.exec(`
    CREATE TABLE runs (run_id TEXT PRIMARY KEY, target TEXT, ts INTEGER);
    CREATE TABLE outcomes (run_id TEXT PRIMARY KEY, verifier_verdict TEXT,
      adjudicated_verdict TEXT, merged INTEGER, durability TEXT,
      durability_checked_ts INTEGER);
  `);
  connection.close();
  return { root, db };
}

function seed(db, rows) {
  const connection = new DatabaseSync(db);
  try {
    const run = connection.prepare('INSERT INTO runs VALUES (?, ?, ?)');
    const outcome = connection.prepare('INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?)');
    for (const [id, ts, verifier, adjudicated, merged = 1] of rows) {
      run.run(id, 'owner/repo#1', ts);
      if (verifier !== undefined) outcome.run(id, verifier, adjudicated, merged, 'durable', now);
    }
  } finally {
    connection.close();
  }
}

function execute(root, binary, args) {
  const out = path.join(root, 'stdout');
  const err = path.join(root, 'stderr');
  const stdout = fs.openSync(out, 'w');
  const stderr = fs.openSync(err, 'w');
  let result;
  try {
    result = spawnSync(binary, args, {
      cwd: repo, timeout: 10000,
      env: { ...process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['ignore', stdout, stderr],
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  assert.ifError(result.error);
  return { status: result.status, stdout: fs.readFileSync(out, 'utf8'),
    stderr: fs.readFileSync(err, 'utf8') };
}

test('inventory exports every synthetic dispute and every exclusion without writes', (t) => {
  const { root, db } = world(t);
  const eligible = Array.from({ length: 71 }, (_, i) => [
    `dispute-${String(i).padStart(2, '0')}`, i === 0 ? cutoff : now,
    i < 57 ? 'NON_PASS' : i < 66 ? 'FAIL_HOLLOW' : 'PASS',
    i < 57 ? 'PASS' : 'FAIL', i < 57 ? 1 : 0,
  ]).reverse();
  seed(db, [...eligible,
    ['old', cutoff - 1, 'NON_PASS', 'PASS'],
    ['agree', now, 'PASS', 'PASS'],
    ['no-verifier', now, null, 'PASS'],
    ['no-adjudication', now, 'NON_PASS', null],
    ['both-missing', now, null, null],
    ['no-outcome', now],
    ['old-agreement', cutoff - 1, 'PASS', 'PASS'],
    ['no-timestamp', null, 'NON_PASS', 'PASS'],
  ]);
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now });
  assert.equal(report.status, 'available');
  assert.equal(report.population_count, 79);
  assert.equal(report.included_count, 71);
  assert.equal(report.excluded_count, 8);
  assert.equal(report.orphaned_outcome_count, 0);
  assert.equal(report.count_delta, 0);
  assert.equal(report.cutoff_inclusive, cutoff);
  assert.deepEqual(report.cases.map((row) => row.run_id), [
    ...eligible.slice(0, -1).reverse().map((row) => row[0]), 'dispute-00',
  ]);
  assert.equal(new Set(report.cases.map((row) => row.run_id)).size, 71);
  assert.equal(new Set(report.cases.map((row) => row.target)).size, 1);
  assert.deepEqual(Object.fromEntries(report.exclusions.map((row) => [row.run_id, row.reasons])), {
    agree: ['agreeing_verdicts'],
    'no-verifier': ['missing_verifier_verdict'],
    'no-adjudication': ['missing_adjudicated_verdict'],
    'both-missing': ['missing_verifier_verdict', 'missing_adjudicated_verdict'],
    'no-outcome': ['missing_outcome'],
    old: ['outside_window'],
    'old-agreement': ['outside_window', 'agreeing_verdicts'],
    'no-timestamp': ['missing_timestamp'],
  });
  assert.equal(report.requested_cohort_verified, false, 'matching counts cannot prove case identities');
  assert.deepEqual(report.evidence_gaps.map((gap) => gap.kind), ['requested_cohort_unverified']);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readdirSync(root), files, 'no report, role store or SQLite sidecar created');
  // Bind the export to the production Python selection, including row ordering
  // and all fields needed to derive its existing case identities.
  const reference = execute(root, process.env.PYTHON || 'python3', ['-c', `
import json
from pathlib import Path
import adjudicator_retro as retro
print(json.dumps(retro.disputes(db=Path(${JSON.stringify(db)}), now=${now})))
`]);
  assert.equal(reference.status, 0, reference.stderr);
  assert.deepEqual(report.cases, JSON.parse(reference.stdout));
});

test('inventory preserves the fixed cutoff and reports a count discrepancy', (t) => {
  const { db } = world(t);
  seed(db, [['boundary', cutoff, 'NON_PASS', 'PASS'], ['future', now + 1, 'PASS', 'FAIL']]);
  const report = inventory({ db, observedAt: now });
  assert.equal(report.included_count, 2);
  assert.equal(report.count_delta, -69);
  assert.equal(report.upper_time_bound, null);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'inventory_count_discrepancy'));
  const moved = inventory({ db, observedAt: now + 1, expectedCount: 1 });
  assert.deepEqual(moved.cases.map((row) => row.run_id), ['future']);
  assert.deepEqual(moved.exclusions[0].reasons, ['outside_window']);
  assert.deepEqual(inventory({ db, observedAt: now }), report);
  assert.equal(inventory({ db, observedAt: now, expectedCount: 1 }).count_delta, 1);
});

for (const state of ['missing', 'schema-less', 'empty', 'incomplete-schema']) {
  test(`inventory distinguishes ${state} evidence from a complete cohort`, (t) => {
    const { root, db } = world(t, state !== 'schema-less');
    if (state === 'missing') fs.unlinkSync(db);
    if (state === 'incomplete-schema') {
      const connection = new DatabaseSync(db);
      connection.exec('DROP TABLE outcomes');
      connection.close();
    }
    const before = fs.existsSync(db) ? fs.readFileSync(db) : null;
    const files = fs.readdirSync(root);
    const report = inventory({ db, observedAt: now });
    if (state === 'empty') {
      assert.equal(report.status, 'available');
      assert.deepEqual(report.cases, []);
      assert.equal(report.included_count, 0);
      assert.equal(report.orphaned_outcome_count, 0);
      assert.equal(report.count_delta, -71);
    } else {
      assert.equal(report.status, 'unavailable');
      for (const key of ['cases', 'exclusions', 'included_count', 'excluded_count',
        'population_count', 'count_delta', 'orphaned_outcome_count']) assert.equal(report[key], null, key);
      assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'brain_unavailable'));
    }
    assert.equal(report.requested_cohort_verified, false);
    assert.deepEqual(fs.existsSync(db) ? fs.readFileSync(db) : null, before);
    assert.deepEqual(fs.readdirSync(root), files);
  });
}

test('inventory CLI exports JSON, signals unavailable evidence and rejects replay options', (t) => {
  const { root, db } = world(t);
  seed(db, [['one', now, 'NON_PASS', 'PASS']]);
  const args = [script, '--db', db, '--observed-at', String(now)];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), inventory({ db, observedAt: now }));
  const missing = path.join(root, 'absent', 'brain.db');
  const unavailable = execute(root, process.execPath,
    [script, '--db', missing, '--observed-at', String(now)]);
  assert.equal(unavailable.status, 2);
  assert.equal(JSON.parse(unavailable.stdout).included_count, null);
  assert.equal(fs.existsSync(path.dirname(missing)), false);
  for (const invalid of [['--db', db], [...args.slice(1), '--dispatch'],
    ['--db', db, '--observed-at', 'NaN'], [...args.slice(1), '--days', '0']]) {
    const failure = execute(root, process.execPath, [script, ...invalid]);
    assert.equal(failure.status, 1, failure.stderr);
    assert.equal(failure.stdout, '', 'invalid arguments cannot publish an inventory');
  }
});

function manifestFile(root, cases, schemaVersion = 1) {
  const file = path.join(root, 'requested.json');
  fs.writeFileSync(file, JSON.stringify({ schema_version: schemaVersion, cases }, null, 2));
  return file;
}

test('matching 71 identities bind the supplied manifest bytes without claiming historical acceptance', (t) => {
  const { root, db } = world(t);
  const cases = Array.from({ length: 71 }, (_, i) => ({
    run_id: `requested-${i}`, target: 'owner/repo#1',
  }));
  seed(db, cases.map(({ run_id }) => [run_id, now, 'NON_PASS', 'PASS']));
  const manifest = manifestFile(root, cases.reverse());
  const before = fs.readFileSync(db);
  const manifestBefore = fs.readFileSync(manifest);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, manifest });
  const reconciliation = report.manifest_reconciliation;
  assert.equal(reconciliation.scope, 'supplied_manifest_only');
  assert.equal(reconciliation.manifest_path, manifest);
  assert.equal(reconciliation.manifest_sha256,
    createHash('sha256').update(manifestBefore).digest('hex'));
  assert.equal(reconciliation.requested_count, 71);
  assert.equal(reconciliation.count_delta, 0);
  assert.equal(reconciliation.exact_match, true);
  assert.deepEqual(reconciliation.cases.map(({ run_id, target }) => ({ run_id, target })), cases);
  assert.ok(reconciliation.cases.every((row) => row.status === 'included' && !row.reasons.length));
  assert.deepEqual(reconciliation.unexpected_cases, []);
  assert.equal(report.requested_cohort_verified, false);
  assert.deepEqual(report.evidence_gaps.map((gap) => gap.kind), ['requested_cohort_unverified']);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(root), files);
});

test('equal inventory counts expose substituted identities, exclusions and absent requested runs', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['included', now, 'NON_PASS', 'PASS'],
    ['unexpected-a', now, 'NON_PASS', 'PASS'],
    ['unexpected-b', now, 'NON_PASS', 'PASS'],
    ['excluded', cutoff - 1, 'PASS', 'PASS'],
  ]);
  const manifest = manifestFile(root, ['included', 'excluded', 'absent'].map((run_id) => ({
    run_id, target: 'owner/repo#1',
  })));
  const report = inventory({ db, observedAt: now, expectedCount: 3, manifest });
  assert.equal(report.count_delta, 0, 'count equality conceals substituted cases');
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.cases.map((row) => row.status),
    ['included', 'excluded', 'missing']);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, [
    { run_id: 'unexpected-a', target: 'owner/repo#1' },
    { run_id: 'unexpected-b', target: 'owner/repo#1' },
  ]);
  assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap'), [
    { kind: 'requested_case_evidence_gap', run_id: 'excluded', target: 'owner/repo#1',
      reasons: ['outside_window', 'agreeing_verdicts'] },
    { kind: 'requested_case_evidence_gap', run_id: 'absent', target: 'owner/repo#1',
      reasons: ['requested_run_missing'] },
  ]);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'requested_manifest_discrepancy'));
});

test('a requested run on another target cannot satisfy case identity', (t) => {
  const { root, db } = world(t);
  seed(db, [['same-run', now, 'NON_PASS', 'PASS']]);
  const manifest = manifestFile(root, [{ run_id: 'same-run', target: 'owner/repo#2' }]);
  const report = inventory({ db, observedAt: now, expectedCount: 1, manifest });
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.cases, [{
    run_id: 'same-run', target: 'owner/repo#2', status: 'identity_mismatch',
    reasons: ['requested_target_mismatch'], observed_target: 'owner/repo#1',
  }]);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases,
    [{ run_id: 'same-run', target: 'owner/repo#1' }]);
});

test('inventory accounts for outcomes without run records as unavailable evidence', (t) => {
  const { root, db } = world(t);
  seed(db, [['included', now, 'NON_PASS', 'PASS'], ['no-outcome', now]]);
  const connection = new DatabaseSync(db);
  connection.exec(`
    INSERT INTO outcomes VALUES ('orphan-dispute', 'NON_PASS', 'PASS', 1, 'durable', ${now});
    INSERT INTO outcomes VALUES ('orphan-agreement', 'PASS', 'PASS', 1, 'durable', ${now});
  `);
  connection.close();
  const manifest = manifestFile(root, ['included', 'orphan-dispute'].map((run_id) => ({
    run_id, target: 'owner/repo#1',
  })));
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
  assert.equal(report.status, 'available');
  assert.equal(report.population_count, 4, 'every run and unmatched outcome is accounted for');
  assert.equal(report.included_count, 1);
  assert.equal(report.excluded_count, 3);
  assert.equal(report.orphaned_outcome_count, 2);
  assert.equal(report.population_count, report.included_count + report.excluded_count);
  assert.deepEqual(report.cases.map((row) => row.run_id), ['included']);
  assert.deepEqual(report.exclusions.filter((row) => row.run_id.startsWith('orphan-')), [
    { run_id: 'orphan-agreement', target: null, ts: null,
      verifier_verdict: 'PASS', adjudicated_verdict: 'PASS', merged: 1,
      durability: 'durable', durability_checked_ts: now,
      reasons: ['missing_run', 'missing_timestamp', 'agreeing_verdicts'] },
    { run_id: 'orphan-dispute', target: null, ts: null,
      verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS', merged: 1,
      durability: 'durable', durability_checked_ts: now,
      reasons: ['missing_run', 'missing_timestamp'] },
  ]);
  assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'orphaned_outcome'), [
    { kind: 'orphaned_outcome', run_id: 'orphan-agreement',
      detail: 'No matching run record; target and replay-window membership are unknown.' },
    { kind: 'orphaned_outcome', run_id: 'orphan-dispute',
      detail: 'No matching run record; target and replay-window membership are unknown.' },
  ]);
  assert.deepEqual(report.manifest_reconciliation.cases, [
    { run_id: 'included', target: 'owner/repo#1', status: 'included', reasons: [] },
    { run_id: 'orphan-dispute', target: 'owner/repo#1', status: 'unavailable',
      reasons: ['missing_run', 'missing_timestamp'] },
  ]);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, []);
  assert.equal(report.count_delta, -1);
  assert.equal(report.requested_cohort_verified, false);
  const result = execute(root, process.execPath, [script, '--db', db,
    '--observed-at', String(now), '--expected-count', '2', '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), report);
  assert.deepEqual(fs.readFileSync(db), before);
  // execute() creates only its stdout/stderr captures, never a SQLite sidecar.
  assert.deepEqual(fs.readdirSync(root).filter((file) => !['stdout', 'stderr'].includes(file)), files);
});

test('an outcomes-only Brain is available with exclusions, rather than an observed empty store', (t) => {
  const { root, db } = world(t);
  const connection = new DatabaseSync(db);
  connection.exec(`INSERT INTO outcomes VALUES ('orphan', 'NON_PASS', 'PASS', 1, 'durable', ${now})`);
  connection.close();
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now });
  assert.equal(report.status, 'available');
  assert.equal(report.population_count, 1);
  assert.equal(report.orphaned_outcome_count, 1);
  assert.equal(report.included_count, 0);
  assert.equal(report.excluded_count, 1);
  assert.equal(report.count_delta, -71);
  assert.equal(report.manifest_reconciliation, null);
  assert.deepEqual(report.cases, []);
  assert.deepEqual(report.exclusions[0].reasons, ['missing_run', 'missing_timestamp']);
  assert.deepEqual(report.evidence_gaps.map((gap) => gap.kind), [
    'requested_cohort_unverified', 'orphaned_outcome', 'inventory_count_discrepancy',
  ]);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readdirSync(root), files);
});

for (const unavailable of [false, true]) {
  test(`manifest retains every requested identity when Brain is ${unavailable ? 'unavailable' : 'empty'}`, (t) => {
    const { root, db } = world(t);
    if (unavailable) fs.unlinkSync(db);
    const manifest = manifestFile(root, ['one', 'two'].map((run_id) => ({
      run_id, target: 'owner/repo#1',
    })));
    const before = fs.readdirSync(root);
    const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
    assert.equal(report.manifest_reconciliation.exact_match, false);
    assert.deepEqual(report.manifest_reconciliation.cases.map((row) => row.run_id), ['one', 'two']);
    assert.ok(report.manifest_reconciliation.cases.every((row) =>
      row.status === (unavailable ? 'unavailable' : 'missing')));
    assert.equal(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap').length, 2);
    assert.deepEqual(report.manifest_reconciliation.unexpected_cases, unavailable ? null : []);
    assert.deepEqual(fs.readdirSync(root), before, 'a missing Brain must not be created');
  });
}

test('manifest count discrepancy prevents an exact match even when all supplied identities match', (t) => {
  const { root, db } = world(t);
  seed(db, [['one', now, 'NON_PASS', 'PASS']]);
  const manifest = manifestFile(root, [{ run_id: 'one', target: 'owner/repo#1' }]);
  const report = inventory({ db, observedAt: now, manifest });
  assert.equal(report.manifest_reconciliation.count_delta, -70);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.ok(report.evidence_gaps.some((gap) => gap.kind === 'requested_manifest_discrepancy'));
});

test('manifest CLI preserves input bytes, rejects malformed identities and signals unavailable Brain', (t) => {
  const { root, db } = world(t);
  seed(db, [['one', now, 'NON_PASS', 'PASS']]);
  const manifest = manifestFile(root, [{ run_id: 'one', target: 'owner/repo#1' }]);
  const manifestBefore = fs.readFileSync(manifest);
  const dbBefore = fs.readFileSync(db);
  const args = [script, '--db', db, '--observed-at', String(now), '--manifest', manifest];
  const result = execute(root, process.execPath, args);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), inventory({ db, observedAt: now, manifest }));
  assert.deepEqual(fs.readFileSync(db), dbBefore);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  fs.unlinkSync(db);
  const unavailable = execute(root, process.execPath, args);
  assert.equal(unavailable.status, 2);
  assert.equal(JSON.parse(unavailable.stdout).manifest_reconciliation.cases[0].status, 'unavailable');
  for (const value of [null, {}, { schema_version: 3, cases: [] },
    { schema_version: 1, cases: {} },
    ...[[null], [{}], [{ run_id: ' ', target: 'owner/repo#1' }],
      [{ run_id: 'one', target: 3 }], [{ run_id: 'one', target: ' ' }],
      [{ run_id: 'one', target: 'owner/repo#1' }, { run_id: 'one', target: 'owner/repo#2' }]]
      .map((cases) => ({ schema_version: 1, cases }))]) {
    fs.writeFileSync(manifest, JSON.stringify(value));
    const invalid = execute(root, process.execPath, args);
    assert.equal(invalid.status, 1, invalid.stderr);
    assert.equal(invalid.stdout, '', 'invalid manifest cannot publish a partial inventory');
  }
  fs.writeFileSync(manifest, '{broken json');
  assert.equal(execute(root, process.execPath, args).status, 1);
  fs.unlinkSync(manifest);
  const missing = execute(root, process.execPath, args);
  assert.equal(missing.status, 1);
  assert.equal(missing.stdout, '');
  assert.equal(fs.existsSync(db), false);
});

test('version 2 reconciles all 71 dispute identities without claiming historical provenance', (t) => {
  const { root, db } = world(t);
  const requested = Array.from({ length: 71 }, (_, i) => ({
    run_id: `dispute-${i}`, target: 'owner/repo#1',
    verifier_verdict: i < 57 ? 'NON_PASS' : 'FAIL_HOLLOW',
    adjudicated_verdict: i < 57 ? 'PASS' : 'FAIL',
  }));
  seed(db, requested.map((row) =>
    [row.run_id, now, row.verifier_verdict, row.adjudicated_verdict]));
  const manifest = manifestFile(root, requested, 2);
  const before = fs.readFileSync(db);
  const manifestBefore = fs.readFileSync(manifest);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, manifest });
  const reconciled = report.manifest_reconciliation;
  assert.equal(reconciled.identity_scope, 'dispute');
  assert.equal(reconciled.exact_match, true);
  assert.deepEqual(reconciled.cases, requested.map((item) =>
    ({ ...item, status: 'included', reasons: [] })));
  assert.deepEqual(reconciled.unexpected_cases, []);
  assert.equal(reconciled.manifest_sha256, createHash('sha256').update(manifestBefore).digest('hex'));
  assert.equal(report.requested_cohort_verified, false);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readFileSync(manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(root), files);
});

for (const [verifier, adjudicated] of [['NON_PASS', 'FAIL'], ['FAIL_HOLLOW', 'PASS'], ['PASS', 'FAIL']]) {
  test(`version 2 exposes substituted verdicts ${verifier}/${adjudicated} despite matching run and count`, (t) => {
    const { root, db } = world(t);
    seed(db, [['same-run', now, verifier, adjudicated]]);
    const expected = { run_id: 'same-run', target: 'owner/repo#1',
      verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
    const manifest = manifestFile(root, [expected], 2);
    const before = fs.readFileSync(db);
    const report = inventory({ db, observedAt: now, expectedCount: 1, manifest });
    assert.equal(report.count_delta, 0);
    assert.equal(report.manifest_reconciliation.exact_match, false);
    assert.deepEqual(report.manifest_reconciliation.cases, [{ ...expected,
      status: 'identity_mismatch', reasons: ['requested_verdict_mismatch'],
      observed_target: 'owner/repo#1', observed_verifier_verdict: verifier,
      observed_adjudicated_verdict: adjudicated,
    }]);
    assert.deepEqual(report.manifest_reconciliation.unexpected_cases, [{
      run_id: 'same-run', target: 'owner/repo#1',
      verifier_verdict: verifier, adjudicated_verdict: adjudicated,
    }]);
    assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap'),
      [{ kind: 'requested_case_evidence_gap', ...expected, reasons: ['requested_verdict_mismatch'] }]);
    const result = execute(root, process.execPath, [script, '--db', db, '--observed-at', String(now),
      '--expected-count', '1', '--manifest', manifest]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), report);
    assert.deepEqual(fs.readFileSync(db), before);
    // Legacy manifests remain readable, with their narrower identity scope explicit.
    manifestFile(root, [expected]);
    const legacy = inventory({ db, observedAt: now, expectedCount: 1, manifest });
    assert.equal(legacy.manifest_reconciliation.identity_scope, 'run_target');
    assert.equal(legacy.manifest_reconciliation.exact_match, true);
    assert.equal(legacy.requested_cohort_verified, false);
  });
}

test('version 2 retains different disputes on one run and rejects duplicate dispute identities', (t) => {
  const { root, db } = world(t);
  seed(db, [['same-run', now, 'NON_PASS', 'PASS']]);
  const original = { run_id: 'same-run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const changed = { ...original, verifier_verdict: 'FAIL_HOLLOW' };
  const manifest = manifestFile(root, [original, changed], 2);
  const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
  assert.deepEqual(report.manifest_reconciliation.cases.map((row) => row.status),
    ['included', 'identity_mismatch']);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, []);
  for (const invalid of [original, { ...original, target: 'owner/repo#2' }]) {
    manifestFile(root, [original, invalid], 2);
    assert.throws(() => inventory({ db, observedAt: now, manifest }), /duplicate manifest identity/);
  }
});

test('version 2 preserves full identities for excluded, missing and unavailable cases', (t) => {
  const { root, db } = world(t);
  seed(db, [['old', cutoff - 1, 'NON_PASS', 'PASS']]);
  const requested = ['old', 'missing'].map((run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const manifest = manifestFile(root, requested, 2);
  const report = inventory({ db, observedAt: now, expectedCount: 2, manifest });
  assert.deepEqual(report.manifest_reconciliation.cases, [
    { ...requested[0], status: 'excluded', reasons: ['outside_window'] },
    { ...requested[1], status: 'missing', reasons: ['requested_run_missing'] },
  ]);
  fs.unlinkSync(db);
  const result = execute(root, process.execPath, [script, '--db', db, '--observed-at', String(now),
    '--expected-count', '2', '--manifest', manifest]);
  assert.equal(result.status, 2, result.stderr);
  const unavailable = JSON.parse(result.stdout);
  assert.equal(unavailable.population_count, null);
  assert.equal(unavailable.manifest_reconciliation.unexpected_cases, null);
  assert.deepEqual(unavailable.manifest_reconciliation.cases, requested.map((item) =>
    ({ ...item, status: 'unavailable', reasons: ['brain_unavailable'] })));
  assert.equal(fs.existsSync(db), false);
});

test('version 2 rejects malformed verdicts before publishing any inventory', (t) => {
  const { root, db } = world(t);
  const original = { run_id: 'run', target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  for (const field of ['verifier_verdict', 'adjudicated_verdict']) {
    for (const value of [undefined, null, '', ' ', 3, [], {}]) {
      const manifest = manifestFile(root, [{ ...original, [field]: value }], 2);
      const result = execute(root, process.execPath,
        [script, '--db', db, '--observed-at', String(now), '--manifest', manifest]);
      assert.equal(result.status, 1, result.stderr);
      assert.equal(result.stdout, '', 'malformed dispute identity cannot publish partial JSON');
      assert.match(result.stderr, /require nonblank verifier_verdict and adjudicated_verdict/);
    }
  }
});

test('version 2 distinguishes unavailable verdict evidence from contradictory verdicts', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['no-outcome', now],
    ['no-verifier', now, null, 'PASS'],
    ['no-adjudication', now, 'NON_PASS', null],
    ['neither-verdict', cutoff - 1, null, null],
    ['changed-verdict', now, 'NON_PASS', 'FAIL'],
  ]);
  const requested = ['no-outcome', 'no-verifier', 'no-adjudication', 'neither-verdict',
    'changed-verdict'].map((run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const manifest = manifestFile(root, requested, 2);
  const before = fs.readFileSync(db);
  const files = fs.readdirSync(root);
  const report = inventory({ db, observedAt: now, expectedCount: 5, manifest });
  assert.equal(report.status, 'available', 'missing case evidence does not hide the observed population');
  assert.equal(report.population_count, 5);
  assert.equal(report.included_count, 1);
  assert.equal(report.excluded_count, 4);
  assert.equal(report.manifest_reconciliation.exact_match, false);
  const reasons = [
    ['missing_outcome'],
    ['missing_verifier_verdict'],
    ['missing_adjudicated_verdict'],
    ['outside_window', 'missing_verifier_verdict', 'missing_adjudicated_verdict'],
  ];
  assert.deepEqual(report.manifest_reconciliation.cases.slice(0, 4),
    requested.slice(0, 4).map((item, i) => ({ ...item, status: 'unavailable', reasons: reasons[i] })));
  assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap'),
    requested.map((item, i) => ({ kind: 'requested_case_evidence_gap', ...item,
      reasons: reasons[i] || ['requested_verdict_mismatch'] })));
  assert.equal(report.manifest_reconciliation.cases[4].status, 'identity_mismatch');
  assert.deepEqual(report.manifest_reconciliation.unexpected_cases, [{
    ...requested[4], adjudicated_verdict: 'FAIL',
  }]);
  assert.equal(report.requested_cohort_verified, false);
  assert.deepEqual(fs.readFileSync(db), before);
  assert.deepEqual(fs.readdirSync(root), files);
  const result = execute(root, process.execPath, [script, '--db', db, '--observed-at', String(now),
    '--expected-count', '5', '--manifest', manifest]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), report);
  assert.deepEqual(fs.readFileSync(db), before);

  // Legacy run/target manifests can still identify an excluded run without
  // requiring the verdict pair that only version 2 promises to reconcile.
  manifestFile(root, requested);
  const legacy = inventory({ db, observedAt: now, expectedCount: 5, manifest });
  assert.deepEqual(legacy.manifest_reconciliation.cases.map((row) => row.status),
    ['excluded', 'excluded', 'excluded', 'excluded', 'included']);
});

test('version 2 retains an observed target mismatch when verdict evidence is unavailable', (t) => {
  const { root, db } = world(t);
  seed(db, [['no-outcome', now]]);
  const requested = { run_id: 'no-outcome', target: 'owner/repo#2',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' };
  const manifest = manifestFile(root, [requested], 2);
  const report = inventory({ db, observedAt: now, expectedCount: 1, manifest });
  assert.deepEqual(report.manifest_reconciliation.cases, [{ ...requested,
    status: 'identity_mismatch', reasons: ['requested_target_mismatch'],
    observed_target: 'owner/repo#1', observed_verifier_verdict: null,
    observed_adjudicated_verdict: null,
  }]);
  assert.equal(report.manifest_reconciliation.exact_match, false);
});

test('missing stored targets remain eligible disputes with explicit unavailable identity evidence', (t) => {
  const { root, db } = world(t);
  seed(db, [
    ['null-target', now, 'NON_PASS', 'PASS'],
    ['empty-target', now, 'NON_PASS', 'PASS'],
    ['blank-target', now, 'NON_PASS', 'PASS'],
    ['excluded-target', cutoff - 1],
    ['other-target', now, 'NON_PASS', 'PASS'],
    ['complete', now, 'NON_PASS', 'PASS'],
  ]);
  const connection = new DatabaseSync(db);
  for (const [id, target] of [
    ['null-target', null], ['empty-target', ''], ['blank-target', ' \t '],
    ['excluded-target', null], ['other-target', 'owner/repo#2'],
  ]) connection.prepare('UPDATE runs SET target=? WHERE run_id=?').run(target, id);
  connection.close();
  const requested = ['null-target', 'empty-target', 'blank-target', 'excluded-target',
    'other-target', 'complete'].map((run_id) => ({ run_id, target: 'owner/repo#1',
    verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS' }));
  const before = fs.readFileSync(db);
  const observed = inventory({ db, observedAt: now, expectedCount: 6 });
  assert.equal(observed.population_count, 6);
  assert.equal(observed.included_count, 5, 'preserve production replay selection');
  assert.equal(observed.excluded_count, 1);
  assert.equal(observed.evidence_gaps.filter((gap) => gap.kind === 'missing_target').length, 4,
    'missing targets must be visible even without a supplied manifest');
  const reference = execute(root, process.env.PYTHON || 'python3', ['-c', `
import json
from pathlib import Path
import adjudicator_retro as retro
print(json.dumps(retro.disputes(db=Path(${JSON.stringify(db)}), now=${now})))
`]);
  assert.equal(reference.status, 0, reference.stderr);
  assert.deepEqual(observed.cases, JSON.parse(reference.stdout));
  for (const version of [1, 2]) {
    const manifest = manifestFile(root, requested, version);
    const files = fs.readdirSync(root);
    const report = inventory({ db, observedAt: now, expectedCount: 6, manifest });
    const reconciled = report.manifest_reconciliation;
    const identities = requested.map(({ run_id, target, ...verdicts }) => ({
      run_id, target, ...(version === 2 ? verdicts : {}),
    }));
    const reasons = [
      ['missing_target'], ['missing_target'], ['missing_target'],
      ['outside_window', 'missing_outcome', 'missing_target'],
    ];
    assert.deepEqual(reconciled.cases.slice(0, 4), identities.slice(0, 4).map((item, i) =>
      ({ ...item, status: 'unavailable', reasons: reasons[i] })));
    assert.equal(reconciled.cases[4].status, 'identity_mismatch',
      'a nonblank contradictory target still represents an identity mismatch');
    assert.equal(reconciled.cases[5].status, 'included', 'continue to the next intact case');
    assert.equal(reconciled.exact_match, false);
    assert.equal(report.requested_cohort_verified, false);
    assert.deepEqual(report.evidence_gaps.filter((gap) => gap.kind === 'requested_case_evidence_gap')
      .slice(0, 4), identities.slice(0, 4).map((item, i) =>
      ({ kind: 'requested_case_evidence_gap', ...item, reasons: reasons[i] })));
    const result = execute(root, process.execPath, [script, '--db', db,
      '--observed-at', String(now), '--expected-count', '6', '--manifest', manifest]);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), report);
    assert.deepEqual(fs.readFileSync(db), before);
    assert.deepEqual(fs.readdirSync(root), files, 'no report or SQLite sidecars created');
  }
});
