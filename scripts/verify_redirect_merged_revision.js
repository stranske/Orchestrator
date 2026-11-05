'use strict';

// Offline acceptance only. Never arm, screen, dispatch, or apply a live lane.
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');

// Some restricted hosts report EPERM from spawnSync even after a successful
// child exit. Use the asynchronous launcher, retaining real errors and bounds.
function runCommand(command, args, options) {
  return new Promise((resolve) => {
    const { input, ...settings } = options;
    let inputError;
    const child = execFile(command, args, {
      ...settings, encoding: settings.encoding || 'buffer',
    }, (error, stdout, stderr) => {
      const failure = error || inputError;
      const processError = failure && typeof failure.code !== 'number';
      resolve({
        status: failure ? (typeof failure.code === 'number' ? failure.code : null) : 0,
        signal: failure?.signal || null,
        error: processError ? { code: failure.code || (failure.killed ? 'ETIMEDOUT' : 'PROCESS_ERROR') } : undefined,
        stdout, stderr,
      });
    });
    child.stdin?.on('error', (error) => { inputError = error; });
    child.stdin?.end(input);
  });
}

const focusedFiles = [
  'tests/test_redirect_chain_gate.py',
  'tests/test_redirect_apply_prescreen.py',
];
const namedNodes = [
  `${focusedFiles[0]}::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine`,
  `${focusedFiles[1]}::test_redirect_apply_daily_cap_refuses_second_apply`,
];
// Include the complete source and test trees, including conftest and fixtures.
// No workflow or installed operational state is copied into the scratch tree.
const archivePaths = ['src', 'tests', 'pyproject.toml'];

async function verify({ repository, revision, python = 'python3', run = runCommand }) {
  const receipt = {
    observed_at: new Date().toISOString(),
    status: 'blocked',
    revision,
    historical_replay_receipt: 'not_established',
    operational_acceptance: 'not_established',
    executions: [],
  };
  if (!/^[a-f0-9]{40}$/.test(revision || '')) {
    receipt.reason = 'full_commit_sha_required';
    return receipt;
  }
  const git = (args) => run('git', ['-C', repository, ...args], {
    encoding: 'utf8', timeout: 30000, maxBuffer: 64 * 1024 * 1024,
  });
  const main = await git(['rev-parse', '--verify', 'refs/remotes/origin/main^{commit}']);
  if (main.status !== 0) {
    receipt.reason = 'merged_main_reference_unavailable';
    return receipt;
  }
  receipt.merged_main_sha = main.stdout.trim();
  if ((await git(['merge-base', '--is-ancestor', revision, receipt.merged_main_sha])).status !== 0) {
    receipt.reason = 'revision_not_on_merged_main';
    return receipt;
  }
  const archive = await run('git', ['-C', repository, 'archive', '--format=tar', revision, ...archivePaths], {
    timeout: 30000, maxBuffer: 64 * 1024 * 1024,
  });
  if (archive.status !== 0) {
    receipt.reason = 'revision_archive_failed';
    return receipt;
  }
  receipt.archive_paths = archivePaths;
  receipt.archive_sha256 = crypto.createHash('sha256').update(archive.stdout).digest('hex');
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-merged-revision-'));
  try {
    const checkout = path.join(scratch, 'checkout');
    const runtime = path.join(scratch, 'runtime');
    fs.mkdirSync(checkout);
    fs.mkdirSync(runtime);
    const extracted = await run('tar', ['-xf', '-', '-C', checkout], {
      input: archive.stdout, timeout: 30000,
    });
    if (extracted.status !== 0) {
      receipt.reason = 'revision_archive_extraction_failed';
      return receipt;
    }
    // Exclude ambient Python test options, source paths, and owner routing flags.
    const env = {
      PATH: process.env.PATH,
      LANG: process.env.LANG || 'C.UTF-8',
      ORCH_LOCAL_RUNTIME: runtime,
      ORCH_STATE_DIR: runtime,
      ORCH_FEEDBACK_DB: path.join(runtime, 'brain.db'),
      ORCH_REDIRECT_SHADOW_CORPUS: path.join(runtime, 'corpus.jsonl'),
      HANDOFF_DIR: path.join(runtime, 'handoff'),
      ORCH_REDIRECT_APPLY_BOOTSTRAP: '0',
      ORCH_CAPABILITY_HEARTBEATS: '0',
    };
    const execute = async (name, args) => {
      const result = await run(python, ['-B', ...args], {
        cwd: checkout, env, encoding: 'utf8', timeout: 60000, maxBuffer: 4 * 1024 * 1024,
      });
      const row = {
        name, argv: [python, '-B', ...args], exit_code: result.status,
        signal: result.signal || null, error_code: result.error?.code || null,
        stdout: result.stdout || '', stderr: result.stderr || '',
      };
      receipt.executions.push(row);
      return row;
    };
    const interpreter = await execute('interpreter', [
      '-c', 'import json, sys; print(json.dumps({"executable": sys.executable, "version": sys.version}))',
    ]);
    const named = await execute('named_tests', ['-m', 'pytest', ...namedNodes, '--color=no', '-v', '-m', 'not slow']);
    const focused = await execute('focused_files', ['-m', 'pytest', ...focusedFiles, '--color=no', '-v', '-m', 'not slow']);
    const replay = await execute('offline_replay', [
      'src/redirect_apply.py', '--replay-stalls', 'tests/fixtures/redirect_stalls',
    ]);
    const cleanExit = (row) => row.exit_code === 0 && !row.signal && !row.error_code;
    // A zero exit with no executed tests (or skips) cannot satisfy acceptance.
    const passed = (row, minimum) => {
      const summary = row.stdout.trim().split('\n').at(-1) || '';
      const count = summary.match(/\b(\d+) passed\b/);
      return cleanExit(row) && count && Number(count[1]) >= minimum
        && !/\b(?:skipped|failed|error)\b/i.test(summary);
    };
    // Counts alone can credit two unrelated tests or two cases from one file.
    // Bind successful execution to the requested node and file identities too.
    const passedNodes = (row) => Array.from(
      row.stdout.matchAll(/^(\S+) PASSED(?:\s|$)/gm), (match) => match[1],
    );
    const namedPassed = passedNodes(named);
    const focusedPassed = passedNodes(focused);
    const requiredNodesPassed = namedNodes.every((node) => namedPassed.some(
      (actual) => actual === node || actual.startsWith(`${node}[`),
    ));
    const requiredFilesPassed = focusedFiles.every((file) => focusedPassed.some(
      (actual) => actual.startsWith(`${file}::`),
    ));
    const authorized = replay.stdout.trim().match(/^authorized (\d+)\/12 with named agents \(dry-run\)$/);
    receipt.authorized = authorized ? Number(authorized[1]) : null;
    receipt.status = cleanExit(interpreter) && passed(named, 2) && passed(focused, 2)
      && requiredNodesPassed && requiredFilesPassed
      && cleanExit(replay) && receipt.authorized >= 9 && receipt.authorized <= 12
      ? 'verified' : 'blocked';
    if (receipt.status !== 'verified') receipt.reason = 'focused_execution_or_replay_not_verified';
    return receipt;
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

async function main(args = process.argv.slice(2)) {
  if (args.length < 1 || args.length > 2) {
    console.error('Usage: node scripts/verify_redirect_merged_revision.js <full-merged-sha> [python-path]');
    return 2;
  }
  const receipt = await verify({ repository: path.resolve(__dirname, '..'), revision: args[0], python: args[1] });
  console.log(JSON.stringify(receipt, null, 2));
  return receipt.status === 'verified' ? 0 : 1;
}

if (require.main === module) main().then((code) => { process.exitCode = code; });
module.exports = { verify, main, runCommand };
