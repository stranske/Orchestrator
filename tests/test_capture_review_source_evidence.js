'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
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
    pull_request_files: { head_sha: head, complete: true, files: [
      { filename: 'module.py', sha, status: 'modified' },
    ] },
  };
  return { root, repo, output, head, metadata, bytes, sha };
}

function collect(w, reader) {
  return capture(Buffer.from(JSON.stringify(w.metadata)), w.repo, w.output, w.head, reader);
}

function runCollector(w, input) {
  const stdoutPath = path.join(w.root, 'cli.stdout');
  const stderrPath = path.join(w.root, 'cli.stderr');
  const stdout = fs.openSync(stdoutPath, 'w');
  const stderr = fs.openSync(stderrPath, 'w');
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  let result;
  try {
    result = spawnSync(process.execPath, [path.resolve(__dirname,
      '../scripts/capture_review_source_evidence.js'), input, w.repo, w.output, w.head],
    { env, timeout: 10000, stdio: ['ignore', stdout, stderr] });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  return { ...result, stdout: fs.readFileSync(stdoutPath, 'utf8'),
    stderr: fs.readFileSync(stderrPath, 'utf8') };
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
  const retainedMetadata = fs.readFileSync(path.join(w.output, 'metadata.json'));
  assert.deepEqual(JSON.parse(retainedMetadata), w.metadata);
  assert.equal(crypto.createHash('sha256').update(retainedMetadata).digest('hex'),
    report.metadata_sha256);
  assert.equal(fs.readFileSync(path.join(w.repo, 'module.py'), 'utf8'), 'different working tree\n');
});

test('CLI retains complete binary, executable, alias and symlink blobs from the exact head', (t) => {
  const w = world(t);
  // Exceed the original comparison's entire supplied-code budget and include
  // every byte value: retention must not decode, trim or truncate blob bytes.
  const bytes = Buffer.alloc(128 * 1024);
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = i % 256;
  fs.writeFileSync(path.join(w.repo, 'program.bin'), bytes, { mode: 0o755 });
  fs.writeFileSync(path.join(w.repo, 'alias.bin'), bytes, { mode: 0o644 });
  const linkBytes = Buffer.from('../absent-external-file');
  fs.symlinkSync(linkBytes.toString(), path.join(w.repo, 'reference'));
  function git(...args) {
    const result = spawnSync('git', ['-C', w.repo, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  git('add', '.');
  git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', '-c', 'commit.gpgsign=false',
    'commit', '-qm', 'binary and symlink acquisition fixture');
  w.head = git('rev-parse', 'HEAD');
  w.metadata.pull_request.head_sha = w.head;
  w.metadata.pull_request_files.head_sha = w.head;
  w.metadata.commit.sha = w.head;
  w.metadata.commit.tree.sha = git('rev-parse', 'HEAD^{tree}');
  w.metadata.tree.sha = w.metadata.commit.tree.sha;
  for (const [name, mode, content] of [
    ['program.bin', '100755', bytes], ['alias.bin', '100644', bytes],
    ['reference', '120000', linkBytes],
  ]) {
    w.metadata.required_paths.push(name);
    w.metadata.tree.tree.push({ path: name, type: 'blob', mode,
      sha: git('rev-parse', `HEAD:${name}`), size: content.length });
  }
  // The CLI must read committed objects, including a dangling link's own
  // content, rather than dereferencing or consuming drifted working-tree files.
  fs.writeFileSync(path.join(w.repo, 'program.bin'), 'working-tree drift\n');
  fs.unlinkSync(path.join(w.repo, 'reference'));
  const input = path.join(w.root, 'authenticated-fixture.json');
  const metadataBytes = Buffer.from(`${JSON.stringify(w.metadata)}\n`);
  fs.writeFileSync(input, metadataBytes);
  const result = runCollector(w, input);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /COMPLETE: 1\/1 changed files retained; 4\/4 total files retained; review PENDING/);
  const report = JSON.parse(fs.readFileSync(path.join(w.output, 'manifest.json')));
  assert.equal(report.files.length, 4);
  assert.equal(report.review_status, 'PENDING');
  assert.equal(report.deployment_status, 'NOT_OBSERVED');
  assert.deepEqual(fs.readFileSync(path.join(w.output, 'metadata.json')), metadataBytes);
  assert.equal(report.metadata_sha256,
    crypto.createHash('sha256').update(metadataBytes).digest('hex'));
  for (const file of report.files) {
    const entry = w.metadata.tree.tree.find((item) => item.path === file.path);
    const expected = file.path === 'module.py' ? w.bytes
      : file.path === 'reference' ? linkBytes : bytes;
    const artifact = path.join(w.output, file.artifact);
    assert.equal(file.status, 'RETRIEVED');
    assert.equal(file.git_mode, entry.mode);
    assert.equal(file.blob_sha, entry.sha);
    assert.equal(file.retained_bytes, expected.length);
    assert.equal(file.sha256, crypto.createHash('sha256').update(expected).digest('hex'));
    assert.deepEqual(fs.readFileSync(artifact), expected);
    assert.ok(fs.lstatSync(artifact).isFile());
    assert.equal(fs.statSync(artifact).mode & 0o777, 0o600);
  }
  // Identical bytes share one retained object while keeping distinct file modes.
  assert.equal(fs.readdirSync(path.join(w.output, 'blobs')).length, 3);
});

test('CLI reports UNKNOWN when all changed files are retained but a supporting blob is unavailable', (t) => {
  const w = world(t);
  w.metadata.required_paths.push('supporting.py');
  w.metadata.tree.tree.push({ path: 'supporting.py', type: 'blob', mode: '100644',
    sha: 'f'.repeat(40), size: 100 });
  const input = path.join(w.root, 'authenticated-fixture.json');
  fs.writeFileSync(input, JSON.stringify(w.metadata));
  const result = runCollector(w, input);
  assert.equal(result.status, 2, result.stderr);
  assert.match(result.stdout, /UNKNOWN: 1\/1 changed files retained; 1\/2 total files retained; review PENDING/);
  const report = JSON.parse(fs.readFileSync(path.join(w.output, 'manifest.json')));
  assert.equal(report.source_status, 'UNKNOWN');
  assert.equal(report.retrieved_changed_files, report.changed_files);
  assert.equal(report.review_status, 'PENDING');
  assert.equal(report.deployment_status, 'NOT_OBSERVED');
  const missing = report.files.find((file) => file.path === 'supporting.py');
  assert.equal(missing.status, 'UNKNOWN');
  assert.equal(missing.blob_sha, 'f'.repeat(40));
  assert.equal(missing.owner, 'owner');
  assert.match(missing.next_action, /authenticated GitHub/);
  assert.ok(!Object.hasOwn(missing, 'artifact'));
});

test('rejects a same-count changed-path substitution with another real tree file', (t) => {
  const w = world(t);
  w.metadata.tree.tree.push({ ...w.metadata.tree.tree[0], path: 'alias.py' });
  w.metadata.changed_paths = ['alias.py'];
  assert.throws(() => collect(w), /authenticated PR file records/);
  assert.equal(fs.existsSync(w.output), false);
});

for (const defect of ['absent', 'incomplete', 'wrong head', 'omitted', 'duplicate', 'invalid filename']) {
  test(`rejects ${defect} PR file records before writing a bundle`, (t) => {
    const w = world(t);
    if (defect === 'absent') delete w.metadata.pull_request_files;
    if (defect === 'incomplete') w.metadata.pull_request_files.complete = false;
    if (defect === 'wrong head') w.metadata.pull_request_files.head_sha = 'a'.repeat(40);
    if (defect === 'omitted') w.metadata.pull_request_files.files = [];
    if (defect === 'duplicate') {
      w.metadata.changed_paths.push('alias.py');
      w.metadata.pull_request.changed_files = 2;
      w.metadata.pull_request_files.files.push({ ...w.metadata.pull_request_files.files[0] });
    }
    if (defect === 'invalid filename') w.metadata.pull_request_files.files[0].filename = null;
    assert.throws(() => collect(w), /authenticated PR file records/);
    assert.equal(fs.existsSync(w.output), false);
  });
}

test('compares the complete aggregated PR file set independently of ordering', (t) => {
  const w = world(t);
  w.metadata.tree.tree.push({ ...w.metadata.tree.tree[0], path: 'alias.py' });
  w.metadata.changed_paths.push('alias.py');
  w.metadata.pull_request.changed_files = 2;
  w.metadata.pull_request_files.files.unshift({ filename: 'alias.py', sha: w.sha });
  assert.equal(collect(w).retrieved_changed_files, 2);
});

for (const defect of ['missing SHA', 'invalid SHA', 'different blob', 'non-blob tree entry']) {
  test(`rejects ${defect} PR/tree identity before reading source or writing a bundle`, (t) => {
    const w = world(t);
    if (defect === 'missing SHA') delete w.metadata.pull_request_files.files[0].sha;
    if (defect === 'invalid SHA') w.metadata.pull_request_files.files[0].sha = 'invalid';
    if (defect === 'different blob') {
      const replacement = Buffer.from('a different complete Git object\n');
      fs.writeFileSync(path.join(w.repo, 'replacement.py'), replacement);
      const result = spawnSync('git', ['-C', w.repo, 'hash-object', '-w', 'replacement.py'],
        { encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      Object.assign(w.metadata.tree.tree[0], {
        sha: result.stdout.trim(), size: replacement.length,
      });
    }
    if (defect === 'non-blob tree entry') w.metadata.tree.tree[0].type = 'tree';
    let reads = 0;
    assert.throws(() => collect(w, () => { reads += 1; return w.bytes; }),
      /authenticated PR file records|PR file blob identity/);
    assert.equal(reads, 0);
    assert.equal(fs.existsSync(w.output), false);
  });
}

test('retained PR receipt refuses a contradictory blob binding with unchanged filenames', (t) => {
  const w = world(t);
  const receipt = JSON.parse(fs.readFileSync(path.join(__dirname,
    '../docs/reviews/pr-438-source-metadata.json')));
  const file = receipt.pull_request_files.files.at(-1);
  const entry = receipt.tree.tree.find((item) => item.path === file.filename);
  const replacement = receipt.tree.tree.find((item) => item.type === 'blob'
    && item.sha !== file.sha);
  Object.assign(entry, { sha: replacement.sha, size: replacement.size });
  let reads = 0;
  assert.throws(() => capture(Buffer.from(JSON.stringify(receipt)), w.repo, w.output,
    receipt.pull_request.head_sha, () => { reads += 1; return Buffer.alloc(0); }),
  /PR file blob identity/);
  assert.equal(reads, 0);
  assert.equal(fs.existsSync(w.output), false);
});

test('an absent changed-file tree binding remains UNKNOWN with an owner and action', (t) => {
  const w = world(t);
  w.metadata.tree.tree = [];
  let reads = 0;
  const report = collect(w, () => { reads += 1; return w.bytes; });
  assert.equal(reads, 0);
  assert.equal(report.source_status, 'UNKNOWN');
  assert.equal(report.retrieved_changed_files, 0);
  assert.equal(report.files[0].owner, 'owner');
  assert.match(report.files[0].next_action, /authenticated GitHub/);
  assert.equal(report.review_status, 'PENDING');
  assert.equal(report.deployment_status, 'NOT_OBSERVED');
});

test('durable metadata receipt reproduces all manifest path/tree bindings without historical blobs', (t) => {
  const w = world(t);
  const reviews = path.join(__dirname, '../docs/reviews');
  const receipt = fs.readFileSync(path.join(reviews, 'pr-438-source-metadata.json'));
  const manifest = JSON.parse(fs.readFileSync(path.join(reviews, 'pr-438-source-evidence.json')));
  const report = capture(receipt, w.repo, w.output, manifest.head_sha, () => {
    throw new Error('historical objects intentionally unavailable');
  });
  assert.equal(report.metadata_sha256, manifest.metadata_sha256);
  assert.equal(report.changed_path_validation, manifest.changed_path_validation);
  assert.equal(report.tree_sha, manifest.tree_sha);
  assert.equal(report.changed_files, manifest.changed_files);
  assert.equal(report.source_status, 'UNKNOWN');
  const bindings = (m) => m.files.map((file) => ({
    path: file.path, changed: file.changed, blob: file.blob_sha,
    mode: file.git_mode, size: file.expected_bytes, source: file.source_url,
  }));
  assert.deepEqual(bindings(report), bindings(manifest));
  assert.deepEqual(fs.readFileSync(path.join(w.output, 'metadata.json')), receipt);
});

test('retained PR receipt rejects substituting its last changed file with a supporting tree file', (t) => {
  const w = world(t);
  const reviews = path.join(__dirname, '../docs/reviews');
  const metadata = JSON.parse(fs.readFileSync(path.join(reviews, 'pr-438-source-metadata.json')));
  const manifest = JSON.parse(fs.readFileSync(path.join(reviews, 'pr-438-source-evidence.json')));
  const supporting = manifest.files.find((file) => !file.changed);
  assert.ok(metadata.tree.tree.some((entry) => entry.path === supporting.path));
  metadata.changed_paths[metadata.changed_paths.length - 1] = supporting.path;
  assert.equal(new Set(metadata.changed_paths).size, metadata.pull_request.changed_files);
  let reads = 0;
  assert.throws(() => capture(Buffer.from(JSON.stringify(metadata)), w.repo, w.output,
    manifest.head_sha, () => { reads += 1; return Buffer.alloc(0); }), /authenticated PR file records/);
  assert.equal(reads, 0, 'refuse the substituted set before reading any source');
  assert.equal(fs.existsSync(w.output), false);
});

// Historical replay requires the bound objects, just like the contract witnesses.
// Ordinary CI still runs the fixture and retained-metadata refusal tests above.
if (process.env.ORCH_CONTRACT_SOURCE_MANIFEST) {
  test('replays the retained exact-head receipt into a complete byte-identical source bundle', (t) => {
    const w = world(t);
    const manifestPath = path.resolve(process.env.ORCH_CONTRACT_SOURCE_MANIFEST);
    const manifest = JSON.parse(fs.readFileSync(manifestPath));
    const receipt = fs.readFileSync(path.join(path.dirname(manifestPath), 'pr-438-source-metadata.json'));
    const report = capture(receipt, path.resolve(__dirname, '..'), w.output,
      process.env.ORCH_CONTRACT_EXPECTED_HEAD);
    assert.deepEqual(report, manifest, 'reacquisition preserves every retained binding and disposition');
    assert.equal(report.source_status, 'COMPLETE');
    assert.equal(report.files.length, 22);
    assert.equal(report.retrieved_changed_files, 14);
    for (const file of report.files) {
      const bytes = fs.readFileSync(path.join(w.output, file.artifact));
      assert.equal(bytes.length, file.expected_bytes);
      assert.equal(blobSha(bytes), file.blob_sha);
      assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), file.sha256);
    }
    assert.deepEqual(fs.readFileSync(path.join(w.output, 'metadata.json')), receipt);
    assert.equal(report.review_status, 'PENDING');
    assert.equal(report.deployment_status, 'NOT_OBSERVED');
  });
}

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
