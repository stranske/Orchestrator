'use strict';

// Run the issue #424 acceptance tests against private source copies. A failed
// collection, timeout, or missing pytest is a blocker, never mutation evidence.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const testFile = 'tests/test_redirect_chain_backend_and_agent.py';
const names = [
  'test_sweep_backend_is_router_chosen_when_unset_or_auto',
  'test_a_redirect_naming_no_agent_gets_a_router_agent_and_no_placeholder',
  'test_plan_refuses_an_applyable_action_without_an_agent',
  'test_role_run_records_source_and_report_state',
];
const counts = [4, 6, 8, 3];

const mutations = [
  {
    label: 'cursor fallback',
    tests: [0],
    edits: [{
      file: 'src/redirect_sweep.py',
      before: 'return None if not selected or selected.strip().lower() == "auto" else selected.strip()',
      after: 'return "cursor" if not selected or selected.strip().lower() == "auto" else selected.strip()',
    }],
  },
  {
    label: 'missing worker and placeholder',
    tests: [1, 2],
    edits: [
      {
        file: 'src/roles.py',
        before: 'and not selected_agent:\n',
        after: 'and not selected_agent and proposal is None:\n',
      },
      {
        file: 'src/redirect_plan.py',
        before: '    if not next_agent or not next_agent.strip() or "<" in next_agent or ">" in next_agent:\n'
          + '        raise MissingRedirectAgent("redirect/decompose requires a concrete next agent")\n'
          + '    selected_agent = next_agent.strip()',
        after: '    selected_agent = next_agent or "<next-agent>"',
      },
    ],
  },
];

function replaceOnce(text, before, after) {
  assert.equal(text.split(before).length - 1, 1, `mutation anchor must occur once: ${before}`);
  return text.replace(before, after);
}

function withMutation(root, mutation, run) {
  const originals = new Map();
  try {
    for (const edit of mutation.edits) {
      const file = path.join(root, edit.file);
      const original = fs.readFileSync(file);
      originals.set(file, original);
      fs.writeFileSync(file, replaceOnce(original.toString('utf8'), edit.before, edit.after));
    }
    return run();
  } finally {
    for (const [file, original] of originals) {
      fs.writeFileSync(file, original);
      assert.deepEqual(fs.readFileSync(file), original, `restoration changed bytes: ${file}`);
    }
  }
}

function runPytest(root, indices) {
  return spawnSync(process.env.PYTHON || 'python3', [
    '-B', '-m', 'pytest', ...indices.map((i) => `${testFile}::${names[i]}`),
    '-v', '-m', 'not slow', '-p', 'no:cacheprovider',
  ], {
    cwd: root,
    env: { ...process.env, ORCH_CAPABILITY_HEARTBEATS: '0', PYTHONDONTWRITEBYTECODE: '1' },
    encoding: 'utf8', timeout: 60000, maxBuffer: 4 * 1024 * 1024,
  });
}

function requireVerdict(result, indices, failed) {
  const output = (result.stdout || '') + (result.stderr || '');
  assert.ok(!result.error, `${result.error?.message}\n${output}`);
  assert.equal(result.status, failed ? 1 : 0, output);
  const expected = indices.reduce((n, i) => n + counts[i], 0);
  const verdict = failed ? 'FAILED' : 'PASSED';
  const rows = output.split('\n').filter((line) => line.startsWith(`${testFile}::`));
  assert.equal(rows.length, expected, output);
  for (const i of indices) {
    const matching = rows.filter((line) => line.startsWith(`${testFile}::${names[i]}[`));
    assert.equal(matching.length, counts[i], output);
    assert.ok(matching.every((line) => line.includes(` ${verdict} `)), output);
  }
  assert.match(output, new RegExp(`\\b${expected} ${failed ? 'failed' : 'passed'}\\b`));
  assert.doesNotMatch(output, /\b\d+ (?:error|skipped|xfailed|xpassed|deselected)/i);
  return `${expected} ${failed ? 'failed' : 'passed'}`;
}

function verify(root, run = runPytest) {
  const results = [`baseline: ${requireVerdict(run(root, [0, 1, 2, 3]), [0, 1, 2, 3], false)}`];
  for (const mutation of mutations) {
    const red = withMutation(root, mutation, () => (
      requireVerdict(run(root, mutation.tests), mutation.tests, true)
    ));
    const green = requireVerdict(run(root, mutation.tests), mutation.tests, false);
    results.push(`${mutation.label}: ${red}; restored: ${green}`);
  }
  results.push(`final: ${requireVerdict(run(root, [0, 1, 2, 3]), [0, 1, 2, 3], false)}`);
  return results;
}

function main() {
  const repo = path.resolve(__dirname, '..');
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-routing-mutations-'));
  try {
    // Keep mutation runs away from both the checkout and installed lane state.
    for (const entry of ['src', 'config', 'pyproject.toml', 'tests/conftest.py',
      testFile, 'tests/fixtures/redirect_stalls']) {
      fs.cpSync(path.join(repo, entry), path.join(sandbox, entry), {
        recursive: true,
        filter: (file) => !/(secret|token|credential|__pycache__)/i.test(path.basename(file)),
      });
    }
    console.log(verify(sandbox).join('\n'));
  } finally {
    fs.rmSync(sandbox, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`Redirect mutation verification blocked or failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { counts, names, mutations, replaceOnce, withMutation, requireVerdict, verify };
