'use strict';

// The measured baseline and the pin payload must reach the flat mirror from
// the same committed source. This witness needs only Node, git and the builder.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const builder = path.join(repo, 'scripts', 'build_exec_mirror.sh');
const baseline = 'docs/CI_LINT_BASELINE.md';
const pins = '.github/workflows/autofix-versions.env';

test('flat mirror ships committed baseline and pins together across a baseline repair', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-toolchain-mirror-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, 'source with spaces');
  const mirror = path.join(root, 'flat mirror');
  fs.mkdirSync(source);
  const env = Object.fromEntries(Object.entries(process.env)
    .filter(([key]) => !key.startsWith('GIT_')));
  env.GIT_CEILING_DIRECTORIES = root;

  function run(command, args, options = {}) {
    const result = spawnSync(command, args, { env, timeout: 30000, ...options });
    assert.equal(result.status, 0,
      [result.error?.message, result.stderr?.toString()].filter(Boolean).join('\n'));
    return result.stdout;
  }
  const git = (...args) => run('git', ['-C', source,
    '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
    '-c', 'commit.gpgsign=false', ...args]);
  const build = () => run('bash', [builder, source, mirror]);
  const checkPayload = () => {
    for (const filename of [baseline, pins]) {
      assert.deepEqual(fs.readFileSync(path.join(mirror, filename)),
        git('show', `HEAD:${filename}`), `${filename} differs from committed source`);
    }
    assert.ok(fs.existsSync(path.join(mirror, 'base.py')));
    assert.ok(!fs.existsSync(path.join(mirror, 'src')));
  };

  // Copy immutable real inputs; never edit the checkout's protected pin file.
  const archive = run('git', ['-C', repo, 'archive', 'HEAD', baseline, pins]);
  run('tar', ['-x', '-C', source], { input: archive });
  for (const [filename, content] of [
    ['src/base.py', 'BASE = 1\n'],
    ['tests/test_base.py', 'def test_base():\n    assert True\n'],
    ['orchestrate.sh', '#!/bin/sh\nexit 0\n'],
  ]) {
    const target = path.join(source, filename);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content);
  }
  git('init', '-q');
  git('add', '--force', '.');
  git('commit', '-qm', 'input baseline and toolchain');
  const originalPins = fs.readFileSync(path.join(source, pins));
  build();
  checkPayload();

  const repairedBaseline = Buffer.concat([
    fs.readFileSync(path.join(source, baseline)),
    Buffer.from('\nFixture baseline repair: must be committed before shipping.\n'),
  ]);
  fs.writeFileSync(path.join(source, baseline), repairedBaseline);
  build();
  checkPayload();
  assert.notDeepEqual(fs.readFileSync(path.join(mirror, baseline)), repairedBaseline,
    'uncommitted baseline edits must not count as delivered measurements');

  git('add', baseline);
  git('commit', '-qm', 'commit baseline repair');
  build();
  checkPayload();
  assert.deepEqual(fs.readFileSync(path.join(mirror, baseline)), repairedBaseline);
  assert.deepEqual(fs.readFileSync(path.join(mirror, pins)), originalPins);
});
