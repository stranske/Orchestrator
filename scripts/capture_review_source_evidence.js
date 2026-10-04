'use strict';

// Preserve complete review inputs from local Git objects, bound to an exported
// authenticated GitHub PR/commit/tree response. This observes source only: it
// never turns retrieval, workflow success, or a deployment claim into PASS.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function blobSha(bytes) {
  return crypto.createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
}

function validSha(value) {
  return typeof value === 'string' && /^[0-9a-f]{40}$/.test(value);
}

function validate(metadata, expectedHead) {
  if (!validSha(expectedHead) || metadata.pull_request?.head_sha !== expectedHead
    || metadata.commit?.sha !== expectedHead) {
    throw new Error('PR and commit must bind to the requested exact head');
  }
  if (!validSha(metadata.commit.tree?.sha)
    || metadata.tree?.sha !== metadata.commit.tree.sha || metadata.tree.truncated !== false) {
    throw new Error('complete recursive tree must bind to the exact-head commit tree');
  }
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(metadata.repository)
    || !Number.isSafeInteger(metadata.pr_number) || metadata.pr_number < 1
    || !Array.isArray(metadata.changed_paths) || metadata.changed_paths.length === 0
    || metadata.pull_request.changed_files !== metadata.changed_paths.length
    || new Set(metadata.changed_paths).size !== metadata.changed_paths.length
    || !Array.isArray(metadata.required_paths) || !Array.isArray(metadata.tree.tree)) {
    throw new Error('repository, PR, and complete changed/required path lists matching the PR file count are required');
  }
  const entries = new Map();
  for (const entry of metadata.tree.tree) {
    if (typeof entry.path !== 'string' || entry.path.startsWith('/')
      || entry.path.includes('\\') || entry.path.split('/').some((part) => !part || part === '..' || part === '.')
      || entries.has(entry.path) || !validSha(entry.sha)) {
      throw new Error('invalid or duplicate recursive tree binding');
    }
    entries.set(entry.path, entry);
  }
  const paths = [...new Set([...metadata.changed_paths, ...metadata.required_paths])].sort();
  if (paths.some((name) => typeof name !== 'string' || name.length === 0)) {
    throw new Error('invalid requested path');
  }
  return { entries, paths };
}

function readBlob(repo, sha) {
  const result = spawnSync('git', ['-C', repo, 'cat-file', 'blob', sha], {
    timeout: 30000, maxBuffer: 64 * 1024 * 1024,
  });
  // The object hash and byte count below independently require complete output.
  if (result.status !== 0) throw new Error('bound Git blob unavailable locally');
  return result.stdout;
}

function inside(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..'
    && !path.isAbsolute(relative));
}

function capture(metadataBytes, repo, output, expectedHead, reader = readBlob) {
  const metadata = JSON.parse(metadataBytes.toString('utf8'));
  const { entries, paths } = validate(metadata, expectedHead);
  repo = fs.realpathSync(repo);
  // Resolve the parent so a symlink cannot place evidence inside the source tree.
  output = path.join(fs.realpathSync(path.dirname(output)), path.basename(output));
  if (inside(output, repo)) throw new Error('evidence output overlaps source checkout');
  // Never overwrite an earlier bundle, including a dangling symlink at its path.
  fs.mkdirSync(output, { mode: 0o700 });
  fs.mkdirSync(path.join(output, 'blobs'), { mode: 0o700 });
  fs.writeFileSync(path.join(output, 'metadata.json'), metadataBytes, { flag: 'wx', mode: 0o600 });
  const files = paths.map((name) => {
    const entry = entries.get(name);
    const record = {
      path: name, changed: metadata.changed_paths.includes(name),
      source_url: `https://github.com/${metadata.repository}/blob/${expectedHead}/${name}`,
      status: 'UNKNOWN', owner: metadata.repository.split('/')[0],
      next_action: 'retrieve the complete exact-head blob through authenticated GitHub access and rerun capture',
    };
    if (!entry || entry.type !== 'blob' || !['100644', '100755', '120000'].includes(entry.mode)
      || !Number.isSafeInteger(entry.size) || entry.size < 0) {
      return { ...record, reason: 'requested file has no regular-file or symlink blob binding in the tree' };
    }
    Object.assign(record, { blob_sha: entry.sha, git_mode: entry.mode, expected_bytes: entry.size });
    try {
      const bytes = reader(repo, entry.sha);
      if (!Buffer.isBuffer(bytes) || bytes.length !== entry.size || blobSha(bytes) !== entry.sha) {
        throw new Error('blob bytes do not match authenticated size and Git object identity');
      }
      const relative = `blobs/${entry.sha}.blob`;
      const target = path.join(output, relative);
      if (!fs.existsSync(target)) fs.writeFileSync(target, bytes, { flag: 'wx', mode: 0o600 });
      // Read retained bytes back: a manifest must describe the artifact actually saved.
      const saved = fs.readFileSync(target);
      if (!saved.equals(bytes)) throw new Error('retained blob differs from retrieved bytes');
      return {
        path: name, changed: record.changed, source_url: record.source_url,
        blob_sha: entry.sha, git_mode: entry.mode, expected_bytes: entry.size,
        retained_bytes: saved.length, sha256: sha256(saved), artifact: relative,
        acquisition: 'local Git object verified against exported exact-head GitHub tree',
        status: 'RETRIEVED',
      };
    } catch (error) {
      // Do not retain provider stderr, which can include authentication material.
      return { ...record, reason: error.message === 'blob bytes do not match authenticated size and Git object identity'
        ? error.message : 'complete bound blob could not be retained' };
    }
  });
  const report = {
    schema_version: 1,
    repository: metadata.repository, pr_number: metadata.pr_number,
    head_sha: expectedHead, tree_sha: metadata.commit.tree.sha,
    metadata_sha256: sha256(metadataBytes),
    retrieval_transport: metadata.retrieval_transport || 'unspecified metadata export',
    source_urls: metadata.source_urls || [],
    source_status: files.every((file) => file.status === 'RETRIEVED') ? 'COMPLETE' : 'UNKNOWN',
    review_status: 'PENDING', deployment_status: 'NOT_OBSERVED',
    changed_files: metadata.changed_paths.length,
    retrieved_changed_files: files.filter((file) => file.changed && file.status === 'RETRIEVED').length,
    files,
  };
  fs.writeFileSync(path.join(output, 'manifest.json'), `${JSON.stringify(report, null, 2)}\n`,
    { flag: 'wx', mode: 0o600 });
  return report;
}

function main(args) {
  if (args.length !== 4) throw new Error(
    'usage: capture_review_source_evidence.js AUTHENTICATED_METADATA_JSON GIT_REPO OUTPUT_DIR EXPECTED_HEAD',
  );
  const [input, repo, output, head] = args;
  const report = capture(fs.readFileSync(input), repo, path.resolve(output), head);
  console.log(`${report.source_status}: ${report.retrieved_changed_files}/${report.changed_files} changed files retained; review PENDING`);
  return report.source_status === 'COMPLETE' ? 0 : 2;
}

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`capture-review-source-evidence: ${error.message}`);
    process.exitCode = 2;
  }
}

module.exports = { blobSha, capture, main };
