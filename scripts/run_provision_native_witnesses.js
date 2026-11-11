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
  const rows = {};
  for (const name of fs.readdirSync(path.join(directory, prefix)).sort()) {
    if (['.git', '.pytest_cache'].includes(name)) continue;
    const file = path.posix.join(prefix, name);
    if (path.posix.basename(prefix) === '__pycache__' && name.endsWith('.pyc')) continue;
    const stat = fs.lstatSync(path.join(directory, file));
    if (stat.isSymbolicLink()) throw new Error(`Mirror contains a symlink: ${file}`);
    if (stat.isDirectory()) Object.assign(rows, manifest(directory, file));
    else if (stat.isFile()) rows[file] = sha256(fs.readFileSync(path.join(directory, file)));
    else throw new Error(`Mirror contains a non-file: ${file}`);
  }
  return rows;
}

function snapshot(root, mirror, revision) {
  if (git(root, ['rev-parse', 'HEAD']).toString().trim() !== revision) {
    throw new Error('Checkout must be at the tested revision');
  }
  if (fs.existsSync(path.join(mirror, '.git')) || fs.existsSync(path.join(mirror, 'src'))) {
    throw new Error('Select the installed flat mirror, without .git or src');
  }
  const contents = manifest(mirror);
  // Bind all flat Python modules, the entire test tree, and pytest configuration
  // to Git objects, including inputs that the selected witnesses import lazily.
  const files = git(root, ['ls-tree', '-r', '-z', revision, '--',
    'src', 'tests', 'pyproject.toml', '.verify-floor.json',
    'scripts/run_provision_native_witnesses.js', 'scripts/run_adjudicator_acceptance.js'])
    .toString().split('\0').filter(Boolean)
    .map((entry) => {
      const [metadata, file] = entry.split('\t');
      const [mode, kind, object] = metadata.split(' ');
      return { file, mode, kind, object };
    }).filter(({ file }) => !file.startsWith('src/') || /^src\/[^/]+\.py$/.test(file));
  const bound = new Set();
  for (const { file, mode, kind, object } of files) {
    const installed = file.startsWith('src/') ? file.slice(4) : file;
    bound.add(installed);
    if (!contents[installed] || kind !== 'blob' || !['100644', '100755'].includes(mode)) {
      throw new Error(`Missing or unsupported committed input: ${installed}`);
    }
    const bytes = fs.readFileSync(path.join(mirror, installed));
    const blob = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (blob !== object) {
      throw new Error(`Mirror differs from tested revision: ${installed}`);
    }
  }
  for (const file of Object.keys(contents)) {
    if ((file.startsWith('tests/') || (!file.includes('/') && /\.pyc?$/.test(file))) && !bound.has(file)) {
      throw new Error(`Uncommitted mirror input: ${file}`);
    }
  }
  for (const required of ['provision.py', suite, 'tests/conftest.py', 'pyproject.toml',
    '.verify-floor.json', 'scripts/run_provision_native_witnesses.js',
    'scripts/run_adjudicator_acceptance.js']) {
    if (!contents[required]) throw new Error(`Missing mirror input: ${required}`);
  }
  if (contents['.verify-floor.json'] !== sha256(git(root, ['show', 'origin/main:.verify-floor.json']))) {
    throw new Error('Verification floor differs from origin/main');
  }
  return contents;
}

function complete(cases) {
  return Array.isArray(cases) && cases.length === expected.length && expected.every((name) =>
    cases.filter((item) => item.name === name && item.classname === 'tests.test_provision_reclaim'
      && item.unsuccessful === false).length === 1);
}

function runWitnesses({ root = path.resolve(__dirname, '..'), mirror, python, revision, output,
  timeout = 60000 }) {
  if (!path.isAbsolute(python || '')) throw new Error('Select an absolute Python interpreter');
  if (!/^[a-f0-9]{40}$/.test(revision || '')) throw new Error('Select a full source revision');
  if (!path.isAbsolute(mirror || '') || !path.isAbsolute(output || '')) {
    throw new Error('Select absolute mirror and new output directories');
  }
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 60000) {
    throw new Error('Timeout must be between 1 and 60000 milliseconds');
  }
  mirror = fs.realpathSync(mirror); // Pin an installed generation before running.
  output = path.join(fs.realpathSync(path.dirname(output)), path.basename(output));
  if (output === mirror || output.startsWith(mirror + path.sep)) {
    throw new Error('Receipts must be outside the mirror');
  }
  const before = snapshot(root, mirror, revision);
  // The executing runner and its subprocess helper must be the bound bytes too.
  for (const name of ['run_provision_native_witnesses.js', 'run_adjudicator_acceptance.js']) {
    if (sha256(fs.readFileSync(path.join(__dirname, name))) !== before[`scripts/${name}`]) {
      throw new Error('Executing runner differs from tested revision');
    }
  }
  fs.mkdirSync(output, { mode: 0o700 }); // Never replace an earlier receipt.
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'provision-witness-'));
  const env = { ...process.env, PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1', PYTHONDONTWRITEBYTECODE: '1',
    ORCH_PROVISION_NATIVE_ACCEPTANCE: '1', ORCH_LOCAL_RUNTIME: state,
    HANDOFF_DIR: path.join(state, 'handoff'), ORCH_STATE_DIR: state,
    ORCH_PROVISION_EVENTS_PATH: path.join(state, 'provision-events.ndjson') };
  delete env.PYTEST_ADDOPTS;
  delete env.PYTEST_PLUGINS;
  const receipt = { schema: 'provision-native-witnesses/v1', checked_at: new Date().toISOString(),
    revision, mirror, hostname: os.hostname(), platform: process.platform, interpreter: python,
    mirror_manifest_sha256: sha256(JSON.stringify(before)), mirror_file_count: Object.keys(before).length,
    floor_matches_origin_main: true, checks: [], source_unchanged: false,
    native_witnesses_passed: false, deployment_acceptance_verified: false };
  try {
    const xml = path.join(output, 'native.xml');
    const args = ['-B', '-m', 'pytest', '-o', 'addopts=', '-p', 'no:cacheprovider', '-m', 'not slow',
      '-q', `--junitxml=${xml}`, ...witnesses.map((name) => `${suite}::${name}`)];
    const result = execute(python, args, { cwd: mirror, env, timeout });
    const log = Buffer.concat([result.stdout, result.stderr]);
    fs.writeFileSync(path.join(output, 'native.log'), log, { flag: 'wx', mode: 0o600 });
    let cases = null;
    if (fs.existsSync(xml)) {
      const parsed = execute(python, ['-I', '-c', junitReader, xml], { cwd: mirror, env, timeout });
      if (parsed.exit_code === 0 && !parsed.error_code && !parsed.signal) {
        try { cases = JSON.parse(parsed.stdout.toString()); } catch { /* Not a verdict. */ }
      }
    }
    receipt.checks.push({ command: [python, ...args], exit_code: result.exit_code,
      signal: result.signal, error_code: result.error_code, log_sha256: sha256(log),
      junit_sha256: fs.existsSync(xml) ? sha256(fs.readFileSync(xml)) : null,
      cases, passed: result.exit_code === 0 && !result.error_code && !result.signal && complete(cases) });
    try { receipt.source_unchanged = JSON.stringify(snapshot(root, mirror, revision)) === JSON.stringify(before); }
    catch { receipt.source_unchanged = false; }
    receipt.native_witnesses_passed = receipt.source_unchanged && receipt.checks.every((item) => item.passed);
    receipt.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'mirror-manifest.json'), JSON.stringify(before, null, 2),
      { flag: 'wx', mode: 0o600 });
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2),
      { flag: 'wx', mode: 0o600 });
    return receipt;
  } finally {
    fs.rmSync(state, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    const { values } = parseArgs({ options: { python: { type: 'string' }, revision: { type: 'string' },
      mirror: { type: 'string' }, output: { type: 'string' }, timeout: { type: 'string' } } });
    const receipt = runWitnesses({ ...values,
      timeout: values.timeout === undefined ? 60000 : Number(values.timeout) });
    process.stdout.write(JSON.stringify({ native_witnesses_passed: receipt.native_witnesses_passed,
      deployment_acceptance_verified: false }) + '\n');
    process.exitCode = receipt.native_witnesses_passed ? 0 : 1;
  } catch (error) {
    process.stderr.write(error.message + '\n');
    process.exitCode = 2;
  }
}

module.exports = { complete, expected, manifest, runWitnesses, snapshot };
