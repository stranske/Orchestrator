'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { execute } = require('../scripts/run_adjudicator_acceptance');
const { runVerify } = require('../scripts/run_provision_verify');

const python = process.env.PYTHON;
assert.ok(path.isAbsolute(python || ''), 'Select an absolute PYTHON interpreter');

function fixture(t, source = 'print("VERIFIED fixture")\n') {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'provision-verify-test-'));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, 'checkout');
  fs.mkdirSync(path.join(root, 'src'), { recursive: true });
  fs.writeFileSync(path.join(root, 'src/verify.py'), source);
  fs.writeFileSync(path.join(root, '.verify-floor.json'), '{"collected":1}\n');
  function git(...args) {
    const result = execute('git', ['-C', root, ...args], { cwd: root });
    assert.equal(result.exit_code, 0, result.stderr.toString());
    return result.stdout.toString().trim();
  }
  git('init', '--initial-branch=main');
  git('add', '.');
  const commit = () => git('-c', 'user.name=Verifier Test', '-c', 'user.email=test@example.com',
    '-c', 'commit.gpgsign=false', 'commit', '-am', 'fixture');
  commit();
  git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  return { options: { root, python, revision: git('rev-parse', 'HEAD'),
    output: path.join(base, 'receipt') }, git, commit };
}

test('retains complete real Python streams, selected revision and both floor blobs', (t) => {
  const { options } = fixture(t, 'import os, sys\n'
    + 'assert os.environ["PYTEST_ADDOPTS"] == \'-m "not slow"\'\n'
    + 'assert os.environ["ORCH_STATE_DIR"] != os.environ["ORCH_LOCAL_RUNTIME"]\n'
    + 'print("x" * 1100000)\nprint("verifier diagnostic", file=sys.stderr)\n');
  const receipt = runVerify(options);
  assert.equal(receipt.passed, true);
  assert.equal(receipt.revision, options.revision);
  assert.deepEqual(receipt.command, [python, 'src/verify.py']);
  assert.equal(receipt.deployment_acceptance_verified, false);
  assert.equal(receipt.floor.comparison_exit_code, 0);
  assert.equal(receipt.floor.delivered_sha256, receipt.floor.origin_main_sha256);
  assert.deepEqual(fs.readFileSync(path.join(options.output, 'delivered-floor.json')),
    fs.readFileSync(path.join(options.output, 'origin-main-floor.json')));
  assert.equal(fs.readFileSync(path.join(options.output, 'verify.stdout.log'), 'utf8'),
    'x'.repeat(1100000) + '\n');
  assert.equal(fs.readFileSync(path.join(options.output, 'verify.stderr.log'), 'utf8'),
    'verifier diagnostic\n');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))), receipt);
  assert.equal(fs.statSync(path.join(options.output, 'receipt.json')).mode & 0o777, 0o600);
  assert.equal(fs.existsSync(path.dirname(receipt.runtime_overrides.ORCH_STATE_DIR)), false);
  assert.throws(() => runVerify(options), /EEXIST/);
});

test('retains the missing-pytest failure without mistaking matching floors for acceptance', (t) => {
  // -S excludes site packages even on a machine that has pytest installed.
  const { options } = fixture(t, 'import subprocess, sys\n'
    + 'p = subprocess.run([sys.executable, "-S", "-m", "pytest"])\n'
    + 'raise SystemExit(p.returncode)\n');
  const receipt = runVerify(options);
  assert.equal(receipt.passed, false);
  assert.equal(receipt.execution.exit_code, 1);
  assert.equal(receipt.floor.matches, true);
  assert.match(fs.readFileSync(path.join(options.output, 'verify.stderr.log'), 'utf8'),
    /No module named pytest/);
});

test('the real verifier pytest stage preserves a missing-interpreter-prerequisite failure', (t) => {
  const modules = path.resolve(__dirname, '../src');
  const { options } = fixture(t, 'import json, sys\nfrom pathlib import Path\n'
    + `sys.path.insert(0, ${JSON.stringify(modules)})\n`
    + 'import verify\n'
    + 'verify.ROOT = Path.cwd()\n'
    // Exercise the production subprocess and parser. Only exclude installed
    // packages at launch, so this regression also works when CI has pytest.
    + 'verify.child_argv = lambda argv: [argv[0], "-S", *argv[1:]]\n'
    + 'result = verify.run_pytest(junit=Path.cwd() / "pytest.xml")\n'
    + 'print(json.dumps(result))\n'
    + 'raise SystemExit(result["returncode"])\n');
  const receipt = runVerify(options);
  const result = JSON.parse(fs.readFileSync(path.join(options.output, 'verify.stdout.log'), 'utf8'));
  assert.equal(result.returncode, 1);
  assert.match(result.output, /No module named pytest/);
  assert.equal(result.collected, 0);
  assert.equal(result.passed, 0);
  assert.equal(result.test_inventory, null);
  assert.match(result.inventory_error, /FileNotFoundError.*pytest\.xml/);
  assert.deepEqual(result.verdicts, {});
  assert.equal(receipt.execution.exit_code, result.returncode);
  assert.equal(receipt.floor.matches, true);
  assert.equal(receipt.passed, false);
  assert.equal(receipt.deployment_acceptance_verified, false);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))), receipt);
});

test('runs the verifier and retains differing floor bytes without changing either floor', (t) => {
  const { options, git, commit } = fixture(t);
  const current = Buffer.from('{"collected":2}\n');
  fs.writeFileSync(path.join(options.root, '.verify-floor.json'), current);
  commit();
  options.revision = git('rev-parse', 'HEAD');
  const receipt = runVerify(options);
  assert.equal(receipt.execution.exit_code, 0);
  assert.equal(receipt.passed, false);
  assert.equal(receipt.floor.matches, false);
  assert.equal(receipt.floor.comparison_exit_code, 1);
  assert.notEqual(receipt.floor.delivered_sha256, receipt.floor.origin_main_sha256);
  assert.deepEqual(fs.readFileSync(path.join(options.root, '.verify-floor.json')), current);
  assert.deepEqual(fs.readFileSync(path.join(options.output, 'delivered-floor.json')), current);
  assert.equal(fs.readFileSync(path.join(options.output, 'origin-main-floor.json'), 'utf8'),
    '{"collected":1}\n');
});

test('rejects a successful command as delivered evidence if source changes during the run', (t) => {
  const { options } = fixture(t, 'from pathlib import Path\n'
    + 'Path("src/verify.py").write_text("# changed\\n")\n');
  const receipt = runVerify(options);
  assert.equal(receipt.execution.exit_code, 0);
  assert.equal(receipt.passed, false);
  assert.match(receipt.checkout_status_after, /src\/verify.py/);
});

test('retains partial output and signal on timeout', (t) => {
  const { options } = fixture(t, 'import time\nprint("started", flush=True)\ntime.sleep(60)\n');
  const receipt = runVerify({ ...options, timeout: 1000 });
  assert.equal(receipt.passed, false);
  assert.equal(receipt.execution.error_code, 'ETIMEDOUT');
  assert.ok(receipt.execution.signal);
  assert.equal(fs.readFileSync(path.join(options.output, 'verify.stdout.log'), 'utf8'), 'started\n');
});

test('retains interpreter launch failure as a failed receipt', (t) => {
  const { options } = fixture(t);
  const receipt = runVerify({ ...options, python: path.join(path.dirname(options.root), 'missing-python') });
  assert.equal(receipt.passed, false);
  assert.equal(receipt.execution.error_code, 'ENOENT');
  assert.equal(receipt.execution.exit_code, null);
  assert.equal(fs.readFileSync(path.join(options.output, 'verify.stdout.log')).length, 0);
  assert.equal(JSON.parse(fs.readFileSync(path.join(options.output, 'receipt.json'))).passed, false);
});

test('inherited explicit runtime paths and live-state opt-in stay outside this run', (t) => {
  const inherited = ['ORCH_CAPABILITIES_PATH', 'ORCH_FEEDBACK_DB', 'ORCH_VERIFY_LIVE_STATE'];
  const old = inherited.map((key) => process.env[key]);
  t.after(() => inherited.forEach((key, i) => {
    if (old[i] === undefined) delete process.env[key];
    else process.env[key] = old[i];
  }));
  process.env.ORCH_CAPABILITIES_PATH = '/unrelated/capabilities.json';
  process.env.ORCH_FEEDBACK_DB = '/unrelated/feedback.db';
  process.env.ORCH_VERIFY_LIVE_STATE = '1';
  const { options } = fixture(t, 'import os\nfrom pathlib import Path\n'
    + 'runtime = Path(os.environ["ORCH_LOCAL_RUNTIME"])\n'
    + 'for key in ["ORCH_CAPABILITIES_PATH", "ORCH_FEEDBACK_DB"]:\n'
    + '    assert Path(os.environ[key]).is_relative_to(runtime)\n'
    + 'assert os.environ["ORCH_VERIFY_LIVE_STATE"] == "0"\n');
  assert.equal(runVerify(options).passed, true);
});

test('uncommitted test inputs cannot yield delivered-checkout acceptance', (t) => {
  const { options } = fixture(t);
  fs.mkdirSync(path.join(options.root, 'tests'));
  fs.writeFileSync(path.join(options.root, 'tests/conftest.py'), '# uncommitted input\n');
  const receipt = runVerify(options);
  assert.equal(receipt.execution.exit_code, 0);
  assert.equal(receipt.passed, false);
  assert.match(receipt.checkout_status_before, /tests\/conftest.py/);
});

test('Git-ignored Python inputs cannot masquerade as committed delivery', (t) => {
  const { options } = fixture(t);
  fs.writeFileSync(path.join(options.root, '.gitignore'), 'tests/\n');
  fs.mkdirSync(path.join(options.root, 'tests'));
  fs.writeFileSync(path.join(options.root, 'tests/conftest.py'), '# ignored input\n');
  const receipt = runVerify(options);
  assert.equal(receipt.execution.exit_code, 0);
  assert.equal(receipt.passed, false);
  assert.match(receipt.checkout_status_before, /!! tests\/conftest.py/);
});

test('refuses wrong revisions and output within the checkout before invoking Python', (t) => {
  const { options } = fixture(t);
  assert.throws(() => runVerify({ ...options, revision: '0'.repeat(40) }), /selected revision/);
  assert.throws(() => runVerify({ ...options, output: path.join(options.root, 'receipt') }),
    /outside the delivered checkout/);
  assert.equal(fs.existsSync(options.output), false);
});
