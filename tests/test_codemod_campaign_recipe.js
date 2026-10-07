'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python3';
const campaignPath = process.env.CODEMOD_CAMPAIGN_TEST_INPUT
  || path.join(repo, 'campaigns/gitignore-caches-2026-10.json');
const campaign = JSON.parse(fs.readFileSync(campaignPath, 'utf8'));
const entries = ['.mypy_cache/', '.pytest_cache/', '.ruff_cache/', 'coverage.xml', '.coverage'];

test('campaign validates the exact fleet and add-only cache-ignore recipe', () => {
  assert.deepEqual(campaign.campaign.repos, [
    'stranske/Counter_Risk',
    'stranske/Manager-Database',
    'stranske/Inv-Man-Intake',
    'stranske/trip-planner',
    'stranske/learning-management-system',
    'stranske/Pension-Data',
  ]);
  assert.deepEqual(campaign.ignore_entries, entries);
  assert.equal(campaign.recipe.tool, 'custom');
  assert.equal(campaign.rollout.pr_strategy, 'per_repo');
  assert.equal(campaign.add_only, true);
  const result = spawnSync(python, [
    path.join(repo, 'src/codemod_lane.py'), '--validate', campaignPath, '--json',
  ], {
    env: { ...process.env, ORCH_CAPABILITY_HEARTBEATS: '0' },
    encoding: 'utf8', timeout: 10000,
  });
  assert.equal(result.status, 0, result.error?.message || result.stderr || result.stdout);
  assert.deepEqual(JSON.parse(result.stdout), { valid: true, errors: [] });
});

const parsed = spawnSync(python, [
  '-c', 'import json, shlex, sys; print(json.dumps(shlex.split(sys.argv[1])))',
  campaign.recipe.command_template,
], { encoding: 'utf8', timeout: 10000 });
assert.equal(parsed.status, 0, parsed.error?.message || parsed.stderr);
const command = JSON.parse(parsed.stdout);
assert.equal(command[0], 'python3');
command[0] = python;

const cases = [
  ['empty file', '', entries],
  ['existing LF entry', 'existing/\n.mypy_cache/\n', entries.slice(1)],
  ['existing CRLF entry', 'existing/\r\n.mypy_cache/\r\n', entries.slice(1)],
  ['entry without final newline', 'existing/\n.coverage', entries.slice(0, -1)],
  ['complete file', entries.join('\n') + '\n', []],
  ['whitespace decoy', ' .coverage \n', entries],
  ...['\r', '\v', '\f', '\x1c', '\x85', '\u2028', '\u2029'].map((separator) => [
    `embedded non-Git separator ${JSON.stringify(separator)}`,
    `# existing comment${separator}.coverage\n`,
    entries,
  ]),
];

for (const [name, content, missing] of cases) {
  test(`campaign recipe previews only missing entries: ${name}`, () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'codemod-recipe-'));
    try {
      const ignorePath = path.join(temporary, '.gitignore');
      const original = Buffer.from(content);
      fs.writeFileSync(ignorePath, original);
      const result = spawnSync(command[0], command.slice(1), {
        cwd: temporary, encoding: 'utf8', timeout: 10000,
      });
      assert.equal(result.status, 0, result.error?.message || result.stderr);
      assert.deepEqual(result.stdout.trim().split('\n').filter(Boolean), missing);
      assert.deepEqual(fs.readFileSync(ignorePath), original, 'preview changed .gitignore');
      assert.deepEqual(fs.readdirSync(temporary), ['.gitignore'], 'preview created extra files');
    } finally {
      fs.rmSync(temporary, { recursive: true, force: true });
    }
  });
}
