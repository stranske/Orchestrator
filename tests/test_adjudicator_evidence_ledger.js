'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { test } = require('node:test');
const { auditLedger } = require('../scripts/audit_adjudicator_evidence_ledger');

const evidence = path.resolve(__dirname, '../docs/verification/issue-1096-recovery');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-ledger-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const directory = path.join(root, 'evidence');
  fs.cpSync(evidence, directory, { recursive: true });
  const file = path.join(directory, 'evidence-ledger.json');
  const ledger = JSON.parse(fs.readFileSync(file));
  return { root, directory, file, ledger,
    save() { fs.writeFileSync(file, JSON.stringify(ledger)); } };
}

test('published recovery ledger is attributable without establishing operational acceptance', () => {
  const result = auditLedger(path.join(evidence, 'evidence-ledger.json'));
  assert.equal(result.attribution_and_artifact_checks_passed, true);
  assert.equal(result.operational_acceptance_verified, false);
  assert.equal(result.external_references_authenticated, false);
  assert.equal(result.source_revision_bindings_verified, false);
  assert.deepEqual([...new Set(result.external_references.map((item) => item.category))],
    ['historical_observations', 'mirror_synchronization', 'dispatch_receipts']);
  assert.ok(result.external_references.every((item) => item.authenticated_by_audit === false));
  assert.ok(result.artifacts.some((item) => item.path === 'historical-inventory.json'));
  assert.ok(result.artifacts.some((item) => item.path === 'named-tests.log'));
  assert.ok(result.artifacts.some((item) => item.path === 'interpreter-followup/runner-before.log'));
  assert.ok(result.missing_evidence.some((item) => item.includes('October 18')));
});

for (const log of ['named-tests.log', 'interpreter-followup/runner-after.log',
  'pre-dispatch-observation.json']) {
  test(`rejects modified evidence instead of trusting the recorded result: ${log}`, (t) => {
    const options = fixture(t);
    fs.appendFileSync(path.join(options.directory, log), '\nchanged\n');
    assert.throws(() => auditLedger(options.file), /SHA-256 mismatch/);
  });
}

for (const name of ['plan.md', 'results.md']) {
  for (const mode of ['changed', 'missing']) {
    test(`rejects ${mode} hash-map evidence: ${name}`, (t) => {
      const options = fixture(t);
      const file = path.join(options.directory, 'retry-identity-followup', name);
      if (mode === 'changed') fs.appendFileSync(file, '\nchanged\n');
      else fs.unlinkSync(file);
      assert.throws(() => auditLedger(options.file), /SHA-256 mismatch|ENOENT/);
    });
  }
}

test('rejects missing evidence even when its parent receipt says it passed', (t) => {
  const options = fixture(t);
  fs.unlinkSync(path.join(options.directory, 'acceptance-run/runner-tests.log'));
  assert.throws(() => auditLedger(options.file), /ENOENT/);
});

for (const missing of [null, undefined]) {
  test(`rejects a missing checksum on a referenced log: ${missing}`, (t) => {
    const options = fixture(t);
    const file = path.join(options.directory, 'checks.json');
    const checks = JSON.parse(fs.readFileSync(file));
    checks.checks[0].log_sha256 = missing;
    fs.writeFileSync(file, JSON.stringify(checks));
    assert.throws(() => auditLedger(options.file), /Missing or invalid SHA-256/);
  });
}

test('audits JUnit bytes and refuses an absent JUnit checksum', (t) => {
  const options = fixture(t);
  const file = path.join(options.directory, 'checks.json');
  const checks = JSON.parse(fs.readFileSync(file));
  const xml = '<testsuites />';
  fs.writeFileSync(path.join(options.directory, 'result.xml'), xml);
  checks.checks[0].junit = 'result.xml';
  checks.checks[0].junit_sha256 = createHash('sha256').update(xml).digest('hex');
  fs.writeFileSync(file, JSON.stringify(checks));
  options.ledger.current_observations.checks_sha256 = createHash('sha256')
    .update(fs.readFileSync(file)).digest('hex');
  options.save();
  assert.equal(auditLedger(options.file).operational_acceptance_verified, false);
  delete checks.checks[0].junit_sha256;
  fs.writeFileSync(file, JSON.stringify(checks));
  assert.throws(() => auditLedger(options.file), /Missing or invalid SHA-256/);
});

for (const mode of ['relative', 'absolute', 'symlink']) {
  test(`refuses evidence outside the ledger directory: ${mode}`, (t) => {
    const options = fixture(t);
    const outside = path.join(options.root, 'outside.json');
    fs.writeFileSync(outside, '{}');
    if (mode === 'symlink') fs.symlinkSync(outside, path.join(options.directory, 'link.json'));
    options.ledger.current_observations.checks = mode === 'relative' ? '../outside.json' :
      mode === 'absolute' ? outside : 'link.json';
    options.save();
    assert.throws(() => auditLedger(options.file), /relative to the ledger|escapes/);
  });
}

test('does not equate a historical backup with the canonical report', (t) => {
  const options = fixture(t);
  options.ledger.historical_observations[0].reference =
    options.ledger.revision_identity.canonical_report.reference;
  options.save();
  assert.throws(() => auditLedger(options.file), /Historical inventory attribution/);
});

test('rejects a retained row count substituted for the saved population', (t) => {
  const options = fixture(t);
  const observation = options.ledger.historical_observations[0];
  assert.notEqual(observation.retained_rows, observation.retained_population);
  observation.retained_population = observation.retained_rows;
  options.save();
  assert.throws(() => auditLedger(options.file), /retained counts disagree/);
});

for (const [name, mutate, message] of [
  ['merged historical/current categories', (ledger) => {
    ledger.historical_observations = [];
  }, /Historical observations must remain separate/],
  ['missing dated limitations', (ledger) => {
    delete ledger.historical_observations[2].limitation;
  }, /dates, classes and limitations/],
  ['missing disclosure', (ledger) => { ledger.missing_evidence = []; }, /must be disclosed/],
  ['missing October 18 disclosure', (ledger) => {
    ledger.missing_evidence = ledger.missing_evidence.filter((item) => !item.includes('October 18'));
  }, /October 18 proof/],
  ['advice grade substitution', (ledger) => {
    ledger.grade_thresholds.advice_graded_is_substitute = true;
  }, /separate from advice_graded/],
  ['unproved acceptance', (ledger) => { ledger.acceptance_complete = true; }, /acceptance_complete/],
  ['replacement cohort count', (ledger) => {
    ledger.fixed_next_20.verified_members_with_decisions = 15;
  }, /fixed membership/],
  ['mirror identity substitution', (ledger) => {
    ledger.revision_identity.current_mirror_revision.status = 'verified';
  }, /unverified accepted\/deployed\/mirror/],
  ['historical dispatch substitution', (ledger) => {
    ledger.dispatch_receipts.current_run.production_dispatches = 27;
  }, /current dispatch\/publication claim/],
]) {
  test(`rejects ${name}`, (t) => {
    const options = fixture(t);
    mutate(options.ledger);
    options.save();
    assert.throws(() => auditLedger(options.file), message);
  });
}

for (const reference of ['checks.json', 'receipt-binding-followup/validation.json',
  'attempt-diagnostics/historical-acquisition.json',
  'packet-floor-followup/historical-acquisition.json']) {
  for (const mode of ['changed', 'missing']) {
    test(`rejects ${mode} pinned receipt/acquisition: ${reference}`, (t) => {
      const options = fixture(t);
      const file = path.join(options.directory, reference);
      if (mode === 'missing') fs.unlinkSync(file);
      else fs.appendFileSync(file, '\n ');
      assert.throws(() => auditLedger(options.file), /SHA-256 mismatch|ENOENT/);
    });
  }
}

for (const field of ['evaluated_revision', 'required_tests_passed']) {
  for (const side of ['receipt', 'ledger']) {
    test(`rejects ${side} disagreement even with a valid receipt pin: ${field}`, (t) => {
      const options = fixture(t);
      const entry = options.ledger.current_observations.required_test_followup;
      const file = path.join(options.directory, entry.checks);
      if (side === 'ledger') {
        entry[entry.checks_bindings[field]] = field === 'required_tests_passed' ? true : '0'.repeat(40);
      } else {
        const receipt = JSON.parse(fs.readFileSync(file));
        receipt[field] = field === 'required_tests_passed' ? true : '0'.repeat(40);
        fs.writeFileSync(file, JSON.stringify(receipt));
        entry.checks_sha256 = createHash('sha256').update(fs.readFileSync(file)).digest('hex');
      }
      options.save();
      assert.throws(() => auditLedger(options.file), /Ledger\/receipt disagreement/);
    });
  }
}

for (const key of ['checks_sha256', 'checks_bindings']) {
  test(`requires explicit receipt pin and correspondence: ${key}`, (t) => {
    const options = fixture(t);
    delete options.ledger.current_observations.required_test_followup[key];
    options.save();
    assert.throws(() => auditLedger(options.file), /Missing.*SHA-256|Missing field bindings|disagreement/);
  });
}
