'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { capture } = require('../scripts/capture_mirror_deployment_evidence');

const repo = path.resolve(__dirname, '..');
const collector = path.join(repo, 'scripts', 'capture_mirror_deployment_evidence.js');
const python = process.env.PYTHON || 'python3';

function run(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

function block(document, heading) {
  const opening = document.indexOf('```bash\n', document.indexOf(heading)) + 8;
  return document.slice(opening, document.indexOf('\n```', opening));
}

function world(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'orch-evidence-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, 'pulled checkout with spaces');
  const snapshot = path.join(root, 'snapshot');
  const mirror = path.join(root, 'mirror');
  const registry = path.join(root, 'runtime', 'repo_review_registry.json');
  const bin = path.join(root, 'bin');
  const receipt = path.join(root, 'verified-payload.sha256');
  const log = path.join(root, 'publication.log');
  const publicationExit = path.join(root, 'publication-exit.txt');
  const output = path.join(root, 'evidence.json');
  for (const dir of [path.join(source, 'scripts'), path.join(source, 'docs'),
    path.join(snapshot, 'scripts'), path.join(mirror, 'docs', 'reports'), bin]) {
    fs.mkdirSync(dir, { recursive: true });
  }
  const installer = path.join(source, 'scripts', 'install_verified_snapshot.py');
  fs.copyFileSync(path.join(repo, 'scripts', 'install_verified_snapshot.py'), installer);
  fs.copyFileSync(installer, path.join(snapshot, 'scripts', 'install_verified_snapshot.py'));
  const document = fs.readFileSync(path.join(repo, 'docs', 'MIRROR_SYNC_PATCH.md'), 'utf8');
  fs.writeFileSync(path.join(source, 'docs', 'MIRROR_SYNC_PATCH.md'), document);
  fs.writeFileSync(path.join(bin, 'orch-mirror-sync.sh'), block(document, '**The patch.**'));
  fs.writeFileSync(path.join(bin, 'orch-sync-mirror.sh'),
    block(document, '### Direct incumbent copier entry guard'));
  fs.writeFileSync(path.join(snapshot, 'orchestrate.sh'), '#!/bin/sh\n');
  fs.writeFileSync(path.join(snapshot, 'module.py'), 'VALUE = "verified"\n');
  fs.writeFileSync(path.join(snapshot, 'repo_review_registry.json'), '{"repos": []}\n');
  fs.writeFileSync(path.join(mirror, 'docs', 'reports', 'runtime.md'), 'runtime report\n');
  const digest = run(python, [installer, snapshot, '--digest']).trim();
  fs.writeFileSync(receipt, `${digest}\n`);
  const publication = run(python, [installer, snapshot, mirror, '--expected-digest', digest,
    '--runtime-registry', registry]);
  fs.writeFileSync(log, `${publication}\n== the exact deployment snapshot above received the verdict; no second run\n`);
  fs.writeFileSync(publicationExit, '0\n');
  run('git', ['-C', source, 'init', '-q']);
  run('git', ['-C', source, 'add', '.']);
  run('git', ['-C', source, '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'pulled source fixture']);
  return { root, source, snapshot, mirror, registry, bin, receipt, log, publicationExit,
    output, installer, digest };
}

function collect(w, output = w.output) {
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  return spawnSync(process.execPath, [collector, w.source, w.mirror, w.receipt, w.registry,
    w.bin, w.log, w.publicationExit, output], { encoding: 'utf8', timeout: 10000, env });
}

test('captures real publisher observations without declaring deployment complete', (t) => {
  const w = world(t);
  const result = collect(w);
  assert.equal(result.status, 0, result.stderr);
  const evidence = JSON.parse(fs.readFileSync(w.output));
  assert.equal(evidence.deployment_status, 'pending-operator-review');
  assert.equal(evidence.observations_match, true);
  assert.equal(evidence.checkout.commit, run('git', ['-C', w.source, 'rev-parse', 'HEAD']).trim());
  assert.equal(evidence.publication.generation, fs.realpathSync(w.mirror));
  assert.equal(evidence.publication.expected_digest, w.digest);
  assert.equal(evidence.publication.observed_digest, w.digest);
  assert.equal(evidence.publication.exit_status, 0);
  assert.equal(evidence.publication.command_succeeded, true);
  assert.equal(evidence.publication.exit_status_file, w.publicationExit);
  assert.match(evidence.publication.exit_status_sha256, /^[0-9a-f]{64}$/);
  assert.equal(evidence.runtime_registry.matches, true);
  assert.equal(evidence.installed_wrappers.length, 2);
  for (const wrapper of evidence.installed_wrappers) {
    assert.match(wrapper.sha256, /^[0-9a-f]{64}$/);
    assert.equal(wrapper.documented_block_present, true);
  }
  assert.ok(evidence.remaining_evidence.includes('actual launchd/cron reader commands'));
  assert.equal(fs.readFileSync(path.join(w.mirror, 'docs', 'reports', 'runtime.md'), 'utf8'), 'runtime report\n');
  assert.equal(fs.readFileSync(w.registry, 'utf8'), '{"repos": []}\n');
});

for (const defect of ['digest', 'permissions', 'registry', 'wrapper', 'copier guard',
  'unverified', 'unfinished wrapper', 'dirty checkout']) {
  test(`records ${defect} mismatch and returns failure`, (t) => {
    const w = world(t);
    if (defect === 'digest') fs.writeFileSync(path.join(w.mirror, 'module.py'), 'CHANGED = True\n');
    if (defect === 'permissions') fs.chmodSync(path.join(w.mirror, 'module.py'), 0o700);
    if (defect === 'registry') fs.writeFileSync(w.registry, '{"old": true}\n');
    if (defect === 'wrapper') fs.writeFileSync(path.join(w.bin, 'orch-mirror-sync.sh'), 'old wrapper\n');
    if (defect === 'copier guard') fs.writeFileSync(path.join(w.bin, 'orch-sync-mirror.sh'), 'old copier\n');
    if (defect === 'unverified') fs.writeFileSync(w.log, `installed UNVERIFIED snapshot ${w.digest.slice(0, 12)}:\n`);
    if (defect === 'unfinished wrapper') fs.writeFileSync(w.log, `installed verified snapshot ${w.digest.slice(0, 12)}:\n`);
    if (defect === 'dirty checkout') fs.appendFileSync(w.installer, '\n# local change\n');
    const result = collect(w);
    assert.equal(result.status, 2, result.stderr);
    const evidence = JSON.parse(fs.readFileSync(w.output));
    assert.equal(evidence.observations_match, false);
    assert.equal(evidence.deployment_status, 'pending-operator-review');
  });
}

for (const status of [1, 3, 137, 255]) {
  test(`records failed publication exit ${status} despite matching success markers`, (t) => {
    const w = world(t);
    fs.writeFileSync(w.publicationExit, `${status}\n`);
    const evidence = capture(w.source, w.mirror, w.receipt, w.registry, w.bin, w.log,
      w.publicationExit);
    assert.equal(evidence.observations_match, false);
    assert.equal(evidence.publication.exit_status, status);
    assert.equal(evidence.publication.command_succeeded, false);
    assert.equal(evidence.publication.success_markers_present, true);
    assert.equal(evidence.deployment_status, 'pending-operator-review');
    const result = collect(w);
    assert.equal(result.status, 2, result.stderr);
    assert.deepEqual(JSON.parse(fs.readFileSync(w.output)).publication, evidence.publication);
  });
}

for (const status of ['', '0', '00\n', '-1\n', '256\n', '0\n1\n', '0\n\n', '0\r\n', 'ok\n']) {
  test(`rejects malformed publication exit ${JSON.stringify(status)}`, (t) => {
    const w = world(t);
    fs.writeFileSync(w.publicationExit, status);
    assert.throws(() => capture(w.source, w.mirror, w.receipt, w.registry, w.bin, w.log,
      w.publicationExit), /invalid one-line publication exit status/);
    const result = collect(w);
    assert.equal(result.status, 2);
    assert.equal(fs.existsSync(w.output), false);
  });
}

test('requires retained exit evidence even when the publication log indicates success', (t) => {
  const w = world(t);
  fs.unlinkSync(w.publicationExit);
  assert.equal(collect(w).status, 2);
  assert.equal(fs.existsSync(w.output), false);
});

for (const receipt of ['', 'a'.repeat(64), 'A'.repeat(64) + '\n', 'a'.repeat(64) + '\nextra\n']) {
  test(`rejects malformed verifier receipt ${JSON.stringify(receipt)}`, (t) => {
    const w = world(t);
    fs.writeFileSync(w.receipt, receipt);
    assert.equal(collect(w).status, 2);
    assert.equal(fs.existsSync(w.output), false);
  });
}

test('refuses to replace earlier evidence or write into deployment and input paths', (t) => {
  const w = world(t);
  fs.writeFileSync(w.output, 'earlier evidence\n');
  assert.equal(collect(w).status, 2);
  assert.equal(fs.readFileSync(w.output, 'utf8'), 'earlier evidence\n');
  const retired = path.join(w.root, 'mirror.retired-saved');
  fs.mkdirSync(retired);
  for (const output of [path.join(w.source, 'evidence.json'), path.join(w.mirror, 'evidence.json'),
    path.join(retired, 'evidence.json'), w.receipt, w.registry, w.log, w.publicationExit,
    path.join(w.bin, 'orch-sync-mirror.sh')]) {
    const before = fs.existsSync(output) ? fs.readFileSync(output) : null;
    const result = collect(w, output);
    assert.equal(result.status, 2, result.stderr);
    if (before) assert.deepEqual(fs.readFileSync(output), before);
    else assert.equal(fs.existsSync(output), false);
  }
});

test('flags a publication overlapping evidence collection instead of mixing observations', (t) => {
  const w = world(t);
  const readFile = fs.readFileSync;
  fs.readFileSync = function(filename, ...args) {
    if (filename === w.log) {
      run(python, [w.installer, w.snapshot, w.mirror, '--expected-digest', w.digest,
        '--runtime-registry', w.registry]);
    }
    return readFile.call(this, filename, ...args);
  };
  let evidence;
  try {
    evidence = capture(w.source, w.mirror, w.receipt, w.registry, w.bin, w.log,
      w.publicationExit);
  } finally {
    fs.readFileSync = readFile;
  }
  assert.equal(evidence.publication.digest_matches, true);
  assert.equal(evidence.publication.generation_still_active, false);
  assert.equal(evidence.observations_match, false);
});

for (const disposition of ['new receipt', 'existing receipt', 'missing parent']) {
  test(`documented verified wrapper retains evidence safely with ${disposition}`, (t) => {
    const w = world(t);
    const document = fs.readFileSync(path.join(w.source, 'docs', 'MIRROR_SYNC_PATCH.md'), 'utf8');
    const wrapper = path.join(w.root, 'wrapper.sh');
    fs.writeFileSync(wrapper, '#!/bin/bash\nset -euo pipefail\n'
      + 'SRC="$1"\nMIRROR="$2"\nRUN_VERIFY=1\nALLOW_UNMERGED=0\n'
      + block(document, '**The patch.**') + '\n');
    fs.writeFileSync(path.join(w.source, 'scripts', 'verify_before_sync.sh'),
      '#!/bin/bash\nset -euo pipefail\n'
      + 'cp -R "$TEST_SNAPSHOT" "$2"\n'
      + 'printf "%s\\n" "$TEST_DIGEST" > "$4"\n');
    const retained = path.join(w.root, disposition === 'missing parent' ? 'absent/receipt' : 'retained.sha256');
    if (disposition === 'existing receipt') fs.writeFileSync(retained, 'earlier receipt\n');
    const originalGeneration = fs.realpathSync(w.mirror);
    const scratchHome = path.join(w.root, 'scratch-home');
    const result = spawnSync('bash', [wrapper, w.source, w.mirror], {
      encoding: 'utf8', timeout: 10000,
      env: { ...process.env, HOME: scratchHome, TEST_SNAPSHOT: w.snapshot, TEST_DIGEST: w.digest,
        ORCH_VERIFIED_RECEIPT_OUT: retained },
    });
    if (disposition === 'new receipt') {
      assert.equal(result.status, 0, result.stderr);
      assert.equal(fs.readFileSync(retained, 'utf8'), `${w.digest}\n`);
      assert.notEqual(fs.realpathSync(w.mirror), originalGeneration);
      assert.match(result.stdout, /the exact deployment snapshot above received the verdict/);
    } else {
      assert.equal(result.status, 3, result.stderr);
      assert.equal(fs.realpathSync(w.mirror), originalGeneration);
      assert.equal(fs.existsSync(path.join(scratchHome, '.codex', 'orchestrator')), false);
      assert.match(result.stderr, /cannot retain verifier receipt; live mirror untouched/);
      if (disposition === 'existing receipt') {
        assert.equal(fs.readFileSync(retained, 'utf8'), 'earlier receipt\n');
      }
    }
  });
}
