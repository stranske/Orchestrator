'use strict';

// Orchestration tests use synthetic JUnit, never operational acceptance proof.
// XML parsing and import-origin checks still use the real selected interpreter.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { runAcceptance, validatePhase, execute, names, controls } =
  require('../scripts/run_adjudicator_acceptance');

const root = path.resolve(__dirname, '..');
const discovery = execute(process.env.PYTHON || 'python3', ['-I', '-c',
  'import sys; print(sys.executable)'], { cwd: root });
assert.equal(discovery.status, 0, discovery.stderr);
const python = discovery.stdout.trim();
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const originalBytes = () => controls.map((c) => fs.readFileSync(path.join(root, c.file)));
const passed = { status: 0, signal: null, error: null, stdout: '', stderr: '' };

function nativeResult(index, red = false) {
  const testNames = index === 2 ? [0, 1, 2, 3, 4].map((n) =>
    `${names[2]}[context${n}-${n === 4 ? 'True' : 'False'}]`) : [names[index]];
  const control = red ? controls.find((c) => c.index === index) : undefined;
  const failures = control?.failures || [];
  return {
    suites: [{ tests: String(testNames.length), failures: String(failures.length),
      errors: '0', skipped: '0' }],
    cases: testNames.map((name) => ({ name, classname: 'tests.test_adjudicator_retro',
      outcomes: failures.includes(name) ? ['failure'] : [],
      failure: failures.includes(name) ? `AssertionError: ${control.assertion}` : '' })),
  };
}

function toXml(result) {
  const attrs = Object.entries(result.suites[0]).map(([k, v]) => `${k}="${v}"`).join(' ');
  return `<testsuites><testsuite ${attrs}>${result.cases.map((c) =>
    `<testcase classname="${c.classname}" name="${c.name}">${c.outcomes.map((tag) =>
      `<${tag}><![CDATA[${c.failure}]]></${tag}>`).join('')}</testcase>`).join('')}` +
    '</testsuite></testsuites>';
}

function world(t, alter = () => {}, selection = python) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-acceptance-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const output = path.join(dir, 'receipt');
  const before = originalBytes();
  const observations = [];
  let overlay;
  function executeTest(command, args, options) {
    assert.equal(command, selection);
    assert.equal(options.env.ORCH_FEEDBACK_DB.startsWith(os.tmpdir()), true);
    assert.equal(options.env.PYTHONDONTWRITEBYTECODE, '1');
    overlay = options.env.PYTHONPATH.split(path.delimiter)[0];
    if (args[0] === '-c' && args[1].startsWith('import pytest;')) {
      const response = { ...passed, stdout: 'synthetic prerequisite for orchestration tests\n' };
      alter({ kind: 'prerequisite', response });
      return response;
    }
    if (args[0] !== '-m' || args[1] !== 'pytest') return execute(command, args, options);
    assert.deepEqual(args.slice(3, 10), ['-q', '-m', 'not slow', '-o', 'addopts=', '-o', 'pythonpath=']);
    const node = args[2];
    const index = names.findIndex((name) => node === `tests/test_adjudicator_retro.py::${name}`);
    assert.ok(index >= 0, 'runner must call an exact contracted pytest node');
    const xmlPath = args.find((arg) => arg.startsWith('--junitxml=')).slice(11);
    const label = path.basename(xmlPath, '.xml');
    const red = label.endsWith('-red');
    const overlayBytes = controls.map((control) =>
      fs.readFileSync(path.join(overlay, path.basename(control.file))));
    for (const [i, control] of controls.entries()) {
      assert.deepEqual(overlayBytes[i], red && control.index === index
        ? Buffer.from(before[i].toString().replace(control.before, control.after)) : before[i]);
    }
    assert.deepEqual(originalBytes(), before, 'original Python source was mutated');
    const xml = nativeResult(index, red);
    const response = { ...passed, status: red ? 1 : 0 };
    const change = { kind: 'test', label, xml, response, omitXml: false };
    alter(change);
    if (!change.omitXml) fs.writeFileSync(xmlPath, toXml(xml));
    observations.push({ label, node, overlayBytes });
    return response;
  }
  return { output, observations, run: () => runAcceptance({ root, python: selection, output, executeTest }),
    overlay: () => overlay, before };
}

test('runner contract matches the retained original named nodes and mutation bytes', () => {
  const retained = JSON.parse(fs.readFileSync(path.join(root,
    'docs/verification/issue-428-named-acceptance-20261007/validation.json')));
  for (const [index, control] of controls.entries()) {
    const expected = retained.required_mutations[index];
    assert.equal(control.name, expected.mutation);
    assert.equal(control.file, expected.source);
    assert.equal(`tests/test_adjudicator_retro.py::${names[control.index]}`, expected.node);
    assert.equal(control.before, expected.before);
    assert.equal(control.after, expected.after);
  }
  const source = JSON.parse(fs.readFileSync(path.join(root,
    'docs/verification/issue-1104-cohort-recovery/source-428.json')));
  assert.ok(JSON.stringify(source).includes(names[0]));
});

test('runner orders exact nodes, verifies real overlay imports, restores bytes and retains logs', (t) => {
  const w = world(t);
  const receipt = w.run();
  assert.equal(receipt.status, 'passed', receipt.evidence_gaps.join('\n'));
  assert.equal(receipt.named_safety_acceptance_complete, true);
  assert.equal(receipt.operational_acceptance_complete, false);
  assert.equal(receipt.repository_inputs_unchanged, true);
  assert.deepEqual(w.observations.map((p) => p.label), [
    'baseline-1', 'baseline-2', 'baseline-3', 'outcomes-red', 'outcomes-restored',
    'unconditional-offer-red', 'unconditional-offer-restored', 'final-1', 'final-2', 'final-3',
  ]);
  assert.equal(receipt.phases.filter((p) => p.verified).length, 10);
  for (const [i, mutation] of receipt.mutations.entries()) {
    assert.equal(mutation.original_sha256, sha(w.before[i]));
    assert.notEqual(mutation.mutated_sha256, mutation.original_sha256);
    assert.equal(mutation.restored_sha256, mutation.original_sha256);
  }
  for (const phase of receipt.phases) {
    for (const stream of ['stdout', 'stderr']) {
      assert.equal(sha(fs.readFileSync(path.join(w.output, phase[stream]))), phase[`${stream}_sha256`]);
    }
    if (phase.verified) assert.equal(sha(fs.readFileSync(path.join(w.output, `${phase.label}.xml`))),
      phase.junit_sha256);
  }
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(w.output, 'receipt.json'))), receipt);
  assert.equal(fs.existsSync(w.overlay()), false, 'temporary mutation files must be removed');
  assert.throws(() => w.run(), /EEXIST/, 'existing receipts cannot be overwritten');
});

test('unavailable pytest persists a specific gap and launches no tests or mutations', (t) => {
  const w = world(t, (p) => {
    if (p.kind === 'prerequisite') Object.assign(p.response,
      { status: 1, stdout: '', stderr: 'ModuleNotFoundError: No module named pytest\n' });
  });
  const receipt = w.run();
  assert.equal(receipt.status, 'blocked');
  assert.deepEqual(receipt.evidence_gaps, ['selected_interpreter_pytest_unavailable']);
  assert.equal(receipt.named_safety_acceptance_complete, false);
  assert.deepEqual(receipt.mutations, []);
  assert.deepEqual(w.observations, []);
  assert.equal(receipt.repository_inputs_unchanged, true);
  assert.equal(fs.existsSync(w.overlay()), false);
  assert.match(fs.readFileSync(path.join(w.output, 'pytest-prerequisite.stderr.log'), 'utf8'),
    /No module named pytest/);
});

test('a baseline skip stops before any deliberate mutation', (t) => {
  const w = world(t, (p) => {
    if (p.label === 'baseline-1') {
      p.xml.suites[0].skipped = '1';
      p.xml.cases[0].outcomes = ['skipped'];
    }
  });
  const receipt = w.run();
  assert.equal(receipt.status, 'failed');
  assert.equal(receipt.named_safety_acceptance_complete, false);
  assert.deepEqual(receipt.mutations, []);
  assert.deepEqual(w.observations.map((p) => p.label), ['baseline-1']);
});

test('selected interpreter symlinks retain their environment instead of invoking the base binary', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-selected-python-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const selection = path.join(dir, 'selected-python');
  fs.symlinkSync(python, selection);
  const w = world(t, (p) => {
    if (p.kind === 'prerequisite') p.response.status = 1;
  }, selection);
  const receipt = w.run();
  assert.equal(receipt.status, 'blocked');
  assert.equal(receipt.interpreter.requested, selection);
  assert.equal(receipt.interpreter.resolved, fs.realpathSync(python));
  assert.equal(receipt.interpreter.observed.executable, selection);
  assert.ok(receipt.phases.every((p) => p.argv[0] === selection));
});

for (const fault of ['wrong assertion', 'missing XML', 'timeout']) {
  test(`invalid mutation witness (${fault}) still restores and reruns the named test`, (t) => {
    const w = world(t, (p) => {
      if (p.label !== 'outcomes-red') return;
      if (fault === 'wrong assertion') p.xml.cases[0].failure = 'AssertionError: unrelated check';
      if (fault === 'missing XML') p.omitXml = true;
      if (fault === 'timeout') Object.assign(p.response,
        { status: null, signal: 'SIGTERM', error: 'spawnSync ETIMEDOUT' });
    });
    const receipt = w.run();
    assert.equal(receipt.status, 'failed');
    assert.equal(receipt.named_safety_acceptance_complete, false);
    assert.equal(receipt.operational_acceptance_complete, false);
    assert.equal(receipt.mutations.length, 1);
    assert.equal(receipt.mutations[0].restored_sha256, receipt.mutations[0].original_sha256);
    assert.equal(w.observations.at(-1).label, 'outcomes-restored');
    assert.equal(receipt.phases.at(-1).verified, true);
    assert.equal(receipt.repository_inputs_unchanged, true);
    assert.deepEqual(originalBytes(), w.before);
    assert.equal(fs.existsSync(w.overlay()), false);
  });
}

test('native receipt validation rejects skipped cases, wrong nodes, errors and unexpected exits', () => {
  const changes = [
    (p, x) => { p.status = 0; },
    (p, x) => { p.signal = 'SIGTERM'; },
    (p, x) => { x.cases[0].name = 'test_unrelated'; },
    (p, x) => { x.cases[0].classname = 'tests.test_unrelated'; },
    (p, x) => { x.cases[0].outcomes = ['error']; },
    (p, x) => { x.cases[0].outcomes = ['skipped']; },
    (p, x) => { x.suites[0].errors = '1'; },
    (p, x) => { x.cases.push({ ...x.cases[0] }); },
    (p, x) => { x.cases[0].failure = 'outcomes() == before'; },
  ];
  for (const change of changes) {
    const phase = { ...passed, status: 1 };
    const xml = nativeResult(1, true);
    change(phase, xml);
    assert.throws(() => validatePhase(phase, xml, 1, controls[0]));
  }
});

test('CLI rejects an unspecified or absent interpreter without producing a success receipt', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-invalid-cli-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  for (const selection of [[], ['--python', 'python3'], ['--python', path.join(dir, 'absent')]]) {
    const output = path.join(dir, 'receipt');
    const result = execute(process.execPath, [path.join(root, 'scripts/run_adjudicator_acceptance.js'),
      '--output', output, ...selection], { cwd: root });
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.equal(fs.existsSync(output), false);
  }
});

test('dirty or untracked acceptance sources stop before interpreter execution or receipt creation', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-dirty-input-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  function git(args) {
    const result = execute('git', args, { cwd: dir,
      env: { PATH: process.env.PATH, GIT_CONFIG_NOSYSTEM: '1' } });
    assert.equal(result.status, 0, result.stderr);
  }
  fs.mkdirSync(path.join(dir, 'src'));
  const source = path.join(dir, 'src', 'example.py');
  fs.writeFileSync(source, 'original = True\n');
  git(['init', '-q']);
  git(['add', 'src/example.py']);
  git(['-c', 'user.name=Acceptance runner test', '-c', 'user.email=test@example.invalid',
    'commit', '-qm', 'record fixture']);
  for (const fault of ['modified', 'untracked']) {
    fs.writeFileSync(source, fault === 'modified' ? 'original = False\n' : 'original = True\n');
    if (fault === 'untracked') fs.writeFileSync(path.join(dir, 'src', 'extra.py'), 'extra = True\n');
    const output = path.join(dir, 'receipt');
    assert.throws(() => runAcceptance({ root: dir, python, output,
      executeTest: () => assert.fail('dirty sources must stop before interpreter execution') }),
    /acceptance inputs must be clean/);
    assert.equal(fs.existsSync(output), false);
  }
});
