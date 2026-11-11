'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { execute } = require('../scripts/run_adjudicator_acceptance');
const { complete, expected, runWitnesses, snapshot } = require('../scripts/run_provision_native_witnesses');

const selectedPython = process.env.PYTHON;
assert.ok(path.isAbsolute(selectedPython || ''), 'Set PYTHON to an absolute repository or CI interpreter');
const pythonCheck = execute(selectedPython, ['-I', '-c',
  'import json, xml.etree.ElementTree; print(json.dumps("stdlib available"))'], { cwd: os.tmpdir() });
assert.equal(pythonCheck.exit_code, 0, pythonCheck.error_code ?? pythonCheck.stderr.toString());
assert.equal(pythonCheck.stdout.toString().trim(), '"stdlib available"');

const success = () => expected.map((name) => ({ name,
  classname: 'tests.test_provision_reclaim', unsuccessful: false }));

function fixture(t, scenario = {}) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'provision-receipt-test-'));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, 'checkout');
  const mirror = path.join(base, 'mirror');
  for (const directory of [root, mirror]) fs.mkdirSync(directory);
  for (const directory of ['src', 'tests', 'scripts']) fs.mkdirSync(path.join(root, directory));
  for (const [file, contents] of Object.entries({ 'src/provision.py': '# fixture\n',
    'tests/test_provision_reclaim.py': '# fixture\n', 'tests/conftest.py': '# fixture\n',
    'pyproject.toml': '', '.verify-floor.json': '{}\n' })) {
    fs.writeFileSync(path.join(root, file), contents);
  }
  for (const name of ['run_provision_native_witnesses.js', 'run_adjudicator_acceptance.js']) {
    fs.copyFileSync(path.join(__dirname, '../scripts', name), path.join(root, 'scripts', name));
  }
  const git = (...args) => {
    const result = execute('git', ['-C', root, ...args], { cwd: root });
    assert.equal(result.exit_code, 0, result.stderr.toString());
    return result.stdout.toString().trim();
  };
  git('init', '--initial-branch=main');
  git('add', '.');
  git('-c', 'user.name=Receipt Test', '-c', 'user.email=test@example.com',
    '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture');
  git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  for (const name of ['tests', 'scripts', 'pyproject.toml', '.verify-floor.json']) {
    fs.cpSync(path.join(root, name), path.join(mirror, name), { recursive: true });
  }
  fs.copyFileSync(path.join(root, 'src/provision.py'), path.join(mirror, 'provision.py'));
  // Pytest is deliberately doubled. The XML reader and import probe use the same interpreter.
  const python = path.join(base, 'python-fixture');
  fs.writeFileSync(python, `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const args = process.argv.slice(2);
const scenario = ${JSON.stringify(scenario)};
if (args[0] === '-I') {
  if (scenario.readerExit) {
    console.log('reader partial output');
    console.error('reader failed before verdict');
    process.exit(scenario.readerExit);
  }
  if (scenario.readerInvalidJson) { console.log('not JSON'); process.exit(0); }
  const result = cp.spawnSync(${JSON.stringify(selectedPython)}, args, { stdio: 'inherit' });
  process.exit(result.status ?? 1);
}
fs.writeFileSync(${JSON.stringify(path.join(base, 'command.json'))}, JSON.stringify(args));
fs.appendFileSync(${JSON.stringify(path.join(base, 'commands.ndjson'))}, JSON.stringify({ args,
  native: process.env.ORCH_PROVISION_NATIVE_ACCEPTANCE ?? null,
  addopts: process.env.PYTEST_ADDOPTS ?? null, plugins: process.env.PYTEST_PLUGINS ?? null }) + '\\n');
if (scenario.checkPrivateTree) {
  if (fs.realpathSync(process.cwd()) === ${JSON.stringify(fs.realpathSync(mirror))}) process.exit(93);
  for (const runtime of ['.venv', '.ruff_cache', '.pytest_cache', 'docs/reports', 'campaigns']) {
    if (fs.existsSync(path.join(process.cwd(), runtime))) process.exit(94);
  }
}
if (scenario.importProbe) {
  const prefix = args.slice(0, args.indexOf('-m'));
  const probe = cp.spawnSync(${JSON.stringify(selectedPython)},
    [...prefix, '-c', 'import json'], { stdio: 'inherit' });
  if (probe.status !== 0) process.exit(92);
}
if (scenario.missing) { console.error('No module named pytest'); process.exit(1); }
const cases = ${JSON.stringify(success())};
const fullSuite = args.includes('tests/test_provision_reclaim.py');
const mode = process.env.ORCH_PROVISION_NATIVE_ACCEPTANCE === '1' ? 'native' : 'default';
if (fullSuite) cases.push({ name: 'test_other_safety_case',
  classname: 'tests.test_provision_reclaim', unsuccessful: false });
if (args.includes('--collect-only')) {
  if (scenario.collectionWrongFile) console.log('tests/test_unrelated.py::test_other_safety_case');
  else if (!scenario.emptyCollection) for (const item of cases) {
    console.log('tests/test_provision_reclaim.py::' + item.name);
  }
  if (scenario.duplicateCollection) console.log('tests/test_provision_reclaim.py::' + cases[0].name);
  process.exit(scenario.collectionExit ?? 0);
}
if (fullSuite && scenario.suiteFailure === mode) cases[0].unsuccessful = true;
if (fullSuite && scenario.suiteOmission) cases.pop();
if (fullSuite && scenario.suiteExtra) cases.push({ ...cases[0], name: 'test_uncollected' });
if (fullSuite && scenario.suiteWrongClass) cases[0].classname = 'tests.unrelated';
if (scenario.skip) cases[0].unsuccessful = true;
if (scenario.omit) cases.pop();
if (scenario.duplicate) cases.push(cases[0]);
const xml = args.find((arg) => arg.startsWith('--junitxml=')).slice('--junitxml='.length);
if (!scenario.noXml) fs.writeFileSync(xml, scenario.malformed ? '<broken' :
  '<testsuite>' + cases.map((item) => '<testcase name="' + item.name + '" classname="' +
    item.classname + '">' + (item.unsuccessful ? '<' + (scenario.failureTag || 'skipped') + '/>' : '') + '</testcase>').join('') + '</testsuite>');
if (scenario.mutate) fs.appendFileSync(${JSON.stringify(path.join(mirror, 'provision.py'))}, '# drift');
if (scenario.extra) fs.writeFileSync(${JSON.stringify(path.join(mirror, 'extra.py'))}, '# drift');
console.log('fixture output');
process.exit(scenario.exit ?? 0);
`, { mode: 0o700 });
  return { root, mirror, python, revision: git('rev-parse', 'HEAD'),
    output: path.join(base, 'receipts') };
}

test('binds named native verdicts, commit, mirror manifest, commands and unchanged floor', (t) => {
  const options = fixture(t);
  const receipt = runWitnesses(options);
  assert.equal(receipt.native_witnesses_passed, true);
  assert.equal(receipt.default_native_suites_passed, false);
  assert.equal(receipt.full_suites_requested, false);
  assert.deepEqual(receipt.suites, []);
  assert.equal(receipt.deployment_acceptance_verified, false);
  assert.equal(receipt.floor_matches_origin_main, true);
  assert.equal(receipt.source_unchanged, true);
  assert.equal(receipt.mirror_manifest_after_sha256, receipt.mirror_manifest_sha256);
  assert.equal(receipt.revision, options.revision);
  assert.equal(receipt.mirror, fs.realpathSync(options.mirror));
  assert.deepEqual(receipt.checks[0].cases, success());
  const reader = receipt.checks[0].junit_reader;
  assert.equal(reader.exit_code, 0);
  assert.equal(reader.json_decoded, true);
  assert.deepEqual(reader.command.slice(0, 3), [options.python, '-I', '-c']);
  const readerLog = fs.readFileSync(path.join(options.output, reader.log));
  assert.equal(reader.log_sha256,
    require('node:crypto').createHash('sha256').update(readerLog).digest('hex'));
  assert.equal(fs.statSync(path.join(options.output, reader.log)).mode & 0o777, 0o600);
  assert.ok(receipt.checks[0].command.includes('not slow'));
  assert.ok(receipt.checks[0].command.includes('-E'));
  assert.ok(receipt.checks[0].command.includes('-rP'));
  assert.deepEqual(receipt.checks[0].command.slice(1),
    JSON.parse(fs.readFileSync(path.join(path.dirname(options.root), 'command.json'))));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))), receipt);
  assert.equal(fs.statSync(path.join(options.output, 'receipt.json')).mode & 0o777, 0o600);
  assert.throws(() => runWitnesses(options), /EEXIST/);
});

test('full suites bind independent default/native collections to all executed JUnit verdicts', (t) => {
  const options = fixture(t);
  const inherited = ['ORCH_PROVISION_NATIVE_ACCEPTANCE', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'];
  const old = inherited.map((name) => process.env[name]);
  t.after(() => inherited.forEach((name, i) => {
    if (old[i] === undefined) delete process.env[name];
    else process.env[name] = old[i];
  }));
  process.env.ORCH_PROVISION_NATIVE_ACCEPTANCE = '1';
  process.env.PYTEST_ADDOPTS = '--collect-only';
  process.env.PYTEST_PLUGINS = 'unbound_plugin';
  const receipt = runWitnesses({ ...options, fullSuites: true });
  assert.equal(receipt.native_witnesses_passed, true);
  assert.equal(receipt.default_native_suites_passed, true);
  assert.equal(receipt.deployment_acceptance_verified, false);
  assert.deepEqual(receipt.suites.map((item) => item.mode), ['default', 'native']);
  for (const item of receipt.suites) {
    assert.equal(item.passed, true);
    assert.equal(item.collection.nodes.length, expected.length + 1);
    assert.equal(item.execution.cases.length, expected.length + 1);
    assert.ok(item.collection.command.includes('--collect-only'));
    assert.ok(item.execution.command.includes('tests/test_provision_reclaim.py'));
    assert.ok(!item.execution.command.includes('--collect-only'));
    for (const check of [item.collection, item.execution]) {
      assert.ok(check.command.includes('not slow'));
      assert.ok(check.command.includes('-E'));
      assert.match(check.log_sha256, /^[a-f0-9]{64}$/);
    }
    assert.match(item.execution.junit_sha256, /^[a-f0-9]{64}$/);
    assert.ok(fs.existsSync(path.join(options.output, `${item.mode}-collection.log`)));
    assert.ok(fs.existsSync(path.join(options.output, `${item.mode}-suite.log`)));
  }
  const commands = fs.readFileSync(path.join(path.dirname(options.root), 'commands.ndjson'), 'utf8')
    .trim().split('\n').map((line) => JSON.parse(line));
  assert.deepEqual(commands.map((item) => item.native), [null, null, '1', '1', '1']);
  assert.ok(commands.every((item) => item.addopts === null && item.plugins === null));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))), receipt);
});

for (const scenario of [{ suiteFailure: 'default' }, { suiteFailure: 'native' },
  { suiteFailure: 'native', failureTag: 'failure' },
  { suiteFailure: 'native', failureTag: 'error' }, { suiteOmission: true },
  { suiteExtra: true }, { suiteWrongClass: true }, { emptyCollection: true },
  { duplicateCollection: true }, { collectionExit: 1 }, { collectionWrongFile: true }]) {
  test(`named witnesses cannot mask an incomplete full suite: ${JSON.stringify(scenario)}`, (t) => {
    const receipt = runWitnesses({ ...fixture(t, scenario), fullSuites: true });
    assert.equal(receipt.native_witnesses_passed, true);
    assert.equal(receipt.default_native_suites_passed, false);
    assert.equal(receipt.deployment_acceptance_verified, false);
    assert.ok(receipt.suites.some((item) => !item.passed));
  });
}

test('changed inputs invalidate both full suites as well as named witnesses', (t) => {
  const receipt = runWitnesses({ ...fixture(t, { mutate: true }), fullSuites: true });
  assert.equal(receipt.source_unchanged, false);
  assert.equal(receipt.default_native_suites_passed, false);
  assert.equal(receipt.native_witnesses_passed, false);
});

test('full-suites CLI exits nonzero when the witnesses pass but a full suite fails', (t) => {
  for (const scenario of [{}, { suiteFailure: 'default' }]) {
    const options = fixture(t, scenario);
    const result = execute(process.execPath,
      [path.join(options.root, 'scripts/run_provision_native_witnesses.js'),
        '--python', options.python, '--revision', options.revision, '--mirror', options.mirror,
        '--output', options.output, '--full-suites'], { cwd: options.root });
    assert.equal(result.error_code, null);
    assert.equal(result.signal, null);
    assert.equal(result.exit_code, scenario.suiteFailure ? 1 : 0, result.stderr.toString());
    const summary = JSON.parse(result.stdout.toString());
    assert.equal(summary.native_witnesses_passed, true);
    assert.equal(summary.default_native_suites_passed, !scenario.suiteFailure);
    assert.equal(summary.deployment_acceptance_verified, false);
  }
});

for (const scenario of [{ missing: true }, { noXml: true }, { malformed: true },
  { skip: true }, { omit: true }, { duplicate: true }, { exit: 1 }, { mutate: true }, { extra: true }]) {
  test(`cannot credit an incomplete or changed run: ${JSON.stringify(scenario)}`, (t) => {
    const options = fixture(t, scenario);
    const receipt = runWitnesses(options);
    assert.equal(receipt.native_witnesses_passed, false);
    assert.equal(receipt.deployment_acceptance_verified, false);
    assert.ok(fs.existsSync(path.join(options.output, 'native.log')));
    if (scenario.mutate || scenario.extra) assert.equal(receipt.source_unchanged, false);
    if (scenario.missing) assert.match(
      fs.readFileSync(path.join(options.output, 'native.log'), 'utf8'), /No module named pytest/);
  });
}

test('refuses drift, missing inputs, symlinks and a floor unequal to origin/main before testing', (t) => {
  for (const file of ['provision.py', 'tests/conftest.py', '.verify-floor.json']) {
    const options = fixture(t);
    fs.appendFileSync(path.join(options.mirror, file), '# drift');
    assert.throws(() => runWitnesses(options), /Mirror differs/);
    assert.equal(fs.existsSync(options.output), false);
  }
  const options = fixture(t);
  fs.symlinkSync(options.root, path.join(options.mirror, 'outside'));
  assert.throws(() => runWitnesses(options), /symlink/);
  fs.unlinkSync(path.join(options.mirror, 'outside'));
  const result = execute('git', ['-C', options.root, 'update-ref', '-d', 'refs/remotes/origin/main'],
    { cwd: options.root });
  assert.equal(result.exit_code, 0);
  assert.throws(() => snapshot(options.root, options.mirror, options.revision), /Cannot bind/);
});

test('unrelated, duplicate, skipped or vacuous verdicts do not prove the witnesses', () => {
  assert.equal(complete(success()), true);
  for (const cases of [null, [], [...success(), success()[0]],
    success().map((item) => ({ ...item, classname: 'tests.unrelated' })),
    success().map((item) => ({ ...item, unsuccessful: true }))]) {
    assert.equal(complete(cases), false);
  }
});

test('refuses extra importable modules and test inputs before executing pytest', (t) => {
  for (const file of ['conftest.py', 'injected.pyc', 'tests/extra.txt', 'pytest.ini',
    '.pytest.ini', 'pytest.toml', '.pytest.toml', 'tox.ini', 'setup.cfg',
    'shadow/pytest/__init__.py', 'shadow/pytest/__main__.py', 'shadow/module.pyc', 'module.so',
    'shadow/setup.pth', 'tests/.ruff_cache/conftest.py', '__pycache__/conftest.py']) {
    const options = fixture(t);
    fs.mkdirSync(path.dirname(path.join(options.mirror, file)), { recursive: true });
    fs.writeFileSync(path.join(options.mirror, file), 'unbound input');
    assert.throws(() => runWitnesses(options), /Uncommitted mirror input/);
    assert.equal(fs.existsSync(options.output), false);
  }
});

test('copies only bound inputs from a published layout with retained runtime links', (t) => {
  const options = fixture(t, { checkPrivateTree: true });
  const retained = path.join(path.dirname(options.mirror), `orchestrator-mirror.retired-${'1'.repeat(32)}`);
  for (const file of ['.venv', '.ruff_cache', '.pytest_cache', 'docs/reports', 'campaigns']) {
    const backing = path.join(retained, file);
    fs.mkdirSync(backing, { recursive: true });
    fs.writeFileSync(path.join(backing, 'conftest.py'), 'raise RuntimeError("runtime input")');
    const link = path.join(options.mirror, file);
    fs.mkdirSync(path.dirname(link), { recursive: true });
    fs.symlinkSync(backing, link);
  }
  const inside = path.join(options.mirror, '.venv', 'python');
  fs.symlinkSync(options.python, path.join(retained, '.venv', 'python'));
  assert.throws(() => runWitnesses({ ...options, python: inside }), /interpreter outside the mirror/);
  assert.equal(runWitnesses({ ...options, fullSuites: true }).default_native_suites_passed, true);
});

test('rejects symlinks in code, tests and configuration, including cache lookalikes', (t) => {
  for (const file of ['provision.py', 'tests/conftest.py', 'pyproject.toml',
    'tests/.ruff_cache', '__pycache__/provision.cpython-312.pyc', 'config', 'campaigns']) {
    const options = fixture(t);
    const link = path.join(options.mirror, file);
    fs.rmSync(link, { force: true });
    fs.mkdirSync(path.dirname(link), { recursive: true });
    fs.symlinkSync(options.root, link);
    assert.throws(() => runWitnesses(options), /Mirror contains a symlink/);
    assert.equal(fs.existsSync(options.output), false);
  }
});

test('requires explicit interpreter, revision and receipt location outside the mirror', (t) => {
  const options = fixture(t);
  for (const override of [{ python: 'python3' }, { revision: 'main' }, { timeout: 0 },
    { output: path.join(options.mirror, 'receipt') }]) {
    assert.throws(() => runWitnesses({ ...options, ...override }));
  }
});

test('inherited PYTHONPATH cannot substitute an imported module', (t) => {
  const options = fixture(t, { importProbe: true });
  const poison = path.join(path.dirname(options.root), 'poison');
  // Use stdlib so the isolation regression needs no real pytest installation.
  // Pytest execution elsewhere in this fixture is deliberately simulated.
  fs.mkdirSync(path.join(poison, 'json'), { recursive: true });
  fs.writeFileSync(path.join(poison, 'json', '__init__.py'),
    'raise RuntimeError("inherited module selected")\n');
  const old = process.env.PYTHONPATH;
  t.after(() => {
    if (old === undefined) delete process.env.PYTHONPATH;
    else process.env.PYTHONPATH = old;
  });
  process.env.PYTHONPATH = poison;
  const receipt = runWitnesses(options);
  assert.equal(receipt.native_witnesses_passed, true);
});

for (const scenario of [{ readerExit: 37 }, { readerInvalidJson: true }, { malformed: true }]) {
  test(`retains JUnit reader failure diagnostics: ${JSON.stringify(scenario)}`, (t) => {
    const options = fixture(t, scenario);
    const receipt = runWitnesses({ ...options, fullSuites: true });
    assert.equal(receipt.native_witnesses_passed, false);
    assert.equal(receipt.default_native_suites_passed, false);
    for (const check of [...receipt.suites.map((item) => item.execution), ...receipt.checks]) {
      assert.equal(check.exit_code, 0); // The reader, rather than pytest, failed.
      assert.equal(check.passed, false);
      assert.equal(check.cases, null);
      const reader = check.junit_reader;
      assert.equal(reader.json_decoded, false);
      assert.equal(reader.error_code, null);
      assert.equal(reader.signal, null);
      assert.equal(reader.exit_code, scenario.readerExit ?? (scenario.malformed ? 1 : 0));
      const log = fs.readFileSync(path.join(options.output, reader.log));
      assert.equal(reader.log_sha256,
        require('node:crypto').createHash('sha256').update(log).digest('hex'));
      assert.match(log.toString(), scenario.readerExit ? /reader partial output\nreader failed/
        : scenario.readerInvalidJson ? /not JSON/ : /ParseError/);
    }
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))), receipt);
  });
}

test('a missing JUnit file retains the pytest failure without inventing a reader command', (t) => {
  const options = fixture(t, { missing: true });
  const receipt = runWitnesses(options);
  assert.equal(receipt.native_witnesses_passed, false);
  assert.equal(receipt.checks[0].exit_code, 1);
  assert.equal(receipt.checks[0].junit_reader, null);
  assert.equal(fs.existsSync(path.join(options.output, 'native-junit-reader.log')), false);
});
