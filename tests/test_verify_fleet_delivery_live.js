'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { test } = require('node:test');
const { names, validateReport, verifyLive, fleetSourceHashes } = require('../scripts/verify_fleet_delivery_live');

const checked = Date.parse('2026-11-05T12:00:00Z');
const runtime = {
  executable: '/resolved/venv/bin/python3.14', version: [3, 14, 10],
  platform: 'linux', architecture: 'x86_64',
};

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

test('Windows report newlines preserve validation of all five rows', () => {
  const { report, text } = example();
  const windowsText = text.replaceAll('\n', '\r\n');
  assert.equal(validateReport(report, windowsText, checked, checked + 2000),
    report.fleet_delivery_measures);
  for (const broken of [
    windowsText + windowsText,
    windowsText.replace('  As of 2026-11-05', '  As of 2026-10-19'),
    windowsText.replace('2 [recorded example]', '0 [recorded example]'),
    windowsText.replace(`  ${names.released_flow}: 2 [recorded example]\r\n`, ''),
    windowsText.replace('\r\n\r\n## Other',
      `\r\n  ${names.merge_to_close}: 2 [recorded example]\r\n\r\n## Other`),
  ]) {
    assert.throws(() => validateReport(report, broken, checked, checked + 2000));
  }
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
      if (args[0] === '-c') {
        assert.equal(calls, 1, 'interpreter must be checked before collecting evidence');
        assert.match(args[1], /sys\.executable/);
        return JSON.stringify(runtime);
      }
      assert.equal(calls, 2);
      assert.equal(args[0], path.resolve(__dirname, '../src/switch_review.py'));
      assert.deepEqual(args.slice(1), ['--json', '--text-out', path.join(output, 'report.txt')]);
      assert.equal(options.env.ORCH_LOCAL_RUNTIME, path.join(output, 'runtime'));
      assert.equal(options.env.ORCH_STATE_DIR, path.join(output, 'state'));
      fs.writeFileSync(args.at(-1), text);
      return JSON.stringify(report);
    },
  });
  assert.equal(calls, 2);
  assert.equal(receipt.checked_at, '2026-11-05T12:00:00.000Z');
  assert.deepEqual(receipt.python_runtime, runtime);
  assert.deepEqual(receipt.source_sha256, fleetSourceHashes(path.resolve(__dirname, '..')));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(output, 'check.json'))), receipt);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(output, 'report.json'))), report);
  await assert.rejects(verifyLive(output), /EEXIST/);
});

test('live verification retains Windows report bytes in the evidence directory', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-crlf-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  const { report, text } = example();
  const windowsText = text.replaceAll('\n', '\r\n');
  const receipt = await verifyLive(output, {
    python: '/selected/python', now: () => checked,
    run: async (python, args) => {
      if (args[0] === '-c') return JSON.stringify(runtime);
      fs.writeFileSync(args.at(-1), windowsText);
      return JSON.stringify(report);
    },
  });
  assert.equal(fs.readFileSync(path.join(output, 'report.txt'), 'utf8'), windowsText);
  assert.deepEqual(receipt.rows, report.fleet_delivery_measures.rows);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(output, 'check.json'))), receipt);
});

test('live receipts fingerprint the fleet implementation, tests, fixture and verifier bytes', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-source-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const files = [
    'src/switch_review.py', 'tests/test_switch_review_fleet.py',
    'tests/fixtures/switch_review_fleet.json', 'scripts/verify_fleet_delivery_live.js',
  ];
  const expected = {};
  for (const file of files) {
    const content = Buffer.from(`${file}\r\n`);
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), content);
    expected[file] = createHash('sha256').update(content).digest('hex');
  }
  assert.deepEqual(fleetSourceHashes(root), expected);
  fs.appendFileSync(path.join(root, files[0]), '\n');
  assert.notEqual(fleetSourceHashes(root)[files[0]], expected[files[0]]);
  fs.unlinkSync(path.join(root, files[1]));
  assert.throws(() => fleetSourceHashes(root), /ENOENT/);
});

test('source changes during live collection block a success receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-source-change-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  const { report, text } = example();
  let collected = false;
  await assert.rejects(verifyLive(output, {
    python: '/selected/python', now: () => checked,
    readSourceHashes: () => ({ 'src/switch_review.py': collected ? 'after' : 'before' }),
    run: async (python, args) => {
      if (args[0] === '-c') return JSON.stringify(runtime);
      fs.writeFileSync(path.join(output, 'report.txt'), text);
      collected = true;
      return JSON.stringify(report);
    },
  }), /fleet source changed during live verification/);
  assert.ok(!fs.existsSync(path.join(output, 'check.json')));
  assert.ok(!fs.existsSync(path.join(output, 'report.json')));
});

test('a missing interpreter fails without a fallback or success receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-missing-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  await assert.rejects(verifyLive(output, { python: path.join(root, 'missing-python') }), /ENOENT/);
  assert.ok(!fs.existsSync(path.join(output, 'check.json')));
});

test('live verification requires explicit interpreter selection before creating evidence', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-selection-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const previous = process.env.PYTHON;
  delete process.env.PYTHON;
  t.after(() => {
    if (previous === undefined) delete process.env.PYTHON;
    else process.env.PYTHON = previous;
  });
  for (const python of [undefined, '', '   ', 42]) {
    const output = path.join(root, `evidence-${String(python).length}`);
    await assert.rejects(verifyLive(output, {
      python, run: async () => assert.fail('unselected interpreter executed'),
    }), /set PYTHON to the repository-selected interpreter/);
    assert.ok(!fs.existsSync(output), 'unselected interpreter created evidence directory');
  }
});

test('a successful command with an incomplete report cannot leave a success receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-incomplete-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  const { report, text } = example();
  delete report.fleet_delivery_measures.rows.released_flow;
  await assert.rejects(verifyLive(output, {
    python: '/selected/venv/bin/python', now: () => checked,
    run: async (python, args) => {
      if (args[0] === '-c') return JSON.stringify(runtime);
      fs.writeFileSync(path.join(output, 'report.txt'), text);
      return JSON.stringify(report);
    },
  }), /five fleet rows/);
  assert.ok(!fs.existsSync(path.join(output, 'check.json')));
});

test('unsupported or malformed interpreter evidence cannot produce a live receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-runtime-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const invalid = [
    '{broken', 'null', '[]', '{}',
    ...[
      { version: [3, 12, 9] }, { version: [3, 15, 0] },
      { version: ['3', '14', '10'] }, { version: [3, 14] },
      { version: [3, 14, -1] }, { executable: '' }, { executable: 'python3' },
      { platform: '' }, { platform: null }, { architecture: '' },
    ].map((change) => JSON.stringify({ ...runtime, ...change })),
  ];
  for (const [index, stdout] of invalid.entries()) {
    const output = path.join(root, `evidence-${index}`);
    let calls = 0;
    await assert.rejects(verifyLive(output, {
      python: '/selected/shim/python3',
      run: async (python, args) => {
        calls += 1;
        assert.equal(args[0], '-c');
        return stdout;
      },
    }), /Python runtime/);
    assert.equal(calls, 1, 'invalid interpreter must block the report command');
    assert.ok(!fs.existsSync(path.join(output, 'check.json')));
    assert.ok(!fs.existsSync(path.join(output, 'report.txt')));
  }
});

test('the backup standard interpreter is accepted and recorded', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-live-backup-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const output = path.join(root, 'evidence');
  const { report, text } = example();
  const backup = { ...runtime, executable: '/resolved/python3.13', version: [3, 13, 18] };
  const receipt = await verifyLive(output, {
    python: '/selected/shim/python3', now: () => checked,
    run: async (python, args) => {
      if (args[0] === '-c') return JSON.stringify(backup);
      fs.writeFileSync(path.join(output, 'report.txt'), text);
      return JSON.stringify(report);
    },
  });
  assert.equal(receipt.command[0], '/selected/shim/python3');
  assert.deepEqual(receipt.python_runtime, backup);
});
