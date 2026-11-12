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

test('consumer sync CLI rejects resolvable declaration drift after a successful sync', async (t) => {
  const { ledger, run, check, persist } = runtime(t);
  persist({
    capability_id: 'offload', status: 'active',
    event_history: [{ type: 'invocation', timestamp: 1 }], outcome_links: [],
  });
  await run(['-c', 'import capabilities; capabilities.load()'], 0);
  assert.deepEqual((await check(0)).consumer_sync.pending, {});
  const synced = JSON.parse(fs.readFileSync(ledger, 'utf8')).capabilities;

  // A different real symbol still resolves. CONSUMERS alone cannot prove that the
  // deployed ledger has retained this tree's reviewed output-consumer binding.
  persist(...Object.values(synced).map((row) => row.capability_id === 'offload'
    ? { ...row, downstream_consumer: 'capability_admission.py:format_report' }
    : row));
  const drifted = await check(1);
  assert.deepEqual(drifted.consumer_sync, {
    live_rows: Object.keys(synced).length,
    pending: { offload: ['downstream_consumer'] },
  });
  assert.deepEqual(drifted.consumers.by_class.prose, []);
  assert.deepEqual(drifted.consumers.by_class.unresolved, []);
  assert.deepEqual(drifted.consumers.drainable, []);
  const before = fs.readFileSync(ledger);
  const text = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 1);
  assert.match(text, /prose 0, missing 0, undeclared 0 — DRAINABLE 0/);
  assert.match(text, /offload: downstream_consumer/);
  assert.deepEqual(fs.readFileSync(ledger), before, 'reporting must preserve the drift for owner sync');

  await run(['-c', 'import capabilities; capabilities.load()'], 0);
  assert.deepEqual((await check(0)).consumer_sync.pending, {});
  const repaired = JSON.parse(fs.readFileSync(ledger, 'utf8')).capabilities.offload;
  assert.equal(repaired.downstream_consumer, synced.offload.downstream_consumer);
  assert.equal(repaired.status, 'active');
  assert.deepEqual(repaired.outcome_links, []);
  assert.deepEqual(repaired.event_history.slice(0, synced.offload.event_history.length),
    synced.offload.event_history);
});

test('consumer sync CLI separates measured action debt from persisted declarations', async (t) => {
  const { ledger, run, check, persist } = runtime(t);
  const declaration = await run([
    '-c', 'import capabilities; print(capabilities.KNOWN_DECLARATIONS["offload"]["downstream_consumer"])',
  ], 0);
  persist({
    capability_id: 'offload', status: 'active', downstream_consumer: declaration.trim(),
    event_history: [{ type: 'invocation', timestamp: 1 }], outcome_links: [],
  });
  const before = fs.readFileSync(ledger);
  const unavailable = await check(0);
  assert.deepEqual(unavailable.consumers.by_class.unmeasured, ['offload']);

  // A real, readable Brain with no consuming runs establishes a measured zero.
  // Use the production schema rather than replacing the monitor's context.
  await run(['-c', 'import feedback; feedback._conn().close()'], 0);
  const measured = await check(0);
  assert.deepEqual(measured.consumer_sync, { live_rows: 1, pending: {} });
  assert.deepEqual(measured.consumers.by_class.unmeasured, []);
  assert.deepEqual(measured.consumers.by_class.not_acting, ['offload']);
  assert.deepEqual(measured.consumers.by_class.acting, []);
  assert.deepEqual(measured.consumers.drainable, []);
  assert.equal(measured.consumers.drained, false, 'action debt must remain visible');
  const text = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 0);
  assert.match(text, /not acting \(trailing window\) 1/);
  assert.match(text, /prose 0, missing 0, undeclared 0 — DRAINABLE 0/);
  assert.match(text, /CONSUMER SYNC: persisted consumer declarations match this tree/);
  assert.deepEqual(fs.readFileSync(ledger), before, 'measuring action debt must preserve the ledger');
});

test('consumer sync CLI tracks each remaining live row during a partial owner sync', async (t) => {
  const { ledger, run, check, persist } = runtime(t);
  const declarations = JSON.parse(await run([
    '-c',
    'import json, capabilities; print(json.dumps(capabilities.KNOWN_GATES | capabilities.KNOWN_DECLARATIONS))',
  ], 0));
  const offload = {
    capability_id: 'offload', status: 'active',
    downstream_consumer: declarations.offload.downstream_consumer,
    consumer_missing: true,
    event_history: [{ type: 'invocation', timestamp: 1 }], outcome_links: [],
  };
  const testgen = {
    capability_id: 'testgen-lane', status: 'active',
    downstream_consumer: 'the acceptance gate consumes candidate tests',
    event_history: [{ type: 'invocation', timestamp: 2 }], outcome_links: [],
  };
  persist(offload, testgen);
  const partial = await check(1);
  assert.deepEqual(partial.consumer_sync, {
    live_rows: 2,
    pending: {
      offload: ['consumer_missing'],
      'testgen-lane': ['downstream_consumer'],
    },
  });
  assert.deepEqual(partial.consumers.drainable, [], 'a clean preview cannot complete a partial sync');

  // Clearing one row's stale absence flag leaves the other live row owing a sync.
  delete offload.consumer_missing;
  persist(offload, testgen);
  const remaining = await check(1);
  assert.deepEqual(remaining.consumer_sync, {
    live_rows: 2, pending: { 'testgen-lane': ['downstream_consumer'] },
  });
  const before = fs.readFileSync(ledger);
  const text = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 1);
  assert.match(text, /prose 0, missing 0, undeclared 0 — DRAINABLE 0/);
  assert.match(text, /CONSUMER SYNC: 1 of 2 live rows pending/);
  assert.match(text, /PENDING SYNC testgen-lane: downstream_consumer/);
  assert.doesNotMatch(text, /PENDING SYNC offload:/);
  assert.deepEqual(fs.readFileSync(ledger), before, 'verification must preserve the unfinished sync');

  testgen.downstream_consumer = declarations['testgen-lane'].downstream_consumer;
  persist(offload, testgen);
  const complete = await check(0);
  assert.deepEqual(complete.consumer_sync, { live_rows: 2, pending: {} });
  assert.deepEqual(complete.consumers.drainable, []);
  assert.deepEqual(complete.consumers.by_class.acting, [], 'declaration sync grants no action credit');
  assert.deepEqual(JSON.parse(fs.readFileSync(ledger, 'utf8')).capabilities, {
    offload, 'testgen-lane': testgen,
  }, 'verification must preserve both rows and their existing lifecycle and evidence');
});

test('consumer sync CLI verifies the full declaration inventory after a real sync', async (t) => {
  const { ledger, run, check, persist } = runtime(t);
  const declarations = JSON.parse(await run([
    '-c',
    'import json, capabilities; print(json.dumps(capabilities.KNOWN_GATES | capabilities.KNOWN_DECLARATIONS))',
  ], 0));
  const ids = Object.keys(declarations).sort();
  assert.ok(ids.length > 0, 'acceptance needs a nonempty declaration inventory');
  const absent = ids.filter((id) => declarations[id].consumer_missing === true);
  persist(...ids.map((id) => ({
    capability_id: id, status: 'active',
    downstream_consumer: 'the run consuming this output',
    event_history: [{ type: 'invocation', timestamp: 1 }], outcome_links: [],
  })));

  const pending = await check(1);
  assert.deepEqual(Object.keys(pending.consumer_sync.pending).sort(), ids);
  assert.deepEqual(pending.consumers.drainable, absent, 'the preview alone is insufficient');
  await run(['-c', 'import capabilities; capabilities.load()'], 0);
  const synced = await check(0);
  assert.deepEqual(synced.consumer_sync, { live_rows: ids.length, pending: {} });
  assert.equal(synced.consumers.total, ids.length);
  assert.deepEqual(synced.consumers.by_class.prose, []);
  assert.deepEqual(synced.consumers.by_class.unresolved, []);
  assert.deepEqual(synced.consumers.by_class.undeclared, []);
  assert.deepEqual(synced.consumers.by_class.consumer_missing, absent);
  assert.deepEqual(synced.consumers.drainable, absent);
  assert.deepEqual(synced.consumers.by_class.acting, [], 'sync grants no action credit');
  const persisted = fs.readFileSync(ledger);
  const stored = JSON.parse(persisted).capabilities;
  assert.deepEqual(Object.keys(stored).sort(), ids, 'sync must preserve the inventory');
  for (const id of ids) {
    assert.equal(stored[id].downstream_consumer, declarations[id].downstream_consumer, id);
    assert.equal(stored[id].status, 'active', id);
    assert.deepEqual(stored[id].outcome_links, [], id);
    assert.deepEqual(stored[id].event_history[0], { type: 'invocation', timestamp: 1 }, id);
  }
  const text = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 0);
  assert.ok(text.includes(
    `CONSUMERS (10th): ${ids.length - absent.length} of ${ids.length} declare a consumer`,
  ));
  assert.ok(text.includes(
    `prose 0, missing 0, undeclared 0 — DRAINABLE ${absent.length}`,
  ));
  assert.deepEqual(fs.readFileSync(ledger), persisted, 'text reporting must preserve the snapshot');

  // A deployed ledger can contain rows absent from both declaration tables.
  // A fully synced source inventory must not hide one extra unresolved live rail.
  const extra = 'ledger-only-consumer-fixture';
  persist(...Object.values(stored), {
    capability_id: extra, status: 'active', downstream_consumer: 'absent_module.py:consume',
  });
  const incomplete = await check(1);
  assert.deepEqual(incomplete.consumer_sync, { live_rows: ids.length + 1, pending: {} });
  assert.deepEqual(incomplete.consumers.by_class.unresolved, [extra]);
  assert.deepEqual(incomplete.consumers.drainable, [...absent, extra].sort());
  const incompleteText = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 1);
  assert.ok(incompleteText.includes(
    `prose 0, missing 1, undeclared 0 — DRAINABLE ${absent.length + 1}`,
  ));
});

for (const [classification, declaration] of [
  ['prose', { downstream_consumer: 'No rail applies this output.' }],
  ['unresolved', { downstream_consumer: 'absent_module.py:consume' }],
  ['undeclared', {}],
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

test('M1 permits reviewed absence debt without granting admission or action credit', async (t) => {
  const { ledger, check, persist, run } = runtime(t);
  const reason = 'No rail applies this output.';
  const absent = {
    capability_id: 'reviewed-absence-fixture', status: 'active',
    downstream_consumer: reason, consumer_missing: true, notes: reason,
  };
  persist(absent);
  const report = await check(0);
  assert.deepEqual(report.consumer_sync, { live_rows: 1, pending: {} });
  assert.deepEqual(report.consumers.drainable, [absent.capability_id]);
  assert.deepEqual(report.consumers.by_class.consumer_missing, [absent.capability_id]);
  assert.deepEqual(report.consumers.reviewed_absences, { [absent.capability_id]: reason });
  assert.equal(report.consumers.drained, false);
  assert.deepEqual(report.consumers.by_class.acting, []);
  assert.equal(report.rows[0].checks.consumer_acts.ok, false);
  assert.equal(report.rows[0].admitted, false);
  assert.ok(report.rows[0].blocking.includes('consumer_acts'));
  assert.deepEqual(report.consumers.blocking, [absent.capability_id]);
  const before = fs.readFileSync(ledger);
  const text = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 0);
  assert.match(text, /0 of 1 declare a consumer this tree can find/);
  assert.match(text, /prose 0, missing 0, undeclared 0 — DRAINABLE 1/);
  assert.match(text, /reviewed absent 1, unresolved 0/);
  assert.ok(text.includes(`REVIEWED ABSENT ${absent.capability_id}: ${reason}`));
  assert.deepEqual(fs.readFileSync(ledger), before);

  // Keeping a reviewed finding cannot hide an unrelated broken declaration.
  persist(absent, {
    capability_id: 'unresolved-fixture', status: 'active',
    downstream_consumer: 'absent_module.py:consume',
  });
  const broken = await check(1);
  assert.deepEqual(broken.consumers.by_class.unresolved, ['unresolved-fixture']);
  assert.deepEqual(broken.consumers.drainable, [absent.capability_id, 'unresolved-fixture']);
  const brokenText = await run([
    path.join(modules, 'capability_admission.py'), '--check-consumer-sync',
  ], 1);
  assert.match(brokenText, /prose 0, missing 1, undeclared 0 — DRAINABLE 2/);
});

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
