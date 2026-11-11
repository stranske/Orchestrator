'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { execute } = require('../scripts/run_adjudicator_acceptance');
const { auditProvisionLedger, formatReport, instant } = require('../scripts/audit_provision_ledger');

const source = path.resolve(__dirname, '../src');
const script = path.resolve(__dirname, '../scripts/audit_provision_ledger.js');
const now = '2026-11-11T12:00:00.123456+00:00';
const cutoff = '2026-11-04T12:00:00.123456+00:00';
const event = (changes = {}) => ({ ts: now, target: 'example/repo#1',
  outcome: 'failed', cause: 'branch-held', ...changes });
const ndjson = (rows) => rows.map((row) => JSON.stringify(row) + '\n').join('');

// Use the real existing collector to create the receipt, with genuine sequential
// hourly observations on disposable inputs. No pytest or fixture receipt doubles.
const collect = `
import json, sys
from datetime import datetime, timedelta
from pathlib import Path
import followup_evidence as evidence
root, source = map(Path, sys.argv[1:3])
now = datetime.fromisoformat(sys.argv[3])
state = {}
for hour in range(169):
    report, state = evidence.provision_report(
        root, state, now - timedelta(hours=168-hour), source / 'provision.py')
print(json.dumps({'provision': report}))
`;

function fixture(t, segments = { '': ndjson([event()]) }, end = now, relativeRoot = false) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'provision-ledger-audit-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const [suffix, bytes] of Object.entries(segments)) {
    fs.writeFileSync(path.join(root, 'provision-events.ndjson' + suffix), bytes);
  }
  const collectorRoot = relativeRoot ? path.relative(process.cwd(), root) : root;
  const collected = execute(process.env.PYTHON || 'python3', ['-c', collect, collectorRoot, source, end],
    { cwd: process.cwd(), env: { ...process.env, PYTHONPATH: source, PYTHONDONTWRITEBYTECODE: '1' } });
  assert.equal(collected.error_code, null);
  assert.equal(collected.signal, null);
  assert.equal(collected.exit_code, 0, collected.stderr.toString());
  fs.mkdirSync(path.join(root, 'followup-evidence'));
  const receipt = path.join(root, 'followup-evidence/latest.json');
  fs.writeFileSync(receipt, collected.stdout);
  const audit = () => auditProvisionLedger(root, source,
    { now: end, writerPath: path.join(root, 'provision-events.ndjson') });
  const changeReceipt = (change) => {
    const document = JSON.parse(fs.readFileSync(receipt, 'utf8'));
    change(document.provision);
    fs.writeFileSync(receipt, JSON.stringify(document));
  };
  return { root, receipt, audit, changeReceipt };
}

test('recounts active and archived failures with multiplicity and exact seven-day boundaries', (t) => {
  const duplicate = event({ ts: cutoff, cause: 'branch-exists' });
  const f = fixture(t, { '': ndjson([event(), duplicate,
    event({ ts: '2026-11-04T12:00:00.123455+00:00' }),
    event({ ts: '2026-11-11T12:00:00.123457+00:00' }),
    event({ outcome: 'reclaimed' }), event({ cause: 'probe-blind' })]),
  '.old': ndjson([duplicate]), '.older': ndjson([duplicate]) });
  const result = f.audit();
  assert.equal(result.coverage, 'continuous');
  assert.equal(result.continuous_since, cutoff);
  assert.equal(result.branch_refusals, 4);
  assert.equal(result.counts['failed/branch-exists'], 3);
  assert.equal(result.counts['failed/branch-held'], 1);
  assert.equal(result.counts['reclaimed/branch-held'], 1);
  assert.equal(result.counts['failed/probe-blind'], 1);
  assert.equal(result.segments.length, 3);
  assert.equal(result.deployment_acceptance_verified, false);
  assert.equal(result.evidence_ready, false);
  assert.match(formatReport(result), /baseline = 2026-11-04T12:00:00.123456\+00:00/);
  assert.match(formatReport(result), /failed\(branch-held\|branch-exists\) = 4 over 7d/);
  assert.match(formatReport(result), /unmet acceptance/);
});

test('an archived-only ledger is measured; absent ledger stays unmeasured', (t) => {
  assert.equal(fixture(t, { '.old': ndjson([event()]) }).audit().branch_refusals, 1);
  const result = fixture(t, {}).audit();
  assert.equal(result.evidence_ready, false);
  assert.match(formatReport(result), /unmeasured: no ledger/);
});

test('empty intact observed week can qualify only ledger evidence', (t) => {
  const result = fixture(t, { '': '' }).audit();
  assert.equal(result.evidence_ready, true);
  assert.equal(result.coverage, 'continuous');
  assert.equal(result.deployment_acceptance_verified, false);
  assert.match(formatReport(result), /native\/full-verifier\/mirror acceptance requires separate receipts/);
});

test('preserves the existing collector owner exclusion before parsing and hashing', (t) => {
  const f = fixture(t, { '': ndjson([event(),
    event({ target: 'STRANSKE/' + 'COLLAB-ADMIN#1' }),
    event({ target: 'stransKe/' + 'Collab-Admin#1' })]) });
  const result = f.audit();
  assert.equal(result.branch_refusals, 2);
  assert.equal(result.coverage, 'continuous');
  assert.equal(result.segments[0].lines, 2);
});

for (const [name, bytes] of Object.entries({ torn: JSON.stringify(event()),
  broken: '{\n', null: 'null\n', array: '[]\n', naive: ndjson([event({ ts: '2026-11-11T12:00:00' })]),
  outcome: ndjson([event({ outcome: null })]) })) {
  test(`malformed ${name} data cannot become an intact zero`, (t) => {
    const result = fixture(t, { '': bytes }).audit();
    assert.equal(result.malformed_lines, 1);
    assert.equal(result.coverage, 'UNKNOWN');
    assert.equal(result.evidence_ready, false);
  });
}

for (const operation of ['delete', 'rewrite', 'append', 'duplicate', 'rotate']) {
  test(`segment ${operation} after observation requires a fresh collector receipt`, (t) => {
    const f = fixture(t);
    const ledger = path.join(f.root, 'provision-events.ndjson');
    if (operation === 'delete') fs.unlinkSync(ledger);
    if (operation === 'rewrite') fs.writeFileSync(ledger, '');
    if (operation === 'append') fs.appendFileSync(ledger, ndjson([event()]));
    if (operation === 'duplicate') fs.copyFileSync(ledger, ledger + '.old');
    if (operation === 'rotate') fs.renameSync(ledger, ledger + '.old');
    assert.equal(f.audit().evidence_ready, false);
    assert.equal(f.audit().coverage, 'UNKNOWN');
  });
}

for (const [name, change] of Object.entries({
  baseline: (r) => { r.continuous_since = now; },
  coverage: (r) => { r.coverage = 'UNKNOWN'; },
  continuity: (r) => { r.continuity_failures = ['previous ledger prefix missing or rewritten']; },
  gap: (r) => { r.collection_max_gap_seconds = 86400; },
  counts: (r) => { r.counts = {}; r.branch_refusals = 0; },
  timestamp: (r) => { r.checked_at = 'bad'; },
  window: (r) => { r.window_start = now; },
  source: (r) => { r.instrument_sha256 = '0'.repeat(64); },
  collector: (r) => { r.collector_sha256 = '0'.repeat(64); },
  readiness: (r) => { r.evidence_ready = true; },
})) {
  test(`inconsistent collector ${name} fails closed`, (t) => {
    const f = fixture(t);
    f.changeReceipt(change);
    assert.equal(f.audit().evidence_ready, false);
    assert.equal(f.audit().coverage, 'UNKNOWN');
  });
}

test('missing or malformed receipt cannot infer continuity from old events', (t) => {
  const f = fixture(t, { '': ndjson([event({ ts: cutoff })]) });
  for (const raw of ['', 'null', '{}', '{"provision":[]}', '{"provision":{"issue":1098}}']) {
    fs.writeFileSync(f.receipt, raw);
    assert.equal(f.audit().evidence_ready, false);
    assert.equal(f.audit().coverage, 'UNKNOWN');
    assert.equal(f.audit().branch_refusals, 1);
    assert.equal(f.audit().counted_through, now);
  }
  fs.unlinkSync(f.receipt);
  assert.equal(f.audit().coverage, 'UNKNOWN');
  assert.equal(f.audit().branch_refusals, 1);
  assert.match(formatReport(f.audit()), /failed\(branch-held\|branch-exists\) = 1 over 7d/);
});

test('a stale or future receipt cannot qualify and timestamps retain microseconds', (t) => {
  const f = fixture(t, { '': '' });
  for (const stamp of ['2026-11-11T14:00:00.123457+00:00',
    '2026-11-11T12:00:00.123455+00:00']) {
    assert.equal(auditProvisionLedger(f.root, source, { now: stamp }).evidence_ready, false);
  }
  assert.equal(instant(now) - instant('2026-11-11T12:00:00.123455Z'), 1n);
  assert.equal(instant(now), instant('2026-11-11T13:00:00.123456+01:00'));
  assert.throws(() => instant('2026-02-30T12:00:00Z'), /invalid calendar/);
});

test('reads all inputs without writing and CLI status distinguishes unmet evidence', (t) => {
  const end = new Date().toISOString();
  const f = fixture(t, { '': '', '.old': '' }, end);
  const before = [f.receipt, path.join(f.root, 'provision-events.ndjson'),
    path.join(f.root, 'provision-events.ndjson.old')].map((file) => [file, fs.readFileSync(file)]);
  const options = { cwd: f.root, env: { ...process.env, ORCH_LOCAL_RUNTIME: f.root } };
  delete options.env.ORCH_PROVISION_EVENTS_PATH;
  const result = execute(process.execPath, [script, f.root, source], options);
  assert.equal(result.error_code, null);
  assert.equal(result.signal, null);
  assert.equal(result.exit_code, 0, result.stderr + result.stdout);
  assert.match(result.stdout.toString(), /failed\(branch-held\|branch-exists\) = 0 over 7d/);
  for (const [file, bytes] of before) assert.deepEqual(fs.readFileSync(file), bytes);
  assert.deepEqual(fs.readdirSync(f.root).sort(), ['followup-evidence',
    'provision-events.ndjson', 'provision-events.ndjson.old']);
  fs.appendFileSync(path.join(f.root, 'provision-events.ndjson'), ndjson([event({ ts: end })]));
  assert.equal(execute(process.execPath, [script, f.root, source], options).exit_code, 1);
  assert.equal(execute(process.execPath, [script], { cwd: f.root }).exit_code, 1);
});

test('fresh observed legitimate refusals remain unmet after successful retries in both lanes', (t) => {
  const end = new Date().toISOString();
  const refused = (lane, reason, cause = 'branch-held') => event({ ts: end,
    target: `example/repo#${lane === 'closer' ? 1 : 2}`, lane, reason, cause });
  const rows = ['closer', 'opener'].flatMap((lane) => [
    refused(lane, 'dirty holder'), refused(lane, 'live holder'),
    refused(lane, 'writer lock'), refused(lane, 'existing branch', 'branch-exists'),
  ]);
  const f = fixture(t, {
    '.old': ndjson(rows.slice(0, 4)),
    '.older': ndjson(rows.slice(4)),
    '': ndjson(rows.map((row) => ({ ...row, outcome: 'reclaimed' }))),
  }, end);
  // The real collector just observed every retained segment. A red here must
  // come from the legitimate refusals, rather than stale or inconsistent data.
  const observed = JSON.parse(fs.readFileSync(f.receipt)).provision;
  assert.equal(observed.coverage, 'continuous');
  assert.deepEqual(observed.continuity_failures, []);
  assert.equal(observed.branch_refusals, 8);
  const inputs = [f.receipt, ...fs.readdirSync(f.root)
    .filter((name) => name.startsWith('provision-events.ndjson'))
    .map((name) => path.join(f.root, name))];
  const before = inputs.map((file) => fs.readFileSync(file));
  const result = execute(process.execPath, [script, f.root, source], { cwd: f.root,
    env: { ...process.env, ORCH_LOCAL_RUNTIME: f.root,
      ORCH_PROVISION_EVENTS_PATH: path.join(f.root, 'provision-events.ndjson') } });
  assert.equal(result.error_code, null);
  assert.equal(result.signal, null);
  assert.equal(result.exit_code, 1, result.stderr.toString());
  const output = result.stdout.toString();
  const report = JSON.parse(output.slice(output.indexOf('{')));
  assert.equal(report.coverage, 'continuous');
  assert.deepEqual(report.continuity_failures, []);
  assert.equal(report.segments.length, 3);
  assert.equal(report.counts['failed/branch-held'], 6);
  assert.equal(report.counts['failed/branch-exists'], 2);
  assert.equal(report.counts['reclaimed/branch-held'], 6);
  assert.equal(report.counts['reclaimed/branch-exists'], 2);
  assert.equal(report.branch_refusals, 8);
  assert.equal(report.evidence_ready, false);
  assert.equal(report.deployment_acceptance_verified, false);
  assert.match(output, /failed\(branch-held\|branch-exists\) = 8 over 7d/);
  assert.match(output, /ledger evidence = unmet acceptance/);
  for (const [index, file] of inputs.entries()) {
    assert.deepEqual(fs.readFileSync(file), before[index], file);
  }
});

test('source absence and nonregular segments are UNKNOWN without following symlinks', (t) => {
  const f = fixture(t, { '': '' });
  const ledger = path.join(f.root, 'provision-events.ndjson');
  fs.unlinkSync(ledger);
  fs.symlinkSync(f.receipt, ledger);
  assert.equal(f.audit().evidence_ready, false);
  assert.match(f.audit().continuity_failures.join('\n'), /not a regular file/);
  assert.equal(auditProvisionLedger(f.root, f.root, { now }).coverage, 'UNKNOWN');
});

for (const counts of [[], '', 0, null]) {
  test(`rejects malformed empty counts ${JSON.stringify(counts)}`, (t) => {
    const f = fixture(t, { '': '' });
    f.changeReceipt((r) => { r.counts = counts; });
    assert.equal(f.audit().coverage, 'UNKNOWN');
    assert.equal(f.audit().evidence_ready, false);
  });
}

test('compares relative collector segment paths by their filesystem identity', (t) => {
  const f = fixture(t, { '': '' }, now, true);
  assert.equal(f.audit().coverage, 'continuous');
  assert.equal(auditProvisionLedger(path.relative(process.cwd(), f.root), source,
    { now, writerPath: path.join(f.root, 'provision-events.ndjson') }).evidence_ready, true);
});

for (const location of ['outside', 'renamed']) {
  test(`rejects an unrelated zero ledger when the writer is ${location}`, (t) => {
    const f = fixture(t, { '': '' });
    const writerPath = location === 'outside'
      ? path.join(f.root, 'writer', 'provision-events.ndjson')
      : path.join(f.root, 'custom.ndjson');
    fs.mkdirSync(path.dirname(writerPath), { recursive: true });
    fs.writeFileSync(writerPath, ndjson([event()]));
    const result = auditProvisionLedger(f.root, source, { now, writerPath });
    assert.equal(result.coverage, 'UNKNOWN');
    assert.equal(result.evidence_ready, false);
    assert.match(result.continuity_failures.join('\n'), /writer ledger/);
  });
}

for (const location of ['configured runtime', 'home runtime']) {
  test(`rejects an unrelated zero ledger with a separate writer ${location}`, (t) => {
    const end = new Date().toISOString();
    const f = fixture(t, { '': '' }, end);
    const home = path.join(f.root, 'home');
    const runtime = location === 'configured runtime'
      ? path.join(f.root, 'writer') : path.join(home, '.codex', 'orchestrator');
    fs.mkdirSync(runtime, { recursive: true });
    fs.writeFileSync(path.join(runtime, 'provision-events.ndjson'), ndjson([event({ ts: end })]));
    const env = { ...process.env, HOME: home };
    delete env.ORCH_PROVISION_EVENTS_PATH;
    delete env.ORCH_LOCAL_RUNTIME;
    if (location === 'configured runtime') env.ORCH_LOCAL_RUNTIME = runtime;
    const result = execute(process.execPath, [script, f.root, source], { env, cwd: f.root });
    assert.equal(result.error_code, null);
    assert.equal(result.exit_code, 1, result.stdout.toString());
    assert.match(result.stdout.toString(), /writer ledger is not included/);
    assert.match(result.stdout.toString(), /"evidence_ready": false/);
  });
}
