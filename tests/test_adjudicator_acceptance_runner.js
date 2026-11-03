'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { test } = require('node:test');
const { execute, namedTests, runAcceptance } = require('../scripts/run_adjudicator_acceptance');

function fixture(t, scenario = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'retro-acceptance-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  function git(...args) {
    const result = execute('git', args, { cwd: root });
    assert.equal(result.exit_code, 0, result.stderr.toString());
    return result.stdout.toString().trim();
  }
  for (const directory of ['src', 'tests', 'scripts']) fs.mkdirSync(path.join(root, directory));
  for (const file of ['src/adjudicator_retro.py', 'tests/test_adjudicator_retro.py',
    'scripts/run_adjudicator_acceptance.js', 'conftest.py', 'pytest.ini', 'setup.cfg', 'pyproject.toml']) {
    fs.writeFileSync(path.join(root, file), '# fixture\n');
  }
  git('init', '-q');
  git('add', 'src', 'tests', 'scripts', 'conftest.py', 'pytest.ini', 'setup.cfg', 'pyproject.toml');
  git('-c', 'user.name=Acceptance test', '-c', 'user.email=acceptance@example.invalid',
    'commit', '-qm', 'fixture');
  const revision = git('rev-parse', 'HEAD');
  // A selected-interpreter double, never a substitute for running real pytest.
  const python = path.join(root, 'interpreter');
  fs.writeFileSync(python, `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
const options = ${JSON.stringify(scenario)};
const required = ${JSON.stringify(namedTests)};
if (args[0] === '--version') { console.log('Python fixture'); process.exit(0); }
if (args[0] === '-I') {
  const { spawnSync } = require('node:child_process');
  const result = spawnSync(${JSON.stringify(process.env.PYTHON || '/opt/hostedtoolcache/Python/3.14.9/x64/bin/python')}, args,
    { stdio: 'inherit' });
  process.exit(result.status ?? 1);
}
fs.appendFileSync(path.join(__dirname, 'calls.jsonl'), JSON.stringify(args) + '\\n');
if (options.timeout) { setTimeout(() => {}, 10000); }
else if (options.missing) { console.error('No module named pytest'); process.exit(1); }
else if (args.includes('--collect-only')) {
  const names = options.collectionDuplicate ? [...required, required[0]] : [...required, 'test_extra[param-1]', 'test_extra[param-2]'];
  if (!options.collectionEmpty) for (const name of names) {
    console.log('tests/test_adjudicator_retro.py::' + name);
  }
  process.exit(options.collectionFailure ? 2 : 0);
}
else {
  const full = !args.some((arg) => arg.includes('::'));
  let names = full ? [...required, 'test_extra[param-1]', 'test_extra[param-2]'] : required;
  if (full && options.omitExtra) names = required;
  if (full && options.duplicateExtra) names = [...names, names[names.length - 1]];
  if (options.incomplete) names = names.slice(1);
  if (options.empty) names = [];
  if (options.duplicate) names = [...names, names[0]];
  const cases = names.map((name) => '<testcase name="' + name + '" classname="' +
    (options.foreign ? 'tests.test_unrelated' : 'tests.test_adjudicator_retro') + '">' +
    (options.skipped && full ? '<skipped />' : '') + '</testcase>').join('');
  const xml = args.find((arg) => arg.startsWith('--junitxml=')).slice('--junitxml='.length);
  if (!options.noXml) fs.writeFileSync(xml, options.malformed ? '<broken' : '<testsuites><testsuite>' + cases + '</testsuite></testsuites>');
  if (options.mutate) fs.appendFileSync(path.join(__dirname, 'src/adjudicator_retro.py'), '# changed\\n');
  console.log('fixture stdout'); console.error('fixture stderr');
  process.exit(options.exit ?? 0);
}
`, { mode: 0o700 });
  return { root, python, revision, output: path.join(root, 'receipt'), timeout: 10000 };
}

test('records exact commands, source hashes, complete JUnit and logs for both checks', (t) => {
  const options = fixture(t);
  const receipt = runAcceptance(options);
  assert.equal(receipt.required_tests_passed, true);
  assert.equal(receipt.accepted_deployment_verified, false);
  assert.equal(receipt.source_unchanged, true);
  assert.equal(receipt.evaluated_revision, options.revision);
  assert.equal(receipt.interpreter, options.python);
  assert.equal(receipt.collection.passed, true);
  assert.deepEqual(receipt.collection.test_names, [...namedTests,
    'test_extra[param-1]', 'test_extra[param-2]']);
  assert.deepEqual(receipt.checks.map((item) => item.test_count), [4, 6]);
  assert.equal(Object.keys(receipt.source_sha256).length, 3);
  for (const item of receipt.checks) {
    const calls = fs.readFileSync(path.join(options.root, 'calls.jsonl'), 'utf8')
      .trim().split('\n').map(JSON.parse);
    assert.deepEqual(receipt.collection.command.slice(1), calls[0]);
    assert.deepEqual(item.command.slice(1), calls[receipt.checks.indexOf(item) + 1]);
    assert.ok(item.command.includes('not slow'));
    const log = fs.readFileSync(path.join(options.output, item.log));
    assert.match(log.toString(), /fixture stdout\nfixture stderr/);
    assert.equal(item.log_sha256, createHash('sha256').update(log).digest('hex'));
    const xml = fs.readFileSync(path.join(options.output, item.junit));
    assert.equal(item.junit_sha256, createHash('sha256').update(xml).digest('hex'));
  }
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'checks.json'))), receipt);
});

for (const scenario of [{ omitExtra: true }, { duplicateExtra: true },
  { collectionFailure: true }, { collectionEmpty: true }, { collectionDuplicate: true }]) {
  test(`full-suite success requires the complete collected identities: ${JSON.stringify(scenario)}`, (t) => {
    const receipt = runAcceptance(fixture(t, scenario));
    assert.equal(receipt.checks[0].passed, true, 'named results remain separately attributable');
    assert.equal(receipt.checks[1].passed, false);
    assert.equal(receipt.required_tests_passed, false);
  });
}

test('missing pytest preserves two failures and cannot complete acceptance', (t) => {
  const options = fixture(t, { missing: true });
  const receipt = runAcceptance(options);
  assert.equal(receipt.required_tests_passed, false);
  assert.equal(receipt.source_unchanged, true);
  assert.equal(receipt.collection.passed, false);
  for (const item of receipt.checks) {
    assert.equal(item.exit_code, 1);
    assert.equal(item.test_count, null);
    assert.equal(item.junit, null);
    assert.match(fs.readFileSync(path.join(options.output, item.log), 'utf8'), /No module named pytest/);
  }
});

for (const scenario of [{ noXml: true }, { malformed: true }, { empty: true },
  { incomplete: true }, { duplicate: true }, { skipped: true }, { foreign: true }, { exit: 1 }]) {
  test(`refuses a successful claim for incomplete evidence: ${JSON.stringify(scenario)}`, (t) => {
    const receipt = runAcceptance(fixture(t, scenario));
    assert.equal(receipt.required_tests_passed, false);
  });
}

test('source changes during execution invalidate otherwise passing checks', (t) => {
  const receipt = runAcceptance(fixture(t, { mutate: true }));
  assert.ok(receipt.checks.every((item) => item.passed));
  assert.equal(receipt.source_unchanged, false);
  assert.equal(receipt.required_tests_passed, false);
});

test('timeout preserves both failed checks without retrying either command', (t) => {
  const options = fixture(t, { timeout: true });
  const receipt = runAcceptance({ ...options, timeout: 500 });
  assert.equal(receipt.checks.length, 2);
  assert.ok(receipt.checks.every((item) => item.timed_out && !item.passed));
  assert.equal(receipt.required_tests_passed, false);
  assert.equal(receipt.collection.timed_out, true);
  assert.equal(fs.readFileSync(path.join(options.root, 'calls.jsonl'), 'utf8').trim().split('\n').length, 3);
});

test('refuses revision mismatch, modified tracked inputs and untracked inputs before execution', (t) => {
  const options = fixture(t);
  assert.throws(() => runAcceptance({ ...options, revision: 'a'.repeat(40) }), /revision mismatch/);
  fs.writeFileSync(path.join(options.root, 'tests', 'extra.py'), '# untracked\n');
  assert.throws(() => runAcceptance(options), /Untracked/);
  fs.unlinkSync(path.join(options.root, 'tests', 'extra.py'));
  fs.appendFileSync(path.join(options.root, 'src', 'adjudicator_retro.py'), '# changed\n');
  assert.throws(() => runAcceptance(options), /Git source check failed/);
  assert.equal(fs.existsSync(options.output), false);
  assert.equal(fs.existsSync(path.join(options.root, 'calls.jsonl')), false);
});

for (const file of ['conftest.py', 'pytest.ini', 'setup.cfg', 'scripts/run_adjudicator_acceptance.js']) {
  test(`refuses uncommitted test execution input: ${file}`, (t) => {
    const options = fixture(t);
    fs.appendFileSync(path.join(options.root, file), '# changed\n');
    assert.throws(() => runAcceptance(options), /Git source check failed/);
    assert.equal(fs.existsSync(options.output), false);
    assert.equal(fs.existsSync(path.join(options.root, 'calls.jsonl')), false);
  });
}

test('requires explicit interpreter/revision/bounds and never overwrites a receipt', (t) => {
  const options = fixture(t, { missing: true });
  for (const change of [{ python: 'python' }, { revision: 'HEAD' }, { timeout: 0 },
    { timeout: 60001 }, { output: 'relative' }]) {
    assert.throws(() => runAcceptance({ ...options, ...change }));
  }
  runAcceptance(options);
  const before = fs.readFileSync(path.join(options.output, 'checks.json'));
  assert.throws(() => runAcceptance(options), /EEXIST/);
  assert.deepEqual(fs.readFileSync(path.join(options.output, 'checks.json')), before);
});
