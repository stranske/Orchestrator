'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { names, validateReport, verifyLive } = require('../scripts/verify_fleet_delivery_live');

const checked = Date.parse('2026-11-05T12:00:00Z');

function example() {
  const rows = Object.fromEntries(Object.entries(names).map(([key, name]) => [key, {
    name, value: '2', evidence_class: 'recorded example', reason: '',
  }]));
  rows.gate_cancellations = {
    name: names.gate_cancellations, value: null, evidence_class: 'unmeasured',
    reason: 'gh HTTP 403 read refused',
  };
  rows.opener_cap = {
    name: names.opener_cap, value: null, evidence_class: 'unmeasured',
    reason: 'would need opener health helper --json',
  };
  const report = {
    generated_at: checked / 1000,
    fleet_delivery_measures: { as_of: checked / 1000, window_hours: 48, rows },
  };
  const text = '## Fleet delivery measures\n\n  As of 2026-11-05T12:00:00+00:00\n'
    + Object.values(rows).map((row) => `  ${row.name}: ${row.value === null
      ? `unmeasured — ${row.reason}` : `${row.value} [${row.evidence_class}]`}`).join('\n')
    + '\n\n## Other checks\n';
  return { report, text };
}

test('mixed measured and failed live rows are valid with a current check date', () => {
  const { report, text } = example();
  assert.equal(validateReport(report, text, checked, checked + 2000), report.fleet_delivery_measures);
});

test('missing rows, evidence, explicit errors, or current dates cannot pass', () => {
  const mutations = [
    (r) => { delete r.fleet_delivery_measures.rows.released_flow; },
    (r) => { r.fleet_delivery_measures.rows.merge_to_close.evidence_class = ''; },
    (r) => { r.fleet_delivery_measures.rows.gate_cancellations.reason = ''; },
    (r) => { r.fleet_delivery_measures.rows.gate_cancellations.value = '0%'; },
    (r) => { r.fleet_delivery_measures.as_of -= 86400; r.generated_at -= 86400; },
    (r) => { r.fleet_delivery_measures.as_of += 86400; r.generated_at += 86400; },
    (r) => { r.generated_at -= 1; },
    (r) => { r.fleet_delivery_measures.window_hours = 24; },
  ];
  for (const mutate of mutations) {
    const { report, text } = example();
    mutate(report);
    assert.throws(() => validateReport(report, text, checked, checked + 2000));
  }
});

test('missing, duplicate, mismatched, or stale text rows cannot pass', () => {
  const { report, text } = example();
  for (const broken of [
    text.replace('## Fleet delivery measures', '## Missing section'),
    text + text,
    text.replace('  As of 2026-11-05', '  As of 2026-10-19'),
    text.replace('2 [recorded example]', '0 [recorded example]'),
    text.replace(`  ${names.released_flow}: 2 [recorded example]\n`, ''),
    text.replace('\n\n## Other', `\n  ${names.merge_to_close}: 2 [recorded example]\n\n## Other`),
  ]) {
    assert.throws(() => validateReport(report, broken, checked, checked + 2000));
  }
});

test('live command uses the selected interpreter and records validated output', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  const { report, text } = example();
  let calls = 0;
  const receipt = await verifyLive(output, {
    python: '/selected/venv/bin/python', now: () => checked,
    run: async (python, args, options) => {
      calls += 1;
      assert.equal(python, '/selected/venv/bin/python');
      assert.equal(args[0], path.resolve(__dirname, '../src/switch_review.py'));
      assert.deepEqual(args.slice(1), ['--json', '--text-out', path.join(output, 'report.txt')]);
      assert.equal(options.env.ORCH_LOCAL_RUNTIME, path.join(output, 'runtime'));
      assert.equal(options.env.ORCH_STATE_DIR, path.join(output, 'state'));
      fs.writeFileSync(args.at(-1), text);
      return JSON.stringify(report);
    },
  });
  assert.equal(calls, 1);
  assert.equal(receipt.checked_at, '2026-11-05T12:00:00.000Z');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(output, 'check.json'))), receipt);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(output, 'report.json'))), report);
  await assert.rejects(verifyLive(output), /EEXIST/);
});

test('a missing interpreter fails without a fallback or success receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-missing-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  await assert.rejects(verifyLive(output, { python: path.join(root, 'missing-python') }), /ENOENT/);
  assert.ok(!fs.existsSync(path.join(output, 'check.json')));
});

test('a successful command with an incomplete report cannot leave a success receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-incomplete-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  const { report, text } = example();
  delete report.fleet_delivery_measures.rows.released_flow;
  await assert.rejects(verifyLive(output, {
    now: () => checked,
    run: async () => {
      fs.writeFileSync(path.join(output, 'report.txt'), text);
      return JSON.stringify(report);
    },
  }), /five fleet rows/);
  assert.ok(!fs.existsSync(path.join(output, 'check.json')));
});
