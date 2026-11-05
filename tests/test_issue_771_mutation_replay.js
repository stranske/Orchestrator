'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const { mutate, passed, assertionFailure, runMutation, replay, recorder } =
  require('../scripts/replay_issue_771_mutations');
const plan = require('../docs/evidence/issue-771-repair/mutations/plan.json');
const historical = require('../docs/evidence/issue-771-repair/mutations/historical-functions.json');

function temporary(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'issue-771-replay-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function result(stdout, exit_code = 0, extra = {}) {
  return { stdout, stderr: '', exit_code, signal: null, error: null, ...extra };
}

function broken(testId) {
  return result(`E       assert 2 == 1\nFAILED ${plan.test_source}::${testId} - assert 2 == 1\n1 failed in 0.1s\n`, 1);
}

test('the plan targets all five original test IDs and matching historical functions', () => {
  assert.equal(historical.commit, plan.commit);
  assert.equal(historical.source_blob_sha, plan.source_blob_sha);
  const original = fs.readFileSync(path.resolve(__dirname,
    '../docs/evidence/issue-771-repair/mutations/original-tests.py.txt'), 'utf8');
  assert.deepEqual(plan.mutations.map(m => m.test_id),
    [...original.matchAll(/^def (test_\w+)\(/gm)].map(m => m[1]));
  assert.equal(plan.mutations.length, 5);
  for (const mutation of plan.mutations) {
    const source = historical.functions.find(f => f.function_name === mutation.function_name).source;
    const changed = mutate(source, mutation);
    assert.notEqual(changed, source);
    assert.ok(changed.includes(mutation.after));
    assert.equal(changed.replace(mutation.after, mutation.before), source);
  }
});

test('mutation replacement is restricted to the named function and rejects source drift', () => {
  const mutation = plan.mutations[0];
  const scope = historical.functions[0].source;
  const unrelated = `def unrelated():\n    ${mutation.before}\n`;
  assert.ok(mutate(scope + unrelated, mutation).endsWith(unrelated));
  assert.throws(() => mutate(unrelated, mutation), /missing function/);
  assert.throws(() => mutate(scope.replace(mutation.before, ''), mutation), /exactly once/);
  assert.throws(() => mutate(scope.replace(mutation.before,
    `${mutation.before}\n${mutation.before}`), mutation), /exactly once/);
});

test('pytest result validation requires the exact passing population', () => {
  assert.ok(passed(result('..... [100%]\n5 passed in 0.1s\n'), 5));
  assert.ok(passed(result('..... [100%]\n5 passed, 1 warning in 0.1s\n'), 5));
  for (const value of [
    result('5 passed in 0.1s\n', 1),
    result('15 passed in 0.1s\n'),
    result('5 passed, 1 skipped in 0.1s\n'),
    result('5 passed, 1 deselected in 0.1s\n'),
    result('5 passed in 0.1s\n', 0, { signal: 'SIGTERM' }),
    result('5 passed in 0.1s\n', 0, { error: 'spawn failed' }),
  ]) assert.equal(passed(value, 5), false);
});

test('only the named assertion failure counts as deliberate-break evidence', () => {
  const id = plan.mutations[0].test_id;
  assert.ok(assertionFailure(broken(id), id));
  assert.equal(assertionFailure(broken(plan.mutations[1].test_id), id), false);
  assert.equal(assertionFailure(result('ModuleNotFoundError: feedback\n1 error in 0.1s\n', 2), id), false);
  assert.equal(assertionFailure(result(`FAILED ${plan.test_source}::${id} - ValueError\n1 failed in 0.1s\n`, 1), id), false);
  assert.equal(assertionFailure({ ...broken(id), exit_code: 0 }, id), false);
  assert.equal(assertionFailure({ ...broken(id), signal: 'SIGKILL' }, id), false);
  assert.equal(assertionFailure({ ...broken(id), stdout: broken(id).stdout + '1 error\n' }, id), false);
});

// These subprocess results are test doubles for recorder orchestration only.
// They are never stored in the evidence directory or claimed as historical runs.
function fixture(t, failure = broken(plan.mutations[0].test_id)) {
  const checkout = temporary(t);
  const output = temporary(t);
  fs.mkdirSync(path.join(checkout, 'src'));
  const target = path.join(checkout, plan.source);
  const original = historical.functions[0].source;
  fs.writeFileSync(target, original);
  const calls = [];
  const mutation = plan.mutations[0];
  const options = { checkout, output, python: '/selected/python', mutation,
    run(label, executable, args) {
      calls.push({ label, executable, args });
      if (label.endsWith('-patch')) {
        assert.ok(fs.readFileSync(target, 'utf8').includes(mutation.after));
        return result(`+${mutation.after}\n`);
      }
      if (label.endsWith('-broken')) {
        if (failure instanceof Error) throw failure;
        return failure;
      }
      assert.equal(fs.readFileSync(target, 'utf8'), original);
      return result(label.endsWith('-restoration') ? '' : '1 passed in 0.1s\n');
    },
  };
  return { options, target, original, calls };
}

test('mutation pairs capture patches, named failures, byte restoration, diff and restored passes', t => {
  const f = fixture(t);
  const receipt = runMutation(f.options);
  assert.equal(receipt.status, 'complete');
  assert.equal(receipt.original_sha256, receipt.restored_sha256);
  assert.notEqual(receipt.original_sha256, receipt.mutated_sha256);
  assert.deepEqual(f.calls.map(c => c.label.split('-').at(-1)),
    ['patch', 'broken', 'restoration', 'restored']);
  assert.deepEqual(f.calls[1].args, ['-m', 'pytest', '-q',
    `${plan.test_source}::${plan.mutations[0].test_id}`, '-m', 'not slow']);
  assert.equal(fs.readFileSync(f.target, 'utf8'), f.original);
  assert.ok(fs.existsSync(path.join(f.options.output, `${receipt.test_id}.patch`)));
});

test('an import failure restores bytes and preserves an incomplete receipt', t => {
  const f = fixture(t, result('ImportError\n1 error in 0.1s\n', 2));
  assert.throws(() => runMutation(f.options), /incomplete failure\/restoration pair/);
  assert.equal(fs.readFileSync(f.target, 'utf8'), f.original);
  const receipt = JSON.parse(fs.readFileSync(path.join(f.options.output,
    `${f.options.mutation.test_id}.json`)));
  assert.equal(receipt.status, 'incomplete');
  assert.equal(receipt.named_assertion_failed, false);
  assert.equal(receipt.restored_pass, true);
});

test('unexpected execution exceptions still restore the source and attempt restored pytest', t => {
  const f = fixture(t, new Error('runner interrupted'));
  assert.throws(() => runMutation(f.options), /runner interrupted/);
  assert.equal(fs.readFileSync(f.target, 'utf8'), f.original);
  assert.ok(f.calls.at(-1).label.endsWith('-restored'));
});

test('a restored failure cannot complete a mutation pair', t => {
  const f = fixture(t);
  const run = f.options.run;
  f.options.run = (...args) => args[0].endsWith('-restored')
    ? result('1 failed in 0.1s\n', 1) : run(...args);
  assert.throws(() => runMutation(f.options), /incomplete failure\/restoration pair/);
  assert.equal(fs.readFileSync(f.target, 'utf8'), f.original);
});

test('a dirty restoration diff prevents completion even when restored pytest passes', t => {
  const f = fixture(t);
  const run = f.options.run;
  f.options.run = (...args) => args[0].endsWith('-restoration')
    ? result('remaining diff\n', 1) : run(...args);
  assert.throws(() => runMutation(f.options), /incomplete failure\/restoration pair/);
  const receipt = JSON.parse(fs.readFileSync(path.join(f.options.output,
    `${f.options.mutation.test_id}.json`)));
  assert.equal(receipt.byte_restored, true);
  assert.equal(receipt.clean_diff, false);
  assert.equal(receipt.restored_pass, true);
  assert.equal(receipt.status, 'incomplete');
});

test('recorder preserves real stdout, stderr, exit status, spawn errors and argument boundaries', t => {
  const directory = temporary(t);
  const { run, records } = recorder(directory, directory, process.env);
  const value = 'spaces; one argument';
  const child = path.join(directory, 'child.js');
  fs.writeFileSync(child,
    'process.stdout.write(process.argv[2]); process.stderr.write("diagnostic"); process.exitCode = 3;');
  const capture = run('process', process.execPath, [child, value]);
  assert.equal(capture.stdout, value, JSON.stringify(capture));
  assert.equal(capture.stderr, 'diagnostic');
  assert.equal(capture.exit_code, 3);
  assert.equal(fs.readFileSync(path.join(directory, 'process.stdout.txt'), 'utf8'), value);
  const missing = run('missing', path.join(directory, 'absent-interpreter'), []);
  assert.equal(missing.exit_code, null);
  assert.match(missing.error, /ENOENT/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(directory, 'commands.json'))).length, 2);
  assert.equal(records.length, 2);
});

test('preflight rejects a different Git head without changing its files and records the blocker', t => {
  const checkout = temporary(t);
  const output = path.join(temporary(t), 'new-evidence');
  assert.equal(spawnSync('git', ['init', '--quiet', checkout]).status, 0);
  assert.equal(spawnSync('git', ['-C', checkout, '-c', 'user.name=Replay test',
    '-c', 'user.email=replay@example.invalid', 'commit', '--quiet', '--allow-empty', '-m', 'fixture']).status, 0);
  assert.throws(() => replay({ checkout, output, python: process.execPath }), /historical head is required/);
  const manifest = JSON.parse(fs.readFileSync(path.join(output, 'manifest.json')));
  assert.equal(manifest.status, 'incomplete');
  assert.equal(manifest.commands[0].label, 'head');
  assert.equal(manifest.mutations.length, 0);
  assert.equal(fs.existsSync(path.join(checkout, plan.source)), false);
});

test('replay refuses the working repository and previously preserved output', t => {
  const output = temporary(t);
  assert.throws(() => replay({ checkout: path.resolve(__dirname, '..'), output, python: 'python3' }),
    /separate historical checkout/);
  assert.throws(() => replay({ checkout: temporary(t), output, python: 'python3' }),
    /never overwrite/);
});
