'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { DatabaseSync } = require('node:sqlite');
const { test } = require('node:test');
const { caseGaps } = require('../scripts/export_adjudicator_case_gaps');

const script = path.resolve(__dirname, '../scripts/export_adjudicator_case_gaps.js');
const now = 2000000000;
const identity = (run_id, target = 'fixture/repo#1') => ({
  run_id, target, verifier_verdict: 'NON_PASS', adjudicated_verdict: 'PASS',
});

function world(t, cases, schema = 2) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-case-gaps-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const db = path.join(root, 'brain.db');
  const manifest = path.join(root, 'manifest.json');
  fs.writeFileSync(manifest, JSON.stringify({ schema_version: schema, cases }));
  const connection = new DatabaseSync(db);
  connection.exec(`
    CREATE TABLE runs (run_id TEXT PRIMARY KEY, target TEXT, ts INTEGER);
    CREATE TABLE outcomes (run_id TEXT PRIMARY KEY, verifier_verdict TEXT,
      adjudicated_verdict TEXT, merged INTEGER, durability TEXT,
      durability_checked_ts INTEGER);
  `);
  connection.close();
  return { root, db, manifest, observedAt: now, expectedCount: cases.length };
}

function invoke(options, extra = []) {
  const stdoutPath = path.join(options.root, 'gap-ledger.json');
  const stderrPath = path.join(options.root, 'stderr');
  const stdout = fs.openSync(stdoutPath, 'w');
  const stderr = fs.openSync(stderrPath, 'w');
  let result;
  try {
    result = spawnSync(process.execPath, [script, '--db', options.db,
      '--observed-at', String(options.observedAt), '--expected-count', String(options.expectedCount),
      '--manifest', options.manifest, ...extra], {
      cwd: options.root, timeout: 10000, stdio: ['ignore', stdout, stderr],
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  assert.ifError(result.error);
  return { status: result.status, stdout: fs.readFileSync(stdoutPath, 'utf8'),
    stderr: fs.readFileSync(stderrPath, 'utf8') };
}

test('gap export preserves every dispute and explicit UNKNOWN execution evidence without writes', (t) => {
  const requested = ['included', 'missing', 'old', 'incomplete', 'changed-target',
    'changed-verdict'].map((id) => identity(id));
  // A second requested dispute on the same run is a distinct obligation.
  requested.push({ ...identity('included'), adjudicated_verdict: 'FAIL' });
  const options = world(t, requested);
  const connection = new DatabaseSync(options.db);
  for (const id of ['included', 'old', 'incomplete', 'changed-target', 'changed-verdict']) {
    connection.prepare('INSERT INTO runs VALUES (?,?,?)').run(id,
      id === 'changed-target' ? 'fixture/repo#2' : 'fixture/repo#1',
      id === 'old' ? now - 100 * 86400 : now);
    if (id !== 'incomplete') connection.prepare('INSERT INTO outcomes VALUES (?,?,?,?,?,?)')
      .run(id, 'NON_PASS', id === 'changed-verdict' ? 'FAIL' : 'PASS', 1, 'durable', now);
  }
  connection.close();
  const dbBefore = fs.readFileSync(options.db);
  const manifestBefore = fs.readFileSync(options.manifest);
  const filesBefore = fs.readdirSync(options.root);
  const ledger = caseGaps(options);
  assert.deepEqual(ledger.cases.map((row) => ({ run_id: row.run_id, target: row.target,
    verifier_verdict: row.verifier_verdict, adjudicated_verdict: row.adjudicated_verdict })), requested);
  assert.deepEqual(ledger.cases.map((row) => row.inventory_status), [
    'included', 'missing', 'excluded', 'unavailable', 'identity_mismatch',
    'identity_mismatch', 'identity_mismatch',
  ]);
  assert.deepEqual(ledger.cases.map((row) => row.inventory_reasons), [
    [], ['requested_run_missing'], ['outside_window'], ['missing_outcome'],
    ['requested_target_mismatch'], ['requested_verdict_mismatch'], ['requested_verdict_mismatch'],
  ]);
  for (const row of ledger.cases) {
    assert.equal(row.status, 'evidence_gap');
    for (const field of ['source_revision', 'shadow_result', 'role_run_provenance', 'cost_usd']) {
      assert.equal(row[field], 'UNKNOWN', `${row.run_id}: ${field}`);
    }
    assert.deepEqual(row.evidence_gaps, [...row.inventory_reasons, 'shadow_evidence_not_collected']);
  }
  assert.equal(ledger.cases[4].observed_target, 'fixture/repo#2');
  assert.equal(ledger.cases[5].observed_adjudicated_verdict, 'FAIL');
  assert.equal(ledger.supplied_case_count, 7);
  assert.equal(ledger.evidence_gap_count, 7);
  assert.equal(ledger.unsupplied_requested_slots, 0);
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.authenticated_requested_case_count, 'UNKNOWN');
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.equal(ledger.inventory_snapshot.manifest_reconciliation.manifest_sha256,
    createHash('sha256').update(manifestBefore).digest('hex'));
  assert.equal(ledger.collector_sha256, createHash('sha256').update(fs.readFileSync(script)).digest('hex'));
  assert.equal(ledger.inventory_collector_sha256, createHash('sha256')
    .update(fs.readFileSync(path.resolve(__dirname, '../scripts/export_adjudicator_inventory.js'))).digest('hex'));
  assert.deepEqual(fs.readdirSync(options.root), filesBefore);
  const cli = invoke(options);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), ledger, 'file-backed stdout preserves every case');
  assert.deepEqual(fs.readFileSync(options.db), dbBefore);
  assert.deepEqual(fs.readFileSync(options.manifest), manifestBefore);
  assert.deepEqual(fs.readdirSync(options.root).sort(), [...filesBefore, 'gap-ledger.json', 'stderr'].sort());
});

test('owner-excluded evidence is never evaluated and remains an identity-only gap', (t) => {
  const excludedTarget = '\tStRaNsKe/CoLlAb-AdMiN\u00a0#1 ';
  const requested = [identity('excluded', excludedTarget), identity('allowed')];
  const options = world(t, requested);
  const connection = new DatabaseSync(options.db);
  connection.prepare('INSERT INTO runs VALUES (?,?,?)').run('excluded', excludedTarget, now);
  connection.exec(`
    INSERT INTO runs VALUES ('allowed','fixture/repo#1',${now});
    ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,
      CASE WHEN run_id='excluded' THEN json_extract('unreadable','$') ELSE ts END AS ts
      FROM run_rows;
    INSERT INTO outcomes VALUES ('excluded','NON_PASS','PASS',1,'durable',${now}),
                                ('allowed','NON_PASS','PASS',1,'durable',${now});
    ALTER TABLE outcomes RENAME TO outcome_rows;
    CREATE VIEW outcomes AS SELECT run_id,
      CASE WHEN run_id='excluded' THEN json_extract('unreadable','$')
        ELSE verifier_verdict END AS verifier_verdict,
      adjudicated_verdict,merged,durability,durability_checked_ts FROM outcome_rows;
  `);
  assert.throws(() => connection.prepare("SELECT ts FROM runs WHERE run_id='excluded'").get(), /malformed JSON/);
  assert.throws(() => connection.prepare("SELECT verifier_verdict FROM outcomes WHERE run_id='excluded'").get(), /malformed JSON/);
  connection.close();
  const before = fs.readFileSync(options.db);
  const result = invoke(options);
  assert.equal(result.status, 0, result.stderr);
  const ledger = JSON.parse(result.stdout);
  assert.equal(ledger.inventory_snapshot.status, 'available');
  assert.deepEqual(ledger.cases[0], {
    ...requested[0], status: 'evidence_gap', inventory_status: 'excluded',
    inventory_reasons: ['owner_excluded_repository'], source_revision: 'UNKNOWN',
    shadow_result: 'UNKNOWN', role_run_provenance: 'UNKNOWN', cost_usd: 'UNKNOWN',
    evidence_gaps: ['owner_excluded_repository'],
  });
  assert.equal(ledger.cases[1].inventory_status, 'included');
  assert.deepEqual(fs.readFileSync(options.db), before);
});

for (const version of [1, 2]) {
  test(`schema ${version} retains requested identities and unknown counts with an unavailable Brain`, (t) => {
    const requested = [identity('missing'), identity('excluded', '\u2003stranske/Collab-Admin\t#2')];
    const options = world(t, requested, version);
    fs.unlinkSync(options.db);
    options.expectedCount = 71;
    const before = fs.readFileSync(options.manifest);
    const result = invoke(options);
    assert.equal(result.status, 2, result.stderr);
    const ledger = JSON.parse(result.stdout);
    assert.equal(ledger.supplied_case_count, 2);
    assert.equal(ledger.unsupplied_requested_slots, 69);
    assert.equal(ledger.evidence_gap_count, 2);
    assert.deepEqual(ledger.cases.map((row) => row.run_id), ['missing', 'excluded']);
    assert.deepEqual(ledger.cases[0].evidence_gaps, ['brain_unavailable', 'shadow_evidence_not_collected']);
    assert.deepEqual(ledger.cases[1].evidence_gaps, ['owner_excluded_repository']);
    for (const key of ['population_count', 'included_count', 'excluded_count', 'count_delta']) {
      assert.equal(ledger.inventory_snapshot[key], null);
    }
    assert.equal(ledger.requested_cohort_verified, false);
    assert.equal(ledger.operational_acceptance_complete, false);
    assert.equal(fs.existsSync(options.db), false);
    assert.deepEqual(fs.readFileSync(options.manifest), before);
  });
}

test('an empty supplied manifest cannot claim recovery or hide the missing requested slots', (t) => {
  const options = world(t, []);
  const ledger = caseGaps({ ...options, expectedCount: 71 });
  assert.equal(ledger.unsupplied_requested_slots, 71);
  assert.equal(ledger.authenticated_requested_case_count, 'UNKNOWN');
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.deepEqual(ledger.cases, []);
  assert.match(ledger.limitations.join('\n'), /No supplied identities/);
  assert.ok(ledger.inventory_snapshot.evidence_gaps.some((row) => row.kind === 'requested_manifest_discrepancy'));
});

test('invalid arguments and manifests produce no partial ledger or Brain writes', (t) => {
  const options = world(t, [identity('one')]);
  const before = fs.readFileSync(options.db);
  assert.throws(() => caseGaps({ ...options, manifest: undefined }), /manifest is required/);
  for (const content of ['{broken', 'null', JSON.stringify({ schema_version: 2, cases: [identity('one'), identity('one')] }),
    JSON.stringify({ schema_version: 2, cases: [{ run_id: 'one', target: 'fixture/repo#1' }] })]) {
    fs.writeFileSync(options.manifest, content);
    const result = invoke(options);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.ok(result.stderr);
    assert.equal(fs.readFileSync(options.manifest, 'utf8'), content);
    assert.deepEqual(fs.readFileSync(options.db), before);
  }
  fs.writeFileSync(options.manifest, JSON.stringify({ schema_version: 2, cases: [identity('one')] }));
  for (const args of [['--observed-at', 'not-a-time'], ['--expected-count', '-1'], ['--days', '0'], ['--unknown']]) {
    const result = invoke(options, args);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.deepEqual(fs.readFileSync(options.db), before);
  }
});

function savedReport(options, rows) {
  const file = path.join(options.root, 'saved-report.json');
  fs.writeFileSync(file, JSON.stringify({ rows, summary: { cost_usd: 999 } }));
  return file;
}

test('saved report joins the complete dispute identity and binds unverified claims to retained bytes', (t) => {
  const requested = [identity('one'), identity('other-target', 'fixture/repo#2'),
    { ...identity('one'), adjudicated_verdict: 'FAIL' }, identity('missing')];
  const options = world(t, requested);
  const rows = [
    { ...requested[1], cost_usd: 4 },
    { ...requested[0], role_run_id: 'role:one', backend_run_id: 'backend:one',
      decision: 'reject_blocker', cost_usd: 0,
      packet: { target: requested[0].target, ground_truth_evidence: { head_sha: 'a'.repeat(40) } } },
    { ...requested[2], role_run_id: 'role:fail', cost_usd: null, error: 'Missing bound evidence' },
    identity('one', 'fixture/repo#99'),
  ];
  const report = savedReport(options, rows);
  const before = [options.db, options.manifest, report].map((file) => fs.readFileSync(file));
  const filesBefore = fs.readdirSync(options.root);
  const ledger = caseGaps({ ...options, report });
  assert.deepEqual(ledger.cases.map((row) => row.saved_report_evidence?.row_index), [1, 0, 2, undefined]);
  const evidence = ledger.cases[0].saved_report_evidence;
  assert.equal(evidence.status, 'unverified_saved_report_row');
  assert.equal(evidence.report.path, report);
  assert.equal(evidence.report.sha256, createHash('sha256').update(before[2]).digest('hex'));
  assert.equal(evidence.row_sha256, createHash('sha256').update(JSON.stringify(rows[1])).digest('hex'));
  assert.equal(evidence.reported_source_revision, 'a'.repeat(40));
  assert.equal(evidence.reported_role_run_id, 'role:one');
  assert.equal(evidence.reported_backend_run_id, 'backend:one');
  assert.equal(evidence.reported_decision, 'reject_blocker');
  assert.equal(evidence.reported_cost_usd, 0, 'reported zero is retained without admitting it as measured');
  for (const row of ledger.cases) {
    for (const field of ['source_revision', 'shadow_result', 'role_run_provenance', 'cost_usd']) {
      assert.equal(row[field], 'UNKNOWN');
    }
  }
  assert.ok(ledger.cases[2].evidence_gaps.includes('saved_case_error'));
  assert.ok(ledger.cases[2].evidence_gaps.includes('saved_backend_run_id_missing'));
  assert.equal(ledger.cases[2].saved_report_evidence.reported_cost_usd, 'UNKNOWN');
  assert.ok(ledger.cases[3].evidence_gaps.includes('saved_case_missing'));
  assert.equal(ledger.saved_report_snapshot.row_count, 4);
  assert.equal(ledger.saved_report_snapshot.matched_case_count, 3);
  assert.equal(ledger.saved_report_snapshot.unmatched_row_count, 1);
  assert.equal(ledger.saved_report_snapshot.persisted_records_verified, false);
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.match(ledger.limitations.join('\n'), /persisted role records and measured costs remain unverified/);
  assert.deepEqual(fs.readdirSync(options.root), filesBefore);
  const cli = invoke(options, ['--report', report]);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), ledger);
  assert.deepEqual([options.db, options.manifest, report].map((file) => fs.readFileSync(file)), before);
});

test('ambiguous saved rows and owner-excluded cases cannot contribute execution claims', (t) => {
  const requested = [identity('duplicate'), identity('excluded', 'stranske/Collab-Admin#1')];
  const options = world(t, requested);
  const report = savedReport(options, [requested[0], { ...requested[0], role_run_id: 'other' },
    { ...requested[1], role_run_id: 'excluded-role', cost_usd: 100 }]);
  const ledger = caseGaps({ ...options, report });
  assert.ok(ledger.cases[0].evidence_gaps.includes('saved_case_ambiguous'));
  assert.equal(ledger.cases[0].saved_report_evidence, undefined);
  assert.equal(ledger.cases[1].saved_report_evidence, undefined);
  assert.deepEqual(ledger.cases[1].evidence_gaps, ['owner_excluded_repository']);
  assert.equal(ledger.saved_report_snapshot.matched_case_count, 0);
  assert.equal(ledger.saved_report_snapshot.unmatched_row_count, 3);
  assert.equal(JSON.stringify(ledger).includes('excluded-role'), false);
});

test('mismatched packets, invalid revisions and invalid costs remain specific gaps', (t) => {
  const options = world(t, [identity('one')]);
  for (const packet of [undefined,
    { target: 'fixture/repo#2', ground_truth_evidence: { head_sha: 'a'.repeat(40) } },
    { target: 'fixture/repo#1', ground_truth_evidence: { head_sha: 'abc123' } }]) {
    for (const cost_usd of [null, -1, '2.5']) {
      const report = savedReport(options, [{ ...identity('one'), packet, cost_usd }]);
      const row = caseGaps({ ...options, report }).cases[0];
      assert.equal(row.saved_report_evidence.reported_source_revision, 'UNKNOWN');
      assert.equal(row.saved_report_evidence.reported_cost_usd, 'UNKNOWN');
      assert.ok(row.evidence_gaps.includes('source_revision_unavailable'));
      assert.ok(row.evidence_gaps.includes('saved_role_run_id_missing'));
      assert.ok(row.evidence_gaps.includes('measured_cost_not_verified'));
      assert.equal(row.evidence_gaps.includes('saved_packet_target_missing'), !packet);
      assert.equal(row.evidence_gaps.includes('saved_packet_target_mismatch'), packet?.target === 'fixture/repo#2');
    }
  }
});

test('invalid saved reports and run-only manifests fail without partial output or writes', (t) => {
  const options = world(t, [identity('one')]);
  const report = savedReport(options, []);
  const before = fs.readFileSync(options.db);
  for (const text of ['{broken', 'null', '{}', '{"rows":[null]}', '{"rows":[{"run_id":"one"}]}']) {
    fs.writeFileSync(report, text);
    const result = invoke(options, ['--report', report]);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.deepEqual(fs.readFileSync(options.db), before);
    assert.equal(fs.readFileSync(report, 'utf8'), text);
  }
  fs.writeFileSync(report, JSON.stringify({ rows: [identity('one')] }));
  fs.writeFileSync(options.manifest, JSON.stringify({ schema_version: 1, cases: [identity('one')] }));
  const result = invoke(options, ['--report', report]);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /requires a schema_version 2 dispute manifest/);
  assert.deepEqual(fs.readFileSync(options.db), before);
});

function persistedWorld(t, { target = 'fixture/repo#1', role = 'adjudicator',
  metadata, cost = 1.5, source = 'ccusage', backendTarget } = {}) {
  const options = world(t, [identity('one')]);
  const connection = new DatabaseSync(options.db);
  connection.exec('ALTER TABLE runs ADD COLUMN role_name TEXT; ALTER TABLE runs ADD COLUMN agent TEXT; ALTER TABLE runs ADD COLUMN decomposition TEXT; CREATE TABLE costs (run_id TEXT PRIMARY KEY,cost_usd REAL,source TEXT);');
  const record = { run_id: 'role:one', target, role_name: role, agent: 'gemini',
    decomposition: metadata === undefined ? JSON.stringify({ role: 'adjudicator',
      backend_run_id: 'backend:one', action: 'uphold_blocker' }) : metadata };
  connection.prepare('INSERT INTO runs (run_id,target,ts,role_name,agent,decomposition) VALUES (?,?,0,?,?,?)')
    .run(record.run_id, record.target, record.role_name, record.agent, record.decomposition);
  if (backendTarget) connection.prepare('INSERT INTO runs (run_id,target,ts) VALUES (?,?,0)')
    .run('backend:one', backendTarget);
  connection.prepare('INSERT INTO costs VALUES (?,?,?)').run('backend:one', cost, source);
  connection.close();
  options.report = savedReport(options, [{ ...identity('one'), role_run_id: 'role:one',
    backend_run_id: 'backend:one', decision: 'uphold_blocker', cost_usd: 1.5,
    packet: { target: 'fixture/repo#1', ground_truth_evidence: { head_sha: 'a'.repeat(40) } } }]);
  options.persistedRoles = true;
  return { options, record };
}

function addRetries(options, attempts) {
  const report = JSON.parse(fs.readFileSync(options.report));
  report.rows[0].retry_attempts = attempts;
  fs.writeFileSync(options.report, JSON.stringify(report));
  return report.rows[0];
}

function retryClaim(id, extra = {}) {
  return { ...identity('one'), role_run_id: `role:${id}`, backend_run_id: `backend:${id}`,
    decision: 'needs_more_evidence', cost_usd: null,
    packet: { target: 'fixture/repo#1', ground_truth_evidence: { head_sha: 'b'.repeat(40) } },
    ...extra };
}

function retryRecord(options, id, cost, source = 'ccusage') {
  const connection = new DatabaseSync(options.db);
  connection.prepare('INSERT INTO runs (run_id,target,ts,role_name,agent,decomposition) VALUES (?,?,0,?,?,?)')
    .run(`role:${id}`, 'fixture/repo#1', 'adjudicator', 'gemini', JSON.stringify({
      role: 'adjudicator', backend_run_id: `backend:${id}`, action: 'needs_more_evidence',
    }));
  if (cost !== undefined) connection.prepare('INSERT INTO costs VALUES (?,?,?)')
    .run(`backend:${id}`, cost, source);
  connection.close();
}

test('retained nested attempts bind their own rows and persisted receipts without becoming cases', (t) => {
  const { options } = persistedWorld(t);
  retryRecord(options, 'prior', 2.5);
  retryRecord(options, 'zero', 0);
  retryRecord(options, 'absent');
  const nested = retryClaim('zero', { retry_attempts: [retryClaim('absent')] });
  const saved = addRetries(options, [retryClaim('prior', { cost_usd: 2.5 }), nested]);
  const inputs = [options.db, options.manifest, options.report];
  const before = inputs.map((file) => fs.readFileSync(file));
  const files = fs.readdirSync(options.root);
  const ledger = caseGaps(options);
  const row = ledger.cases[0];
  const attempts = row.saved_retry_attempts;
  assert.deepEqual(attempts.map((attempt) => attempt.retry_path), [[0], [1], [1, 0]]);
  assert.deepEqual(attempts.map((attempt) => attempt.saved_report_evidence.retry_path), [[0], [1], [1, 0]]);
  assert.deepEqual(attempts.map((attempt) => attempt.persisted_role_evidence.backend_cost.cost_usd),
    [2.5, 'UNKNOWN', 'UNKNOWN']);
  assert.ok(attempts[1].evidence_gaps.includes('persisted_cost_not_measured'));
  assert.ok(attempts[2].evidence_gaps.includes('persisted_cost_missing'));
  assert.equal(attempts[0].saved_report_evidence.row_index, 0);
  assert.equal(attempts[0].saved_report_evidence.row_sha256, createHash('sha256')
    .update(JSON.stringify(saved.retry_attempts[0])).digest('hex'));
  assert.equal(attempts[1].saved_report_evidence.row_sha256, createHash('sha256')
    .update(JSON.stringify(nested)).digest('hex'));
  assert.equal(attempts[0].saved_report_evidence.report.sha256, createHash('sha256')
    .update(before[2]).digest('hex'));
  for (const attempt of attempts) {
    assert.equal(attempt.status, 'evidence_gap');
    assert.ok(attempt.evidence_gaps.includes('source_revision_not_bound_to_persisted_role'));
    for (const field of ['source_revision', 'shadow_result', 'role_run_provenance', 'cost_usd']) {
      assert.equal(attempt[field], 'UNKNOWN');
    }
  }
  assert.equal(ledger.supplied_case_count, 1);
  assert.equal(ledger.evidence_gap_count, 1);
  assert.equal(ledger.saved_report_snapshot.row_count, 1);
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
  assert.deepEqual(fs.readdirSync(options.root), files, 'retry reads create no SQLite sidecars');
  const cli = invoke(options, ['--report', options.report, '--persisted-roles']);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), ledger);
  const optOut = caseGaps({ ...options, persistedRoles: false }).cases[0];
  assert.ok(optOut.saved_retry_attempts[0].saved_report_evidence);
  assert.equal(optOut.saved_retry_attempts[0].persisted_role_evidence, undefined);
  assert.ok(optOut.saved_retry_attempts[0].evidence_gaps.includes('persisted_role_record_not_verified'));
});

test('retry lookup cannot borrow identity from its parent or a different dispute', (t) => {
  const { options } = persistedWorld(t);
  retryRecord(options, 'foreign', 2.5);
  const incomplete = retryClaim('foreign');
  delete incomplete.verifier_verdict;
  addRetries(options, [incomplete,
    retryClaim('foreign', { run_id: 'another' }),
    retryClaim('foreign', { target: 'fixture/repo#2' }),
    retryClaim('foreign', { verifier_verdict: 'PASS' }),
    retryClaim('foreign', { adjudicated_verdict: 'FAIL' })]);
  const connection = new DatabaseSync(options.db);
  connection.exec(`ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,ts,role_name,agent,
      CASE WHEN run_id='role:foreign' THEN json_extract('unreadable','$') ELSE decomposition END AS decomposition
      FROM run_rows;`);
  connection.close();
  const attempts = caseGaps(options).cases[0].saved_retry_attempts;
  assert.deepEqual(attempts.map((row) => row.evidence_gaps), [
    ['saved_retry_identity_missing'], ...Array.from({ length: 4 }, () => ['saved_retry_identity_mismatch']),
  ]);
  for (const row of attempts) {
    assert.equal(row.saved_report_evidence, undefined);
    assert.equal(row.persisted_role_evidence, undefined);
    assert.equal(row.cost_usd, 'UNKNOWN');
  }
});

test('excluded retry identities withhold case execution claims before role or cost reads', (t) => {
  const { options } = persistedWorld(t);
  const excluded = retryClaim('excluded', {
    target: '\tStRaNsKe/CoLlAb-AdMiN\u00a0#9',
    retry_attempts: [retryClaim('hidden')],
  });
  addRetries(options, [retryClaim('prior', { retry_attempts: [excluded] })]);
  const connection = new DatabaseSync(options.db);
  connection.exec(`ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,ts,json_extract('unreadable','$') AS role_name,
      agent,json_extract('unreadable','$') AS decomposition FROM run_rows;
    ALTER TABLE costs RENAME TO cost_rows;
    CREATE VIEW costs AS SELECT run_id,json_extract('unreadable','$') AS cost_usd,source FROM cost_rows;`);
  connection.close();
  const before = fs.readFileSync(options.db);
  const row = caseGaps(options).cases[0];
  assert.equal(row.saved_report_evidence, undefined, 'parent digest would serialize excluded history');
  assert.equal(row.persisted_role_evidence, undefined);
  assert.ok(row.evidence_gaps.includes('saved_retry_owner_excluded'));
  assert.deepEqual(row.saved_retry_attempts.map((attempt) => attempt.retry_path), [[0], [0, 0]]);
  assert.deepEqual(row.saved_retry_attempts[1].evidence_gaps, ['owner_excluded_repository']);
  for (const attempt of row.saved_retry_attempts) {
    assert.equal(attempt.saved_report_evidence, undefined);
    assert.equal(attempt.persisted_role_evidence, undefined);
  }
  assert.deepEqual(fs.readFileSync(options.db), before);
});

test('a retry backend exclusion prevents its cost read while preserving the current role receipt', (t) => {
  const { options } = persistedWorld(t);
  retryRecord(options, 'prior', 2.5);
  addRetries(options, [retryClaim('prior')]);
  const connection = new DatabaseSync(options.db);
  connection.exec(`INSERT INTO runs (run_id,target,ts) VALUES
      ('backend:prior','stranske/Collab-Admin#9',0);
    ALTER TABLE costs RENAME TO cost_rows;
    CREATE VIEW costs AS SELECT run_id,
      CASE WHEN run_id='backend:prior' THEN json_extract('unreadable','$') ELSE cost_usd END AS cost_usd,
      source FROM cost_rows;`);
  assert.throws(() => connection.prepare("SELECT cost_usd FROM costs WHERE run_id='backend:prior'").get(), /malformed JSON/);
  connection.close();
  const before = fs.readFileSync(options.db);
  const row = caseGaps(options).cases[0];
  assert.equal(row.persisted_role_evidence.backend_cost.cost_usd, 1.5);
  const retry = row.saved_retry_attempts[0];
  assert.ok(retry.evidence_gaps.includes('persisted_backend_owner_excluded'));
  assert.equal(retry.persisted_role_evidence.role_run_id, 'role:prior');
  assert.equal(retry.persisted_role_evidence.backend_cost, undefined);
  assert.equal(retry.cost_usd, 'UNKNOWN');
  assert.deepEqual(fs.readFileSync(options.db), before);
});

test('malformed retry histories retain specific paths while valid siblings remain recoverable', (t) => {
  const { options } = persistedWorld(t);
  const row = addRetries(options, [null, [], 'invalid', retryClaim('missing', { retry_attempts: null })]);
  let output = caseGaps(options).cases[0];
  assert.deepEqual(output.saved_retry_history_gaps, [
    { retry_path: [2], kind: 'saved_retry_attempt_invalid' },
    { retry_path: [1], kind: 'saved_retry_attempt_invalid' },
    { retry_path: [0], kind: 'saved_retry_attempt_invalid' },
    { retry_path: [3], kind: 'saved_retry_history_invalid' },
  ]);
  assert.equal(output.saved_retry_attempts.length, 1);
  assert.deepEqual(output.saved_retry_attempts[0].retry_path, [3]);
  assert.ok(output.saved_retry_attempts[0].evidence_gaps.includes('persisted_role_missing'));
  assert.ok(output.evidence_gaps.includes('saved_retry_history_invalid'));
  row.retry_attempts = 'invalid';
  fs.writeFileSync(options.report, JSON.stringify({ rows: [row] }));
  output = caseGaps(options).cases[0];
  assert.deepEqual(output.saved_retry_attempts, []);
  assert.deepEqual(output.saved_retry_history_gaps, [{ retry_path: [], kind: 'saved_retry_history_invalid' }]);
});

test('retry claims retain individual unavailable-store gaps without creating a Brain', (t) => {
  const { options } = persistedWorld(t);
  addRetries(options, [retryClaim('prior')]);
  const connection = new DatabaseSync(options.db);
  connection.exec('ALTER TABLE runs DROP COLUMN role_name');
  connection.close();
  let output = caseGaps(options).cases[0].saved_retry_attempts[0];
  assert.ok(output.evidence_gaps.includes('persisted_role_missing'));
  // Use an existing role identity to exercise the unavailable schema, too.
  addRetries(options, [retryClaim('one')]);
  output = caseGaps(options).cases[0].saved_retry_attempts[0];
  assert.ok(output.evidence_gaps.includes('persisted_role_store_unavailable'));
  fs.unlinkSync(options.db);
  const result = invoke(options, ['--report', options.report, '--persisted-roles']);
  assert.equal(result.status, 2);
  const ledger = JSON.parse(result.stdout);
  output = ledger.cases[0].saved_retry_attempts[0];
  assert.ok(output.evidence_gaps.includes('persisted_role_store_unavailable'));
  assert.equal(output.cost_usd, 'UNKNOWN');
  assert.equal(ledger.inventory_snapshot.population_count, null);
  assert.equal(fs.existsSync(options.db), false);
});

test('opt-in persisted links retain role and cost digests without claiming revision or cohort proof', (t) => {
  const { options, record } = persistedWorld(t);
  const inputs = [options.db, options.manifest, options.report];
  const before = inputs.map((file) => fs.readFileSync(file));
  const files = fs.readdirSync(options.root);
  const ledger = caseGaps(options);
  const row = ledger.cases[0];
  assert.deepEqual(row.persisted_role_evidence, {
    status: 'matched_role_record_only', role_run_id: 'role:one', target: 'fixture/repo#1',
    agent: 'gemini', backend_run_id: 'backend:one', decision: 'uphold_blocker',
    record_sha256: createHash('sha256').update(JSON.stringify(record)).digest('hex'),
    source_revision: 'UNKNOWN', original_dispute_binding_verified: false,
    backend_cost: { status: 'measured_whole_run_receipt', cost_usd: 1.5, source: 'ccusage',
      record_sha256: createHash('sha256').update(JSON.stringify({ run_id: 'backend:one',
        cost_usd: 1.5, source: 'ccusage' })).digest('hex') },
  });
  assert.ok(row.evidence_gaps.includes('source_revision_not_bound_to_persisted_role'));
  assert.ok(!row.evidence_gaps.includes('persisted_role_record_not_verified'));
  for (const field of ['source_revision', 'shadow_result', 'role_run_provenance', 'cost_usd']) {
    assert.equal(row[field], 'UNKNOWN', `a persisted ID alone cannot establish case-level ${field}`);
  }
  assert.equal(ledger.operational_acceptance_complete, false);
  assert.equal(ledger.requested_cohort_verified, false);
  assert.equal(ledger.saved_report_snapshot.persisted_records_verified, false);
  assert.equal(ledger.saved_report_snapshot.persisted_role_lookup_requested, true);
  assert.deepEqual(fs.readdirSync(options.root), files, 'read-only transaction creates no sidecars');
  const cli = invoke(options, ['--report', options.report, '--persisted-roles']);
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), ledger);
  assert.deepEqual(inputs.map((file) => fs.readFileSync(file)), before);
  assert.deepEqual(caseGaps({ ...options, persistedRoles: false }),
    caseGaps({ ...options, persistedRoles: undefined }), 'existing opt-out output is preserved');
});

test('persisted lookup requires the complete role, target, backend and decision identity', (t) => {
  const variants = [
    [{ target: 'fixture/repo#2' }, 'persisted_role_target_mismatch'],
    [{ role: 'redirect' }, 'persisted_role_kind_mismatch'],
    [{ metadata: '{bad' }, 'persisted_role_metadata_invalid'],
    [{ metadata: 'null' }, 'persisted_role_metadata_invalid'],
    [{ metadata: '{"role":"redirect"}' }, 'persisted_role_metadata_invalid'],
    [{ metadata: '{"role":"adjudicator","backend_run_id":"another"}' }, 'persisted_backend_run_id_mismatch'],
    [{ metadata: '{"role":"adjudicator","backend_run_id":"backend:one","action":"reject_blocker"}' }, 'persisted_role_decision_mismatch'],
  ];
  for (const [variant, gap] of variants) {
    const { options } = persistedWorld(t, variant);
    const row = caseGaps(options).cases[0];
    assert.ok(row.evidence_gaps.includes(gap), gap);
    assert.equal(row.persisted_role_evidence, undefined);
    assert.ok(row.evidence_gaps.includes('persisted_role_record_not_verified'));
  }
});

test('missing role identities, ambiguous claims and missing stores have individual gaps', (t) => {
  const { options } = persistedWorld(t);
  const report = JSON.parse(fs.readFileSync(options.report));
  const saved = report.rows[0];
  for (const [row, gap] of [
    [{ ...saved, role_run_id: 'missing' }, 'persisted_role_missing'],
    [{ ...saved, role_run_id: null }, 'persisted_role_lookup_identity_unavailable'],
    [{ ...saved, backend_run_id: null }, 'persisted_backend_run_id_mismatch'],
    [{ ...saved, decision: null }, 'persisted_role_decision_mismatch'],
  ]) {
    fs.writeFileSync(options.report, JSON.stringify({ rows: [row] }));
    const output = caseGaps(options).cases[0];
    assert.ok(output.evidence_gaps.includes(gap), gap);
    assert.equal(output.persisted_role_evidence, undefined);
  }
  fs.writeFileSync(options.report, JSON.stringify({ rows: [saved, saved] }));
  const ambiguous = caseGaps(options).cases[0];
  assert.ok(ambiguous.evidence_gaps.includes('saved_case_ambiguous'));
  assert.equal(ambiguous.persisted_role_evidence, undefined);
  fs.writeFileSync(options.report, JSON.stringify(report));
  const connection = new DatabaseSync(options.db);
  connection.exec('ALTER TABLE runs DROP COLUMN role_name');
  connection.close();
  assert.ok(caseGaps(options).cases[0].evidence_gaps.includes('persisted_role_store_unavailable'));
  fs.unlinkSync(options.db);
  const unavailable = invoke(options, ['--report', options.report, '--persisted-roles']);
  assert.equal(unavailable.status, 2);
  assert.ok(JSON.parse(unavailable.stdout).cases[0].evidence_gaps.includes('persisted_role_store_unavailable'));
  assert.equal(fs.existsSync(options.db), false, 'unavailable store must not be initialized');
});

test('persisted costs follow the production whole-run policy and expose saved cost discrepancies', (t) => {
  for (const [cost, source, measured] of [
    [1.5, 'ccusage', true], [2.5, 'ccusage', true],
    [0, 'ccusage', false], [-1, 'ccusage', false], [null, 'ccusage', false],
    [1.5, 'ledger', false], [1.5, 'untrusted', false],
  ]) {
    const { options } = persistedWorld(t, { cost, source });
    const row = caseGaps(options).cases[0];
    const receipt = row.persisted_role_evidence.backend_cost;
    assert.equal(receipt.status, measured ? 'measured_whole_run_receipt' : 'unknown');
    assert.equal(receipt.cost_usd, measured ? cost : 'UNKNOWN');
    assert.equal(row.evidence_gaps.includes('persisted_cost_not_measured'), !measured);
    assert.equal(row.evidence_gaps.includes('saved_cost_differs_from_persisted_receipt'), measured && cost !== 1.5);
    assert.equal(row.cost_usd, 'UNKNOWN');
  }
  const { options } = persistedWorld(t);
  const connection = new DatabaseSync(options.db);
  connection.exec('DELETE FROM costs');
  assert.ok(caseGaps(options).cases[0].evidence_gaps.includes('persisted_cost_missing'));
  connection.exec('DROP TABLE costs');
  connection.close();
  const row = caseGaps(options).cases[0];
  assert.ok(row.evidence_gaps.includes('persisted_cost_store_unavailable'));
  assert.equal(row.persisted_role_evidence.backend_cost.status, 'unknown');
});

test('excluded role target is rejected before metadata is evaluated', (t) => {
  const { options } = persistedWorld(t, { target: '\tStRaNsKe/CoLlAb-AdMiN\u00a0#9' });
  const connection = new DatabaseSync(options.db);
  connection.exec(`ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,ts,role_name,agent,
      CASE WHEN run_id='role:one' THEN json_extract('unreadable','$') ELSE decomposition END AS decomposition
      FROM run_rows;`);
  assert.throws(() => connection.prepare("SELECT decomposition FROM runs WHERE run_id='role:one'").get(), /malformed JSON/);
  connection.close();
  const before = fs.readFileSync(options.db);
  const row = caseGaps(options).cases[0];
  assert.ok(row.evidence_gaps.includes('persisted_role_target_mismatch'));
  assert.equal(row.persisted_role_evidence, undefined);
  assert.deepEqual(fs.readFileSync(options.db), before);
});

test('excluded backend identity prevents cost reads', (t) => {
  const { options } = persistedWorld(t, { backendTarget: '\tStRaNsKe/CoLlAb-AdMiN\u00a0#9' });
  const connection = new DatabaseSync(options.db);
  connection.exec(`ALTER TABLE costs RENAME TO cost_rows;
    CREATE VIEW costs AS SELECT run_id,json_extract('unreadable','$') AS cost_usd,source FROM cost_rows;`);
  assert.throws(() => connection.prepare('SELECT cost_usd FROM costs').get(), /malformed JSON/);
  connection.close();
  const before = fs.readFileSync(options.db);
  const row = caseGaps(options).cases[0];
  assert.ok(row.evidence_gaps.includes('persisted_backend_owner_excluded'));
  assert.equal(row.persisted_role_evidence.backend_cost, undefined);
  assert.deepEqual(fs.readFileSync(options.db), before);
});

test('excluded requested identities trigger no persisted role or cost evidence reads', (t) => {
  const { options } = persistedWorld(t);
  const requested = identity('one', 'stranske/Collab-Admin#1');
  fs.writeFileSync(options.manifest, JSON.stringify({ schema_version: 2, cases: [requested] }));
  fs.writeFileSync(options.report, JSON.stringify({ rows: [{ ...requested,
    role_run_id: 'role:one', backend_run_id: 'backend:one' }] }));
  const connection = new DatabaseSync(options.db);
  connection.exec(`ALTER TABLE runs RENAME TO run_rows;
    CREATE VIEW runs AS SELECT run_id,target,ts,json_extract('unreadable','$') AS role_name,
      agent,json_extract('unreadable','$') AS decomposition FROM run_rows;
    ALTER TABLE costs RENAME TO cost_rows;
    CREATE VIEW costs AS SELECT run_id,json_extract('unreadable','$') AS cost_usd,source FROM cost_rows;`);
  connection.close();
  const row = caseGaps(options).cases[0];
  assert.deepEqual(row.evidence_gaps, ['owner_excluded_repository']);
  assert.equal(row.persisted_role_evidence, undefined);
});

test('persisted role CLI flag requires a saved report and produces no partial output', (t) => {
  const options = world(t, [identity('one')]);
  const before = fs.readFileSync(options.db);
  assert.throws(() => caseGaps({ ...options, persistedRoles: true }), /requires --report/);
  const result = invoke(options, ['--persisted-roles']);
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /requires --report/);
  assert.deepEqual(fs.readFileSync(options.db), before);
});

test('persisted lookup reads the production role recorder and agrees with production cost grading', (t) => {
  const { options } = persistedWorld(t);
  addRetries(options, [retryClaim('prior', { cost_usd: 2.5 })]);
  fs.unlinkSync(options.db);
  const out = fs.openSync(path.join(options.root, 'production-stdout'), 'w');
  const err = fs.openSync(path.join(options.root, 'production-stderr'), 'w');
  let child;
  try {
    child = spawnSync(process.env.PYTHON || 'python3', ['-c', `
import json
import time
import feedback
feedback.record_role_run(
    'role:one', 'adjudicator', 'fixture/repo#1', 'gemini',
    backend_run_id='backend:one', action='uphold_blocker',
    decision_source='adjudicator_agent', source='retrospective',
)
feedback.record_role_run(
    'role:prior', 'adjudicator', 'fixture/repo#1', 'gemini',
    backend_run_id='backend:prior', action='needs_more_evidence',
    decision_source='adjudicator_agent', source='retrospective',
)
with feedback._conn() as conn:
    conn.execute("INSERT INTO runs(run_id,target,ts) VALUES ('one','fixture/repo#1',?)", (int(time.time()),))
    conn.execute("INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict) VALUES ('one','NON_PASS','PASS')")
    conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend:one',1.5,'ccusage')")
    conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend:prior',2.5,'ccusage')")
positive = feedback.role_cost_report('backend:one', 'gemini')
prior = feedback.role_cost_report('backend:prior', 'gemini')
with feedback._conn() as conn:
    conn.execute("UPDATE costs SET cost_usd=0 WHERE run_id='backend:one'")
zero = feedback.role_cost_report('backend:one', 'gemini')
print(json.dumps({'positive': positive, 'zero': zero, 'prior': prior}))
`], {
      cwd: options.root, timeout: 30000, stdio: ['ignore', out, err],
      env: { PATH: process.env.PATH,
        PYTHONPATH: path.resolve(__dirname, '../src'), PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: path.join(options.root, 'state'),
        ORCH_LOCAL_RUNTIME: path.join(options.root, 'runtime'),
        ORCH_FEEDBACK_DB: options.db,
        ORCH_CAPABILITIES_PATH: path.join(options.root, 'capabilities.json') },
    });
  } finally { fs.closeSync(out); fs.closeSync(err); }
  assert.ifError(child.error);
  assert.equal(child.status, 0, fs.readFileSync(path.join(options.root, 'production-stderr'), 'utf8'));
  const grades = JSON.parse(fs.readFileSync(path.join(options.root, 'production-stdout')));
  const zeroBefore = fs.readFileSync(options.db);
  const zero = caseGaps(options).cases[0];
  assert.equal(zero.persisted_role_evidence.backend_cost.cost_usd, 'UNKNOWN');
  assert.equal(grades.zero.status, 'unmeasured');
  assert.deepEqual(fs.readFileSync(options.db), zeroBefore);
  const connection = new DatabaseSync(options.db);
  connection.exec("UPDATE costs SET cost_usd=1.5 WHERE run_id='backend:one'");
  connection.close();
  const positiveBefore = fs.readFileSync(options.db);
  const positive = caseGaps(options).cases[0];
  assert.equal(positive.persisted_role_evidence.backend_cost.cost_usd, grades.positive.cost_usd);
  assert.equal(grades.positive.status, 'measured');
  assert.equal(positive.persisted_role_evidence.decision, 'uphold_blocker');
  assert.equal(positive.source_revision, 'UNKNOWN');
  assert.equal(positive.role_run_provenance, 'UNKNOWN');
  const retry = positive.saved_retry_attempts[0];
  assert.deepEqual(retry.retry_path, [0]);
  assert.equal(retry.persisted_role_evidence.role_run_id, 'role:prior');
  assert.equal(retry.persisted_role_evidence.decision, 'needs_more_evidence');
  assert.equal(retry.persisted_role_evidence.backend_cost.cost_usd, grades.prior.cost_usd);
  assert.equal(grades.prior.status, 'measured');
  assert.equal(retry.role_run_provenance, 'UNKNOWN');
  assert.equal(retry.cost_usd, 'UNKNOWN');
  assert.deepEqual(fs.readFileSync(options.db), positiveBefore);
});
