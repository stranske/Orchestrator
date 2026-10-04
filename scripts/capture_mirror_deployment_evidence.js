'use strict';

// Capture installed observations for post-merge review. This never installs code
// or declares deployment complete: merge/pull, live readers, runtime preservation,
// and verify:compare still require evidence from the operator.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

function run(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 30000 });
  if (result.status !== 0) throw result.error
    || new Error(`${command} observation failed: ${(result.stderr || '').trim()}`);
  return result.stdout.trim();
}

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function documentedBlock(document, heading) {
  const section = document.indexOf(heading);
  const opening = document.indexOf('```bash\n', section);
  const closing = document.indexOf('\n```', opening + 8);
  if (section < 0 || opening < 0 || closing < 0) throw new Error(`missing block: ${heading}`);
  return document.slice(opening + 8, closing);
}

function inside(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..'
    && !path.isAbsolute(relative));
}

function capture(source, mirror, receipt, registry, bin, publicationLog) {
  source = fs.realpathSync(source);
  mirror = path.join(fs.realpathSync(path.dirname(mirror)), path.basename(mirror));
  if (!fs.lstatSync(mirror).isSymbolicLink()) throw new Error('mirror is not a published generation link');
  const generation = fs.realpathSync(mirror);
  const receiptBytes = fs.readFileSync(receipt, 'utf8');
  if (!/^[0-9a-f]{64}\n$/.test(receiptBytes)) throw new Error('invalid one-line verifier receipt');
  const expected = receiptBytes.trim();
  // Use the pulled checkout's inspector, never execute code selected by the mirror.
  const actual = run(process.env.PYTHON || 'python3', [
    '-I', path.join(source, 'scripts', 'install_verified_snapshot.py'), generation, '--digest',
  ]);
  const commit = run('git', ['-C', source, 'rev-parse', 'HEAD']);
  const clean = run('git', ['--no-optional-locks', '-C', source, 'status', '--porcelain', '--untracked-files=no']) === '';
  const document = fs.readFileSync(path.join(source, 'docs', 'MIRROR_SYNC_PATCH.md'), 'utf8');
  const wrappers = [
    ['orch-mirror-sync.sh', '**The patch.**'],
    ['orch-sync-mirror.sh', '### Direct incumbent copier entry guard'],
  ].map(([name, heading]) => {
    const filename = path.join(bin, name);
    const bytes = fs.readFileSync(filename);
    return {
      path: fs.realpathSync(filename),
      sha256: sha256(bytes),
      documented_block_present: bytes.toString('utf8').includes(documentedBlock(document, heading)),
    };
  });
  const registryBytes = fs.readFileSync(registry);
  const shippedRegistry = fs.readFileSync(path.join(generation, 'repo_review_registry.json'));
  const logBytes = fs.readFileSync(publicationLog);
  const log = logBytes.toString('utf8');
  const successMarkers = log.includes(`installed verified snapshot ${expected.slice(0, 12)}:`)
    && log.includes('== the exact deployment snapshot above received the verdict; no second run');
  const stillActive = fs.realpathSync(mirror) === generation;
  const observationsMatch = clean && actual === expected && stillActive
    && registryBytes.equals(shippedRegistry) && successMarkers
    && wrappers.every((wrapper) => wrapper.documented_block_present);
  return {
    schema_version: 1,
    deployment_status: 'pending-operator-review',
    observed_at: new Date().toISOString(),
    checkout: { path: source, commit, tracked_files_clean: clean },
    publication: {
      mirror, generation, verifier_receipt: path.resolve(receipt),
      expected_digest: expected, observed_digest: actual, digest_matches: actual === expected,
      generation_still_active: stillActive,
      log: path.resolve(publicationLog), log_sha256: sha256(logBytes),
      success_markers_present: successMarkers,
    },
    installed_wrappers: wrappers,
    runtime_registry: {
      path: fs.realpathSync(registry), sha256: sha256(registryBytes),
      deployment_sha256: sha256(shippedRegistry), matches: registryBytes.equals(shippedRegistry),
    },
    observations_match: observationsMatch,
    remaining_evidence: [
      'gated merge and pull correspondence',
      'installed wrapper control flow and successful command exit',
      'actual launchd/cron reader commands',
      'runtime reports and markers before and after publication',
      'durable verify:compare output and source issue disposition',
    ],
  };
}

function main(args) {
  if (args.length !== 7) throw new Error(
    'usage: capture_mirror_deployment_evidence.js SOURCE MIRROR RECEIPT REGISTRY BIN PUBLICATION_LOG OUTPUT',
  );
  const [source, mirrorArgument, receipt, registry, bin, log, output] = args.map((value) => path.resolve(value));
  const mirror = path.join(fs.realpathSync(path.dirname(mirrorArgument)), path.basename(mirrorArgument));
  const target = path.join(fs.realpathSync(path.dirname(output)), path.basename(output));
  const inputs = [receipt, registry, log, ...['orch-mirror-sync.sh', 'orch-sync-mirror.sh']
    .map((name) => path.join(bin, name))].map((filename) => fs.realpathSync(filename));
  // Evidence must not change the checkout, live tree, or any retained backing tree.
  const roots = [fs.realpathSync(source), mirror, fs.realpathSync(mirror)];
  if (roots.some((root) => inside(target, root)) || inputs.includes(target)
    || path.relative(path.dirname(mirror), target).split(path.sep).some((part) =>
      part.startsWith(`${path.basename(mirror)}.generation-`)
      || part.startsWith(`${path.basename(mirror)}.retired-`))) {
    throw new Error('evidence output overlaps deployment or observation inputs');
  }
  const report = capture(source, mirror, receipt, registry, bin, log);
  // Exclusive creation preserves earlier evidence, including a dangling leaf link.
  fs.writeFileSync(target, `${JSON.stringify(report, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
  console.log(`deployment pending operator review; evidence: ${target}`);
  return report.observations_match ? 0 : 2;
}

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`capture-mirror-deployment-evidence: ${error.message}`);
    process.exitCode = 2;
  }
}

module.exports = { capture, main };
