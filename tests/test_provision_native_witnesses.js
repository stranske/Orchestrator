'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { execute } = require('../scripts/run_adjudicator_acceptance');
const { complete, expected, runWitnesses, snapshot } = require('../scripts/run_provision_native_witnesses');

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
  // Pytest is deliberately doubled. Only JUnit parsing runs real stdlib Python.
  const python = path.join(base, 'python-fixture');
  fs.writeFileSync(python, `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const args = process.argv.slice(2);
const scenario = ${JSON.stringify(scenario)};
if (args[0] === '-I') {
  const result = cp.spawnSync('/usr/bin/python3', args, { stdio: 'inherit' });
  process.exit(result.status ?? 1);
}
fs.writeFileSync(${JSON.stringify(path.join(base, 'command.json'))}, JSON.stringify(args));
if (scenario.missing) { console.error('No module named pytest'); process.exit(1); }
const cases = ${JSON.stringify(success())};
if (scenario.skip) cases[0].unsuccessful = true;
if (scenario.omit) cases.pop();
if (scenario.duplicate) cases.push(cases[0]);
const xml = args.find((arg) => arg.startsWith('--junitxml=')).slice('--junitxml='.length);
if (!scenario.noXml) fs.writeFileSync(xml, scenario.malformed ? '<broken' :
  '<testsuite>' + cases.map((item) => '<testcase name="' + item.name + '" classname="' +
    item.classname + '">' + (item.unsuccessful ? '<skipped/>' : '') + '</testcase>').join('') + '</testsuite>');
if (scenario.mutate) fs.appendFileSync(path.join(process.cwd(), 'provision.py'), '# drift');
if (scenario.extra) fs.writeFileSync(path.join(process.cwd(), 'extra.py'), '# drift');
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
  assert.equal(receipt.deployment_acceptance_verified, false);
  assert.equal(receipt.floor_matches_origin_main, true);
  assert.equal(receipt.source_unchanged, true);
  assert.equal(receipt.revision, options.revision);
  assert.equal(receipt.mirror, fs.realpathSync(options.mirror));
  assert.deepEqual(receipt.checks[0].cases, success());
  assert.ok(receipt.checks[0].command.includes('not slow'));
  assert.deepEqual(receipt.checks[0].command.slice(1),
    JSON.parse(fs.readFileSync(path.join(path.dirname(options.root), 'command.json'))));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))), receipt);
  assert.equal(fs.statSync(path.join(options.output, 'receipt.json')).mode & 0o777, 0o600);
  assert.throws(() => runWitnesses(options), /EEXIST/);
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
  for (const file of ['conftest.py', 'injected.pyc', 'tests/extra.txt']) {
    const options = fixture(t);
    fs.writeFileSync(path.join(options.mirror, file), 'unbound input');
    assert.throws(() => runWitnesses(options), /Uncommitted mirror input/);
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
