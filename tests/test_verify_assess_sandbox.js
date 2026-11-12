'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { deadlineMs, main, mutate, requireVerdict, runPython, target, verify } = require('../scripts/verify_assess_sandbox');

function verdict(broken = false) {
  return {
    status: broken ? 1 : 0, signal: null, error: null, elapsed_ms: 123,
    stdout: `${target} ${broken ? 'FAILED' : 'PASSED'} [100%]\n`
      + (broken ? 'E   AssertionError: workspace-write and the fallback preamble are absent\n' : '')
      + `============================== 1 ${broken ? 'failed' : 'passed'} in 0.01s ==============================\n`,
    stderr: '',
  };
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'assess-sandbox-runner-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'src'));
  fs.mkdirSync(path.join(root, 'tests'));
  const source = 'def _sandbox_has_network(offload_argv: list[str], *, cwd: str) -> dict:\n'
    + '    """Probe docstring."""\n    return {"has_network": False}\n';
  fs.writeFileSync(path.join(root, 'src/dispatcher.py'), source);
  fs.writeFileSync(path.join(root, 'tests/test_dispatcher_offload.py'), '# fixture only\n');
  return { root, source };
}

test('mutation inserts availability before the body and preserves LF and CRLF syntax', () => {
  for (const newline of ['\n', '\r\n']) {
    const source = ['def _sandbox_has_network(offload_argv: list[str], *, cwd: str) -> dict:',
      '    """Docstring."""', '    return {}', ''].join(newline);
    const mutant = mutate(source);
    assert.equal(mutant, source.replace(`:${newline}`, `:${newline}    return True  # deliberate availability mutation${newline}`));
    assert.throws(() => mutate(''), /signature must occur once/);
    assert.throws(() => mutate(source + source), /signature must occur once/);
  }
});

test('valid evidence includes sandbox parameter IDs containing spaces', () => {
  requireVerdict(verdict(), false);
  requireVerdict(verdict(true), true);
  const spaced = verdict();
  spaced.stdout = spaced.stdout.replace(target,
    'tests/test_dispatcher_offload.py::test_sandbox_non_network_rejections_never_widen[HTTP 401: Bad credentials-auth_or_api]');
  requireVerdict(spaced, false);
});

test('empty, skipped, slow, duplicate and unrelated results cannot establish acceptance', () => {
  for (const broken of [false, true]) {
    const valid = verdict(broken);
    for (const invalid of [
      { ...valid, status: null, error: 'spawn failed' },
      { ...valid, status: 1, stdout: '', stderr: 'No module named pytest' },
      { ...valid, signal: 'SIGKILL' },
      { ...valid, elapsed_ms: deadlineMs },
      { ...valid, elapsed_ms: undefined },
      { ...valid, stdout: '================ 0 passed in 0.01s ================\n' },
      { ...valid, stdout: valid.stdout.replace(target, 'tests/test_wrong.py::test_wrong') },
      { ...valid, stdout: valid.stdout.replace(/1 (passed|failed) in/, '1 $1, 1 skipped in') },
      { ...valid, stdout: valid.stdout + `${target} PASSED [100%]\n` },
      { ...valid, stdout: valid.stdout.replace(/1 (passed|failed) in/, '2 $1 in') },
      { ...valid, stdout: valid.stdout.replace(/PASSED|FAILED/, 'SKIPPED') },
      { ...valid, stdout: valid.stdout.replace(/=+ .+ in .+ =+\n/, '') },
    ]) {
      assert.throws(() => requireVerdict(invalid, broken), JSON.stringify(invalid));
    }
  }
  const red = verdict(true);
  assert.throws(() => requireVerdict({ ...red,
    stdout: red.stdout.replace('AssertionError: workspace-write and the fallback preamble are absent',
      'AssertionError: unrelated'),
  }, true));
});

test('baseline, isolated mutation and original rerun retain commands and source bindings', async (t) => {
  const { root, source } = fixture(t);
  const retained = [];
  let mutantPath;
  const run = async (python, args, options) => {
    assert.equal(python, process.execPath);
    assert.equal(options.cwd, root);
    assert.equal(args[args.indexOf('-m', 3) + 1], 'not slow');
    assert.equal(fs.readFileSync(path.join(root, 'src/dispatcher.py'), 'utf8'), source);
    const broken = args.includes('-o');
    if (broken) {
      const roots = [...args.at(-1).matchAll(/"([^"]+)"/g)].map((match) => match[1]);
      mutantPath = path.join(roots[0], 'dispatcher.py');
      assert.equal(fs.readFileSync(mutantPath, 'utf8'), mutate(source));
      assert.equal(roots[1], path.join(root, 'src'));
      assert.ok(args.includes(target));
    } else {
      assert.ok(args.includes('tests/test_dispatcher_offload.py'));
    }
    return verdict(broken);
  };
  const hashes = await verify(root, process.execPath, (row) => retained.push(row), run);
  assert.deepEqual(retained.map((row) => row.phase), ['baseline', 'deliberate-break', 'restored']);
  assert.ok(retained.every((row) => row.source_sha256 === hashes.source_sha256
    && row.test_sha256 === hashes.test_sha256));
  assert.notEqual(retained[1].mutant_sha256, hashes.source_sha256);
  assert.ok(!fs.existsSync(mutantPath));
});

test('a blocked baseline stops immediately and retains its failing output', async (t) => {
  const { root } = fixture(t);
  const retained = [];
  let calls = 0;
  await assert.rejects(verify(root, process.execPath, (row) => retained.push(row), async () => {
    calls += 1;
    return { status: 1, elapsed_ms: 1, stderr: 'No module named pytest' };
  }), /No module named pytest/);
  assert.equal(calls, 1);
  assert.equal(retained[0].phase, 'baseline');
});

test('failed mutation validation cleans temporary sources and never edits the checkout', async (t) => {
  const { root, source } = fixture(t);
  let scratch;
  await assert.rejects(verify(root, process.execPath, () => {}, async (python, args) => {
    if (!args.includes('-o')) return verdict();
    scratch = args.at(-1).match(/"([^"]+)"/)[1];
    throw new Error('runner interrupted');
  }), /runner interrupted/);
  assert.ok(!fs.existsSync(scratch));
  assert.equal(fs.readFileSync(path.join(root, 'src/dispatcher.py'), 'utf8'), source);
  await assert.rejects(verify(root, 'python3', () => {}), /absolute interpreter/);
});

test('real child execution reports exit status and missing interpreters', async () => {
  const result = await runPython(process.env.PYTHON || 'python3',
    ['-c', 'import sys; print("child output"); sys.exit(3)'], {});
  assert.equal(result.status, 3);
  assert.equal(result.stdout, 'child output\n');
  assert.equal(result.error, null);
  const absent = await runPython('/nonexistent/assess-sandbox-python', [], {});
  assert.equal(absent.status, null);
  assert.match(absent.error, /ENOENT/);
});

test('CLI retains an unverified manifest for missing tools and refuses evidence overwrite', async (t) => {
  const { root } = fixture(t);
  const evidence = path.join(root, 'evidence');
  const previous = process.env.PYTHON;
  t.after(() => {
    if (previous === undefined) delete process.env.PYTHON;
    else process.env.PYTHON = previous;
  });
  process.env.PYTHON = '/nonexistent/assess-sandbox-python';
  await assert.rejects(main([evidence]), /ENOENT/);
  const bytes = fs.readFileSync(path.join(evidence, 'result.json'));
  const report = JSON.parse(bytes);
  assert.equal(report.status, 'unverified');
  assert.equal(report.runs.length, 1);
  assert.equal(report.runs[0].phase, 'baseline');
  await assert.rejects(main([evidence]), /EEXIST/);
  assert.deepEqual(fs.readFileSync(path.join(evidence, 'result.json')), bytes);
});
