'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python3';
const campaign = JSON.parse(fs.readFileSync(
  path.join(repo, 'campaigns/gitignore-caches-2026-10.json'), 'utf8'));
const title = '[P2] Ignore tool caches and coverage artifacts (fleet codemod campaign)';

// Exercise the real CLI and target issue-format subprocess. The only substituted
// boundary is gh, whose requests and effects stay in a temporary directory.
const fakeGh = `#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const args = process.argv.slice(2);
const file = process.env.ORCH_TEST_GH_STATE;
const state = JSON.parse(fs.readFileSync(file, 'utf8'));
state.calls.push(args);
const value = (flag) => args[args.indexOf(flag) + 1];
const field = (name) => args.find((arg) => arg.startsWith(name + '='))?.slice(name.length + 1);
let result;
try {
  if (args[0] === 'api' && args[1].includes('/contents/')) {
    const [repo, name] = args[1].slice('repos/'.length).split('/contents/');
    const content = name === '.gitignore' ? state.ignores[repo] : state.files[name];
    if (typeof content !== 'string') throw new Error('unexpected file ' + args[1]);
    result = { encoding: 'base64', content: Buffer.from(content).toString('base64') };
  } else if (args[0] === 'issue' && args[1] === 'list') {
    const issue = state.issues[value('--repo')];
    result = issue ? [issue] : [];
  } else if (args[0] === 'label' && args[1] === 'list') {
    result = [];
  } else if (args[0] === 'api' && args[1].endsWith('/labels')) {
    result = { name: field('name') };
  } else if (args[0] === 'api' && args[1].endsWith('/comments')) {
    if (state.failLink) throw new Error('temporary parent link failure');
    state.comments.push({ endpoint: args[1], body: field('body') });
    result = { id: state.comments.length };
  } else if (args[0] === 'api' && args[1].endsWith('/issues')) {
    const repo = args[1].slice('repos/'.length, -'/issues'.length);
    if (state.issues[repo]) throw new Error('duplicate issue creation');
    const number = Object.keys(state.issues).length + 10;
    const url = 'https://github.com/' + repo + '/issues/' + number;
    state.issues[repo] = { number, url, state: 'OPEN', body: field('body'),
      title: field('title'), labels: [{ name: field('labels[]') }] };
    result = { number, html_url: url };
  } else {
    throw new Error('unexpected GitHub request ' + JSON.stringify(args));
  }
  fs.writeFileSync(file, JSON.stringify(state));
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  fs.writeFileSync(file, JSON.stringify(state));
  process.stderr.write(error.message);
  process.exitCode = 1;
}
`;

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codemod-campaign-cli-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const bin = path.join(root, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'gh'), fakeGh, { mode: 0o755 });
  const stateFile = path.join(root, 'github.json');
  const campaignFile = path.join(root, 'campaign.json');
  fs.writeFileSync(campaignFile, JSON.stringify(campaign));
  fs.writeFileSync(stateFile, JSON.stringify({
    calls: [], issues: {}, comments: [],
    ignores: Object.fromEntries(campaign.campaign.repos.map((name) =>
      [name, '# Keep existing rules\nexisting/\n!keep/\n.mypy_cache/\n'])),
    files: Object.fromEntries(['.github/scripts/issue_format.py',
      'docs/AGENT_ISSUE_FORMAT.md', 'README.md'].map((name) =>
      [name, fs.readFileSync(path.join(repo, name), 'utf8')])),
  }));
  const env = {
    PATH: bin + path.delimiter + process.env.PATH,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_TEST_GH_STATE: stateFile,
    ORCH_STATE_DIR: path.join(root, 'state'),
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_FEEDBACK_DB: path.join(root, 'brain.db'),
    HANDOFF_DIR: path.join(root, 'handoff'),
  };
  const programFile = path.join(env.ORCH_STATE_DIR, 'capability-program/codemod-campaign.json');
  return {
    root, env, campaignFile, programFile,
    github: () => JSON.parse(fs.readFileSync(stateFile, 'utf8')),
    updateGithub: (mutate) => {
      const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      mutate(state);
      fs.writeFileSync(stateFile, JSON.stringify(state));
    },
    run: (script, args) => {
      // File capture also works on runners that disallow Node subprocess pipes.
      const output = path.join(root, 'stdout');
      const errors = path.join(root, 'stderr');
      const stdout = fs.openSync(output, 'w');
      const stderr = fs.openSync(errors, 'w');
      try {
        const result = spawnSync(python, [path.join(repo, 'src', script), ...args],
          { cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 30000 });
        assert.ifError(result.error);
        return { status: result.status, stdout: fs.readFileSync(output, 'utf8'),
          stderr: fs.readFileSync(errors, 'utf8') };
      } finally {
        fs.closeSync(stdout);
        fs.closeSync(stderr);
        fs.rmSync(output);
        fs.rmSync(errors);
      }
    },
  };
}

function fileTargets(w) {
  const result = w.run('codemod_lane.py', ['--file-targets', w.campaignFile]);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test('file-targets CLI files exact work orders, prints URLs and links the parent once', (t) => {
  const w = world(t);
  const program = fileTargets(w);
  assert.deepEqual(Object.keys(program.repos), campaign.campaign.repos);
  assert.deepEqual(JSON.parse(fs.readFileSync(w.programFile, 'utf8')), program);
  const github = w.github();
  assert.equal(Object.keys(github.issues).length, 6);
  assert.equal(github.comments.length, 1);
  assert.equal(github.comments[0].endpoint, 'repos/stranske/Orchestrator/issues/435/comments');
  for (const name of campaign.campaign.repos) {
    const issue = github.issues[name];
    assert.equal(issue.title, title);
    assert.deepEqual(issue.labels, [{ name: 'codemod' }]);
    assert.ok(issue.body.includes(campaign.delegate_prompt));
    assert.ok(issue.body.includes('<!-- codemod-campaign:' + campaign.campaign.id + ' -->'));
    const scope = issue.body.split('## Scope\n')[1].split('## Non-Goals')[0];
    assert.ok(!scope.includes('`.mypy_cache/`'));
    for (const entry of campaign.ignore_entries.slice(1)) assert.ok(scope.includes('`' + entry + '`'));
    const row = program.repos[name];
    assert.equal(row.target, name + '#' + issue.number);
    assert.equal(row.url, issue.url);
    assert.deepEqual(row.missing, campaign.ignore_entries.slice(1));
    assert.deepEqual([row.merged, row.durable, row.cost_usd], [null, null, null]);
    assert.ok(github.comments[0].body.includes(issue.url));
  }
  assert.deepEqual(fileTargets(w), program);
  assert.equal(w.github().comments.length, 1);
  assert.equal(w.github().calls.filter((args) =>
    args[0] === 'api' && args[1].endsWith('/issues')).length, 6);
});

test('file-targets CLI skips complete repositories and records explicit unknown outcomes', (t) => {
  const w = world(t);
  const complete = campaign.campaign.repos[0];
  w.updateGithub((state) => { state.ignores[complete] = campaign.ignore_entries.join('\r\n') + '\r\n'; });
  const program = fileTargets(w);
  assert.equal(program.repos[complete].state, 'already-complete');
  assert.deepEqual(program.repos[complete].missing, []);
  assert.equal(program.repos[complete].target, undefined);
  assert.deepEqual(['merged', 'durable', 'cost_usd'].map((key) => program.repos[complete][key]),
    [null, null, null]);
  assert.equal(Object.keys(w.github().issues).length, 5);
  assert.ok(w.github().comments[0].body.includes(complete + ': already-complete'));
});

test('file-targets CLI retries a failed parent link without recreating filed issues', (t) => {
  const w = world(t);
  w.updateGithub((state) => { state.failLink = true; });
  const failed = w.run('codemod_lane.py', ['--file-targets', w.campaignFile]);
  assert.equal(failed.status, 1);
  assert.match(failed.stderr, /temporary parent link failure/);
  const saved = JSON.parse(fs.readFileSync(w.programFile, 'utf8'));
  assert.equal(Object.keys(saved.repos).length, 6);
  assert.equal(saved.linked_targets, undefined);
  w.updateGithub((state) => { state.failLink = false; });
  const retried = fileTargets(w);
  assert.deepEqual(retried.repos, saved.repos);
  assert.equal(w.github().comments.length, 1);
  assert.equal(w.github().calls.filter((args) =>
    args[0] === 'api' && args[1].endsWith('/issues')).length, 6);
});

test('add-only CLI rejects a destructive prompt before GitHub or receipt changes', (t) => {
  const w = world(t);
  const broken = structuredClone(campaign);
  broken.delegate_prompt += ' Remove an existing ignore line.';
  fs.writeFileSync(w.campaignFile, JSON.stringify(broken));
  const invalid = w.run('codemod_lane.py', ['--validate', w.campaignFile, '--json']);
  assert.equal(invalid.status, 1);
  assert.ok(JSON.parse(invalid.stdout).errors.includes('delegate_prompt contradicts the add-only contract'));
  const filing = w.run('codemod_lane.py', ['--file-targets', w.campaignFile]);
  assert.equal(filing.status, 1);
  assert.match(filing.stderr, /add-only contract/);
  assert.deepEqual(w.github().calls, []);
  assert.equal(fs.existsSync(w.programFile), false);
  fs.writeFileSync(w.campaignFile, JSON.stringify(campaign));
  const restored = w.run('codemod_lane.py', ['--validate', w.campaignFile, '--json']);
  assert.equal(restored.status, 0, restored.stderr);
  assert.equal(JSON.parse(restored.stdout).valid, true);
});

test('rollout CLI requires both the owner window and confirmation before reading the campaign', (t) => {
  const w = world(t);
  for (const window of [undefined, '0', 'true', '1']) {
    for (const confirm of [false, true]) {
      if (window === '1' && confirm) continue;
      if (window === undefined) delete w.env.ORCH_RANGE_LANE_ROLLOUT;
      else w.env.ORCH_RANGE_LANE_ROLLOUT = window;
      const args = ['--apply', '--campaign', path.join(w.root, 'absent.json'), '--json'];
      if (confirm) args.push('--confirm-rollout');
      const result = w.run('range_lane_rollout.py', args);
      assert.equal(result.status, 2, result.stderr);
      const report = JSON.parse(result.stdout);
      assert.match(report.error, /active dispatch requires/);
      assert.equal(report.read_only, true);
      assert.deepEqual(w.github().calls, []);
      assert.equal(fs.existsSync(w.env.ORCH_STATE_DIR), false);
      assert.equal(fs.existsSync(w.env.HANDOFF_DIR), false);
    }
  }
});
