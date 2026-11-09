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
const installer = 'scripts/install_verified_snapshot.py';
const python = process.env.PYTHON || 'python3';
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
  const digest = () => run(python, [path.join(mirror, installer), mirror, '--digest'])
    .toString().trim();
  const checkPayload = () => {
    for (const filename of [baseline, pins]) {
      assert.deepEqual(fs.readFileSync(path.join(mirror, filename)),
        git('show', `HEAD:${filename}`), `${filename} differs from committed source`);
    }
    assert.ok(fs.existsSync(path.join(mirror, 'base.py')));
    assert.ok(!fs.existsSync(path.join(mirror, 'src')));
  };

  // Copy immutable real inputs; never edit the checkout's protected pin file.
  const archive = run('git', ['-C', repo, 'archive', 'HEAD', baseline, pins, installer,
    'scripts/check_checks_reported.py', 'src/source_snapshot_digest.py']);
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
  const originalDigest = digest();
  assert.match(originalDigest, /^[0-9a-f]{64}$/);

  const repairedBaseline = Buffer.concat([
    fs.readFileSync(path.join(source, baseline)),
    Buffer.from('\nFixture baseline repair: must be committed before shipping.\n'),
  ]);
  fs.writeFileSync(path.join(source, baseline), repairedBaseline);
  build();
  checkPayload();
  assert.notDeepEqual(fs.readFileSync(path.join(mirror, baseline)), repairedBaseline,
    'uncommitted baseline edits must not count as delivered measurements');
  assert.equal(digest(), originalDigest,
    'uncommitted source measurements must not change the mirror seal');

  git('add', baseline);
  git('commit', '-qm', 'commit baseline repair');
  build();
  checkPayload();
  assert.deepEqual(fs.readFileSync(path.join(mirror, baseline)), repairedBaseline);
  assert.deepEqual(fs.readFileSync(path.join(mirror, pins)), originalPins);
  const repairedDigest = digest();
  assert.notEqual(repairedDigest, originalDigest,
    'the payload seal must include the committed baseline repair');

  // The aggregate wrapper uses this same digest before and after verification.
  // A baseline changed after sealing must fail publication before any write.
  const destination = path.join(root, 'installed mirror');
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, 'sentinel.txt'), 'previous generation\n');
  fs.appendFileSync(path.join(mirror, baseline), 'changed after sealing\n');
  assert.notEqual(digest(), repairedDigest);
  const installArgs = [path.join(mirror, installer), mirror, destination,
    '--expected-digest', repairedDigest, '--unverified'];
  const refused = spawnSync(python, installArgs, { env, encoding: 'utf8', timeout: 30000 });
  assert.equal(refused.status, 2, refused.stderr);
  assert.match(refused.stderr, /retained snapshot digest.*!= verified payload/);
  assert.deepEqual(fs.readdirSync(destination), ['sentinel.txt']);
  assert.equal(fs.readFileSync(path.join(destination, 'sentinel.txt'), 'utf8'),
    'previous generation\n');

  fs.writeFileSync(path.join(mirror, baseline), repairedBaseline);
  assert.equal(digest(), repairedDigest, 'restoring the baseline must restore its seal');
  run(python, installArgs);
  for (const filename of [baseline, pins]) {
    assert.deepEqual(fs.readFileSync(path.join(destination, filename)),
      git('show', `HEAD:${filename}`), `${filename} changed during publication`);
  }
  assert.equal(run(python, [path.join(destination, installer), destination, '--digest'])
    .toString().trim(), repairedDigest);
});
