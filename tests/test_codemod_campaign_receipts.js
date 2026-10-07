'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const root = path.resolve(__dirname, '..');
const evidence = process.env.ORCH_TEST_CAMPAIGN_EVIDENCE ||
  path.join(root, 'docs/evidence/issue-435-campaign');
const read = (name) => JSON.parse(fs.readFileSync(path.join(evidence, name), 'utf8'));
const campaign = JSON.parse(fs.readFileSync(
  path.join(root, 'campaigns/gitignore-caches-2026-10.json'), 'utf8'));
const targets = new Map([
  ['stranske/Counter_Risk', [1145, 1146]],
  ['stranske/Manager-Database', [1755, 1756]],
  ['stranske/Inv-Man-Intake', [1005, 1006]],
  ['stranske/learning-management-system', [748, 749]],
  ['stranske/trip-planner', [1887, 1888]],
  ['stranske/Pension-Data', [948, 950]],
]);

function blobSha(content) {
  const bytes = Buffer.from(content, 'utf8');
  return crypto.createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
}

function merged(pr, repo, number) {
  assert.equal(pr.number, number);
  assert.equal(pr.url, `https://github.com/${repo}/pull/${number}`);
  assert.equal(pr.state, 'closed');
  assert.equal(pr.merged, true);
  assert.equal(pr.draft, false);
  for (const field of ['head_sha', 'merge_commit_sha']) {
    assert.match(pr[field], /^[0-9a-f]{40}$/);
  }
  assert.match(pr.merged_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.ok(Number.isFinite(Date.parse(pr.merged_at)));
}

test('closure receipt covers exactly the six filed campaign targets', () => {
  const summary = read('receipt.json');
  assert.equal(summary.issue, campaign.source_target);
  assert.equal(summary.campaign_id, campaign.campaign.id);
  assert.equal(summary.targets.length, targets.size);
  assert.deepEqual(summary.targets.map((row) => row.repo).sort(),
    [...targets.keys()].sort());
  assert.deepEqual([...targets.keys()].sort(), [...campaign.campaign.repos].sort());
  for (const row of summary.targets) {
    const [issue, pr] = targets.get(row.repo);
    assert.equal(row.issue, issue);
    assert.equal(row.merged_pr, pr);
    assert.equal(row.receipt, `${row.repo.split('/')[1]}.json`);
    const delivery = read(row.receipt);
    assert.equal(row.merged_at, delivery.pull_request.merged_at);
    assert.equal(row.merge_commit_sha, delivery.pull_request.merge_commit_sha);
  }
});

test('campaign rails have a merged source receipt', () => {
  const rails = read('rails.json');
  assert.equal(rails.repo, 'stranske/Orchestrator');
  merged(rails.pull_request, rails.repo, 502);
  assert.equal(read('receipt.json').rails_pr, `${rails.repo}#502`);
});

for (const [repo, [issueNumber, prNumber]] of targets) {
  test(`${repo} retains merged delivery and effective add-only ignores`, (t) => {
    const receipt = read(`${repo.split('/')[1]}.json`);
    assert.equal(receipt.repo, repo);
    assert.equal(receipt.campaign_id, campaign.campaign.id);
    assert.equal(receipt.issue.number, issueNumber);
    assert.equal(receipt.issue.url, `https://github.com/${repo}/issues/${issueNumber}`);
    assert.equal(receipt.issue.state, 'closed');
    assert.equal(receipt.issue.state_reason, 'completed');
    assert.ok(Number.isFinite(Date.parse(receipt.issue.closed_at)));
    merged(receipt.pull_request, repo, prNumber);
    assert.equal(receipt.comparison.head_sha, receipt.pull_request.merge_commit_sha);
    assert.match(receipt.comparison.base_sha, /^[0-9a-f]{40}$/);
    assert.equal(receipt.comparison.url,
      `https://github.com/${repo}/compare/${receipt.comparison.base_sha}...` +
      receipt.pull_request.merge_commit_sha);

    const ignore = receipt.gitignore;
    const original = ignore.before.content;
    const delivered = original + ignore.appended_text;
    assert.equal(blobSha(original), ignore.before.sha, 'original bytes match the GitHub blob');
    assert.equal(blobSha(delivered), ignore.after.sha, 'delivery bytes match the merged blob');
    assert.equal(ignore.before.url,
      `https://github.com/${repo}/blob/${receipt.comparison.base_sha}/.gitignore`);
    assert.equal(ignore.after.url,
      `https://github.com/${repo}/blob/${receipt.pull_request.merge_commit_sha}/.gitignore`);
    assert.equal(ignore.original_bytes_preserved, true);
    const originalLines = original.split(/\r?\n/);
    const missing = campaign.ignore_entries.filter((entry) =>
      !originalLines.includes(entry) && !originalLines.includes(`/${entry}`));
    assert.deepEqual(ignore.appended_entries, missing);
    assert.deepEqual(ignore.appended_text.trimEnd().split(/\r?\n/), missing);
    assert.equal(ignore.additions, missing.length);
    assert.equal(ignore.deletions, 0);
    const file = receipt.comparison.files.find((row) => row.filename === '.gitignore');
    assert.equal(file.additions, missing.length);
    assert.equal(file.deletions, 0);
    assert.deepEqual(receipt.comparison.files.map((row) => row.filename).sort(),
      repo === 'stranske/learning-management-system'
        ? ['.gitignore', 'tests/test_repo_hygiene.py'] : ['.gitignore']);
    assert.equal(receipt.measurements.durable, null);
    assert.equal(receipt.measurements.cost_usd, null);
    assert.equal(receipt.measurements.follow_up,
      'https://github.com/stranske/Orchestrator/pull/513');

    const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-campaign-ignore-'));
    t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
    const env = { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' };
    for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
      'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG_COUNT']) {
      delete env[key];
    }
    const git = (...args) => {
      const result = spawnSync('git', ['-c', 'core.excludesFile=/dev/null', ...args],
        { cwd, env, encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      return result.stdout;
    };
    git('init', '-q');
    fs.writeFileSync(path.join(cwd, '.gitignore'), delivered);
    const probes = ['.mypy_cache/probe', '.pytest_cache/probe', '.ruff_cache/probe',
      'coverage.xml', '.coverage'];
    assert.deepEqual(git('check-ignore', '--no-index', '--', ...probes).trim().split('\n'), probes);
  });
}
