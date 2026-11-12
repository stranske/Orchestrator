'use strict';

// Issue #1098: collect existing native witnesses without granting deployment or
// seven-day acceptance. The mirror is read only; pytest fixtures use temp state.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const { execute } = require('./run_adjudicator_acceptance');

const suite = 'tests/test_provision_reclaim.py';
const witnesses = ['test_native_clean_idle_holder_is_reclaimed',
  'test_native_reclaim_disabled_exit_128_names_holder'];
const expected = witnesses.flatMap((name) => ['closer', 'opener'].map((lane) => `${name}[${lane}]`));
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
const pytestConfigs = new Set(['pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml',
  'pyproject.toml', 'tox.ini', 'setup.cfg']);
// Retained publisher output and host tooling. None of these paths is copied to
// the private test tree, and Python must be selected outside the generation.
const runtimePaths = new Set(['.venv', '.pytest_cache', '.ruff_cache', 'docs/reports',
  'ux_reviews', 'path.txt', 'private-ledger.lock', 'repo_review_registry.json.bak-2026-09-21',
  'README.md', 'renovate.json', 'uv.lock', 'experiments/.last-ship-gate',
  'experiments/invman1', 'experiments/orchskill-lanes-20260620', 'experiments/strategy434-20261006']);
const isInside = (root, file) => file === root || file.startsWith(root + path.sep);
const junitReader = `
import json, sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
print(json.dumps([{"name": c.get("name"), "classname": c.get("classname"),
    "unsuccessful": any(e.tag in ("failure", "error", "skipped") for e in c)}
    for c in root.iter("testcase")]))
`;

function git(root, args) {
  const result = execute('git', ['--no-replace-objects', '-C', root, ...args], { cwd: root });
  if (result.exit_code !== 0 || result.error_code || result.signal) {
    throw new Error('Cannot bind committed provisioning inputs');
  }
  return result.stdout;
}

function manifest(directory, prefix = '') {
  const rows = Object.create(null);
  for (const name of fs.readdirSync(path.join(directory, prefix)).sort()) {
    const file = path.posix.join(prefix, name);
    if (runtimePaths.has(file) || /^experiments\/tick-\d+-[\w-]+-\d+$/.test(file)) continue;
    const stat = fs.lstatSync(path.join(directory, file));
    // The publisher retains the storage-maintenance campaign as runtime data
    // in older generations. Accept only that backing-link shape, never follow it.
    if (file === 'campaigns' && stat.isSymbolicLink()) {
      const backing = path.resolve(directory, fs.readlinkSync(path.join(directory, file)));
      const parent = path.dirname(backing);
      if (path.basename(backing) === 'campaigns' && path.dirname(parent) === path.dirname(directory)
          && /^orchestrator-mirror\.(?:retired|generation)-[0-9a-f]{32}$/.test(path.basename(parent))) {
        continue;
      }
    }
    if (stat.isSymbolicLink()) throw new Error(`Mirror contains a symlink: ${file}`);
    if (stat.isFile() && path.posix.basename(prefix) === '__pycache__' && name.endsWith('.pyc')) continue;
    if (stat.isDirectory()) Object.assign(rows, manifest(directory, file));
    else if (stat.isFile()) rows[file] = sha256(fs.readFileSync(path.join(directory, file)));
    else throw new Error(`Mirror contains a non-file: ${file}`);
  }
  return rows;
}

function snapshot(root, mirror, revision, testTree) {
  if (git(root, ['rev-parse', 'HEAD']).toString().trim() !== revision) {
    throw new Error('Checkout must be at the tested revision');
  }
  if (fs.readdirSync(mirror).some((name) => ['.git', 'src'].includes(name))) {
    throw new Error('Select the installed flat mirror, without .git or src');
  }
  const contents = manifest(mirror);
  // Bind all flat Python modules, the entire test tree, and pytest configuration
  // to Git objects, including inputs that the selected witnesses import lazily.
  const files = git(root, ['ls-tree', '-r', '-z', revision, '--', 'src', 'tests',
    'scripts', '.github', 'docs', 'tools', 'pyproject.toml', '.verify-floor.json'])
    .toString().split('\0').filter(Boolean)
    .map((entry) => {
      const [metadata, file] = entry.split('\t');
      const [mode, kind, object] = metadata.split(' ');
      return { file, mode, kind, object };
    });
  const isImportOrConfig = (file) => /\.(?:py[co]?|so|pyd|pth)$/.test(file)
    || pytestConfigs.has(path.posix.basename(file));
  const required = ['provision.py', suite, 'tests/conftest.py', 'pyproject.toml',
    '.verify-floor.json', 'scripts/run_provision_native_witnesses.js',
    'scripts/run_adjudicator_acceptance.js'];
  const bound = new Set();
  for (const { file, mode, kind, object } of files) {
    const flatModule = /^src\/[^/]+\.py$/.test(file);
    const installed = flatModule ? file.slice(4) : file;
    const requiredInput = flatModule || file.startsWith('tests/') || required.includes(file);
    if (!requiredInput && !(contents[installed] && isImportOrConfig(installed))) continue;
    bound.add(installed);
    if (!contents[installed] || kind !== 'blob' || !['100644', '100755'].includes(mode)) {
      throw new Error(`Missing or unsupported committed input: ${installed}`);
    }
    const bytes = fs.readFileSync(path.join(mirror, installed));
    const blob = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (blob !== object || sha256(bytes) !== contents[installed]) {
      throw new Error(`Mirror differs from tested revision: ${installed}`);
    }
    if (testTree) {
      const destination = path.join(testTree, installed);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.writeFileSync(destination, bytes, { flag: 'wx', mode: mode === '100755' ? 0o700 : 0o600 });
    }
  }
  for (const file of Object.keys(contents)) {
    if ((file.startsWith('tests/') || isImportOrConfig(file)) && !bound.has(file)) {
      throw new Error(`Uncommitted mirror input: ${file}`);
    }
  }
  for (const file of required) {
    if (!bound.has(file)) throw new Error(`Missing mirror input: ${file}`);
  }
  if (contents['.verify-floor.json'] !== sha256(git(root, ['show', 'origin/main:.verify-floor.json']))) {
    throw new Error('Verification floor differs from origin/main');
  }
  return contents;
}

function complete(cases, names = expected) {
  return names.length > 0 && new Set(names).size === names.length
    && Array.isArray(cases) && cases.length === names.length && names.every((name) =>
    cases.filter((item) => item.name === name && item.classname === 'tests.test_provision_reclaim'
      && item.unsuccessful === false).length === 1);
}

function runWitnesses({ root = path.resolve(__dirname, '..'), mirror, python, revision, output,
  timeout = 60000, fullSuites = false }) {
  if (!path.isAbsolute(python || '')) throw new Error('Select an absolute Python interpreter');
  if (!/^[a-f0-9]{40}$/.test(revision || '')) throw new Error('Select a full source revision');
  if (!path.isAbsolute(mirror || '') || !path.isAbsolute(output || '')) {
    throw new Error('Select absolute mirror and new output directories');
  }
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 60000) {
    throw new Error('Timeout must be between 1 and 60000 milliseconds');
  }
  const mirrorAlias = path.resolve(mirror);
  mirror = fs.realpathSync(mirror); // Pin an installed generation before running.
  output = path.join(fs.realpathSync(path.dirname(output)), path.basename(output));
  if (isInside(mirror, output)) {
    throw new Error('Receipts must be outside the mirror');
  }
  if (isInside(mirrorAlias, path.resolve(python)) || isInside(mirror, path.resolve(python))
      || isInside(mirror, fs.realpathSync(path.dirname(python)))
      || isInside(mirror, fs.realpathSync(python))) {
    throw new Error('Select a Python interpreter outside the mirror');
  }
  const tempRoot = fs.realpathSync(os.tmpdir());
  if (isInside(mirror, tempRoot)) throw new Error('Temporary test state must be outside the mirror');
  const state = fs.mkdtempSync(path.join(tempRoot, 'provision-witness-'));
  try {
    return runInState(state);
  } finally {
    fs.rmSync(state, { recursive: true, force: true });
  }

  function runInState(state) {
    const testTree = path.join(state, 'source');
    const before = snapshot(root, mirror, revision, testTree);
    // The executing runner and its subprocess helper must be the bound bytes too.
    for (const name of ['run_provision_native_witnesses.js', 'run_adjudicator_acceptance.js']) {
      if (sha256(fs.readFileSync(path.join(__dirname, name))) !== before[`scripts/${name}`]) {
        throw new Error('Executing runner differs from tested revision');
      }
    }
    fs.mkdirSync(output, { mode: 0o700 }); // Never replace an earlier receipt.
    const env = { ...process.env, PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1', PYTHONDONTWRITEBYTECODE: '1',
      ORCH_PROVISION_NATIVE_ACCEPTANCE: '1', ORCH_LOCAL_RUNTIME: state,
      HANDOFF_DIR: path.join(state, 'handoff'), ORCH_STATE_DIR: state,
      ORCH_PROVISION_EVENTS_PATH: path.join(state, 'provision-events.ndjson') };
    delete env.PYTEST_ADDOPTS;
    delete env.PYTEST_PLUGINS;
    const receipt = { schema: 'provision-native-witnesses/v1', checked_at: new Date().toISOString(),
      revision, mirror, hostname: os.hostname(), platform: process.platform, interpreter: python,
      mirror_manifest_sha256: sha256(JSON.stringify(before)), mirror_file_count: Object.keys(before).length,
      mirror_manifest_after_sha256: null,
      floor_matches_origin_main: true, checks: [], source_unchanged: false,
      native_witnesses_passed: false, full_suites_requested: fullSuites, suites: [],
      default_native_suites_passed: false, deployment_acceptance_verified: false };
    const pytestArgs = ['-E', '-B', '-m', 'pytest', '-o', 'addopts=', '-p', 'no:cacheprovider',
      '-m', 'not slow', '-q'];
    function runCheck(name, args, environment, names) {
      const result = execute(python, args, { cwd: testTree, env: environment, timeout });
      const log = Buffer.concat([result.stdout, result.stderr]);
      fs.writeFileSync(path.join(output, `${name}.log`), log, { flag: 'wx', mode: 0o600 });
      const check = { command: [python, ...args], exit_code: result.exit_code,
        signal: result.signal, error_code: result.error_code, log_sha256: sha256(log) };
      if (names !== undefined) {
        const xml = path.join(output, `${name}.xml`);
        let cases = null;
        let reader = null;
        if (fs.existsSync(xml)) {
          const command = [python, '-I', '-c', junitReader, xml];
          const parsed = execute(python, command.slice(1),
            { cwd: testTree, env: environment, timeout });
          const readerLog = Buffer.concat([parsed.stdout, parsed.stderr]);
          const readerLogName = `${name}-junit-reader.log`;
          fs.writeFileSync(path.join(output, readerLogName), readerLog,
            { flag: 'wx', mode: 0o600 });
          let decoded = false;
          if (parsed.exit_code === 0 && !parsed.error_code && !parsed.signal) {
            try {
              cases = JSON.parse(parsed.stdout.toString());
              decoded = true;
            } catch { /* Retain the undecodable output for diagnosis. */ }
          }
          reader = { command, exit_code: parsed.exit_code, signal: parsed.signal,
            error_code: parsed.error_code, json_decoded: decoded,
            log: readerLogName, log_sha256: sha256(readerLog) };
        }
        Object.assign(check, { cases, junit_reader: reader,
          junit_sha256: fs.existsSync(xml) ? sha256(fs.readFileSync(xml)) : null,
          passed: result.exit_code === 0 && !result.error_code && !result.signal
            && complete(cases, names) });
      }
      return { check, stdout: result.stdout.toString() };
    }
    if (fullSuites) {
      for (const mode of ['default', 'native']) {
        const environment = { ...env };
        if (mode === 'default') delete environment.ORCH_PROVISION_NATIVE_ACCEPTANCE;
        // Discover the selected suite first. A zero exit or a handful of native
        // verdicts cannot substitute for execution of every collected case.
        const collected = runCheck(`${mode}-collection`,
          [...pytestArgs, '--collect-only', suite], environment);
        const prefix = `${suite}::`;
        const nodes = collected.stdout.split(/\r?\n/).filter((line) => line.startsWith(prefix));
        const names = nodes.map((node) => node.slice(prefix.length));
        const collection = { ...collected.check, nodes,
          passed: collected.check.exit_code === 0 && !collected.check.error_code
            && !collected.check.signal && names.length > 0
            && new Set(names).size === names.length };
        const execution = runCheck(`${mode}-suite`, [...pytestArgs,
          `--junitxml=${path.join(output, `${mode}-suite.xml`)}`, suite], environment, names).check;
        receipt.suites.push({ mode, native_acceptance: mode === 'native', collection, execution,
          passed: collection.passed && execution.passed });
      }
    }
    receipt.checks.push(runCheck('native', [...pytestArgs, '-rP',
      `--junitxml=${path.join(output, 'native.xml')}`,
      ...witnesses.map((name) => `${suite}::${name}`)], env, expected).check);
    try {
      const after = snapshot(root, mirror, revision);
      receipt.mirror_manifest_after_sha256 = sha256(JSON.stringify(after));
      receipt.source_unchanged = receipt.mirror_manifest_after_sha256 === receipt.mirror_manifest_sha256;
    } catch (error) {
      receipt.source_validation_error = error.message;
    }
    receipt.native_witnesses_passed = receipt.source_unchanged && receipt.checks.every((item) => item.passed);
    receipt.default_native_suites_passed = receipt.source_unchanged && fullSuites
      && receipt.suites.length === 2 && receipt.suites.every((item) => item.passed);
    receipt.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'mirror-manifest.json'), JSON.stringify(before, null, 2),
      { flag: 'wx', mode: 0o600 });
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2),
      { flag: 'wx', mode: 0o600 });
    return receipt;
  }
}

if (require.main === module) {
  try {
    const { values } = parseArgs({ options: { python: { type: 'string' }, revision: { type: 'string' },
      mirror: { type: 'string' }, output: { type: 'string' }, timeout: { type: 'string' },
      'full-suites': { type: 'boolean', default: false } } });
    const receipt = runWitnesses({ ...values, fullSuites: values['full-suites'],
      timeout: values.timeout === undefined ? 60000 : Number(values.timeout) });
    process.stdout.write(JSON.stringify({ native_witnesses_passed: receipt.native_witnesses_passed,
      default_native_suites_passed: receipt.default_native_suites_passed,
      deployment_acceptance_verified: false }) + '\n');
    process.exitCode = receipt.native_witnesses_passed
      && (!receipt.full_suites_requested || receipt.default_native_suites_passed) ? 0 : 1;
  } catch (error) {
    process.stderr.write(error.message + '\n');
    process.exitCode = 2;
  }
}

module.exports = { complete, expected, manifest, runWitnesses, snapshot };
