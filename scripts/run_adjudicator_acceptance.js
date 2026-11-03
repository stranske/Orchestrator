'use strict';

// Repository-local test evidence only. This never dispatches or authenticates
// a deployment, native receipt, canonical grade, or live cohort.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { parseArgs } = require('node:util');

const namedTests = [
  'test_build_packet_includes_diff_and_acceptance_criteria',
  'test_adjudicator_validator_accepts_recommendation_prose',
  'test_floor_still_demotes_a_metadata_only_packet',
  'test_abstained_row_is_reattempted_when_the_packet_gains_diff_and_criteria',
];
const sourcePaths = ['src', 'tests', 'scripts/run_adjudicator_acceptance.js',
  'conftest.py', 'pytest.ini', 'setup.cfg', 'pyproject.toml', 'requirements.lock'];
const junitReader = `
import json, sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
print(json.dumps([{"name": c.get("name"), "classname": c.get("classname"),
    "unsuccessful": any(e.tag in ("failure", "error", "skipped") for e in c)}
    for c in root.iter("testcase")]))
`;

function digest(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function execute(command, args, { cwd, env = process.env, timeout = 60000 }) {
  // Some automation runners prohibit child-process pipes. Keep complete output
  // in files rather than a bounded in-memory spawnSync buffer.
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'retro-check-io-'));
  const descriptors = ['stdout', 'stderr'].map((name) =>
    fs.openSync(path.join(temporary, name), 'w'));
  try {
    const result = spawnSync(command, args, {
      cwd, env, timeout, stdio: ['ignore', ...descriptors],
    });
    return { exit_code: result.status, signal: result.signal,
      error_code: result.error?.code ?? null,
      stdout: fs.readFileSync(path.join(temporary, 'stdout')),
      stderr: fs.readFileSync(path.join(temporary, 'stderr')) };
  } finally {
    descriptors.forEach((fd) => fs.closeSync(fd));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

function git(root, args) {
  const result = execute('git', args, { cwd: root,
    env: { ...process.env, GIT_NO_REPLACE_OBJECTS: '1' } });
  if (result.exit_code !== 0 || result.error_code) throw new Error('Git source check failed');
  return result.stdout.toString('utf8').trimEnd();
}

function snapshot(root, revision) {
  if (git(root, ['rev-parse', 'HEAD']) !== revision) throw new Error('Checkout revision mismatch');
  // Include fixtures and configuration as well as Python: tests import and read
  // both. Refuse uncommitted inputs instead of labelling them as revision bytes.
  git(root, ['diff', '--quiet', revision, '--', ...sourcePaths]);
  if (git(root, ['ls-files', '--others', '--exclude-standard', '--', ...sourcePaths])) {
    throw new Error('Untracked acceptance inputs');
  }
  const files = git(root, ['ls-files', '-z', '--', ...sourcePaths]).split('\0').filter(Boolean);
  return Object.fromEntries(files.map((file) => [file, digest(fs.readFileSync(path.join(root, file)))]));
}

function runAcceptance({ root = path.resolve(__dirname, '..'), python, revision, output,
  timeout = 60000 }) {
  if (!path.isAbsolute(python || '')) throw new Error('Select an absolute Python interpreter');
  if (!/^[a-f0-9]{40}$/.test(revision || '')) throw new Error('Select a full source revision');
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 60000) {
    throw new Error('Timeout must be between 1 and 60000 milliseconds');
  }
  if (!output || !path.isAbsolute(output)) throw new Error('Select an absolute new output directory');
  const before = snapshot(root, revision);
  fs.mkdirSync(output); // Exclusive: never replace a prior run's receipts.
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'retro-check-state-'));
  const env = { ...process.env, PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1',
    ORCH_STATE_DIR: state, ORCH_CAPABILITIES_PATH: path.join(state, 'capabilities.json') };
  delete env.PYTEST_ADDOPTS;
  delete env.PYTEST_PLUGINS;
  const receipt = { schema_version: 1, checked_at: new Date().toISOString(),
    evaluated_revision: revision, evaluated_tree: git(root, ['rev-parse', `${revision}^{tree}`]),
    accepted_deployment_verified: false, interpreter: python,
    runner_sha256: digest(fs.readFileSync(__filename)),
    input_manifest_sha256: digest(JSON.stringify(before)), input_file_count: Object.keys(before).length,
    source_sha256: Object.fromEntries(Object.entries(before).filter(([file]) => [
      'src/adjudicator_retro.py', 'src/roles.py', 'src/feedback.py', 'src/capability_advisor.py',
      'src/verifier_evidence.py', 'tests/test_adjudicator_retro.py', 'pyproject.toml',
    ].includes(file))),
    checks: [], source_unchanged: false, required_tests_passed: false };
  try {
    const version = execute(python, ['--version'], { cwd: root, env, timeout });
    receipt.interpreter_version = Buffer.concat([version.stdout, version.stderr]).toString().trim();
    receipt.interpreter_usable = version.exit_code === 0 && !version.error_code && !version.signal;
    // A zero exit and the four named cases cannot establish that the full suite
    // ran. Bind its JUnit identities to an independent collection at this tree.
    const collectionCommand = [python, '-m', 'pytest', '-o', 'addopts=', '-m', 'not slow',
      '--color=no', '--collect-only', '-q', 'tests/test_adjudicator_retro.py'];
    const collection = execute(python, collectionCommand.slice(1), { cwd: root, env, timeout });
    const collectionLog = Buffer.concat([collection.stdout, collection.stderr]);
    fs.writeFileSync(path.join(output, 'collection.log'), collectionLog, { flag: 'wx' });
    const prefix = 'tests/test_adjudicator_retro.py::';
    const collected = collection.stdout.toString('utf8').split(/\r?\n/)
      .filter((line) => line.startsWith(prefix)).map((line) => line.slice(prefix.length));
    const collectionPassed = collection.exit_code === 0 && !collection.error_code &&
      !collection.signal && collected.length > 0 && new Set(collected).size === collected.length &&
      namedTests.every((test) => collected.includes(test));
    receipt.collection = { command: collectionCommand, exit_code: collection.exit_code,
      signal: collection.signal, error_code: collection.error_code,
      timed_out: collection.error_code === 'ETIMEDOUT', log: 'collection.log',
      log_sha256: digest(collectionLog), test_names: collected, passed: collectionPassed };
    for (const [name, nodes] of [
      ['named-tests', namedTests.map((test) => `tests/test_adjudicator_retro.py::${test}`)],
      ['retrospective-suite', ['tests/test_adjudicator_retro.py']],
    ]) {
      const xml = path.join(output, `${name}.xml`);
      const command = [python, '-m', 'pytest', '-o', 'addopts=', '-m', 'not slow', '-q',
        `--junitxml=${xml}`, ...nodes];
      const result = execute(python, command.slice(1), { cwd: root, env, timeout });
      const log = Buffer.concat([result.stdout, result.stderr]);
      fs.writeFileSync(path.join(output, `${name}.log`), log, { flag: 'wx' });
      let cases = null;
      if (fs.existsSync(xml)) {
        const parsed = execute(python, ['-I', '-c', junitReader, xml], { cwd: root, env, timeout });
        if (parsed.exit_code === 0 && !parsed.error_code) {
          try { cases = JSON.parse(parsed.stdout.toString()); } catch { /* Invalid receipt. */ }
        }
      }
      const expected = name === 'named-tests' ? namedTests : collected;
      const complete = (name === 'named-tests' || collectionPassed) &&
        Array.isArray(cases) && cases.length === expected.length && cases.length > 0 &&
        cases.every((item) => !item.unsuccessful && item.classname === 'tests.test_adjudicator_retro') &&
        expected.every((test) => cases.filter((item) => item.name === test).length === 1);
      receipt.checks.push({ name, command, exit_code: result.exit_code, signal: result.signal,
        error_code: result.error_code, timed_out: result.error_code === 'ETIMEDOUT',
        log: `${name}.log`, log_sha256: digest(log),
        junit: fs.existsSync(xml) ? `${name}.xml` : null,
        junit_sha256: fs.existsSync(xml) ? digest(fs.readFileSync(xml)) : null,
        test_count: Array.isArray(cases) ? cases.length : null,
        passed: result.exit_code === 0 && !result.error_code && !result.signal && complete });
    }
    try { receipt.source_unchanged = JSON.stringify(snapshot(root, revision)) === JSON.stringify(before); }
    catch { receipt.source_unchanged = false; }
    receipt.required_tests_passed = receipt.interpreter_usable && receipt.source_unchanged &&
      receipt.checks.every((item) => item.passed);
    receipt.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'checks.json'), `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
    return receipt;
  } finally {
    fs.rmSync(state, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    const { values } = parseArgs({ options: { python: { type: 'string' },
      revision: { type: 'string' }, output: { type: 'string' }, timeout: { type: 'string' } } });
    const receipt = runAcceptance({ ...values,
      timeout: values.timeout === undefined ? 60000 : Number(values.timeout) });
    process.stdout.write(`${JSON.stringify({ required_tests_passed: receipt.required_tests_passed,
      accepted_deployment_verified: false, checks: receipt.checks.map(({ name, exit_code, passed }) =>
        ({ name, exit_code, passed })) })}\n`);
    process.exitCode = receipt.required_tests_passed ? 0 : 1;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}

module.exports = { execute, namedTests, runAcceptance };
