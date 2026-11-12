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
state = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
hours = 1 if state else 169
for hour in range(hours):
    report, state = evidence.provision_report(
        root, state, now - timedelta(hours=hours-1-hour), source / 'provision.py')
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
  const audit = (at = end) => auditProvisionLedger(root, source,
    { now: at, writerPath: path.join(root, 'provision-events.ndjson') });
  const observe = (at, installedSource = source) => {
    // The report carries every field of the collector's continuation state.
    // Carry those values into one new observation instead of inventing a week.
    const previous = JSON.parse(fs.readFileSync(receipt, 'utf8')).provision;
    const collected = execute(process.env.PYTHON || 'python3',
      ['-c', collect, collectorRoot, installedSource, at, JSON.stringify(previous)],
      { cwd: process.cwd(), env: { ...process.env, PYTHONPATH: installedSource,
        PYTHONDONTWRITEBYTECODE: '1' } });
    assert.equal(collected.error_code, null);
    assert.equal(collected.signal, null);
    assert.equal(collected.exit_code, 0, collected.stderr.toString());
    fs.writeFileSync(receipt, collected.stdout);
    return JSON.parse(collected.stdout).provision;
  };
  const changeReceipt = (change) => {
    const document = JSON.parse(fs.readFileSync(receipt, 'utf8'));
    change(document.provision);
    fs.writeFileSync(receipt, JSON.stringify(document));
  };
  return { root, receipt, audit, changeReceipt, observe };
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

test('post-deployment auditing requires a strictly later collector observation', (t) => {
  const f = fixture(t, { '': '' });
  const audit = (observedAfter) => auditProvisionLedger(f.root, source,
    { now, observedAfter, writerPath: path.join(f.root, 'provision-events.ndjson') });
  assert.equal(audit('2026-11-11T12:00:00.123455Z').evidence_ready, true);
  for (const boundary of [now, '2026-11-11T13:00:00.123456+01:00',
    '2026-11-11T12:00:00.123457Z']) {
    const report = audit(boundary);
    assert.equal(report.evidence_ready, false);
    assert.equal(report.coverage, 'UNKNOWN');
    assert.equal(report.observed_after, boundary);
    assert.match(report.continuity_failures.join('\n'), /not after the required observation boundary/);
    assert.match(formatReport(report), /observed_after = /);
  }
  for (const boundary of ['', 'bad', '2026-11-11T12:00:00', '2026-02-30T12:00:00Z']) {
    const report = audit(boundary);
    assert.equal(report.evidence_ready, false);
    assert.match(report.continuity_failures.join('\n'), /observation boundary invalid/);
  }
});

for (const refusals of [false, true]) {
  test(`CLI observation boundary waits for the collector and retains ${refusals ? 'legitimate refusals' : 'the zero count'}`, (t) => {
    const end = new Date().toISOString();
    const previousHour = new Date(Date.parse(end) - 3600000).toISOString();
    const boundary = new Date(Date.parse(previousHour) + 1).toISOString();
    const rows = refusals ? ['closer', 'opener'].map((lane) => event({ ts: previousHour,
      lane, cause: lane === 'closer' ? 'branch-held' : 'branch-exists' })) : [];
    const bytes = ndjson(rows);
    const f = fixture(t, { '': bytes, '.old': bytes }, previousHour);
    const inputs = [f.receipt, path.join(f.root, 'provision-events.ndjson'),
      path.join(f.root, 'provision-events.ndjson.old')];
    const segments = inputs.slice(1).map((file) => fs.readFileSync(file));
    const env = { ...process.env, ORCH_LOCAL_RUNTIME: f.root,
      ORCH_PROVISION_EVENTS_PATH: inputs[1] };
    const run = () => {
      const receipt = fs.readFileSync(f.receipt);
      const cli = execute(process.execPath,
        [script, f.root, source, '--observed-after', boundary], { cwd: f.root, env });
      assert.equal(cli.error_code, null);
      assert.equal(cli.signal, null);
      assert.deepEqual(fs.readFileSync(f.receipt), receipt);
      for (const [index, file] of inputs.slice(1).entries()) {
        assert.deepEqual(fs.readFileSync(file), segments[index]);
      }
      const output = cli.stdout.toString();
      const report = JSON.parse(output.slice(output.indexOf('{')));
      assert.equal(report.branch_refusals, refusals ? 4 : 0);
      assert.equal(report.segments.length, 2);
      assert.equal(report.observed_after, boundary);
      assert.equal(report.deployment_acceptance_verified, false);
      return { cli, report, output };
    };
    const before = run();
    assert.equal(before.cli.exit_code, 1);
    assert.equal(before.report.coverage, 'UNKNOWN');
    assert.equal(before.report.evidence_ready, false);
    assert.match(before.output, /not after the required observation boundary/);
    // Only the real collector advances its receipt; the auditor never does.
    f.observe(end);
    const after = run();
    assert.equal(after.cli.exit_code, refusals ? 1 : 0);
    assert.equal(after.report.coverage, 'continuous');
    assert.deepEqual(after.report.continuity_failures, []);
    assert.equal(after.report.evidence_ready, !refusals);
    assert.equal(instant(after.report.checked_at), instant(end));
    if (refusals) assert.match(after.output, /ledger evidence = unmet acceptance/);
  });
}

test('reads all inputs without writing and CLI status distinguishes unmet evidence', (t) => {
  const end = new Date().toISOString();
  const f = fixture(t, { '': '', '.old': '' }, end);
  const before = [f.receipt, path.join(f.root, 'provision-events.ndjson'),
    path.join(f.root, 'provision-events.ndjson.old')].map((file) => [file, fs.readFileSync(file)]);
  const options = { cwd: f.root, env: { ...process.env, ORCH_LOCAL_RUNTIME: f.root } };
  delete options.env.ORCH_PROVISION_EVENTS_PATH;
  const result = execute(process.execPath, [script, f.root, source, '--trailing-week'], options);
  assert.equal(result.error_code, null);
  assert.equal(result.signal, null);
  assert.equal(result.exit_code, 0, result.stderr + result.stdout);
  assert.match(result.stdout.toString(), /failed\(branch-held\|branch-exists\) = 0 over 7d/);
  for (const [file, bytes] of before) assert.deepEqual(fs.readFileSync(file), bytes);
  assert.deepEqual(fs.readdirSync(f.root).sort(), ['followup-evidence',
    'provision-events.ndjson', 'provision-events.ndjson.old']);
  fs.appendFileSync(path.join(f.root, 'provision-events.ndjson'), ndjson([event({ ts: end })]));
  assert.equal(execute(process.execPath, [script, f.root, source, '--trailing-week'], options).exit_code, 1);
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
  const result = execute(process.execPath, [script, f.root, source, '--trailing-week'], { cwd: f.root,
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

test('a refusal ages out only after a fresh observation with every archive retained', (t) => {
  const f = fixture(t, { '': ndjson([event({ outcome: 'reclaimed' })]),
    '.old': ndjson([event({ ts: cutoff })]) });
  assert.equal(f.audit().branch_refusals, 1);
  assert.equal(f.audit().coverage, 'continuous');
  assert.equal(f.audit().evidence_ready, false);
  const archives = fs.readdirSync(f.root).filter((name) => name.startsWith('provision-events.ndjson'));
  const before = archives.map((name) => fs.readFileSync(path.join(f.root, name)));
  const nextHour = '2026-11-11T13:00:00.123456+00:00';
  // A still-fresh receipt continues to report its own observed window.
  assert.equal(f.audit(nextHour).branch_refusals, 1);
  assert.equal(f.audit(nextHour).evidence_ready, false);
  const observed = f.observe(nextHour);
  assert.equal(observed.evidence_ready, true);
  assert.deepEqual(observed.continuity_failures, []);
  const result = f.audit(nextHour);
  assert.equal(result.counted_through, nextHour);
  assert.equal(result.continuous_since, cutoff);
  assert.equal(result.coverage, 'continuous');
  assert.equal(result.branch_refusals, 0);
  assert.equal(result.evidence_ready, true);
  assert.equal(result.deployment_acceptance_verified, false);
  assert.deepEqual(result.counts, { 'reclaimed/branch-held': 1 });
  for (const [index, name] of archives.entries()) {
    assert.deepEqual(fs.readFileSync(path.join(f.root, name)), before[index], name);
  }
});

for (const changed of ['provision.py', 'followup_evidence.py']) {
  test(`fresh observations after deploying changed ${changed} cannot inherit the previous seven-day baseline`, (t) => {
    const f = fixture(t, {
      '': ndjson([event({ outcome: 'reclaimed' })]),
      '.old': ndjson([event({ ts: cutoff, lane: 'closer' })]),
      '.older': ndjson([event({ ts: cutoff, lane: 'opener', cause: 'branch-exists' })]),
    });
    assert.equal(f.audit().coverage, 'continuous');
    assert.equal(f.audit().branch_refusals, 2);
    const installed = path.join(f.root, 'installed');
    fs.mkdirSync(installed);
    for (const name of ['provision.py', 'followup_evidence.py', 'paths.py']) {
      fs.copyFileSync(path.join(source, name), path.join(installed, name));
    }
    // Change real installed bytes rather than altering hashes in a receipt.
    fs.appendFileSync(path.join(installed, changed), '\n# New installed generation.\n');
    const audit = (at) => auditProvisionLedger(f.root, installed,
      { now: at, writerPath: path.join(f.root, 'provision-events.ndjson') });
    const stale = audit(now);
    assert.equal(stale.coverage, 'UNKNOWN');
    assert.ok(stale.continuity_failures.includes(`${changed} differs from the observed implementation`));
    const segments = ['provision-events.ndjson', 'provision-events.ndjson.old',
      'provision-events.ndjson.older'].map((name) => path.join(f.root, name));
    const before = segments.map((file) => fs.readFileSync(file));
    const nextHour = '2026-11-11T13:00:00.123456+00:00';
    const observed = f.observe(nextHour, installed);
    const reason = changed === 'provision.py' ? 'provision instrument' : 'collector implementation';
    assert.deepEqual(observed.continuity_failures, [`${reason} changed; new baseline required`]);
    assert.equal(observed.continuous_since, nextHour);
    assert.equal(observed.branch_refusals, 0);
    assert.equal(observed.evidence_ready, false);
    const first = audit(nextHour);
    assert.equal(first.coverage, 'UNKNOWN');
    assert.equal(first.evidence_ready, false);
    assert.equal(first.deployment_acceptance_verified, false);
    assert.equal(first.segments.length, 3);

    // A subsequent hourly observation matches the deployed sources, but only
    // one hour of the replacement baseline has elapsed. The aged-out refusals
    // remain archived and a zero recount still cannot qualify this deployment.
    const resumed = '2026-11-11T14:00:00.123456+00:00';
    const following = f.observe(resumed, installed);
    assert.deepEqual(following.continuity_failures, []);
    assert.equal(following.continuous_since, nextHour);
    const result = audit(resumed);
    assert.equal(result.counted_through, resumed);
    assert.equal(result.continuous_since, nextHour);
    assert.equal(result.branch_refusals, 0);
    assert.equal(result.coverage, 'UNKNOWN');
    assert.equal(result.evidence_ready, false);
    assert.equal(result.instrument_sha256, following.instrument_sha256);
    assert.equal(result.collector_sha256, following.collector_sha256);
    assert.match(result.continuity_failures.join('\n'), /seven continuous observed days/);
    assert.match(formatReport(result), /failed\(branch-held\|branch-exists\) = 0 over 7d/);
    assert.match(formatReport(result), /ledger evidence = unmet acceptance/);
    for (const [index, file] of segments.entries()) {
      assert.deepEqual(fs.readFileSync(file), before[index], file);
    }
  });
}

for (const interrupted of [false, true]) {
  test(`fresh zero-refusal observation ${interrupted ? 'resets' : 'preserves'} the baseline at the two-hour collector limit`, (t) => {
    const f = fixture(t, {
      '': ndjson(['closer', 'opener'].map((lane) => event({ lane, outcome: 'reclaimed' }))),
      '.old': ndjson([event({ ts: cutoff, lane: 'closer' })]),
      '.older': ndjson([event({ ts: cutoff, lane: 'opener', cause: 'branch-exists' })]),
    });
    const initial = f.audit();
    assert.equal(initial.coverage, 'continuous');
    assert.equal(initial.branch_refusals, 2);
    assert.equal(initial.evidence_ready, false);
    const segments = fs.readdirSync(f.root)
      .filter((name) => name.startsWith('provision-events.ndjson'));
    const before = segments.map((name) => fs.readFileSync(path.join(f.root, name)));
    const next = interrupted ? '2026-11-11T14:00:00.123457+00:00'
      : '2026-11-11T14:00:00.123456+00:00';
    const observed = f.observe(next);
    assert.equal(observed.branch_refusals, 0);
    assert.equal(observed.coverage, interrupted ? 'UNKNOWN' : 'continuous');
    assert.equal(observed.evidence_ready, !interrupted);
    assert.equal(observed.continuous_since, interrupted ? next : cutoff);
    assert.deepEqual(observed.continuity_failures, interrupted
      ? ['collector continuity gap exceeds two hours'] : []);

    const result = f.audit(next);
    assert.equal(result.counted_through, next);
    assert.equal(result.coverage, observed.coverage);
    assert.equal(result.continuous_since, observed.continuous_since);
    assert.equal(result.branch_refusals, 0);
    assert.equal(result.evidence_ready, !interrupted);
    assert.equal(result.deployment_acceptance_verified, false);
    assert.equal(result.segments.length, 3);
    assert.deepEqual(result.counts, { 'reclaimed/branch-held': 2 });
    assert.match(formatReport(result), /failed\(branch-held\|branch-exists\) = 0 over 7d/);
    assert.match(formatReport(result), interrupted
      ? /ledger evidence = unmet acceptance/ : /ledger evidence = ready/);

    if (interrupted) {
      // Resuming the hourly tick clears the immediate gap, but it does not
      // restore the week of observation that the interruption invalidated.
      const resumed = '2026-11-11T15:00:00.123457+00:00';
      const following = f.observe(resumed);
      assert.deepEqual(following.continuity_failures, []);
      assert.equal(following.continuous_since, next);
      assert.equal(following.branch_refusals, 0);
      assert.equal(following.coverage, 'UNKNOWN');
      assert.equal(following.evidence_ready, false);
      const audit = f.audit(resumed);
      assert.equal(audit.evidence_ready, false);
      assert.equal(audit.coverage, 'UNKNOWN');
      assert.equal(audit.continuous_since, next);
      assert.match(audit.continuity_failures.join('\n'), /seven continuous observed days/);
    }
    for (const [index, name] of segments.entries()) {
      assert.deepEqual(fs.readFileSync(path.join(f.root, name)), before[index], name);
    }
  });
}

test('a fresh collector observation after archive loss cannot qualify a zero-refusal window', (t) => {
  const f = fixture(t, { '': ndjson([event({ outcome: 'reclaimed' })]),
    '.old': ndjson([event({ ts: cutoff })]), '.older': ndjson([event({ ts: cutoff })]) });
  assert.equal(f.audit().branch_refusals, 2);
  const retained = path.join(f.root, 'provision-events.ndjson.old');
  const bytes = fs.readFileSync(retained);
  fs.unlinkSync(path.join(f.root, 'provision-events.ndjson.older'));
  const nextHour = '2026-11-11T13:00:00.123456+00:00';
  const observed = f.observe(nextHour);
  assert.match(observed.continuity_failures.join('\n'), /previous ledger prefix missing or rewritten/);
  assert.equal(observed.continuous_since, nextHour);
  assert.equal(observed.branch_refusals, 0);
  assert.equal(observed.evidence_ready, false);
  const result = f.audit(nextHour);
  assert.equal(result.coverage, 'UNKNOWN');
  assert.equal(result.branch_refusals, 0);
  assert.equal(result.evidence_ready, false);
  assert.deepEqual(fs.readFileSync(retained), bytes);
});

for (const newRefusals of [false, true]) {
  test(`fresh observation preserves rotated archives with ${newRefusals ? 'repeated refusals' : 'an empty active segment'}`, (t) => {
    const end = new Date().toISOString();
    const previousHour = new Date(Date.parse(end) - 3600000).toISOString();
    const rows = ['closer', 'opener'].flatMap((lane) => {
      const cause = lane === 'closer' ? 'branch-held' : 'branch-exists';
      return [event({ ts: previousHour, lane, cause }),
        event({ ts: previousHour, lane, cause, outcome: 'reclaimed' })];
    });
    // Identical segments are distinct retained events, including after renaming.
    const bytes = ndjson(rows);
    const f = fixture(t, { '': bytes, '.old': bytes }, previousHour);
    const initial = f.audit();
    assert.equal(initial.coverage, 'continuous');
    assert.equal(initial.branch_refusals, 4);
    const active = path.join(f.root, 'provision-events.ndjson');
    fs.renameSync(active + '.old', active + '.older');
    fs.renameSync(active, active + '.old');
    fs.writeFileSync(active, newRefusals ? bytes : '');
    const stale = f.audit(end);
    assert.equal(stale.coverage, 'UNKNOWN');
    assert.equal(stale.evidence_ready, false);
    assert.match(stale.continuity_failures.join('\n'), /segments differ/);

    const observed = f.observe(end);
    const expectedPerCause = newRefusals ? 3 : 2;
    assert.deepEqual(observed.continuity_failures, []);
    assert.equal(observed.continuous_since, initial.continuous_since);
    assert.equal(observed.branch_refusals, expectedPerCause * 2);
    const result = f.audit(end);
    assert.equal(result.coverage, 'continuous');
    assert.equal(result.continuous_since, initial.continuous_since);
    assert.equal(instant(result.counted_through), instant(end));
    assert.equal(result.segments.length, 3);
    assert.deepEqual(result.counts, {
      'failed/branch-exists': expectedPerCause,
      'failed/branch-held': expectedPerCause,
      'reclaimed/branch-exists': expectedPerCause,
      'reclaimed/branch-held': expectedPerCause,
    });
    assert.equal(result.evidence_ready, false);
    assert.equal(result.deployment_acceptance_verified, false);

    const inputs = [f.receipt, active, active + '.old', active + '.older'];
    const before = inputs.map((file) => fs.readFileSync(file));
    const env = { ...process.env, ORCH_LOCAL_RUNTIME: f.root };
    delete env.ORCH_PROVISION_EVENTS_PATH;
    const cli = execute(process.execPath, [script, f.root, source, '--trailing-week'], { cwd: f.root, env });
    assert.equal(cli.error_code, null);
    assert.equal(cli.signal, null);
    assert.equal(cli.exit_code, 1, cli.stdout.toString() + cli.stderr.toString());
    const output = cli.stdout.toString();
    assert.match(output, /coverage = continuous/);
    assert.ok(output.includes(`failed(branch-held|branch-exists) = ${expectedPerCause * 2} over 7d`));
    assert.match(output, /ledger evidence = unmet acceptance/);
    for (const [index, file] of inputs.entries()) {
      assert.deepEqual(fs.readFileSync(file), before[index], file);
    }
    assert.equal(fs.readFileSync(active + '.old', 'utf8'), bytes);
    assert.equal(fs.readFileSync(active + '.older', 'utf8'), bytes);
  });
}

test('source absence and nonregular segments are UNKNOWN without following symlinks', (t) => {
  const f = fixture(t, { '': '' });
  const ledger = path.join(f.root, 'provision-events.ndjson');
  fs.unlinkSync(ledger);
  fs.symlinkSync(f.receipt, ledger);
  assert.equal(f.audit().evidence_ready, false);
  assert.match(f.audit().continuity_failures.join('\n'), /not a regular file/);
  assert.equal(auditProvisionLedger(f.root, f.root, { now }).coverage, 'UNKNOWN');
});

for (const replacement of ['directory', 'symlink']) {
  test(`an archive replaced by a ${replacement} retains every readable refusal in CLI output`, (t) => {
    const end = new Date().toISOString();
    const rows = ['closer', 'opener'].flatMap((lane) => [
      event({ ts: end, lane }), event({ ts: end, lane, cause: 'branch-exists' }),
    ]);
    const bytes = ndjson(rows);
    const f = fixture(t, { '': bytes, '.old': bytes, '.older': bytes }, end);
    assert.equal(f.audit().branch_refusals, 12);
    assert.equal(f.audit().coverage, 'continuous');
    const active = path.join(f.root, 'provision-events.ndjson');
    const archive = active + '.older';
    // Only disposable test inputs are replaced. The receipt still records all
    // three segments, so the missing archive cannot qualify the visible count.
    fs.unlinkSync(archive);
    if (replacement === 'directory') fs.mkdirSync(archive);
    else fs.symlinkSync(active, archive);
    const inputs = [f.receipt, active, active + '.old'];
    const before = inputs.map((file) => fs.readFileSync(file));
    const env = { ...process.env, ORCH_LOCAL_RUNTIME: f.root };
    delete env.ORCH_PROVISION_EVENTS_PATH;
    const cli = execute(process.execPath, [script, f.root, source, '--trailing-week'], { cwd: f.root, env });
    assert.equal(cli.error_code, null);
    assert.equal(cli.signal, null);
    assert.equal(cli.exit_code, 1, cli.stderr.toString());
    const output = cli.stdout.toString();
    const report = JSON.parse(output.slice(output.indexOf('{')));
    assert.equal(report.coverage, 'UNKNOWN');
    assert.equal(report.evidence_ready, false);
    assert.equal(report.deployment_acceptance_verified, false);
    assert.equal(report.branch_refusals, 8);
    assert.deepEqual(report.counts, { 'failed/branch-exists': 4, 'failed/branch-held': 4 });
    assert.equal(report.segments.length, 2);
    assert.match(output, /failed\(branch-held\|branch-exists\) = 8 over 7d/);
    assert.match(output, /ledger evidence = unmet acceptance/);
    assert.ok(report.continuity_failures.includes(
      'ledger segment unreadable or not a regular file: provision-events.ndjson.older'));
    assert.ok(report.continuity_failures.includes(
      'active/archive segments differ from the collector observation'));
    assert.ok(report.continuity_failures.includes('recount differs from the collector observation'));
    for (const [index, file] of inputs.entries()) {
      assert.deepEqual(fs.readFileSync(file), before[index], file);
    }
    if (replacement === 'directory') assert.deepEqual(fs.readdirSync(archive), []);
    else assert.equal(fs.readlinkSync(archive), active);
  });
}

for (const refusals of [false, true]) {
  test(`malformed receipt segment retains ${refusals ? 'all refusals' : 'an unmet zero'} in CLI output`, (t) => {
    const end = new Date().toISOString();
    const rows = refusals ? ['closer', 'opener'].map((lane) => event({ ts: end, lane })) : [];
    const f = fixture(t, { '': ndjson(rows), '.old': ndjson(rows) }, end);
    // Damage only the receipt metadata; all ledger bytes remain readable.
    f.changeReceipt((r) => { r.segments[0] = null; });
    const inputs = [f.receipt, path.join(f.root, 'provision-events.ndjson'),
      path.join(f.root, 'provision-events.ndjson.old')];
    const before = inputs.map((file) => fs.readFileSync(file));
    const expected = refusals ? 4 : 0;
    const report = f.audit();
    assert.equal(report.branch_refusals, expected);
    assert.equal(report.coverage, 'UNKNOWN');
    assert.equal(report.evidence_ready, false);
    assert.match(report.continuity_failures.join('\n'), /segments differ/);
    const env = { ...process.env, ORCH_LOCAL_RUNTIME: f.root };
    delete env.ORCH_PROVISION_EVENTS_PATH;
    const cli = execute(process.execPath, [script, f.root, source, '--trailing-week'], { cwd: f.root, env });
    assert.equal(cli.error_code, null);
    assert.equal(cli.signal, null);
    assert.equal(cli.exit_code, 1, cli.stderr.toString());
    assert.ok(cli.stdout.toString().includes(`failed(branch-held|branch-exists) = ${expected} over 7d`));
    assert.match(cli.stdout.toString(), /ledger evidence = unmet acceptance/);
    assert.match(cli.stdout.toString(), /active\/archive segments differ/);
    for (const [index, file] of inputs.entries()) {
      assert.deepEqual(fs.readFileSync(file), before[index], file);
    }
  });
}

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

for (const refusals of [false, true]) {
  test(`runtime directory aliases retain ${refusals ? 'every refusal' : 'a qualifying observation'}`, (t) => {
    const end = new Date().toISOString();
    const rows = refusals ? ['closer', 'opener'].map((lane) => event({ ts: end, lane })) : [];
    const f = fixture(t, { '': ndjson(rows), '.old': ndjson(rows) }, end);
    const alias = path.join(f.root, 'runtime-alias');
    fs.symlinkSync(f.root, alias, 'dir');
    // Observe through the directory alias with the real collector. Final ledger
    // components remain regular files; a directory alias is not a linked ledger.
    const observed = execute(process.env.PYTHON || 'python3',
      ['-c', collect, alias, source, end], { cwd: f.root,
        env: { ...process.env, PYTHONPATH: source, PYTHONDONTWRITEBYTECODE: '1' } });
    assert.equal(observed.exit_code, 0, observed.stderr.toString());
    fs.writeFileSync(f.receipt, observed.stdout);
    const inputs = [f.receipt, path.join(f.root, 'provision-events.ndjson'),
      path.join(f.root, 'provision-events.ndjson.old')];
    const before = inputs.map((file) => fs.readFileSync(file));
    // The writer, collector and CLI can spell the same runtime differently.
    for (const [runtime, writerRoot] of [[f.root, alias], [alias, f.root]]) {
      const result = execute(process.execPath, [script, runtime, source, '--trailing-week'], { cwd: f.root,
        env: { ...process.env, ORCH_LOCAL_RUNTIME: writerRoot,
          ORCH_PROVISION_EVENTS_PATH: path.join(writerRoot, 'provision-events.ndjson') } });
      assert.equal(result.error_code, null);
      assert.equal(result.signal, null);
      assert.equal(result.exit_code, refusals ? 1 : 0, result.stdout.toString());
      const output = result.stdout.toString();
      const report = JSON.parse(output.slice(output.indexOf('{')));
      assert.equal(report.coverage, 'continuous');
      assert.deepEqual(report.continuity_failures, []);
      assert.equal(report.branch_refusals, refusals ? 4 : 0);
      assert.equal(report.segments.length, 2);
      assert.equal(report.evidence_ready, !refusals);
      assert.equal(report.deployment_acceptance_verified, false);
      assert.match(output, refusals ? /ledger evidence = unmet acceptance/
        : /ledger evidence = ready/);
    }
    for (const [index, file] of inputs.entries()) {
      assert.deepEqual(fs.readFileSync(file), before[index], file);
    }
  });
}

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
    const result = execute(process.execPath, [script, f.root, source, '--trailing-week'], { env, cwd: f.root });
    assert.equal(result.error_code, null);
    assert.equal(result.exit_code, 1, result.stdout.toString());
    assert.match(result.stdout.toString(), /writer ledger is not included/);
    assert.match(result.stdout.toString(), /"evidence_ready": false/);
  });
}

for (const observedWriter of [false, true]) {
  test(`explicit writer path overrides the runtime directory when ${observedWriter ? 'observed' : 'unobserved'}`, (t) => {
    const end = new Date().toISOString();
    const f = fixture(t, { '': '', '.old': '' }, end);
    const elsewhere = path.join(f.root, 'other-runtime');
    fs.mkdirSync(elsewhere);
    const otherWriter = path.join(elsewhere, 'provision-events.ndjson');
    fs.writeFileSync(otherWriter, ndjson(['closer', 'opener'].map((lane) =>
      event({ ts: end, lane }))));
    const env = { ...process.env,
      ORCH_LOCAL_RUNTIME: observedWriter ? elsewhere : f.root,
      ORCH_PROVISION_EVENTS_PATH: observedWriter
        ? path.join(f.root, 'provision-events.ndjson') : otherWriter,
      PYTHONPATH: source, PYTHONDONTWRITEBYTECODE: '1' };
    // Check the actual writer's choice too: an internally consistent receipt
    // for another directory cannot stand in for the effective writer ledger.
    const writer = execute(process.env.PYTHON || 'python3', ['-c',
      'import provision; print(provision.PROVISION_EVENTS_PATH)'], { cwd: f.root, env });
    assert.equal(writer.error_code, null);
    assert.equal(writer.signal, null);
    assert.equal(writer.exit_code, 0, writer.stderr.toString());
    assert.equal(writer.stdout.toString().trim(), env.ORCH_PROVISION_EVENTS_PATH);
    const inputs = [f.receipt, path.join(f.root, 'provision-events.ndjson'),
      path.join(f.root, 'provision-events.ndjson.old'), otherWriter];
    const before = inputs.map((file) => fs.readFileSync(file));
    const result = execute(process.execPath, [script, f.root, source, '--trailing-week'], { cwd: f.root, env });
    assert.equal(result.error_code, null);
    assert.equal(result.signal, null);
    assert.equal(result.exit_code, observedWriter ? 0 : 1, result.stderr.toString());
    const output = result.stdout.toString();
    const report = JSON.parse(output.slice(output.indexOf('{')));
    assert.equal(report.evidence_ready, observedWriter);
    assert.equal(report.coverage, observedWriter ? 'continuous' : 'UNKNOWN');
    assert.equal(report.deployment_acceptance_verified, false);
    if (observedWriter) assert.deepEqual(report.continuity_failures, []);
    else assert.match(report.continuity_failures.join('\n'), /writer ledger is not included/);
    for (const [index, file] of inputs.entries()) {
      assert.deepEqual(fs.readFileSync(file), before[index], file);
    }
  });
}

function deploymentFixture(t, rows) {
  const deployedAt = '2026-11-11T11:30:00.000000Z';
  const before = '2026-11-11T11:00:00.000000Z';
  const f = fixture(t, { '': ndjson([event({ ts: before })]) }, before);
  const name = 'orchestrator-mirror.generation-' + 'a'.repeat(32);
  const generation = path.join(f.root, name);
  const checkout = path.join(f.root, 'checkout');
  for (const directory of [path.join(generation, 'scripts'), path.join(checkout, 'src'),
    path.join(checkout, 'scripts')]) {
    fs.mkdirSync(directory, { recursive: true });
  }
  for (const file of ['provision.py', 'followup_evidence.py']) {
    fs.copyFileSync(path.join(source, file), path.join(generation, file));
    fs.copyFileSync(path.join(source, file), path.join(checkout, 'src', file));
  }
  fs.copyFileSync(path.join(source, 'paths.py'), path.join(generation, 'paths.py'));
  for (const directory of [generation, checkout]) {
    fs.copyFileSync(script, path.join(directory, 'scripts', path.basename(script)));
  }
  const git = (...args) => {
    const result = execute('git', args, { cwd: checkout });
    assert.equal(result.exit_code, 0, result.stderr.toString());
    return result.stdout.toString().trim();
  };
  git('init', '-q');
  git('add', 'src', 'scripts');
  git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', '-c', 'commit.gpgsign=false',
    '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Fixture deployment');
  const revision = git('rev-parse', 'HEAD');
  const history = path.join(f.root, 'mirror-sync-history.jsonl');
  const deployment = { ts: deployedAt, sha: revision.slice(0, 8), mode: 'ci-verdict',
    outcome: 'deployed', detail: name };
  fs.writeFileSync(history, ndjson([deployment]));
  const active = path.join(f.root, 'provision-events.ndjson');
  fs.appendFileSync(active, ndjson(rows));
  // Run the actual collector from the installed flat layout.
  f.observe(now, generation);
  const audit = (sourcePath = generation, sha = revision, at = now) => {
    const run = `
      const { auditProvisionLedger, formatReport } = require(process.argv[1]);
      const [root, source, revision, now] = process.argv.slice(2);
      const report = auditProvisionLedger(root, source, { revision, now });
      console.log(formatReport(report));
      console.log(JSON.stringify(report));
      process.exitCode = report.evidence_ready ? 0 : 1;
    `;
    const cli = execute(process.execPath, ['-e', run, script, f.root, sourcePath, sha, at],
      { cwd: checkout, env: { ...process.env, ORCH_PROVISION_EVENTS_PATH: active } });
    const stdout = cli.stdout.toString();
    assert.match(stdout, /first-100 = /, cli.stderr.toString());
    return { exit: cli.exit_code, report: JSON.parse(stdout.slice(stdout.indexOf('{'))) };
  };
  return { ...f, audit, active, generation, history, deployment, checkout };
}

const operation = (position, changes = {}) => event({
  ts: `2026-11-11T11:31:00.${String(position).padStart(6, '0')}Z`,
  outcome: 'ok', cause: 'new-worktree', ...changes,
});

for (const size of [99, 100, 101]) {
  test(`deployment acceptance evaluates exactly the first 100 of ${size} operations`, (t) => {
    const rows = Array.from({ length: size }, (_, index) => index === 100
      ? operation(index, { outcome: 'failed', cause: 'branch-exists' }) : operation(index));
    // Pre-deploy and exact-boundary failures cannot enter the cohort. Neither
    // can an operation timestamped after the collector's observation.
    const f = deploymentFixture(t, [event({ ts: '2026-11-11T06:30:00-05:00' }),
      event({ ts: '2026-11-11T11:29:59.999999Z' }),
      event({ ts: '2026-11-11T12:00:00.123457Z' }), ...rows.reverse()]);
    const { exit, report } = f.audit();
    assert.deepEqual(report.continuity_failures, []);
    assert.equal(exit, size < 100 ? 1 : 0);
    assert.equal(report.cohort.available, size);
    assert.equal(report.cohort.evaluated, Math.min(size, 100));
    assert.equal(report.cohort.branch_refusals, 0);
    assert.ok(report.branch_refusals >= 3);
  });
}

for (const cause of ['branch-held', 'branch-exists']) {
  test(`a ${cause} refusal in the cohort fails, including across rotation`, (t) => {
    const duplicate = operation(0);
    const f = deploymentFixture(t, [...Array(99).fill(duplicate),
      operation(1, { outcome: 'failed', cause }), operation(2)]);
    const initial = f.audit();
    assert.equal(initial.exit, 1);
    assert.equal(initial.report.cohort.counts['ok/new-worktree'], 99);
    assert.equal(initial.report.cohort.counts[`failed/${cause}`], 1);
    fs.renameSync(f.active, f.active + '.old');
    fs.writeFileSync(f.active, ndjson([operation(3)]));
    assert.equal(f.audit().report.coverage, 'UNKNOWN');
    f.observe(now, f.generation);
    const rotated = f.audit();
    assert.equal(rotated.exit, 1);
    assert.equal(rotated.report.coverage, 'continuous');
    assert.equal(rotated.report.cohort.available, 102);
    assert.equal(rotated.report.cohort.sha256, initial.report.cohort.sha256);
    fs.unlinkSync(f.active + '.old');
    f.observe(now, f.generation);
    assert.match(f.audit().report.continuity_failures.join('\n'), /previous ledger prefix missing/);
  });
}

test('deployment provenance and gaps cannot qualify an otherwise complete cohort', (t) => {
  const f = deploymentFixture(t, Array(100).fill(operation(0)));
  assert.equal(f.audit().exit, 0);
  assert.equal(f.audit(source).exit, 1);
  assert.equal(f.audit(f.generation, 'b'.repeat(40)).exit, 1);
  f.changeReceipt((report) => { report.deployment.identity.generation += '-other'; });
  assert.equal(f.audit().exit, 1);
  f.changeReceipt((report) => { report.deployment.identity.generation = fs.realpathSync(f.generation); });
  f.observe('2026-11-11T14:01:00Z', f.generation);
  f.observe('2026-11-11T15:01:00Z', f.generation);
  const gap = f.audit(f.generation, undefined, '2026-11-11T15:01:00Z');
  assert.equal(gap.exit, 1);
  assert.match(gap.report.continuity_failures.join('\n'), /continuity gap exceeds two hours/);
  fs.writeFileSync(f.history, ndjson([{ ...f.deployment, detail: f.deployment.detail + '-other' }]));
  assert.match(f.audit().report.continuity_failures.join('\n'), /deployment provenance unavailable/);
  fs.unlinkSync(f.history);
  const cli = execute(process.execPath, [script, f.root, f.generation], {
    cwd: f.checkout, env: { ...process.env, ORCH_PROVISION_EVENTS_PATH: f.active },
  });
  assert.equal(cli.exit_code, 1, cli.stderr.toString());
  assert.match(cli.stdout.toString(), /deployment provenance unavailable/);
});

test('a collector starting after deployment cannot establish continuity retroactively', (t) => {
  const f = deploymentFixture(t, Array(100).fill(operation(0)));
  f.changeReceipt((report) => { delete report.deployment; });
  f.observe(now, f.generation);
  f.observe(now, f.generation);
  const { exit, report } = f.audit();
  assert.equal(exit, 1);
  assert.equal(report.cohort.evaluated, 100);
  assert.match(report.continuity_failures.join('\n'), /no retained collector observation/);
});
