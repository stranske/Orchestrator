'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { inventory } = require('../scripts/export_adjudicator_inventory');

const repo = path.resolve(__dirname, '..');
const script = path.join(repo, 'scripts/export_adjudicator_inventory.js');
const now = 2000000000;
const cutoff = now - 90 * 86400;

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

function manifestFile(root, cases) {
  const file = path.join(root, 'requested.json');
  fs.writeFileSync(file, JSON.stringify({ schema_version: 1, cases }, null, 2));
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
  for (const value of [null, {}, { schema_version: 2, cases: [] },
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
