'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';

function runtime(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'consumer-sync-cli-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const ledger = path.join(directory, 'capabilities.json');
  const env = {
    PATH: process.env.PATH,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_LOCAL_RUNTIME: directory,
    ORCH_STATE_DIR: path.join(directory, 'state'),
    ORCH_CAPABILITIES_PATH: ledger,
    ORCH_CAPABILITY_HEARTBEATS: '0',
  };
  async function run(args, status) {
    const result = await new Promise((resolve, reject) => {
      const child = spawn(python, args, { cwd: repo, env, timeout: 30000 });
      let stdout = '';
      let stderr = '';
      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      child.stdout.on('data', (data) => { stdout += data; });
      child.stderr.on('data', (data) => { stderr += data; });
      child.on('error', reject);
      child.on('close', (code, signal) => resolve({ code, signal, stdout, stderr }));
    });
    assert.equal(result.signal, null, result.stderr || 'Python process was terminated');
    assert.equal(result.code, status, result.stderr || result.stdout);
    return result.stdout;
  }
  async function check(status) {
    const before = fs.existsSync(ledger) ? fs.readFileSync(ledger) : null;
    const report = JSON.parse(await run([
      path.join(modules, 'capability_admission.py'), '--check-consumer-sync', '--json',
    ], status));
    const after = fs.existsSync(ledger) ? fs.readFileSync(ledger) : null;
    assert.deepEqual(after, before, 'verification must not persist a preview or seed a ledger');
    return report;
  }
  function persist(...rows) {
    fs.writeFileSync(ledger, JSON.stringify({
      schema_version: 1,
      capabilities: Object.fromEntries(rows.map((row) => [row.capability_id, row])),
    }));
  }
  return { ledger, run, check, persist };
}

test('consumer sync CLI requires persisted reconciliation across fresh Python processes', async (t) => {
  const { ledger, run, check, persist } = runtime(t);
  assert.deepEqual((await check(1)).consumer_sync, { live_rows: 0, pending: {} });
  persist({
    capability_id: 'offload', status: 'active',
    downstream_consumer: 'the run consuming offload output', consumer_missing: true,
    event_history: [{ type: 'invocation', timestamp: 1 }], outcome_links: [],
  });
  const pending = await check(1);
  assert.deepEqual(pending.consumer_sync, {
    live_rows: 1, pending: { offload: ['downstream_consumer', 'consumer_missing'] },
  });
  assert.deepEqual(pending.consumers.drainable, [], 'a clean preview is insufficient');

  // Exercise the production writing loader, as the owner\'s normal sync/tick does.
  await run(['-c', 'import capabilities; capabilities.load()'], 0);
  const synced = await check(0);
  assert.ok(synced.consumer_sync.live_rows > 0);
  assert.deepEqual(synced.consumer_sync.pending, {});
  assert.deepEqual(synced.consumers.drainable, []);
  assert.deepEqual(synced.consumers.by_class.prose, []);
  assert.deepEqual(synced.consumers.by_class.unresolved, []);
  assert.deepEqual(synced.consumers.by_class.consumer_missing, []);
  assert.ok(synced.consumers.by_class.unmeasured.includes('offload'));
  assert.deepEqual(synced.consumers.by_class.acting, [], 'sync grants no action credit');
  const persisted = fs.readFileSync(ledger);
  const text = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 0);
  assert.match(text, /CONSUMERS \(10th\): \d+ of \d+ declare a consumer this tree can find/);
  assert.match(text, /prose 0, missing 0, undeclared 0 — DRAINABLE 0/);
  assert.match(text, /CONSUMER SYNC: persisted consumer declarations match this tree/);
  assert.deepEqual(fs.readFileSync(ledger), persisted, 'text reporting must also be read-only');
  const row = JSON.parse(fs.readFileSync(ledger, 'utf8')).capabilities.offload;
  assert.equal(row.status, 'active');
  assert.deepEqual(row.outcome_links, []);
  assert.deepEqual(row.event_history[0], { type: 'invocation', timestamp: 1 });
});

for (const [classification, declaration] of [
  ['prose', { downstream_consumer: 'No rail applies this output.' }],
  ['unresolved', { downstream_consumer: 'absent_module.py:consume' }],
  ['undeclared', {}],
  ['consumer_missing', { consumer_missing: true, notes: 'No rail applies this output.' }],
]) {
  test(`consumer sync CLI rejects persisted ${classification} without pending declarations`, async (t) => {
    const { check, persist } = runtime(t);
    persist({ capability_id: 'sync-fixture', status: 'active', ...declaration });
    const report = await check(1);
    assert.deepEqual(report.consumer_sync, { live_rows: 1, pending: {} });
    assert.deepEqual(report.consumers.by_class[classification], ['sync-fixture']);
    assert.deepEqual(report.consumers.drainable, ['sync-fixture']);
  });
}

for (const status of ['retired', 'superseded']) {
  test(`consumer sync CLI excludes ${status} rows without masking live sync debt`, async (t) => {
    const { ledger, check, persist, run } = runtime(t);
    // A known declaration still reconciles in the report preview after retirement.
    // Neither that edit nor the row's existence is evidence of a live owner sync.
    const terminal = {
      capability_id: 'offload', status,
      downstream_consumer: 'the run consuming offload output', consumer_missing: true,
      event_history: [{ type: 'invocation', timestamp: 1 }],
    };
    persist(terminal);
    const inactive = await check(1);
    assert.deepEqual(inactive.consumer_sync, { live_rows: 0, pending: {} });
    assert.equal(inactive.consumers.total, 0);
    assert.deepEqual(inactive.consumers.drainable, []);
    const text = await run([
      path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
    ], 1);
    assert.match(text, /CONSUMER SYNC: unverified \(no live ledger rows/);

    // Add a real live declaration with a clean preview but an unpersisted binding.
    const live = {
      capability_id: 'testgen-lane', status: 'active',
      downstream_consumer: 'the acceptance gate consumes candidate tests',
    };
    persist(terminal, live);
    const pending = await check(1);
    assert.deepEqual(pending.consumer_sync, {
      live_rows: 1, pending: { 'testgen-lane': ['downstream_consumer'] },
    });
    assert.equal(pending.consumers.total, 1);
    assert.deepEqual(pending.consumers.drainable, [], 'a clean preview cannot mask pending sync');

    // Persist only the live binding. Terminal declaration debt must remain excluded.
    const declaration = await run([
      '-c',
      'import capabilities; print(capabilities.KNOWN_DECLARATIONS["testgen-lane"]["downstream_consumer"])',
    ], 0);
    persist(terminal, { ...live, downstream_consumer: declaration.trim() });
    const synced = await check(0);
    assert.deepEqual(synced.consumer_sync, { live_rows: 1, pending: {} });
    assert.equal(synced.consumers.total, 1);
    assert.deepEqual(synced.consumers.drainable, []);
    assert.deepEqual(synced.consumers.by_class.acting, [], 'verification grants no action credit');
    const stored = JSON.parse(fs.readFileSync(ledger, 'utf8')).capabilities;
    assert.deepEqual(stored.offload, terminal, 'verification must preserve terminal rows');
    assert.equal(stored['testgen-lane'].status, 'active');
  });
}
