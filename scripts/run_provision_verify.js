'use strict';

// Retain the checkout verifier and floor comparison even when either fails.
// A local receipt does not establish deployment or seven-day ledger acceptance.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');
const { execute } = require('./run_adjudicator_acceptance');

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

function runVerify({ root = path.resolve(__dirname, '..'), python, revision, output,
  timeout = 1800000 }) {
  if (!path.isAbsolute(python || '') || !path.isAbsolute(output || '')
      || !/^[a-f0-9]{40}$/.test(revision || '')) {
    throw new Error('Select an absolute Python interpreter, new output directory and full revision');
  }
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 1800000) {
    throw new Error('Timeout must be between 1 and 1800000 milliseconds');
  }
  root = fs.realpathSync(root);
  output = path.join(fs.realpathSync(path.dirname(output)), path.basename(output));
  if (output === root || output.startsWith(root + path.sep)) {
    throw new Error('Retain verifier output outside the delivered checkout');
  }
  function git(args) {
    const result = execute('git', ['--no-replace-objects', '-C', root, ...args], { cwd: root });
    if (result.exit_code !== 0 || result.error_code || result.signal) {
      throw new Error('Cannot read verifier revision or floor');
    }
    return result.stdout;
  }
  const head = () => git(['rev-parse', 'HEAD']).toString().trim();
  if (head() !== revision) throw new Error('Checkout must be at the selected revision');
  const inputPaths = ['src', 'tests', 'scripts', 'config', 'conftest.py', 'pyproject.toml',
    'pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml', 'tox.ini', 'setup.cfg',
    '.verify-floor.json'];
  const status = () => {
    const trackedAndNew = git(['status', '--porcelain', '--untracked-files=all', '--',
      ...inputPaths]).toString();
    // Git ignores do not stop Python importing code or pytest reading fixtures.
    const ignoredInputs = git(['ls-files', '--others', '--ignored', '--exclude-standard',
      '-z', '--', ...inputPaths]).toString().split('\0').filter((file) => file
        && !(path.posix.basename(path.posix.dirname(file)) === '__pycache__' && file.endsWith('.pyc')));
    return trackedAndNew + ignoredInputs.map((file) => `!! ${file}\n`).join('');
  };
  const before = status();
  const main = git(['rev-parse', 'origin/main']).toString().trim();
  const deliveredFloor = git(['show', `${revision}:.verify-floor.json`]);
  const mainFloor = git(['show', `${main}:.verify-floor.json`]);
  const floorMatches = deliveredFloor.equals(mainFloor);
  fs.mkdirSync(output, { mode: 0o700 });
  fs.writeFileSync(path.join(output, 'delivered-floor.json'), deliveredFloor, { flag: 'wx' });
  fs.writeFileSync(path.join(output, 'origin-main-floor.json'), mainFloor, { flag: 'wx' });
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'provision-verify-'));
  const receipt = { schema: 'provision-verify/v1', revision, origin_main: main,
    interpreter: python, command: [python, 'src/verify.py'],
    started_at: new Date().toISOString(), checkout_status_before: before,
    floor: { matches: floorMatches, comparison_exit_code: floorMatches ? 0 : 1,
      delivered_sha256: digest(deliveredFloor), origin_main_sha256: digest(mainFloor) },
    passed: false, deployment_acceptance_verified: false };
  try {
    const env = { ...process.env, ORCH_STATE_DIR: path.join(state, 'state'),
      ORCH_LOCAL_RUNTIME: path.join(state, 'runtime'), HANDOFF_DIR: path.join(state, 'handoff'),
      ORCH_CAPABILITIES_PATH: path.join(state, 'runtime', 'capabilities.json'),
      ORCH_FEEDBACK_DB: path.join(state, 'runtime', 'feedback', 'orchestrator.db'),
      ORCH_VERIFY_LIVE_STATE: '0',
      PYTHONDONTWRITEBYTECODE: '1', PYTEST_ADDOPTS: '-m "not slow"' };
    // verify.py copies the selected runtime before its writing gates run.
    receipt.runtime_overrides = Object.fromEntries(['ORCH_STATE_DIR', 'ORCH_LOCAL_RUNTIME',
      'HANDOFF_DIR', 'ORCH_CAPABILITIES_PATH', 'ORCH_FEEDBACK_DB', 'ORCH_VERIFY_LIVE_STATE',
      'PYTHONDONTWRITEBYTECODE', 'PYTEST_ADDOPTS'].map((key) => [key, env[key]]));
    const result = execute(python, ['src/verify.py'], { cwd: root, env, timeout });
    for (const stream of ['stdout', 'stderr']) {
      fs.writeFileSync(path.join(output, `verify.${stream}.log`), result[stream],
        { flag: 'wx', mode: 0o600 });
    }
    receipt.execution = { exit_code: result.exit_code, error_code: result.error_code,
      signal: result.signal, stdout_sha256: digest(result.stdout), stderr_sha256: digest(result.stderr) };
    receipt.checkout_status_after = status();
    receipt.head_after = head();
    receipt.passed = result.exit_code === 0 && !result.error_code && !result.signal
      && floorMatches && !before && !receipt.checkout_status_after && receipt.head_after === revision;
  } finally {
    receipt.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n',
      { flag: 'wx', mode: 0o600 });
    fs.rmSync(state, { recursive: true, force: true });
  }
  return receipt;
}

if (require.main === module) {
  try {
    const { values } = parseArgs({ options: { python: { type: 'string' },
      revision: { type: 'string' }, output: { type: 'string' } } });
    const receipt = runVerify(values);
    process.stdout.write(JSON.stringify(receipt) + '\n');
    process.exitCode = receipt.passed ? 0 : 1;
  } catch (error) {
    process.stderr.write(error.message + '\n');
    process.exitCode = 2;
  }
}

module.exports = { runVerify };
