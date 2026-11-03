'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { observe, main, sourceTreeState } = require('../scripts/observe_redirect_runtime');

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

test('source edits during screening invalidate counts for the entry point and imported modules', (t) => {
  const s = sandbox(t);
  const base = control(s);
  const sourceRoot = path.resolve(__dirname, '../src');
  const originalRead = fs.readFileSync;
  let changedFile;
  let afterExecution = false;
  t.mock.method(fs, 'readFileSync', function(file, ...args) {
    const bytes = originalRead.call(this, file, ...args);
    if (afterExecution && file === changedFile) {
      return Buffer.concat([Buffer.from(bytes), Buffer.from('\n# manufactured source change\n')]);
    }
    return bytes;
  });
  const before = snapshot(s.root);
  // Inject changed reads without writing any checkout source. The imported
  // module must be covered even though it is not the Python command's entry point.
  for (const name of ['redirect_apply.py', 'redirect_shadow.py']) {
    changedFile = path.join(sourceRoot, name);
    afterExecution = false;
    const result = observe({ ...base, run(...args) {
      const execution = base.run(...args);
      afterExecution = true;
      return execution;
    } });
    assert.equal(result.status, 'unavailable', name);
    assert.deepEqual(result.changed_artifacts, ['screen_sources']);
    assert.deepEqual(result.limitations, ['artifacts_changed_during_capture']);
    assert.equal(result.candidates, null);
    assert.equal(result.drainable, null);
    assert.equal(result.blocking, null);
    assert.equal(result.operational_acceptance, 'not_established');
    assert.match(result.artifacts.screen_sources.sha256, /^[a-f0-9]{64}$/);
  }
  assert.deepEqual(snapshot(s.root), before);
});

test('source fingerprints bind nested module bytes and roster without modifying the tree', (t) => {
  const s = sandbox(t);
  const directory = path.join(s.root, 'PRIVATE-sources');
  const nested = path.join(directory, 'package');
  fs.mkdirSync(nested, { recursive: true });
  const firstFile = path.join(directory, 'entry.py');
  const moduleFile = path.join(nested, 'PRIVATE-module.py');
  fs.writeFileSync(firstFile, '# manufactured entry\n');
  fs.writeFileSync(moduleFile, '# manufactured module A\n');
  const originalTime = fs.statSync(moduleFile).mtime;
  const before = snapshot(s.root);
  const initial = sourceTreeState(directory);
  assert.equal(initial.status, 'read');
  assert.equal(initial.files, 2);
  assert.match(initial.sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(snapshot(s.root), before);
  assert.doesNotMatch(JSON.stringify(initial), /PRIVATE-|manufactured/);

  // Equal sizes and restored mtimes cannot conceal changed source bytes.
  fs.writeFileSync(moduleFile, '# manufactured module B\n');
  fs.utimesSync(moduleFile, originalTime, originalTime);
  assert.notEqual(sourceTreeState(directory).sha256, initial.sha256);
  fs.writeFileSync(moduleFile, '# manufactured module A\n');
  assert.deepEqual(sourceTreeState(directory), initial);

  fs.renameSync(moduleFile, path.join(nested, 'renamed.py'));
  assert.notEqual(sourceTreeState(directory).sha256, initial.sha256);
  fs.renameSync(path.join(nested, 'renamed.py'), moduleFile);
  fs.unlinkSync(firstFile);
  assert.equal(sourceTreeState(directory).files, 1);
  fs.writeFileSync(firstFile, '# manufactured entry\n');
  assert.deepEqual(sourceTreeState(directory), initial);
  fs.writeFileSync(path.join(nested, 'new.py'), '# added module\n');
  assert.equal(sourceTreeState(directory).files, 3);

  assert.deepEqual(sourceTreeState(path.join(s.root, 'absent')), { status: 'missing' });
  assert.deepEqual(sourceTreeState(firstFile), { status: 'unreadable' });
  fs.symlinkSync(nested, path.join(directory, 'linked-package'), 'dir');
  assert.deepEqual(sourceTreeState(directory), { status: 'unreadable' });
});

test('unreadable source trees refuse screening rather than accepting unattributed counts', (t) => {
  const s = sandbox(t);
  const imported = path.resolve(__dirname, '../src/redirect_shadow.py');
  const originalRead = fs.readFileSync;
  t.mock.method(fs, 'readFileSync', function(file, ...args) {
    if (file === imported) throw Object.assign(new Error('PRIVATE-error'), { code: 'EACCES' });
    return originalRead.call(this, file, ...args);
  });
  const result = observe({ ...s, run() { assert.fail('source tree cannot be captured'); } });
  assert.equal(result.status, 'unavailable');
  assert.equal(result.candidates, null);
  assert.equal(result.blocking, null);
  assert.equal(result.drainable, null);
  assert.deepEqual(result.artifacts.screen_sources, { status: 'unreadable' });
  assert.deepEqual(result.limitations, ['screen_sources_unavailable']);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
});

test('report directory refreshes invalidate capture for default and overridden paths', (t) => {
  const s = sandbox(t);
  for (const overridden of [false, true]) {
    const reportDir = overridden ? path.join(s.root, 'PRIVATE-reports')
      : path.join(s.stateDir, 'keepalive-supervisor-stage2');
    fs.mkdirSync(reportDir);
    const file = path.join(reportDir, 'PRIVATE-target.keepalive-supervisor-report.json');
    const env = { ...s.env };
    if (overridden) env.ORCH_KEEPALIVE_STAGE2_REPORT_DIR = reportDir;
    const base = control({ ...s, env });
    const stable = observe(base);
    assert.equal(stable.status, 'observed');

    const changes = [
      () => fs.writeFileSync(file, '{}'),
      () => fs.appendFileSync(file, '\n'),
      () => fs.utimesSync(file, new Date(1000), new Date(1000)),
      () => fs.unlinkSync(file),
      () => fs.rmdirSync(reportDir),
      () => fs.mkdirSync(reportDir),
    ];
    for (const change of changes) {
      const result = observe({ ...base, run(...args) {
        assert.equal(args[1][args[1].indexOf('--report-dir') + 1], reportDir);
        const execution = base.run(...args);
        change();
        return execution;
      } });
      assert.equal(result.status, 'unavailable');
      assert.deepEqual(result.changed_artifacts, ['report_dir']);
      assert.deepEqual(result.limitations, ['artifacts_changed_during_capture']);
      assert.equal(result.blocking, null);
      assert.equal(result.drainable, null);
      assert.equal(result.candidates, null);
      assert.equal(result.operational_acceptance, 'not_established');
      assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
    }
    assert.equal(stable.artifacts.report_dir.status, 'read');
    assert.match(stable.artifacts.report_dir.sha256, /^[a-f0-9]{64}$/);
    fs.rmdirSync(reportDir);
  }
});

test('report directory fingerprints are private, deterministic and read-only', (t) => {
  const s = sandbox(t);
  const reportDir = path.join(s.stateDir, 'keepalive-supervisor-stage2');
  assert.deepEqual(observe(control(s)).artifacts.report_dir, { status: 'missing' });
  fs.mkdirSync(reportDir);
  const files = ['PRIVATE-b.keepalive-supervisor-report.json',
    'PRIVATE-a.keepalive-supervisor-report.json'];
  const timestamp = new Date(1000);
  const save = (names) => {
    for (const name of names) {
      const file = path.join(reportDir, name);
      fs.writeFileSync(file, '{}');
      fs.utimesSync(file, timestamp, timestamp);
    }
  };
  save(files);
  const before = snapshot(s.root);
  const first = observe(control(s));
  assert.equal(first.status, 'observed');
  assert.deepEqual(snapshot(s.root), before);
  for (const name of files) fs.unlinkSync(path.join(reportDir, name));
  save([...files].reverse());
  const second = observe(control(s));
  assert.deepEqual(second.artifacts.report_dir, first.artifacts.report_dir);
  assert.doesNotMatch(JSON.stringify(second), /PRIVATE-/);

  // An unusable path is distinct from a missing directory or a read empty one.
  fs.writeFileSync(path.join(s.root, 'PRIVATE-file'), '{}');
  const invalid = observe(control({ ...s, env: { ...s.env,
    ORCH_KEEPALIVE_STAGE2_REPORT_DIR: path.join(s.root, 'PRIVATE-file') } }));
  assert.deepEqual(invalid.artifacts.report_dir, { status: 'unreadable' });
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

// Explicitly manufactured snapshots exercise extraction only. Their rolling
// demand and acted-on counts cannot establish a production population or apply.
function reviewControl(s, row = {}) {
  const file = path.join(s.stateDir, 'switch-review.json');
  const report = { generated_at: 1793664060, value_chain: {
    generated_at: 1793664000, window_days: 7,
    rows: [{ capability: 'redirect-apply-bootstrap', situation_count: 18,
      offered: 0, invocation_count: 3, usable: 2, acted_on: 0, outcome: 0,
      outcome_success: 0, first_break: 'not_acted_on', ...row }],
    errors: ['PRIVATE-error'],
  }, unrelated: 'PRIVATE-target' };
  const save = () => fs.writeFileSync(file, JSON.stringify(report));
  save();
  return { file, report, save };
}

test('retains the recorded redirect chain and its time window independently of current candidates', (t) => {
  const s = sandbox(t);
  const review = reviewControl(s);
  const before = snapshot(s.root);
  const result = observe(control(s));
  assert.deepEqual(result.recorded_switch_review, {
    status: 'recorded', recorded_at: 1793664060, monitor_generated_at: 1793664000,
    window_days: 7, counts: { situation_count: 18, offered: 0, invocation_count: 3,
      usable: 2, acted_on: 0, outcome: 0, outcome_success: 0 }, first_break: 'not_acted_on',
  });
  assert.equal(result.candidates, 0, 'demand 18 must not become eligible candidates');
  assert.equal(result.drainable, 0);
  assert.equal(result.operational_acceptance, 'not_established');
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
  assert.deepEqual(snapshot(s.root), before);

  review.report.value_chain.rows[0].acted_on = 1;
  review.report.value_chain.rows[0].first_break = 'no_outcome';
  review.save();
  fs.unlinkSync(s.corpus);
  const absent = observe({ ...s, run() { assert.fail('owner corpus missing'); } });
  assert.equal(absent.recorded_switch_review.counts.acted_on, 1);
  assert.equal(absent.candidates, null);
  assert.equal(absent.operational_acceptance, 'not_established', 'rolling activity is not individual attribution');
});

test('missing, disabled, ambiguous and malformed review rows never become zero acted-on counts', (t) => {
  const s = sandbox(t);
  assert.deepEqual(observe(control(s)).recorded_switch_review, { status: 'missing' });
  const review = reviewControl(s, { acted_on: null });
  delete review.report.value_chain.rows[0].outcome;
  review.save();
  let result = observe(control(s));
  assert.equal(result.recorded_switch_review.counts.acted_on, null);
  assert.equal(result.recorded_switch_review.counts.outcome, null);
  const original = structuredClone(review.report);
  const cases = [
    ['monitor_disabled', (r) => { r.value_chain.disabled = true; }],
    ['row_unavailable', (r) => { r.value_chain.rows = [{ capability: 'other', acted_on: 100 }]; }],
    ['invalid', (r) => { r.generated_at = 'PRIVATE-date'; }],
    ['invalid', (r) => { delete r.value_chain.generated_at; }],
    ['invalid', (r) => { r.value_chain.window_days = 0; }],
    ['invalid', (r) => { r.value_chain.rows.push(r.value_chain.rows[0]); }],
    ...[-1, true, '1', 1.5, Number.MAX_SAFE_INTEGER + 1].map((value) =>
      ['invalid', (r) => { r.value_chain.rows[0].acted_on = value; }]),
    ['invalid', (r) => { r.value_chain.rows[0].first_break = 'PRIVATE-target'; }],
  ];
  for (const [status, mutate] of cases) {
    const report = structuredClone(original);
    mutate(report);
    fs.writeFileSync(review.file, JSON.stringify(report));
    result = observe(control(s));
    assert.equal(result.recorded_switch_review.status, status);
    assert.equal(result.recorded_switch_review.counts, undefined);
    assert.equal(result.candidates, 0, 'an invalid old review must not replace the current screen');
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE-/);
  }
  fs.writeFileSync(review.file, 'PRIVATE-malformed');
  assert.deepEqual(observe(control(s)).recorded_switch_review, { status: 'invalid' });
});

test('a review rewritten during screening cannot retain verified chain counts', (t) => {
  const s = sandbox(t);
  const review = reviewControl(s);
  const base = control(s);
  const result = observe({ ...base, run(...args) {
    const execution = base.run(...args);
    review.report.value_chain.rows[0].acted_on = 1;
    review.save();
    return execution;
  } });
  assert.deepEqual(result.changed_artifacts, ['switch_review']);
  assert.deepEqual(result.recorded_switch_review, { status: 'changed_during_capture' });
  assert.equal(result.candidates, null);
  assert.equal(result.drainable, null);
  assert.equal(result.operational_acceptance, 'not_established');
});
