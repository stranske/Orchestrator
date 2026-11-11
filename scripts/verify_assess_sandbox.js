'use strict';

// Retain the assess fallback's baseline, deliberate break and restored outputs.
// Mutate only a temporary import copy; the checkout remains usable if interrupted.
const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const testFile = 'tests/test_dispatcher_offload.py';
const target = `${testFile}::test_sandbox_network_failure_falls_back_to_workspace_write`;
const deadlineMs = 120000;
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

function mutate(source) {
  const signature = /^def _sandbox_has_network\(offload_argv: list\[str\], \*, cwd: str\) -> dict:\r?\n/gm;
  assert.equal([...source.matchAll(signature)].length, 1, 'probe signature must occur once');
  const newline = source.includes('\r\n') ? '\r\n' : '\n';
  return source.replace(signature, (line) =>
    `${line}    return True  # deliberate availability mutation${newline}`);
}

function runPython(python, args, options) {
  const started = performance.now();
  return new Promise((resolve) => {
    execFile(python, args, {
      ...options, encoding: 'utf8', timeout: deadlineMs, killSignal: 'SIGKILL',
      maxBuffer: 4 * 1024 * 1024,
    }, (error, stdout, stderr) => resolve({
      status: error ? (Number.isInteger(error.code) ? error.code : null) : 0,
      signal: error?.signal || null,
      error: error && !Number.isInteger(error.code) ? error.message : null,
      elapsed_ms: performance.now() - started,
      stdout, stderr,
    }));
  });
}

function requireVerdict(result, broken) {
  const output = `${result.stdout || ''}\n${result.stderr || ''}`;
  assert.ok(!result.error && !result.signal, result.error || result.signal || output);
  assert.equal(result.status, broken ? 1 : 0, output);
  assert.ok(Number.isFinite(result.elapsed_ms) && result.elapsed_ms >= 0
    && result.elapsed_ms < deadlineMs, 'sandbox suite must finish within two minutes');
  const rows = [...output.matchAll(/^(tests\/[^\r\n]+?::[^\r\n]+?) (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b/gm)];
  assert.ok(rows.length > 0, 'no sandbox test verdicts collected');
  assert.equal(new Set(rows.map((row) => row[1])).size, rows.length, 'duplicate test verdicts');
  assert.ok(rows.every((row) => row[1].startsWith(`${testFile}::test_sandbox_`)
    && row[2] === (broken ? 'FAILED' : 'PASSED')), output);
  const summaries = [...output.matchAll(/^=+ (.+ in [^\r\n]+) =+\s*$/gm)];
  assert.equal(summaries.length, 1, 'missing or ambiguous pytest terminal summary');
  const summary = summaries[0][1];
  const counts = [...summary.matchAll(/\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b/g)];
  assert.deepEqual(counts.filter((count) => count[2] !== 'deselected')
    .map((count) => [Number(count[1]), count[2]]), [[rows.length, broken ? 'failed' : 'passed']], summary);
  if (broken) {
    assert.equal(rows.length, 1, output);
    assert.equal(rows[0][1], target, output);
    assert.match(output, /AssertionError: workspace-write and the fallback preamble are absent/);
  }
}

async function verify(root, python, retain, run = runPython) {
  assert.ok(typeof python === 'string' && path.isAbsolute(python),
    'PYTHON must select an absolute interpreter');
  const sourcePath = path.join(root, 'src/dispatcher.py');
  const testPath = path.join(root, testFile);
  const original = fs.readFileSync(sourcePath);
  const originalTest = fs.readFileSync(testPath);
  const mutant = mutate(original.toString('utf8'));
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'assess-sandbox-mutation-'));
  try {
    fs.writeFileSync(path.join(scratch, 'dispatcher.py'), mutant);
    for (const phase of ['baseline', 'deliberate-break', 'restored']) {
      const broken = phase === 'deliberate-break';
      const args = ['-B', '-m', 'pytest', broken ? target : testFile,
        '-k', 'sandbox', '-m', 'not slow', '-v', '--tb=short', '--color=no',
        '-p', 'no:cacheprovider'];
      if (broken) {
        // Override pytest's configured src-first path so it imports the mutant.
        // Only dispatcher is copied; its sibling imports still use real src/.
        args.push('-o', `pythonpath=${JSON.stringify(scratch)} ${JSON.stringify(path.join(root, 'src'))} .`);
      }
      const result = await run(python, args, {
        cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
      });
      retain({ phase, recorded_at: new Date().toISOString(), command: [python, ...args],
        source_sha256: digest(original), test_sha256: digest(originalTest),
        ...(broken ? { mutant_sha256: digest(mutant) } : {}), ...result });
      requireVerdict(result, broken);
    }
    return { source_sha256: digest(original), test_sha256: digest(originalTest) };
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
    assert.deepEqual(fs.readFileSync(sourcePath), original, 'checkout source changed during verification');
    assert.deepEqual(fs.readFileSync(testPath), originalTest, 'checkout test changed during verification');
  }
}

async function main(argv = process.argv.slice(2)) {
  assert.equal(argv.length, 1, 'usage: PYTHON=/absolute/python node scripts/verify_assess_sandbox.js NEW_EVIDENCE_DIR');
  const destination = path.resolve(argv[0]);
  fs.mkdirSync(destination); // Refuse to overwrite retained evidence.
  const report = { recorded_at: new Date().toISOString(), status: 'unverified', runs: [] };
  try {
    Object.assign(report, await verify(path.resolve(__dirname, '..'),
      process.env.PYTHON || '/opt/anaconda3/bin/python3', (run) => {
        report.runs.push(run);
        fs.writeFileSync(path.join(destination, `${run.phase}.json`), `${JSON.stringify(run, null, 2)}\n`);
      }));
    report.status = 'verified';
  } catch (error) {
    report.reason = error.message;
    throw error;
  } finally {
    fs.writeFileSync(path.join(destination, 'result.json'), `${JSON.stringify(report, null, 2)}\n`);
  }
  console.log('Sandbox baseline passed; fallback mutation detected; original suite passed again.');
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`Assess sandbox verification unverified: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { deadlineMs, main, mutate, requireVerdict, runPython, target, verify };
