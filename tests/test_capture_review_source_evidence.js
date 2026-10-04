'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { blobSha, capture, main } = require('../scripts/capture_review_source_evidence');

function world(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'orch-source-evidence-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const repo = path.join(root, 'source with spaces');
  const output = path.join(root, 'bundle');
  fs.mkdirSync(repo);
  function git(...args) {
    const result = spawnSync('git', ['-C', repo, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  git('init', '-q');
  const bytes = Buffer.from('VALUE = "complete source ☃"\n');
  fs.writeFileSync(path.join(repo, 'module.py'), bytes);
  git('add', '.');
  git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false',
    'commit', '-qm', 'exact head fixture');
  const head = git('rev-parse', 'HEAD');
  const tree = git('rev-parse', 'HEAD^{tree}');
  const sha = git('rev-parse', 'HEAD:module.py');
  assert.equal(blobSha(bytes), sha, 'independent Git hash agrees with actual object');
  const metadata = {
    repository: 'owner/repo', pr_number: 438, retrieval_transport: 'test fixture',
    pull_request: { head_sha: head, changed_files: 1 }, commit: { sha: head, tree: { sha: tree } },
    tree: { sha: tree, truncated: false, tree: [
      { path: 'module.py', type: 'blob', mode: '100644', sha, size: bytes.length },
    ] },
    changed_paths: ['module.py'], required_paths: [],
  };
  return { root, repo, output, head, metadata, bytes, sha };
}

function collect(w, reader) {
  return capture(Buffer.from(JSON.stringify(w.metadata)), w.repo, w.output, w.head, reader);
}

test('retains complete exact-head bytes and bindings despite working-tree drift', (t) => {
  const w = world(t);
  fs.writeFileSync(path.join(w.repo, 'module.py'), 'different working tree\n');
  const report = collect(w);
  assert.equal(report.source_status, 'COMPLETE');
  assert.equal(report.review_status, 'PENDING');
  assert.equal(report.deployment_status, 'NOT_OBSERVED');
  assert.equal(report.retrieved_changed_files, 1);
  assert.equal(report.files[0].blob_sha, w.sha);
  assert.equal(report.files[0].retained_bytes, w.bytes.length);
  assert.equal(report.files[0].git_mode, '100644');
  assert.deepEqual(fs.readFileSync(path.join(w.output, report.files[0].artifact)), w.bytes);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(w.output, 'manifest.json'))), report);
  assert.equal(fs.readFileSync(path.join(w.repo, 'module.py'), 'utf8'), 'different working tree\n');
});

for (const defect of ['head', 'commit', 'tree', 'truncated', 'duplicates', 'traversal', 'omitted file']) {
  test(`rejects ${defect} metadata before writing a bundle`, (t) => {
    const w = world(t);
    if (defect === 'head') w.metadata.pull_request.head_sha = 'a'.repeat(40);
    if (defect === 'commit') w.metadata.commit.sha = 'a'.repeat(40);
    if (defect === 'tree') w.metadata.tree.sha = 'a'.repeat(40);
    if (defect === 'truncated') w.metadata.tree.truncated = true;
    if (defect === 'duplicates') w.metadata.changed_paths.push('module.py');
    if (defect === 'traversal') w.metadata.tree.tree[0].path = '../module.py';
    if (defect === 'omitted file') w.metadata.pull_request.changed_files = 2;
    assert.throws(() => collect(w));
    assert.equal(fs.existsSync(w.output), false);
  });
}

for (const defect of ['truncation', 'same-length corruption', 'missing object', 'missing binding']) {
  test(`records ${defect} as UNKNOWN with an owner and action`, (t) => {
    const w = world(t);
    let reader;
    if (defect === 'truncation') reader = () => w.bytes.subarray(0, w.bytes.length - 1);
    if (defect === 'same-length corruption') reader = () => Buffer.alloc(w.bytes.length);
    if (defect === 'missing object') reader = () => { throw new Error('provider stderr must stay private'); };
    if (defect === 'missing binding') w.metadata.required_paths.push('missing.py');
    const report = collect(w, reader);
    assert.equal(report.source_status, 'UNKNOWN');
    const unknown = report.files.find((file) => file.status === 'UNKNOWN');
    assert.equal(unknown.owner, 'owner');
    assert.match(unknown.next_action, /authenticated GitHub/);
    assert.ok(!JSON.stringify(report).includes('provider stderr'));
    assert.equal(report.review_status, 'PENDING');
  });
}

test('retains aliases by blob identity and preserves executable mode bindings', (t) => {
  const w = world(t);
  w.metadata.tree.tree.push({ ...w.metadata.tree.tree[0], path: 'alias.sh', mode: '100755' });
  w.metadata.required_paths.push('alias.sh');
  const report = collect(w);
  assert.equal(report.source_status, 'COMPLETE');
  assert.equal(report.files.length, 2);
  assert.equal(report.files[0].git_mode, '100755');
  assert.equal(fs.readdirSync(path.join(w.output, 'blobs')).length, 1);
});

test('refuses to overwrite retained evidence or write inside the source via a symlink', (t) => {
  const w = world(t);
  collect(w);
  const before = fs.readFileSync(path.join(w.output, 'manifest.json'));
  assert.throws(() => collect(w), /EEXIST/);
  assert.deepEqual(fs.readFileSync(path.join(w.output, 'manifest.json')), before);
  const alias = path.join(w.root, 'alias');
  fs.symlinkSync(w.repo, alias, 'dir');
  w.output = path.join(alias, 'evidence');
  assert.throws(() => collect(w), /overlaps source/);
  assert.equal(fs.existsSync(path.join(w.repo, 'evidence')), false);
});

test('CLI reports incomplete retrieval with a nonzero exit and durable manifest', (t) => {
  const w = world(t);
  w.metadata.required_paths.push('unavailable.py');
  const input = path.join(w.root, 'input.json');
  fs.writeFileSync(input, JSON.stringify(w.metadata));
  assert.equal(main([input, w.repo, w.output, w.head]), 2);
  assert.equal(JSON.parse(fs.readFileSync(path.join(w.output, 'manifest.json'))).source_status, 'UNKNOWN');
});
