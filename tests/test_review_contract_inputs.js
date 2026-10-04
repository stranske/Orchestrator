'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { bindReviewInputs } = require('../scripts/review_contract_inputs');

function world(t) {
  const repo = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'orch-bound-inputs-')));
  t.after(() => fs.rmSync(repo, { recursive: true, force: true }));
  function git(...args) {
    const result = spawnSync('git', ['-C', repo, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  git('init', '-q');
  const files = [];
  for (const [name, bytes, mode] of [
    ['.verify-floor.json', '{"collected": 10}\n', 0o644],
    ['scripts/program.sh', '#!/bin/sh\nexit 0\n', 0o755],
  ]) {
    const filename = path.join(repo, name);
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    fs.writeFileSync(filename, bytes, { mode });
    files.push({ path: name, changed: true, status: 'RETRIEVED',
      blob_sha: git('hash-object', '-w', filename), git_mode: mode === 0o755 ? '100755' : '100644',
      expected_bytes: Buffer.byteLength(bytes), retained_bytes: Buffer.byteLength(bytes),
      sha256: crypto.createHash('sha256').update(bytes).digest('hex') });
  }
  const head = 'a'.repeat(40);
  const manifest = { head_sha: head, source_status: 'COMPLETE', changed_files: files.length,
    retrieved_changed_files: files.length, files };
  const bind = () => bindReviewInputs(repo, Buffer.from(JSON.stringify(manifest)), head);
  return { repo, manifest, bind };
}

test('reads the historical floor from its bound object without rewriting the current floor', (t) => {
  const w = world(t);
  const current = '{"collected": 20}\n';
  fs.writeFileSync(path.join(w.repo, '.verify-floor.json'), current);
  const bound = w.bind();
  assert.equal(bound.floorBytes.toString(), '{"collected": 10}\n');
  assert.equal(fs.readFileSync(path.join(w.repo, '.verify-floor.json'), 'utf8'), current);
  assert.equal(bound.check(), 2);
  assert.match(bound.manifestSha256, /^[0-9a-f]{64}$/);
});

for (const mutation of ['bytes', 'executable mode', 'leaf symlink', 'parent symlink']) {
  test(`refuses ${mutation} drift after the witness runs`, (t) => {
    const w = world(t);
    const bound = w.bind();
    const filename = path.join(w.repo, 'scripts/program.sh');
    if (mutation === 'bytes') fs.appendFileSync(filename, '# changed\n');
    if (mutation === 'executable mode') fs.chmodSync(filename, 0o644);
    if (mutation === 'leaf symlink') {
      fs.renameSync(filename, path.join(w.repo, 'same-bytes'));
      fs.symlinkSync('../same-bytes', filename);
    }
    if (mutation === 'parent symlink') {
      fs.renameSync(path.dirname(filename), path.join(w.repo, 'same-directory'));
      fs.symlinkSync('same-directory', path.dirname(filename));
    }
    assert.throws(bound.check, /source.*(differs|symlink)/);
    assert.throws(w.bind, /source.*(differs|symlink)/);
  });
}

for (const defect of ['head', 'incomplete', 'count', 'duplicate', 'traversal', 'hash', 'size', 'blob', 'floor']) {
  test(`rejects ${defect} bindings before running a witness`, (t) => {
    const w = world(t);
    if (defect === 'head') w.manifest.head_sha = 'b'.repeat(40);
    if (defect === 'incomplete') w.manifest.source_status = 'UNKNOWN';
    if (defect === 'count') w.manifest.changed_files += 1;
    if (defect === 'duplicate') w.manifest.files[1].path = '.verify-floor.json';
    if (defect === 'traversal') w.manifest.files[1].path = '../program.sh';
    if (defect === 'hash') w.manifest.files[1].sha256 = '0'.repeat(64);
    if (defect === 'size') {
      w.manifest.files[1].expected_bytes += 1;
      w.manifest.files[1].retained_bytes += 1;
    }
    if (defect === 'blob') w.manifest.files[1].blob_sha = '0'.repeat(40);
    if (defect === 'floor') w.manifest.files[0].path = 'not-the-floor.json';
    assert.throws(w.bind);
  });
}

test('refuses a manifest omitting a required executable input', (t) => {
  const w = world(t);
  assert.throws(() => bindReviewInputs(w.repo, Buffer.from(JSON.stringify(w.manifest)),
    w.manifest.head_sha, ['scripts/omitted.sh']), /omits an executed review input/);
});
