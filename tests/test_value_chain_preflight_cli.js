'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const moduleDir = fs.existsSync(path.join(repo, 'src', 'capability_admission.py'))
  ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';
const task = 'print, for every live capability, situation_count / invocation_count / usable / '
  + 'acted_on / outcome with the first broken step named';

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'value-chain-preflight-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const env = { ...process.env,
    PYTHONPATH: moduleDir,
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_STATE_DIR: path.join(root, 'state'),
    ORCH_CAPABILITIES_PATH: path.join(root, 'runtime', 'capabilities.json') };
  fs.mkdirSync(env.ORCH_LOCAL_RUNTIME);
  fs.writeFileSync(env.ORCH_CAPABILITIES_PATH, '{}\n');
  function run(args) {
    // File capture also works on sandboxes that disallow child-process pipes.
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(python, args, {
        cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 30000,
      });
      assert.ifError(result.error);
      assert.equal(result.status, 0, fs.readFileSync(errors, 'utf8'));
      return JSON.parse(fs.readFileSync(output, 'utf8'));
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      fs.rmSync(output);
      fs.rmSync(errors);
    }
  }
  const spec = run(['-c',
    'import json, capabilities\n'
    + 'print(json.dumps({"capability_id": "value-chain-monitor", '
    + '**capabilities.KNOWN_DECLARATIONS["value-chain-monitor"]}))']);
  spec.task = task;
  return { root, spec,
    assertReadOnly: () => {
      assert.equal(fs.readFileSync(env.ORCH_CAPABILITIES_PATH, 'utf8'), '{}\n');
      assert.equal(fs.existsSync(env.ORCH_STATE_DIR), false);
    },
    preflight: (argument) => run([path.join(moduleDir, 'capability_admission.py'),
      '--preflight', argument]) };
}

function assertReady(result) {
  assert.equal(result.capability_id, 'value-chain-monitor');
  assert.equal(result.ready_to_build, true);
  assert.deepEqual(result.declarable_missing, []);
  assert.deepEqual(Object.keys(result.checks).sort(), [
    'caller_exists', 'dedup_recorded', 'expiry_or_cadence', 'findable', 'fixture',
    'heartbeat', 'kill_switch', 'outcome_path', 'rollback',
  ]);
  assert.deepEqual(result.obligations, ['caller_exists', 'findable', 'fixture', 'heartbeat']);
  for (const [name, check] of Object.entries(result.checks)) {
    assert.equal(check.ok, result.obligations.includes(name) ? null : true, name);
  }
}

test('value-chain preflight CLI accepts the stated task and all nine admission parts', (t) => {
  const w = world(t);
  assertReady(w.preflight(JSON.stringify(w.spec)));
  // Preflight is a design check; it must not register a capability or create state.
  w.assertReadOnly();
  assert.deepEqual(fs.readdirSync(w.root).sort(), ['runtime']);
});

test('value-chain preflight CLI accepts a spec file without modifying its bytes', (t) => {
  const w = world(t);
  const file = path.join(w.root, 'spec with spaces.json');
  const bytes = JSON.stringify(w.spec, null, 2) + '\n';
  fs.writeFileSync(file, bytes);
  const inline = w.preflight(JSON.stringify(w.spec));
  const fromFile = w.preflight('@' + file);
  assertReady(fromFile);
  assert.deepEqual(fromFile, inline);
  assert.equal(fs.readFileSync(file, 'utf8'), bytes);
  w.assertReadOnly();
  assert.deepEqual(fs.readdirSync(w.root).sort(), ['runtime', 'spec with spaces.json']);
});

test('value-chain preflight CLI names a missing dedup requirement', (t) => {
  const w = world(t);
  const result = w.preflight(JSON.stringify({ ...w.spec, notes: '' }));
  assert.equal(result.ready_to_build, false);
  assert.deepEqual(result.declarable_missing, ['dedup_recorded']);
  assert.equal(result.checks.dedup_recorded.ok, false);
  assert.equal(result.checks.kill_switch.ok, true);
  w.assertReadOnly();
  assert.deepEqual(fs.readdirSync(w.root).sort(), ['runtime']);
});

for (const [label, changes, requirement] of [
  ['consumer', { downstream_consumer: '' }, 'outcome_path'],
  ['learning sink', { learning_sink: '', outcome_links: [] }, 'outcome_path'],
  ['kill switch', { kill_switch: '' }, 'kill_switch'],
  ['rollback', { rollback: {} }, 'rollback'],
  ['review cadence', { trigger_cadence: '', expiry: null, activation_deadline: null },
    'expiry_or_cadence'],
]) {
  test(`value-chain preflight CLI rejects a missing ${label}`, (t) => {
    const w = world(t);
    const result = w.preflight(JSON.stringify({ ...w.spec, ...changes }));
    assert.equal(result.ready_to_build, false);
    assert.deepEqual(result.declarable_missing, [requirement]);
    assert.equal(result.checks[requirement].ok, false);
    // A declaration failure must not turn unverified implementation obligations into passes.
    assert.deepEqual(result.obligations, ['caller_exists', 'findable', 'fixture', 'heartbeat']);
    for (const obligation of result.obligations) {
      assert.equal(result.checks[obligation].ok, null, obligation);
    }
    w.assertReadOnly();
    assert.deepEqual(fs.readdirSync(w.root), ['runtime']);
  });
}

test('value-chain preflight CLI reports every missing declaration in one result', (t) => {
  const w = world(t);
  const result = w.preflight(JSON.stringify({ ...w.spec,
    notes: '', downstream_consumer: '', learning_sink: '', outcome_links: [],
    kill_switch: '', rollback: {}, trigger_cadence: '', expiry: null,
    activation_deadline: null }));
  assert.equal(result.ready_to_build, false);
  assert.deepEqual(result.declarable_missing, [
    'dedup_recorded', 'outcome_path', 'kill_switch', 'rollback', 'expiry_or_cadence',
  ]);
  for (const requirement of result.declarable_missing) {
    assert.equal(result.checks[requirement].ok, false, requirement);
  }
  w.assertReadOnly();
  assert.deepEqual(fs.readdirSync(w.root), ['runtime']);
});

for (const [label, changes, cause] of [
  ['exercise category', { findability_category: '' }, 'bound_nowhere'],
  ['exercise rationale', { findability_rationale: '' }, 'bound_nowhere'],
  ['obsolete exemption', { findability_category: 'no_surface' }, 'declared_no_surface'],
]) {
  test(`value-chain preflight CLI rejects findability with a missing ${label}`, (t) => {
    const w = world(t);
    const result = w.preflight(JSON.stringify({ ...w.spec, ...changes }));
    assert.equal(result.ready_to_build, false);
    assert.deepEqual(result.declarable_missing, ['findable']);
    assert.equal(result.checks.findable.ok, false);
    assert.ok(result.checks.findable.detail.startsWith(cause + ':'),
      result.checks.findable.detail);
    // Without justified exercise intent, findability must be checked against the
    // empty ledger instead of being deferred as an implementation obligation.
    assert.deepEqual(result.obligations, ['caller_exists', 'fixture', 'heartbeat']);
    for (const obligation of result.obligations) {
      assert.equal(result.checks[obligation].ok, null, obligation);
    }
    for (const declaration of [
      'dedup_recorded', 'outcome_path', 'kill_switch', 'rollback', 'expiry_or_cadence',
    ]) {
      assert.equal(result.checks[declaration].ok, true, declaration);
    }
    w.assertReadOnly();
    assert.deepEqual(fs.readdirSync(w.root), ['runtime']);
  });
}
