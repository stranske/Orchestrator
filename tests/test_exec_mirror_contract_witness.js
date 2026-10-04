'use strict';

// Reproducible focused witnesses when pytest is unavailable. These exercise the
// repository implementation in scratch trees; they never inspect or install a
// live deployment, and do not replace the full regression suite or Sol review.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { after, test } = require('node:test');
const { bindReviewInputs } = require('../scripts/review_contract_inputs');

const repo = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python3';
const builder = path.join(repo, 'scripts', 'build_exec_mirror.sh');
const presync = path.join(repo, 'scripts', 'verify_before_sync.sh');
const installer = path.join(repo, 'scripts', 'install_verified_snapshot.py');
// Normal CI checks today's implementation. The opt-in review run must bind the
// actual executable inputs to the retained historical manifest before and after.
if (process.env.ORCH_CONTRACT_EXPECTED_MANIFEST_SHA256 !== undefined
  && !process.env.ORCH_CONTRACT_SOURCE_MANIFEST) {
  throw new Error('a witness receipt digest requires a source manifest');
}
const bound = process.env.ORCH_CONTRACT_SOURCE_MANIFEST ? bindReviewInputs(repo,
  fs.readFileSync(process.env.ORCH_CONTRACT_SOURCE_MANIFEST),
  process.env.ORCH_CONTRACT_EXPECTED_HEAD, [
    'scripts/build_exec_mirror.sh', 'scripts/verify_before_sync.sh',
    'scripts/install_verified_snapshot.py', 'docs/MIRROR_SYNC_PATCH.md',
    'src/env_prereq.py', 'src/verify.py', 'src/paths.py', 'src/mirror_reader.py',
  ], process.env.ORCH_CONTRACT_EXPECTED_MANIFEST_SHA256) : null;
if (bound) after(() => assert.ok(bound.check() > 0));

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: 'utf8', timeout: 30000, ...options,
  });
  assert.equal(result.status, 0, [result.error?.message, result.stderr].filter(Boolean).join('\n'));
  return result.stdout.trim();
}

function write(filename, bytes, mode = 0o644) {
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  fs.writeFileSync(filename, bytes, { mode });
}

function pythonCode(code, ...args) {
  // Pass fixed program text as an argv element. No shell or interpolation of
  // paths into executable text; stdin pipes can stall on the constrained runner.
  return run(python, ['-I', '-c', code, ...args]);
}

function world(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'orch-contract-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, 'source with spaces');
  const home = path.join(root, 'home');
  fs.mkdirSync(source);
  fs.mkdirSync(home);
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !key.startsWith('ORCH_') && !key.startsWith('VERIFY_BEFORE')
    && !['PYTHONPATH', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'].includes(key)));
  Object.assign(env, { HOME: home, GIT_CEILING_DIRECTORIES: root, PYTHON: python });
  const git = (...args) => run('git', ['-C', source, ...args], { env });
  git('init', '-q');
  write(path.join(source, 'src', 'base.py'), 'BASE = 1\n');
  write(path.join(source, 'orchestrate.sh'), '#!/bin/sh\nexit 0\n', 0o755);
  write(path.join(source, 'tests', 'test_a.py'), 'def test_a():\n    assert True\n');
  write(path.join(source, 'docs', 'guide.md'), 'guide\n');
  write(path.join(source, 'scripts', 'check_checks_reported.py'), 'print("ok")\n');
  for (const filename of [builder, installer]) {
    fs.copyFileSync(filename, path.join(source, 'scripts', path.basename(filename)));
  }
  // Use a superset of possible working-tree inputs, rather than duplicating the
  // builder's list of shipped root files. Skip Git metadata and all directories.
  const tracked = run('git', ['-C', repo, 'ls-files', '-z']).split('\0');
  for (const name of tracked) {
    if (name && !name.includes('/') && !name.endsWith('.py')
      && !/secret|token|credential/i.test(name)) {
      fs.copyFileSync(path.join(repo, name), path.join(source, name));
    }
  }
  const registries = new Set(fs.readFileSync(builder, 'utf8')
    .match(/\b(?:experiments|data|config)\/[\w.-]+\.json\b/g));
  assert.ok(registries.size > 0);
  for (const name of registries) write(path.join(source, name), '{}\n');
  git('add', '--force', '.');
  git('-c', 'user.name=Test', '-c', 'user.email=test@example.com',
    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'synthetic contract inputs');
  return { root, source, home, env };
}

function tree(root, exclude = []) {
  const entries = {};
  function walk(directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      const filename = path.join(directory, name);
      const relative = path.relative(root, filename);
      if (exclude.includes(relative)) continue;
      const info = fs.lstatSync(filename);
      entries[relative] = [info.mode & 0o7777,
        info.isDirectory() ? null : fs.readFileSync(filename).toString('hex')];
      if (info.isDirectory()) walk(filename);
    }
  }
  walk(root);
  return entries;
}

function build(w, destination) {
  run('bash', [builder, w.source, destination], { env: w.env });
}

test('installer owns every builder-shipped leaf in a superset of source inputs', (t) => {
  const w = world(t);
  const mirror = path.join(w.root, 'mirror');
  build(w, mirror);
  const owned = JSON.parse(pythonCode(
    'import json, sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n'
      + 'import install_verified_snapshot as installer\n'
      + 'print(json.dumps([p.as_posix() for p in installer.owned_entries(Path(sys.argv[2]))]))\n',
    path.dirname(installer), mirror));
  const leaves = Object.entries(tree(mirror)).filter(([, [, bytes]]) => bytes !== null)
    .map(([name]) => name);
  assert.ok(leaves.length > 0);
  assert.deepEqual(leaves.filter((name) => !owned.includes(name)), []);
  assert.ok(leaves.includes('AGENTS.md') && leaves.includes('ORCHESTRATOR.md'));
});

test('scratch installer publishes every builder byte and mode and preserves runtime output', (t) => {
  const w = world(t);
  const snapshot = path.join(w.root, 'snapshot');
  const mirror = path.join(w.root, 'installed');
  build(w, snapshot);
  write(path.join(snapshot, 'repo_review_registry.json'), '{"repos": []}\n');
  const digest = run(python, [installer, snapshot, '--digest'], { env: w.env });
  const before = tree(snapshot);
  write(path.join(mirror, 'stale.py'), 'STALE = True\n');
  write(path.join(mirror, 'AGENTS.md'), 'stale instructions\n');
  write(path.join(mirror, 'ORCHESTRATOR.md'), 'stale contract\n');
  write(path.join(mirror, '.docs-shipped.txt'), 'docs/removed.md\n');
  write(path.join(mirror, 'docs', 'removed.md'), 'removed source doc\n');
  write(path.join(mirror, 'docs', 'reports', 'runtime.md'), 'runtime report\n');
  write(path.join(mirror, 'experiments', '.last-ship-gate'), 'runtime marker\n');
  const registry = path.join(w.root, 'runtime-registry.json');
  write(registry, '{"old": true}\n');
  run(python, [installer, snapshot, mirror, '--expected-digest', digest,
    '--runtime-registry', registry, '--unverified'], { env: w.env });
  // Runtime content is linked into the retained prior generation. Inspect these
  // paths separately; the contract comparison concerns the shipped entries.
  const installed = tree(mirror, ['docs/reports', 'experiments/.last-ship-gate']);
  for (const [name, entry] of Object.entries(before)) {
    assert.deepEqual(installed[name], entry, `${name} changed bytes or mode during installation`);
  }
  assert.equal(run(python, [installer, mirror, '--digest'], { env: w.env }), digest);
  assert.deepEqual(tree(snapshot), before, 'installer modified its retained snapshot');
  for (const name of ['stale.py', 'docs/removed.md']) {
    assert.equal(fs.existsSync(path.join(mirror, name)), false, `${name} was not retired`);
  }
  assert.equal(fs.readFileSync(path.join(mirror, 'docs/reports/runtime.md'), 'utf8'), 'runtime report\n');
  assert.equal(fs.readFileSync(path.join(mirror, 'experiments/.last-ship-gate'), 'utf8'), 'runtime marker\n');
  assert.equal(fs.readFileSync(registry, 'utf8'), '{"repos": []}\n');
});

test('scratch installer refuses a changed snapshot before publication or registry update', (t) => {
  const w = world(t);
  const snapshot = path.join(w.root, 'snapshot');
  const mirror = path.join(w.root, 'installed');
  build(w, snapshot);
  write(path.join(snapshot, 'repo_review_registry.json'), '{"repos": []}\n');
  const digest = run(python, [installer, snapshot, '--digest'], { env: w.env });
  write(path.join(mirror, 'base.py'), 'old generation\n');
  const registry = path.join(w.root, 'runtime-registry.json');
  write(registry, '{"old": true}\n');
  const before = tree(mirror);
  const target = path.join(snapshot, 'AGENTS.md');
  const bytes = fs.readFileSync(target);
  const mode = fs.statSync(target).mode & 0o7777;
  for (const mutation of ['bytes', 'mode']) {
    try {
      if (mutation === 'bytes') fs.appendFileSync(target, 'changed after verification\n');
      else fs.chmodSync(target, mode ^ 0o100);
      const refused = spawnSync(python, [installer, snapshot, mirror, '--expected-digest', digest,
        '--runtime-registry', registry], { env: w.env, encoding: 'utf8', timeout: 30000 });
      assert.equal(refused.status, 2, refused.stderr);
      assert.match(refused.stderr, /retained snapshot digest.*!= verified payload/);
      assert.deepEqual(tree(mirror), before, `${mutation} mutation changed live scratch bytes`);
      assert.equal(fs.lstatSync(mirror).isSymbolicLink(), false);
      assert.equal(fs.readFileSync(registry, 'utf8'), '{"old": true}\n');
    } finally {
      fs.writeFileSync(target, bytes);
      fs.chmodSync(target, mode);
    }
  }
});

test('each input that changes builder output also changes the pre-sync source identity', (t) => {
  const w = world(t);
  const clean = path.join(w.root, 'clean');
  const edited = path.join(w.root, 'edited');
  build(w, clean);
  const before = tree(clean);
  const identity = () => run('bash', [presync, '--identity', w.source], { env: w.env });
  const baseline = identity();
  const candidates = Object.keys(tree(w.source, ['.git']));
  const moved = [];
  for (const name of candidates) {
    const filename = path.join(w.source, name);
    if (!fs.lstatSync(filename).isFile()) continue;
    const original = fs.readFileSync(filename);
    try {
      fs.appendFileSync(filename, '\n# independent mutation witness\n');
      build(w, edited);
      if (JSON.stringify(tree(edited)) !== JSON.stringify(before)) {
        moved.push(name);
        assert.notEqual(identity(), baseline, `${name} shipped without moving source identity`);
      }
    } finally {
      fs.writeFileSync(filename, original);
    }
  }
  assert.ok(moved.includes('src/base.py') && moved.includes('AGENTS.md')
    && moved.includes('ORCHESTRATOR.md'));
  assert.ok(!moved.some((name) => name.startsWith('tests/') || name.startsWith('docs/')));
});

test('documented copier matches builder bytes and modes and refuses a missing builder', (t) => {
  const w = world(t);
  const document = fs.readFileSync(path.join(repo, 'docs', 'MIRROR_SYNC_PATCH.md'), 'utf8');
  const heading = document.indexOf('**The copier, whole.**');
  const opening = document.indexOf('```bash\n', heading);
  const closing = document.indexOf('\n```', opening + 8);
  assert.ok(heading >= 0 && opening >= 0 && closing > opening);
  const copier = path.join(w.root, 'copier.sh');
  write(copier, document.slice(opening + 8, closing));
  const bin = path.join(w.root, 'bin');
  // Emulate only the authenticated registry read, using a fixed public fixture.
  write(path.join(bin, 'gh'), `#!${process.execPath}\n`
    + 'process.stdout.write(Buffer.from("{\\"repos\\": []}").toString("base64"));\n', 0o755);
  fs.mkdirSync(path.join(w.home, '.codex', 'orchestrator'), { recursive: true });
  const mirror = path.join(w.root, 'copied');
  const env = { ...w.env, PATH: `${bin}${path.delimiter}${w.env.PATH}`,
    ORCH_MIRROR: mirror, ORCH_PRIVATE_COPY_ROOT: w.root, TMPDIR: w.root };
  run('bash', [copier, w.source], { env });
  const contract = path.join(w.root, 'contract');
  build(w, contract);
  assert.deepEqual(tree(mirror, ['repo_review_registry.json']), tree(contract));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(mirror, 'repo_review_registry.json'))), { repos: [] });
  fs.rmSync(mirror, { recursive: true });
  fs.unlinkSync(path.join(w.source, 'scripts', 'build_exec_mirror.sh'));
  const refused = spawnSync('bash', [copier, w.source], { env, encoding: 'utf8', timeout: 30000 });
  assert.equal(refused.status, 2);
  assert.match(refused.stderr, /NOT SYNCED/);
  assert.equal(fs.existsSync(mirror), false);
});

test('every unreadable machine mark keeps strict mirror ceilings; verdict and rendering agree', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-contract-floor-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const floor = path.join(root, 'floor.json');
  fs.writeFileSync(floor, bound ? bound.floorBytes : fs.readFileSync(path.join(repo, '.verify-floor.json')));
  const output = pythonCode(
    'import json, sys\nfrom unittest.mock import patch\n'
      + 'sys.path.insert(0, sys.argv[1])\nimport env_prereq, verify\n'
      + 'floor = json.load(open(sys.argv[2]))\n'
      + 'marks = dict(_seat_signals={}, skill_resource_absent="absent", codex_profile_binary_absent="absent")\n'
      + 'with patch.multiple(env_prereq, **{k: lambda v=v: v for k, v in marks.items()}):\n'
      + '    assert env_prereq.bare_machine() is not None\n'
      + '    for mark in marks:\n'
      + '        with patch.object(env_prereq, mark, side_effect=OSError("unreadable")):\n'
      + '            assert env_prereq.bare_machine() is None, mark\n'
      + '            with patch.object(verify, "exec_mirror_shape", return_value="flat, no git"):\n'
      + '                assert verify.tree_shape()[0] == verify.EXEC_MIRROR, mark\n'
      + '    for mark, present in [("_seat_signals", {"seat": True}), ("skill_resource_absent", None), ("codex_profile_binary_absent", None)]:\n'
      + '        with patch.object(env_prereq, mark, return_value=present):\n'
      + '            assert env_prereq.bare_machine() is None, mark\n'
      + 'for mirror, bare, expected in [(None, "bare", verify.CHECKOUT), ("flat", None, verify.EXEC_MIRROR), ("flat", "bare", verify.BARE_EXEC_MIRROR)]:\n'
      + '    with patch.object(verify, "exec_mirror_shape", return_value=mirror), patch.object(verify, "bare_machine", return_value=bare):\n'
      + '        assert verify.tree_shape()[0] == expected\n'
      + 'for shape in (verify.CHECKOUT, verify.EXEC_MIRROR, verify.BARE_EXEC_MIRROR):\n'
      + '    limits = {k: verify.ceiling_limit(floor, k, shape=shape)[0] for k, _ in verify.CEILINGS}\n'
      + '    for key in ("skipped_max", "selftest_skipped_max", "gate_skipped_max"):\n'
      + '        expected = floor.get(verify.shape_key(key, shape), floor[key])\n'
      + '        assert limits[key] == expected, (shape, key)\n'
      + '    actual = {k: v for k, v in limits.items() if v is not None}\n'
      + '    problems, rendered = verify._ceiling_report(floor, actual, shape=shape)\n'
      + '    assert not problems, problems\n'
      + '    for key, limit in actual.items():\n'
      + '        key_in_force = verify.ceiling_limit(floor, key, shape=shape)[1]\n'
      + '        problems, rendered = verify._ceiling_report(floor, {**actual, key: limit + 1}, shape=shape)\n'
      + '        assert problems and any(key_in_force in p for p in problems), (shape, key)\n'
      + '        assert rendered[key].startswith(f"{limit + 1}/{limit} max"), rendered[key]\n'
      + 'print("three shapes, all present and unreadable machine marks, every ceiling boundary checked")\n',
    path.join(repo, 'src'), floor);
  assert.match(output, /every ceiling boundary checked/);
});
