'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { observe, main } = require('../scripts/observe_redirect_runtime');

function sandbox(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-observation-control-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const runtime = path.join(root, 'runtime');
  const stateDir = path.join(root, 'state');
  fs.mkdirSync(path.join(runtime, 'redirect-shadow'), { recursive: true });
  fs.mkdirSync(stateDir);
  const corpus = path.join(runtime, 'redirect-shadow/shadow.jsonl');
  const plan = path.join(stateDir, 'keepalive-supervisor-stage2-plan.json');
  fs.writeFileSync(corpus, '');
  fs.writeFileSync(plan, JSON.stringify({ generated_at: Math.floor(Date.now() / 1000), plans: [] }));
  const env = { PATH: process.env.PATH, ORCH_CLAIMS_DIR: path.join(root, 'claims') };
  return { root, runtime, stateDir, corpus, plan, env };
}

function snapshot(root) {
  return Object.fromEntries(fs.readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile()).map((entry) => {
      const file = path.join(entry.parentPath, entry.name);
      return [path.relative(root, file), fs.readFileSync(file)];
    }));
}

// Manufactured screen responses check the collector, never production eligibility.
function control(s, screen = {}) {
  return { ...s, run(command, args, options) {
    assert.equal(command, 'python3');
    assert.deepEqual(args.slice(0, 4), ['-B', path.resolve(__dirname, '../src/redirect_apply.py'), '--screen', '--json']);
    assert.ok(!args.some((arg) => ['--status', '--apply', '--link-outcomes', '--dry-run'].includes(arg)));
    assert.equal(options.env.ORCH_STATE_DIR, s.stateDir);
    assert.equal(options.env.ORCH_LOCAL_RUNTIME, s.runtime);
    assert.equal(options.env.PYTHONPATH, undefined);
    assert.equal(options.env.PYTHONHOME, undefined);
    return { status: 0, stdout: JSON.stringify({
      gate: { synced_needed: 5, disagreements_needed: 3 },
      current_candidates: 0, passing_screen: 0, candidates: [],
      population: { status: 'current' }, sweep_population: { status: 'missing' }, ...screen,
    }) };
  } };
}

test('missing owner corpus is unknown, never a verified empty population or source defaults', (t) => {
  const s = sandbox(t);
  fs.unlinkSync(s.corpus);
  const result = observe({ ...s, run() { assert.fail('missing runtime must not invoke Python'); } });
  assert.equal(result.status, 'unavailable');
  assert.equal(result.candidates, null);
  assert.equal(result.drainable, null);
  assert.equal(result.blocking, null);
  assert.equal(result.process_flag, null);
  assert.deepEqual(result.limitations, ['owner_corpus_unavailable']);
});

test('captures zero from the authoritative screen with hashes and no runtime writes', (t) => {
  const s = sandbox(t);
  const before = snapshot(s.root);
  const started = Date.now();
  const result = observe(control(s));
  assert.equal(result.status, 'observed');
  assert.equal(result.candidates, 0);
  assert.equal(result.drainable, 0);
  assert.deepEqual(result.blocking, { synced_outcomes_needed: 5, disagreements_needed: 3 });
  assert.match(result.artifacts.plan.sha256, /^[a-f0-9]{64}$/);
  assert.match(result.screen_source_sha256, /^[a-f0-9]{64}$/);
  assert.ok(Date.parse(result.observed_at) >= started);
  assert.equal(result.operational_acceptance, 'not_established');
  assert.equal(result.historical_replay_receipt, 'not_established');
  assert.deepEqual(snapshot(s.root), before);
});

test('retains private refusal and lineage bindings without promoting recorded applies to outcomes', (t) => {
  const s = sandbox(t);
  fs.writeFileSync(s.corpus, JSON.stringify({ kind: 'redirect_apply', ts: 1793664000,
    target: 'PRIVATE-target', role_run_id: 'PRIVATE-role', applied: true,
    authorized: true, dry_run: false }) + '\n');
  fs.writeFileSync(path.join(s.stateDir, 'redirect-apply.log'),
    '[2026-11-03T00:00:00Z] redirect apply/link\n'
    + 'candidates: supervisor 1, sweep 0, eligible 0\n'
    + 'offloads_spent=0 authorized=0 applied=0 (flag ORCH_REDIRECT_APPLY_BOOTSTRAP=1)\n'
    + 'PRIVATE-target: PRIVATE-refusal\n');
  const result = observe(control({ ...s, env: { ...s.env, ORCH_REDIRECT_APPLY_BOOTSTRAP: '1' } }, {
    current_candidates: 1, passing_screen: 0,
    candidates: [{ target: 'PRIVATE-target', passes_screen: false, blocks: ['PRIVATE-refusal'] }],
  }));
  assert.equal(result.candidates, 1);
  assert.equal(result.drainable, 0);
  assert.equal(result.refusals.length, 1);
  assert.equal(result.recorded_lineage[0].target_sha256, result.refusals[0].target_sha256);
  assert.equal(result.recorded_lineage[0].applied, true);
  assert.equal(result.recorded_lineage[0].synced, false);
  assert.equal(result.logged_runs.latest[0].recorded_at, '2026-11-03T00:00:00Z');
  assert.equal(result.logged_runs.latest[0].summaries.length, 2);
  assert.match(result.flag_scope, /deployed_tick_flag_not_established/);
  assert.equal(result.operational_acceptance, 'not_established');
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
});

test('unavailable populations and invalid corpora cannot become demand-derived counts', (t) => {
  const s = sandbox(t);
  fs.writeFileSync(path.join(s.stateDir, 'switch-review.json'), JSON.stringify({ demand: 18 }));
  const result = observe(control(s, { current_candidates: null, passing_screen: null,
    population: { status: 'stale' } }));
  assert.equal(result.status, 'unavailable');
  assert.equal(result.candidates, null);
  assert.equal(result.drainable, null);
  fs.writeFileSync(s.corpus, 'PRIVATE-malformed\n');
  const invalid = observe({ ...s, run() { assert.fail('invalid corpus must not be screened'); } });
  assert.deepEqual(invalid.limitations, ['corpus_invalid']);
  assert.doesNotMatch(JSON.stringify(invalid), /PRIVATE-/);
});

test('changing input, failed processes and malformed screen output invalidate capture', (t) => {
  const s = sandbox(t);
  const base = control(s);
  let result = observe({ ...base, run(...args) {
    const output = base.run(...args);
    fs.appendFileSync(s.corpus, '{"kind":"new-event"}\n');
    return output;
  } });
  assert.equal(result.candidates, null);
  assert.deepEqual(result.changed_artifacts, ['corpus']);
  for (const execution of [
    { status: 0, error: { code: 'EPERM' }, stdout: 'PRIVATE-output' },
    { status: null, signal: 'SIGTERM' },
    { status: 1, stderr: 'PRIVATE-error' },
    { status: 0, stdout: 'PRIVATE-not-json' },
  ]) {
    result = observe({ ...s, run: () => execution });
    assert.equal(result.status, 'unavailable');
    assert.equal(result.candidates, null);
    assert.equal(result.drainable, null);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
  }
});

test('real screen distinguishes empty, refused and stale populations without writes', (t) => {
  const s = sandbox(t);
  for (const scenario of ['empty', 'refused', 'stale']) {
    const report = { target: 'PRIVATE-running-target', agent: 'keepalive', lane: 'closer',
      state: 'running', recommended_action: 'wait' };
    fs.writeFileSync(s.plan, JSON.stringify({
      generated_at: scenario === 'stale' ? 1 : Math.floor(Date.now() / 1000),
      plans: scenario === 'empty' ? [] : [{ eligible: true, report, acceptance_criteria: 'AC' }],
    }));
    const before = snapshot(s.root);
    let execution;
    const result = observe({ ...s, python: process.env.PYTHON || 'python3', run(...args) {
      execution = spawnSync(...args);
      return execution;
    } });
    assert.equal(execution.status, 0, execution.stderr);
    const screen = JSON.parse(execution.stdout);
    const candidates = scenario === 'stale' ? null : scenario === 'refused' ? 1 : 0;
    const drainable = scenario === 'stale' ? null : 0;
    assert.equal(screen.current_candidates, candidates);
    assert.equal(screen.passing_screen, drainable);
    if (scenario === 'refused') assert.ok(screen.candidates[0].blocks.length);
    // This restricted runner can return EPERM alongside a zero child exit. Keep
    // that real failure visible; do not substitute a successful process response.
    assert.equal(result.status, execution.error || scenario === 'stale' ? 'unavailable' : 'observed');
    assert.equal(result.candidates, execution.error ? null : candidates);
    assert.equal(result.drainable, execution.error ? null : drainable);
    assert.deepEqual(snapshot(s.root), before);
  }
});

test('CLI requires an explicit owner path', (t) => {
  const error = t.mock.method(console, 'error', () => {});
  assert.equal(main([]), 2);
  assert.equal(main(['--apply']), 2);
  error.mock.restore();
});
