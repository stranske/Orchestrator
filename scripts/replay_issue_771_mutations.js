'use strict';

// Run only in a separate, clean checkout at the original head. No checkout or
// dependency installation is performed here; the caller supplies both.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const plan = require('../docs/evidence/issue-771-repair/mutations/plan.json');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const blob = bytes => crypto.createHash('sha1')
  .update(`blob ${bytes.length}\0`).update(bytes).digest('hex');

function mutate(source, mutation) {
  const start = source.indexOf(`def ${mutation.function_name}(`);
  assert.ok(start >= 0, `missing function: ${mutation.function_name}`);
  const next = source.indexOf('\ndef ', start + 5);
  const end = next < 0 ? source.length : next;
  const scope = source.slice(start, end);
  assert.equal(scope.split(mutation.before).length - 1, 1, 'mutation must match exactly once');
  return source.slice(0, start) + scope.replace(mutation.before, mutation.after) + source.slice(end);
}

function successful(result) {
  return result.exit_code === 0 && !result.signal && !result.error;
}

function passed(result, count) {
  const text = `${result.stdout}\n${result.stderr}`;
  return successful(result) && new RegExp(`(?:^|\\n)${count} passed(?:,| in |\\n|$)`).test(text)
    && !/\b\d+ (?:failed|errors?|skipped|deselected|xfailed|xpassed)\b/i.test(text);
}

function assertionFailure(result, testId) {
  const text = `${result.stdout}\n${result.stderr}`;
  const named = text.split('\n').some(line =>
    line.startsWith(`FAILED ${plan.test_source}::${testId} - `));
  return result.exit_code === 1 && !result.signal && !result.error && named
    && /(?:\bAssertionError\b|^E\s+assert\b)/m.test(text)
    && /(?:^|\n)1 failed in /.test(text)
    && !/\b\d+ (?:errors?|passed|skipped|deselected|xfailed|xpassed)\b/i.test(text);
}

function recorder(directory, cwd, env) {
  const records = [];
  return {
    records,
    run(label, executable, args) {
      const started = new Date().toISOString();
      const stdoutFile = `${label}.stdout.txt`;
      const stderrFile = `${label}.stderr.txt`;
      // Write to receipts directly: large pytest logs cannot overflow a pipe buffer,
      // and output written before a timeout survives termination.
      const stdoutFd = fs.openSync(path.join(directory, stdoutFile), 'wx');
      const stderrFd = fs.openSync(path.join(directory, stderrFile), 'wx');
      let result;
      try {
        result = spawnSync(executable, args, {
          cwd, env, shell: false, timeout: 120000, stdio: ['ignore', stdoutFd, stderrFd],
        });
      } finally {
        fs.closeSync(stdoutFd);
        fs.closeSync(stderrFd);
      }
      const stdout = fs.readFileSync(path.join(directory, stdoutFile), 'utf8');
      const stderr = fs.readFileSync(path.join(directory, stderrFile), 'utf8');
      const record = {
        label, command: [executable, ...args], cwd, started_at: started,
        finished_at: new Date().toISOString(), exit_code: result.status,
        signal: result.signal, error: result.error ? String(result.error) : null,
        stdout: stdoutFile, stderr: stderrFile,
        stdout_sha256: digest(stdout), stderr_sha256: digest(stderr),
      };
      records.push(record);
      fs.writeFileSync(path.join(directory, 'commands.json'), JSON.stringify(records, null, 2) + '\n');
      return { ...record, stdout, stderr };
    },
  };
}

function runMutation({ checkout, output, python, mutation, run }) {
  const target = path.join(checkout, plan.source);
  const original = fs.readFileSync(target);
  const changed = Buffer.from(mutate(original.toString('utf8'), mutation));
  const receipt = {
    test_id: mutation.test_id, source_commit: plan.commit,
    original_sha256: digest(original), mutated_sha256: digest(changed),
    status: 'incomplete',
  };
  const prefix = mutation.test_id;
  let failure;
  try {
    fs.writeFileSync(target, changed);
    const patch = run(`${prefix}-patch`, 'git', ['diff', '--', plan.source]);
    assert.ok(successful(patch) && patch.stdout.includes(mutation.after), 'missing mutation patch');
    fs.writeFileSync(path.join(output, `${prefix}.patch`), patch.stdout);
    failure = run(`${prefix}-broken`, python,
      ['-m', 'pytest', '-q', `${plan.test_source}::${prefix}`, '-m', 'not slow']);
    receipt.named_assertion_failed = assertionFailure(failure, prefix);
  } finally {
    // Restore even when pytest, patch capture, or evidence validation fails.
    fs.writeFileSync(target, original);
    receipt.restored_sha256 = digest(fs.readFileSync(target));
    receipt.byte_restored = receipt.restored_sha256 === receipt.original_sha256;
    const diff = run(`${prefix}-restoration`, 'git', ['diff', '--exit-code', '--', plan.source]);
    receipt.clean_diff = successful(diff) && diff.stdout === '';
    const restored = run(`${prefix}-restored`, python,
      ['-m', 'pytest', '-q', `${plan.test_source}::${prefix}`, '-m', 'not slow']);
    receipt.restored_pass = passed(restored, 1);
    if (receipt.named_assertion_failed && receipt.byte_restored
        && receipt.clean_diff && receipt.restored_pass) receipt.status = 'complete';
    fs.writeFileSync(path.join(output, `${prefix}.json`), JSON.stringify(receipt, null, 2) + '\n');
  }
  assert.equal(receipt.status, 'complete', `incomplete failure/restoration pair: ${prefix}`);
  return receipt;
}

function replay({ checkout, output, python }) {
  checkout = fs.realpathSync(checkout);
  output = path.resolve(output);
  const workspace = path.resolve(__dirname, '..');
  assert.notEqual(checkout, workspace, 'supply a separate historical checkout');
  assert.ok(!output.startsWith(checkout + path.sep) && output !== checkout,
    'evidence output must be outside the historical checkout');
  assert.ok(!fs.existsSync(output), 'output must be new; never overwrite earlier receipts');
  fs.mkdirSync(output, { recursive: true });
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'issue-771-state-'));
  const overrides = {
    PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: `${checkout}/src${path.delimiter}${checkout}`,
    ORCH_STATE_DIR: path.join(state, 'state'), ORCH_LOCAL_RUNTIME: path.join(state, 'runtime'),
    PYTEST_ADDOPTS: '',
  };
  const env = { ...process.env, ...overrides };
  delete env.ORCH_FEEDBACK_DB;
  const { run, records } = recorder(output, checkout, env);
  const manifest = {
    kind: 'new reproduction, not recovered historical execution', commit: plan.commit,
    checkout, interpreter: python, environment_overrides: overrides,
    environment_removed: ['ORCH_FEEDBACK_DB'], status: 'incomplete', mutations: [],
  };
  try {
    const head = run('head', 'git', ['rev-parse', 'HEAD']);
    assert.ok(successful(head) && head.stdout.trim() === plan.commit, 'historical head is required');
    const root = run('root', 'git', ['rev-parse', '--show-toplevel']);
    assert.ok(successful(root) && fs.realpathSync(root.stdout.trim()) === checkout,
      'checkout must be the repository root');
    const status = run('status-before', 'git', ['status', '--porcelain', '--untracked-files=all']);
    assert.ok(successful(status) && status.stdout === '', 'checkout must be clean');
    assert.equal(blob(fs.readFileSync(path.join(checkout, plan.source))), plan.source_blob_sha,
      'historical implementation blob mismatch');
    assert.equal(blob(fs.readFileSync(path.join(checkout, plan.test_source))), plan.test_blob_sha,
      'original five-test blob mismatch');
    const version = run('python-version', python, ['--version']);
    assert.ok(successful(version), 'selected interpreter unavailable');
    const pytest = run('pytest-version', python, ['-m', 'pytest', '--version']);
    assert.ok(successful(pytest), 'pytest unavailable in selected interpreter');
    const population = run('collection', python,
      ['-m', 'pytest', '--collect-only', '-q', plan.test_source, '-m', 'not slow']);
    assert.ok(successful(population), 'collection failed');
    const ids = population.stdout.split('\n').filter(line => line.startsWith(`${plan.test_source}::`));
    assert.deepEqual(ids, plan.mutations.map(m => `${plan.test_source}::${m.test_id}`),
      'population must contain exactly the five original tests');
    manifest.test_population = ids;
    const baseline = run('five-tests-before', python,
      ['-m', 'pytest', '-q', plan.test_source, '-m', 'not slow']);
    assert.ok(passed(baseline, 5), 'five original tests must pass before mutation');
    for (const mutation of plan.mutations) {
      manifest.mutations.push(runMutation({ checkout, output, python, mutation, run }));
    }
    const final = run('five-tests-after', python,
      ['-m', 'pytest', '-q', plan.test_source, '-m', 'not slow']);
    assert.ok(passed(final, 5), 'five original tests must pass after restoration');
    const clean = run('status-after', 'git', ['status', '--porcelain', '--untracked-files=all']);
    assert.ok(successful(clean) && clean.stdout === '', 'checkout must remain clean');
    manifest.status = 'complete';
  } catch (error) {
    manifest.error = String(error);
    throw error;
  } finally {
    manifest.commands = records;
    fs.writeFileSync(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
    fs.rmSync(state, { recursive: true, force: true });
  }
  return manifest;
}

if (require.main === module) {
  try {
    const [checkout, python, output, ...extra] = process.argv.slice(2);
    assert.ok(checkout && python && output && extra.length === 0,
      'usage: node scripts/replay_issue_771_mutations.js CHECKOUT INTERPRETER NEW_OUTPUT');
    const manifest = replay({ checkout, python, output });
    process.stdout.write(`${manifest.status}: ${manifest.mutations.length} verified mutation pairs\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { mutate, passed, assertionFailure, runMutation, replay, recorder };
