'use strict';

// Bind focused witnesses to a previously authenticated source manifest. The
// caller supplies that provenance; this helper does not authenticate exports.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { blobSha } = require('./capture_review_source_evidence');

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function bindReviewInputs(repo, manifestBytes, expectedHead, requiredPaths = [], expectedManifestSha256) {
  const manifestSha256 = sha256(manifestBytes);
  // A head binding identifies source, but a receipt also identifies the exact
  // manifest bytes it used. Refuse a replaced or re-exported receipt input even
  // when it still describes the same head and file objects.
  if (expectedManifestSha256 !== undefined
    && (!/^[0-9a-f]{64}$/.test(expectedManifestSha256)
      || manifestSha256 !== expectedManifestSha256)) {
    throw new Error('source manifest bytes differ from the witness receipt digest');
  }
  repo = fs.realpathSync(repo);
  const manifest = JSON.parse(manifestBytes.toString('utf8'));
  if (!/^[0-9a-f]{40}$/.test(expectedHead) || manifest.head_sha !== expectedHead
    || manifest.source_status !== 'COMPLETE' || !Array.isArray(manifest.files)
    || manifest.files.length === 0 || !Number.isSafeInteger(manifest.changed_files)
    || manifest.changed_files < 1
    || manifest.retrieved_changed_files !== manifest.changed_files
    || manifest.files.filter((file) => file.changed === true).length !== manifest.changed_files) {
    throw new Error('complete source manifest must match the requested exact head');
  }
  const names = new Set();
  const bytesByPath = new Map();
  for (const file of manifest.files) {
    if (typeof file.path !== 'string' || file.path.startsWith('/') || file.path.includes('\\')
      || file.path.split('/').some((part) => !part || part === '.' || part === '..')
      || names.has(file.path) || file.status !== 'RETRIEVED'
      || !/^[0-9a-f]{40}$/.test(file.blob_sha)
      || !/^[0-9a-f]{64}$/.test(file.sha256)
      || !['100644', '100755'].includes(file.git_mode)
      || !Number.isSafeInteger(file.expected_bytes) || file.expected_bytes < 0
      || file.retained_bytes !== file.expected_bytes) {
      throw new Error('invalid source file binding');
    }
    names.add(file.path);
    const result = spawnSync('git', ['-C', repo, 'cat-file', 'blob', file.blob_sha], {
      timeout: 30000, maxBuffer: 64 * 1024 * 1024,
    });
    const bytes = result.stdout;
    if (result.status !== 0 || !Buffer.isBuffer(bytes)
      || bytes.length !== file.expected_bytes || blobSha(bytes) !== file.blob_sha
      || sha256(bytes) !== file.sha256) {
      throw new Error(`complete bound Git blob unavailable or corrupt: ${file.path}`);
    }
    bytesByPath.set(file.path, bytes);
  }
  if (!bytesByPath.has('.verify-floor.json')) throw new Error('exact-head floor binding required');
  if (requiredPaths.some((name) => !bytesByPath.has(name))) {
    throw new Error('manifest omits an executed review input');
  }

  function check() {
    // The historical floor is read from its verified object, never restored into
    // the checkout. All other inputs must be the bytes actually executed/read.
    for (const file of manifest.files) {
      if (file.path === '.verify-floor.json') continue;
      const filename = path.join(repo, file.path);
      for (let parent = path.dirname(filename); parent !== repo; parent = path.dirname(parent)) {
        if (fs.lstatSync(parent).isSymbolicLink()) {
          throw new Error(`source binding has a symlink parent: ${file.path}`);
        }
      }
      const info = fs.lstatSync(filename);
      if (!info.isFile() || !fs.readFileSync(filename).equals(bytesByPath.get(file.path))
        || Boolean(info.mode & 0o111) !== (file.git_mode === '100755')) {
        throw new Error(`executed source differs from exact-head binding: ${file.path}`);
      }
    }
    return manifest.files.length;
  }
  check();
  return { check, floorBytes: bytesByPath.get('.verify-floor.json'),
    manifestSha256, headSha: expectedHead };
}

module.exports = { bindReviewInputs };
