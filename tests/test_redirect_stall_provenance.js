'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { audit, fixtureNames, main } = require('../scripts/audit_redirect_stall_provenance');

const fixtures = path.join(__dirname, 'fixtures/redirect_stalls');
const digest = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');

function cli(t, args) {
  const stdout = [];
  const stderr = [];
  const log = t.mock.method(console, 'log', (value) => stdout.push(value));
  const error = t.mock.method(console, 'error', (value) => stderr.push(value));
  try {
    return { status: main(args), stdout: stdout.join('\n'), stderr: stderr.join('\n') };
  } finally {
    log.mock.restore();
    error.mock.restore();
  }
}

function sandbox(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-provenance-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const copy = path.join(root, 'fixtures');
  fs.mkdirSync(copy);
  // Manufactured inputs exercise the auditor only. They are NEVER historical
  // provenance evidence; the real private corpus is unavailable in this runner.
  const targets = new Map();
  const entries = fixtureNames.map((name) => {
    const bytes = fs.readFileSync(path.join(fixtures, name));
    fs.writeFileSync(path.join(copy, name), bytes);
    const report = JSON.parse(bytes);
    if (!targets.has(report.target)) targets.set(report.target, `PRIVATE-TARGET-${targets.size}`);
    report.target = targets.get(report.target);
    delete report.policy_decision;
    return {
      kind: 'redirect_proposal', entry_id: `PRIVATE-ENTRY-${name}`,
      ts: 1792454400, report,
    };
  });
  const corpus = path.join(root, 'shadow.jsonl');
  function save() {
    fs.writeFileSync(corpus, entries.map((entry) => JSON.stringify(entry)).join('\n') + '\n');
  }
  save();
  return { root, copy, corpus, entries, save };
}

test('binds all twelve reports by retained facts and corpus bytes, without writes or private values', (t) => {
  const s = sandbox(t);
  // Healthy proposals and other event types are not historical stall bindings.
  s.entries.unshift({ kind: 'outcome', report: { state: 'stalled' } });
  s.entries.unshift({ kind: 'redirect_proposal', report: { state: 'healthy' } });
  s.save();
  const files = [s.corpus, ...fixtureNames.map((name) => path.join(s.copy, name))];
  const before = files.map((file) => fs.readFileSync(file));
  const started = Date.now();
  const result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'verified');
  assert.equal(result.verified_bindings, 12);
  assert.equal(result.stalled_source_reports, 12);
  assert.equal(result.corpus_sha256, digest(before[0]));
  assert.deepEqual(result.bindings.map((row) => row.fixture), fixtureNames);
  for (let i = 0; i < 12; i++) {
    assert.equal(result.bindings[i].fixture_sha256, digest(before[i + 1]));
    assert.equal(result.bindings[i].source_line, i + 3);
    assert.equal(result.bindings[i].source_entry_sha256, digest(JSON.stringify(s.entries[i + 2])));
  }
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.equal(result.source_authenticity, 'requires_owner_receipt');
  assert.ok(Date.parse(result.observed_at) >= started);
  assert.ok(Date.parse(result.observed_at) <= Date.now());
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-|sweep_fingerprint|1792454400/);
  assert.deepEqual(files.map((file) => fs.readFileSync(file)), before);
  assert.deepEqual(fs.readdirSync(s.root).sort(), ['fixtures', 'shadow.jsonl']);
});

test('missing private corpus is unavailable with a nonzero CLI exit, never zero candidates', (t) => {
  const s = sandbox(t);
  const run = cli(t, [path.join(s.root, 'PRIVATE-missing'), s.copy]);
  assert.equal(run.status, 1, run.stderr);
  const result = JSON.parse(run.stdout);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.verified_bindings, 0);
  assert.deepEqual(result.limitations, ['private_source_corpus_unavailable']);
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.doesNotMatch(run.stdout + run.stderr, /PRIVATE-|candidates/);
});

test('rejects missing, duplicate and reused source bindings', (t) => {
  const s = sandbox(t);
  const original = structuredClone(s.entries);
  s.entries.pop();
  s.save();
  let result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'mismatch');
  assert.equal(result.verified_bindings, 11);
  assert.match(result.limitations.join(), /source_binding_missing_or_ambiguous/);

  s.entries.splice(0, s.entries.length, ...original, original[0]);
  s.save();
  result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'mismatch');
  assert.equal(result.verified_bindings, 11);

  s.entries.pop();
  s.save();
  fs.copyFileSync(path.join(s.copy, fixtureNames[0]), path.join(s.copy, fixtureNames[1]));
  result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'mismatch');
  assert.equal(result.verified_bindings, 11);
});

test('rejects changed policy, liveness, hints, drift and all other retained source facts', (t) => {
  const s = sandbox(t);
  const report = s.entries[0].report;
  for (const key of Object.keys(report).filter((key) => key !== 'target')) {
    const original = report[key];
    report[key] = { changed: true };
    s.save();
    const result = audit(s.corpus, s.copy);
    assert.notEqual(result.status, 'verified', key);
    assert.ok(result.limitations.length, key);
    report[key] = original;
  }
  s.save();
  assert.equal(audit(s.corpus, s.copy).status, 'verified');

  const file = path.join(s.copy, fixtureNames[0]);
  const fixture = JSON.parse(fs.readFileSync(file));
  fixture.policy_decision.action = 'wait';
  fs.writeFileSync(file, JSON.stringify(fixture));
  assert.match(audit(s.corpus, s.copy).limitations.join(), /policy_mapping_differs/);
});

test('rejects both merged distinct targets and split repeated targets', (t) => {
  const s = sandbox(t);
  const first = s.entries[0].report.target;
  const distinct = s.entries.find((entry) => entry.report.target !== first);
  const original = distinct.report.target;
  distinct.report.target = first;
  s.save();
  assert.match(audit(s.corpus, s.copy).limitations.join(), /target_anonymization_not_one_to_one/);
  distinct.report.target = original;

  const counts = new Map();
  const repeated = s.entries.find((entry) => {
    const target = entry.report.target;
    counts.set(target, (counts.get(target) || 0) + 1);
    return counts.get(target) > 1;
  });
  assert.ok(repeated, 'the real fixture roster includes repeated anonymous targets');
  repeated.report.target = 'PRIVATE-new-distinct-target';
  s.save();
  const result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'mismatch');
  assert.match(result.limitations.join(), /target_anonymization_not_one_to_one/);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
});

test('rejects unchanged source targets even when the mapping is one-to-one', (t) => {
  const s = sandbox(t);
  const files = [s.corpus, ...fixtureNames.map((name) => path.join(s.copy, name))];
  // Preserve repeated targets and every retained fact. Only anonymization is absent.
  for (let i = 0; i < fixtureNames.length; i++) {
    const fixture = JSON.parse(fs.readFileSync(files[i + 1]));
    s.entries[i].report.target = fixture.target;
  }
  s.save();
  const before = files.map((file) => fs.readFileSync(file));
  const run = cli(t, [s.corpus, s.copy]);
  assert.equal(run.status, 1);
  const result = JSON.parse(run.stdout);
  assert.equal(result.status, 'mismatch');
  assert.equal(result.verified_bindings, 0);
  assert.deepEqual(result.bindings, []);
  assert.deepEqual(result.limitations,
    fixtureNames.map((name) => `${name}: target_not_anonymized`));
  assert.equal(result.historical_replay_receipt, 'not_established');
  for (const entry of s.entries) {
    assert.ok(!run.stdout.includes(entry.report.target), 'source targets must stay private');
  }
  assert.deepEqual(files.map((file) => fs.readFileSync(file)), before);
});

test('rejects original source targets reused as anonymous labels for other bindings', (t) => {
  const s = sandbox(t);
  const targets = [...new Set(s.entries.map((entry) => entry.report.target))];
  assert.ok(targets.length > 1);
  // Rotating every distinct target preserves a bijection and changes every
  // individual label, but still discloses the original source target set.
  for (let i = 0; i < fixtureNames.length; i++) {
    const file = path.join(s.copy, fixtureNames[i]);
    const fixture = JSON.parse(fs.readFileSync(file));
    const original = s.entries[i].report.target;
    fixture.target = targets[(targets.indexOf(original) + 1) % targets.length];
    assert.notEqual(fixture.target, original);
    fs.writeFileSync(file, JSON.stringify(fixture));
  }
  const files = [s.corpus, ...fixtureNames.map((name) => path.join(s.copy, name))];
  const before = files.map((file) => fs.readFileSync(file));
  const run = cli(t, [s.corpus, s.copy]);
  assert.equal(run.status, 1);
  const result = JSON.parse(run.stdout);
  assert.equal(result.status, 'mismatch');
  assert.equal(result.verified_bindings, 0);
  assert.deepEqual(result.bindings, []);
  assert.deepEqual(result.limitations,
    fixtureNames.map((name) => `${name}: target_not_anonymized`));
  assert.doesNotMatch(run.stdout + run.stderr, /PRIVATE-/);
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.deepEqual(files.map((file) => fs.readFileSync(file)), before);
});

test('also rejects source targets from additional unbound stalled reports', (t) => {
  const s = sandbox(t);
  const file = path.join(s.copy, fixtureNames[0]);
  const fixture = JSON.parse(fs.readFileSync(file));
  fixture.target = 'PRIVATE-unbound-source-target';
  fs.writeFileSync(file, JSON.stringify(fixture));
  s.entries.push({ kind: 'redirect_proposal', report: {
    state: 'stalled', target: fixture.target, sweep_fingerprint: 'PRIVATE-unbound-fingerprint',
  } });
  s.save();
  const result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'mismatch');
  assert.equal(result.stalled_source_reports, 13);
  assert.equal(result.verified_bindings, 11);
  assert.deepEqual(result.limitations, [`${fixtureNames[0]}: target_not_anonymized`]);
  assert.ok(result.bindings.every((row) => row.fixture !== fixtureNames[0]));
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
});

test('fails closed on malformed private input and missing fixture files', (t) => {
  const s = sandbox(t);
  fs.appendFileSync(s.corpus, 'PRIVATE-invalid-json\n');
  let result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.verified_bindings, 0);
  assert.deepEqual(result.limitations, ['invalid_source_corpus_json']);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
  s.save();
  fs.unlinkSync(path.join(s.copy, fixtureNames[11]));
  result = audit(s.corpus, s.copy);
  assert.equal(result.status, 'mismatch');
  assert.deepEqual(result.limitations, ['expected_twelve_fixture_files']);
  assert.equal(result.verified_bindings, 0);
});

test('successful CLI audits remain current observations with no replay receipt credit', (t) => {
  const s = sandbox(t);
  const run = cli(t, [s.corpus, s.copy]);
  assert.equal(run.status, 0, run.stderr);
  const result = JSON.parse(run.stdout);
  assert.equal(result.status, 'verified');
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.doesNotMatch(run.stdout, /PRIVATE-|1792454400/);
  const usage = cli(t, []);
  assert.equal(usage.status, 2);
  assert.match(usage.stderr, /Usage:/);
});
