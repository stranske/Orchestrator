'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { test } = require('node:test');

const execFileAsync = promisify(execFile);

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { timeout: 15000, ...options });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (data) => { stdout += data; });
    child.stderr.on('data', (data) => { stderr += data; });
    child.on('error', reject);
    child.on('close', (status, signal) => resolve({ status, signal, stdout, stderr }));
  });
}

const validProvenance = {
  published_commit: 'a'.repeat(40),
  deployed_at: '2026-11-05T00:00:00Z',
  deployment_evidence: 'publication-audit/run-123/successful-push',
};
const invalidProvenance = [
  ['missing file', undefined, /No such file/],
  ['invalid JSON', '{', /Expecting property name/],
  ['null provenance', 'null', /provenance must be an object/],
  ['array provenance', '[]', /provenance must be an object/],
  ['missing commit', { ...validProvenance, published_commit: undefined }, /full Git commit SHA/],
  ['abbreviated commit', { ...validProvenance, published_commit: 'a'.repeat(7) }, /full Git commit SHA/],
  ...[
    ['null commit', null],
    ['numeric commit', 123],
    ['array commit', ['a'.repeat(40)]],
    ['object commit', { sha: 'a'.repeat(40) }],
    ['non-hex SHA-1 commit', `${'a'.repeat(39)}g`],
    ['non-hex SHA-256 commit', `${'a'.repeat(63)}g`],
  ].map(([name, published_commit]) => [
    name, { ...validProvenance, published_commit }, /full Git commit SHA/,
  ]),
  ['missing deployment time', { ...validProvenance, deployed_at: undefined }, /deployed_at/],
  ['malformed deployment time', { ...validProvenance, deployed_at: 'yesterday' }, /deployed_at/],
  ['date without instant', { ...validProvenance, deployed_at: '2026-11-05' }, /deployed_at/],
  ['timezone missing', { ...validProvenance, deployed_at: '2026-11-05T00:00:00' }, /deployed_at/],
  ...[
    ['null deployment time', null, /explicit UTC timestamp/],
    ['boolean deployment time', true, /explicit UTC timestamp/],
    ['numeric deployment epoch', 1793836800, /explicit UTC timestamp/],
    ['impossible deployment date', '2026-02-30T00:00:00Z', /explicit UTC timestamp/],
    ['non-UTC deployment offset', '2026-11-05T01:00:00+01:00', /UTC timestamp with whole seconds/],
    ['fractional deployment second', '2026-11-05T00:00:00.000001Z', /UTC timestamp with whole seconds/],
    ['deployment at Unix epoch', '1970-01-01T00:00:00Z', /after the Unix epoch/],
    ['deployment before Unix epoch', '1969-12-31T23:59:59Z', /after the Unix epoch/],
  ].map(([name, deployed_at, diagnostic]) => [
    name, { ...validProvenance, deployed_at }, diagnostic,
  ]),
  ['future deployment', { ...validProvenance, deployed_at: '9999-01-01T00:00:00Z' }, /in the future/],
  ['missing audit evidence', { ...validProvenance, deployment_evidence: undefined }, /deployment_evidence/],
  ['blank audit evidence', { ...validProvenance, deployment_evidence: '  ' }, /deployment_evidence/],
  // Truthy JSON containers and numbers cannot stand in for an audit reference.
  ...[
    ['null audit evidence', null],
    ['boolean audit evidence', true],
    ['numeric audit evidence', 123],
    ['array audit evidence', [validProvenance.deployment_evidence]],
    ['object audit evidence', { reference: validProvenance.deployment_evidence }],
  ].map(([name, deployment_evidence]) => [
    name, { ...validProvenance, deployment_evidence }, /deployment_evidence/,
  ]),
];

const invalidPublishedContracts = [
  ['missing challengers', (doc) => { delete doc.exploration.challengers; }, /contract is malformed/],
  ['array challengers', (doc) => { doc.exploration.challengers = []; }, /contract is malformed/],
  ['missing task', (doc) => { delete doc.exploration.challengers.review; }, /contract is malformed/],
  ['extra task', (doc) => { doc.exploration.challengers.unknown = 'codex'; }, /contract is malformed/],
  ['hosted-unrunnable challenger', (doc) => { doc.exploration.challengers.review = 'gemini'; }, /contract is malformed/],
  ['non-string challenger', (doc) => { doc.exploration.challengers.review = []; }, /contract is malformed/],
  ['missing generation time', (doc) => { delete doc.generated_at; }, /generated_at/],
  ['malformed generation time', (doc) => { doc.generated_at = 'yesterday'; }, /generated_at/],
  // Parseable clocks still need an explicit UTC instant with whole seconds.
  // Validate the artifact's clock independently of the valid audit timestamp.
  ...[
    ['generation timezone missing', '2026-11-04T10:00:00'],
    ['non-UTC generation offset', '2026-11-04T11:00:00+01:00'],
    ['fractional generation second', '2026-11-04T10:00:00.000001Z'],
  ].map(([name, timestamp]) => [
    name, (doc) => { doc.generated_at = timestamp; },
    /generated_at must be a UTC timestamp with whole seconds/,
  ]),
];

// Each case needs its own history: an earlier malformed marked artifact would
// otherwise stop verification before the defect under test is reached.
for (const [name, corrupt, diagnostic] of invalidPublishedContracts) {
  test(`deployment CLI refuses ${name} even after a later repair`, async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-invalid-publication-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const home = path.join(root, 'home');
    const state = path.join(root, 'state');
    const repositories = path.join(root, 'repos');
    const checkout = path.join(repositories, 'stranske__Orchestrator');
    const remote = path.join(root, 'remote.git');
    const audit = path.join(root, 'audit.json');
    const receipt = path.join(state, 'route-weights-challenger-deployment.json');
    const database = path.join(root, 'absent.db');
    for (const directory of [home, state, repositories]) fs.mkdirSync(directory);
    const env = {
      PATH: process.env.PATH,
      HOME: home,
      PYTHONPATH: modules,
      PYTHONDONTWRITEBYTECODE: '1',
      ORCH_STATE_DIR: state,
      ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
      ORCH_REPOS_DIR: repositories,
      ORCH_FEEDBACK_DB: database,
      ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
      HANDOFF_DIR: path.join(root, 'handoff'),
      GIT_CONFIG_NOSYSTEM: '1',
      GIT_AUTHOR_NAME: 'Deployment Test',
      GIT_AUTHOR_EMAIL: 'deploy@example.invalid',
      GIT_COMMITTER_NAME: 'Deployment Test',
      GIT_COMMITTER_EMAIL: 'deploy@example.invalid',
    };
    async function git(directory, ...args) {
      const result = await run('git', ['-C', directory, ...args], { env });
      assert.equal(result.status, 0, result.stderr);
      return result.stdout.trim();
    }
    const branch = 'exports/route-weights';
    const artifact = path.join(checkout, 'config', 'route-weights.json');
    await git(root, 'init', '--bare', '--initial-branch=main', remote);
    await git(root, 'clone', remote, checkout);
    const valid = {
      generated_at: '2026-11-04T10:00:00Z',
      exploration: {
        challenger_filter: 'hosted-runnable-alternatives/v1',
        challengers: Object.fromEntries(
          ['implement', 'review', 'testgen', 'mechanical', 'codemod', 'cross_repo']
            .map((task) => [task, 'codex'])),
      },
    };
    const invalid = structuredClone(valid);
    corrupt(invalid);
    async function publishFixture(document) {
      fs.mkdirSync(path.dirname(artifact), { recursive: true });
      fs.writeFileSync(artifact, JSON.stringify(document));
      await git(checkout, 'add', '.');
      await git(checkout, 'commit', '-m', 'publication fixture');
      await git(checkout, 'push', 'origin', `HEAD:${branch}`);
      return git(remote, 'rev-parse', branch);
    }
    async function refuse(commit, retained) {
      fs.writeFileSync(audit, JSON.stringify({
        published_commit: commit,
        deployed_at: '2026-11-04T11:00:00Z',
        deployment_evidence: 'fixture-publication-audit/successful-push',
      }));
      const modified = retained === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
      const result = await run(process.env.PYTHON || 'python3', [
        path.join(modules, 'route_weights_export.py'),
        '--record-challenger-deployment', audit, '--state-dir', state,
      ], { cwd: root, env });
      assert.equal(result.status, 1, result.stderr);
      assert.equal(result.stdout, '');
      assert.match(result.stderr, /deployment provenance refused:/);
      assert.match(result.stderr, diagnostic);
      assert.doesNotMatch(result.stderr, /Traceback/);
      if (retained === null) {
        assert.equal(fs.existsSync(receipt), false);
      } else {
        assert.equal(fs.readFileSync(receipt, 'utf8'), retained);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
      }
    }
    const first = await publishFixture(invalid);
    await refuse(first, null);
    const repaired = await publishFixture(valid);
    assert.notEqual(first, repaired);
    for (const retained of [null, '{"earlier_receipt":"preserve"}\n']) {
      if (retained !== null) fs.writeFileSync(receipt, retained);
      // Neither the invalid original nor the repaired tip establishes a valid
      // first publication. A later repair must not reset the observation clock.
      await refuse(first, retained);
      await refuse(repaired, retained);
    }
    assert.equal(await git(remote, 'rev-parse', branch), repaired);
    assert.equal(fs.existsSync(database), false);
    assert.deepEqual(fs.readdirSync(home), []);
  });
}

test('deployment CLI records the export merge rather than an unpublished or side-branch commit', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-deployment-merge-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home');
  const state = path.join(root, 'state');
  const repositories = path.join(root, 'repos');
  const checkout = path.join(repositories, 'stranske__Orchestrator');
  const remote = path.join(root, 'remote.git');
  const audit = path.join(root, 'audit.json');
  const receipt = path.join(state, 'route-weights-challenger-deployment.json');
  const database = path.join(root, 'absent.db');
  for (const directory of [home, state, repositories]) fs.mkdirSync(directory);
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: state,
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_REPOS_DIR: repositories,
    ORCH_FEEDBACK_DB: database,
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
    HANDOFF_DIR: path.join(root, 'handoff'),
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_AUTHOR_NAME: 'Deployment Test',
    GIT_AUTHOR_EMAIL: 'deploy@example.invalid',
    GIT_COMMITTER_NAME: 'Deployment Test',
    GIT_COMMITTER_EMAIL: 'deploy@example.invalid',
  };
  async function git(directory, ...args) {
    const result = await run('git', ['-C', directory, ...args], { env });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  const branch = 'exports/route-weights';
  const artifact = path.join(checkout, 'config', 'route-weights.json');
  const document = {
    generated_at: '2026-11-04T10:00:00Z',
    exploration: {
      challenger_filter: 'hosted-runnable-alternatives/v1',
      challengers: Object.fromEntries(
        ['implement', 'review', 'testgen', 'mechanical', 'codemod', 'cross_repo']
          .map((task) => [task, null])),
    },
  };
  await git(root, 'init', '--bare', '--initial-branch=main', remote);
  await git(root, 'clone', remote, checkout);
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  fs.writeFileSync(artifact, JSON.stringify({ generated_at: document.generated_at }));
  await git(checkout, 'add', '.');
  await git(checkout, 'commit', '-m', 'unmarked export fixture');
  await git(checkout, 'switch', '-c', branch);
  await git(checkout, 'push', 'origin', branch);
  const initial = await git(remote, 'rev-parse', branch);

  // A valid artifact exists locally, then on another remote branch. Neither
  // establishes a publication on the export branch named in the receipt.
  await git(checkout, 'switch', '-c', 'filter-candidate');
  fs.writeFileSync(artifact, JSON.stringify(document));
  await git(checkout, 'add', '.');
  await git(checkout, 'commit', '-m', 'candidate filter fixture');
  const candidate = await git(checkout, 'rev-parse', 'HEAD');
  const record = async (commit) => {
    fs.writeFileSync(audit, JSON.stringify({
      published_commit: commit,
      deployed_at: '2026-11-04T11:00:00Z',
      deployment_evidence: 'fixture-publication-audit/successful-push',
    }));
    return run(process.env.PYTHON || 'python3', [
      path.join(modules, 'route_weights_export.py'),
      '--record-challenger-deployment', audit, '--state-dir', state,
    ], { cwd: root, env });
  };
  const refuse = async (commit) => {
    const result = await record(commit);
    assert.equal(result.status, 1, result.stderr);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /not the first verified challenger-filter publication/);
    assert.doesNotMatch(result.stderr, /Traceback/);
    assert.deepEqual(fs.readdirSync(state), []);
  };
  await refuse(candidate);
  await git(checkout, 'push', 'origin', 'filter-candidate');
  await refuse(candidate);
  assert.equal(await git(remote, 'rev-parse', branch), initial);

  // A merge is the first commit exposing the filter on the export branch.
  // The candidate is now reachable, but it was never the published branch tip.
  await git(checkout, 'switch', branch);
  await git(checkout, 'merge', '--no-ff', 'filter-candidate', '-m', 'export filter fixture');
  const merged = await git(checkout, 'rev-parse', 'HEAD');
  assert.notEqual(merged, candidate);
  await refuse(merged);
  await git(checkout, 'push', 'origin', branch);
  assert.equal(await git(remote, 'rev-parse', `${branch}^2`), candidate);
  await refuse(candidate);
  const accepted = await record(merged);
  assert.equal(accepted.status, 0, accepted.stderr);
  const recorded = JSON.parse(accepted.stdout);
  const publishedDocument = JSON.parse(await git(remote, 'show', `${merged}:config/route-weights.json`));
  assert.ok(Object.values(publishedDocument.exploration.challengers).every((agent) => agent === null),
    'a verified filter may have no runnable alternatives for any task');
  assert.equal(recorded.branch, branch);
  assert.equal(recorded.published_commit, merged);
  assert.equal(recorded.generated_at, document.generated_at);
  assert.equal(recorded.deployed_at, '2026-11-04T11:00:00Z');
  assert.equal(recorded.deployed_epoch, Date.parse(recorded.deployed_at) / 1000);
  assert.deepEqual(JSON.parse(fs.readFileSync(receipt, 'utf8')), recorded);
  assert.equal(await git(remote, 'rev-parse', branch), merged);
  assert.equal(fs.existsSync(database), false);
  assert.deepEqual(fs.readdirSync(home), []);
});

test('deployment CLI refuses truncated history and recovers the first publication after unshallowing', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-deployment-shallow-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home');
  const state = path.join(root, 'state');
  const repositories = path.join(root, 'repos');
  const checkout = path.join(repositories, 'stranske__Orchestrator');
  const publisher = path.join(root, 'publisher');
  const remote = path.join(root, 'remote.git');
  const audit = path.join(root, 'audit.json');
  const receipt = path.join(state, 'route-weights-challenger-deployment.json');
  const database = path.join(root, 'absent.db');
  for (const directory of [home, state, repositories]) fs.mkdirSync(directory);
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: state,
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_REPOS_DIR: repositories,
    ORCH_FEEDBACK_DB: database,
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
    HANDOFF_DIR: path.join(root, 'handoff'),
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_AUTHOR_NAME: 'Deployment Test',
    GIT_AUTHOR_EMAIL: 'deploy@example.invalid',
    GIT_COMMITTER_NAME: 'Deployment Test',
    GIT_COMMITTER_EMAIL: 'deploy@example.invalid',
  };
  async function git(directory, ...args) {
    const result = await run('git', ['-C', directory, ...args], { env });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  const branch = 'exports/route-weights';
  await git(root, 'init', '--bare', '--initial-branch=main', remote);
  await git(root, 'clone', remote, publisher);
  const artifact = path.join(publisher, 'config', 'route-weights.json');
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  const document = {
    generated_at: '2026-11-04T10:00:00Z',
    exploration: {
      challenger_filter: 'hosted-runnable-alternatives/v1',
      challengers: Object.fromEntries(
        ['implement', 'review', 'testgen', 'mechanical', 'codemod', 'cross_repo']
          .map((task) => [task, 'codex'])),
    },
  };
  async function publishFixture(generatedAt) {
    fs.writeFileSync(artifact, JSON.stringify({ ...document, generated_at: generatedAt }));
    await git(publisher, 'add', '.');
    await git(publisher, 'commit', '-m', 'publication fixture');
    await git(publisher, 'push', 'origin', `HEAD:${branch}`);
    return git(remote, 'rev-parse', branch);
  }
  const first = await publishFixture(document.generated_at);
  const later = await publishFixture('2026-11-04T11:00:00Z');
  // Local-path clones ignore --depth; file:// makes this a genuinely shallow clone.
  await git(root, 'clone', '--depth=1', '--branch', branch, pathToFileURL(remote).href, checkout);
  assert.equal(await git(checkout, 'rev-parse', '--is-shallow-repository'), 'true');
  assert.equal(await git(checkout, 'rev-list', '--count', 'HEAD'), '1');
  const provenance = {
    published_commit: first,
    deployed_at: '2026-11-04T12:00:00Z',
    deployment_evidence: 'fixture-publication-audit/first-successful-push',
  };
  const record = async (commit) => {
    fs.writeFileSync(audit, JSON.stringify({ ...provenance, published_commit: commit }));
    return run(process.env.PYTHON || 'python3', [
      path.join(modules, 'route_weights_export.py'),
      '--record-challenger-deployment', audit, '--state-dir', state,
    ], { cwd: root, env });
  };
  let retained = null;
  for (const existing of [false, true]) {
    if (existing) {
      await git(checkout, 'fetch', '--unshallow', 'origin');
      assert.equal(await git(checkout, 'rev-parse', '--is-shallow-repository'), 'false');
      const recovered = await record(first);
      assert.equal(recovered.status, 0, recovered.stderr);
      const recoveredReceipt = JSON.parse(recovered.stdout);
      assert.equal(recoveredReceipt.published_commit, first);
      assert.equal(recoveredReceipt.generated_at, document.generated_at);
      assert.equal(recoveredReceipt.deployed_at, provenance.deployed_at);
      assert.equal(recoveredReceipt.deployed_epoch, Date.parse(provenance.deployed_at) / 1000);
      assert.equal(recoveredReceipt.deployment_evidence, provenance.deployment_evidence);
      retained = fs.readFileSync(receipt);
      // Reproduce a later verifier with truncated history and a valid saved receipt.
      await git(checkout, 'fetch', '--depth=1', 'origin');
    }
    const modified = retained === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
    for (const commit of [first, later]) {
      const refused = await record(commit);
      assert.equal(refused.status, 1, refused.stderr);
      assert.equal(refused.stdout, '');
      assert.match(refused.stderr, /complete export history is required/);
      assert.doesNotMatch(refused.stderr, /Traceback/);
      if (retained === null) {
        assert.equal(fs.existsSync(receipt), false);
      } else {
        assert.deepEqual(fs.readFileSync(receipt), retained);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
      }
    }
  }
  await git(checkout, 'fetch', '--unshallow', 'origin');
  const modified = fs.statSync(receipt, { bigint: true }).mtimeNs;
  const recovered = await record(first);
  assert.equal(recovered.status, 0, recovered.stderr);
  assert.deepEqual(JSON.parse(recovered.stdout), JSON.parse(retained));
  assert.deepEqual(fs.readFileSync(receipt), retained);
  assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
  const wrongCommit = await record(later);
  assert.equal(wrongCommit.status, 1, wrongCommit.stderr);
  assert.equal(wrongCommit.stdout, '');
  assert.match(wrongCommit.stderr, /not the first verified challenger-filter publication/);
  assert.doesNotMatch(wrongCommit.stderr, /Traceback/);
  assert.deepEqual(fs.readFileSync(receipt), retained);
  assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
  assert.equal(await git(remote, 'rev-parse', branch), later, 'recovery must not republish');
  assert.equal(fs.existsSync(database), false);
  assert.deepEqual(fs.readdirSync(home), []);
});

test('deployment CLI recovers fetch and receipt failures after an unchanged real publication', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-deployment-recovery-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home');
  const state = path.join(root, 'state');
  const remote = path.join(root, 'remote.git');
  const repositories = path.join(root, 'repos');
  const checkout = path.join(repositories, 'stranske__Orchestrator');
  const database = path.join(root, 'absent.db');
  const audit = path.join(root, 'audit.json');
  const receipt = path.join(state, 'route-weights-challenger-deployment.json');
  for (const directory of [home, state, repositories]) fs.mkdirSync(directory);
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: state,
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_REPOS_DIR: repositories,
    ORCH_FEEDBACK_DB: database,
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
    HANDOFF_DIR: path.join(root, 'handoff'),
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_AUTHOR_NAME: 'Deployment Test',
    GIT_AUTHOR_EMAIL: 'deploy@example.invalid',
    GIT_COMMITTER_NAME: 'Deployment Test',
    GIT_COMMITTER_EMAIL: 'deploy@example.invalid',
  };
  async function git(directory, ...args) {
    const result = await run('git', ['-C', directory, ...args], { env });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  const python = process.env.PYTHON || 'python3';
  const branch = 'exports/route-weights';
  const artifact = path.join(checkout, 'config', 'route-weights.json');
  await git(root, 'init', '--bare', '--initial-branch=main', remote);
  await git(root, 'clone', remote, checkout);
  const document = {
    generated_at: new Date(Date.now() - 3600000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
    source_version: 1,
    task_types: { implement: { evidence_ok: true } },
  };
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  fs.writeFileSync(artifact, JSON.stringify(document));
  await git(checkout, 'add', '.');
  await git(checkout, 'commit', '-m', 'unmarked publication fixture');
  await git(checkout, 'push', 'origin', `HEAD:${branch}`);
  const initial = await git(remote, 'rev-parse', branch);
  const provenance = {
    published_commit: initial,
    deployed_at: new Date(Date.now() - 1800000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
    deployment_evidence: 'fixture-publication-audit/successful-push',
  };
  const record = (recordEnv = env) => run(python, [
    path.join(modules, 'route_weights_export.py'),
    '--record-challenger-deployment', audit, '--state-dir', state,
  ], { cwd: root, env: recordEnv });
  fs.writeFileSync(audit, JSON.stringify(provenance));
  const unmarked = await record();
  assert.equal(unmarked.status, 1, unmarked.stderr);
  assert.match(unmarked.stderr, /first verified challenger-filter publication/);
  assert.equal(fs.existsSync(receipt), false);

  // Build the real filter contract, then exercise publish_document and provisioning
  // against the isolated bare remote. Neither publication nor recording is mocked.
  const publish = () => run(python, ['-c', `
import json
import sys
from pathlib import Path
import route_weights_export as export
document = json.loads(sys.argv[1])
document["exploration"] = export._exploration_block(
    1,
    {task: [] for task in export.CONSUMER_TASK_TYPES},
    {task: {"ranking": []} for task in export.CONSUMER_TASK_TYPES},
    100,
)
published = export.publish_document(document, state_dir=Path(sys.argv[2]))
print("publication_result=" + str(published))
`, JSON.stringify(document), state], { cwd: root, env });
  // A real rejected push leaves a valid local filter commit. It must not
  // establish deployment provenance until that exact commit reaches the remote.
  const rejectPush = path.join(remote, 'hooks', 'pre-receive');
  fs.writeFileSync(rejectPush, '#!/bin/sh\nexit 1\n', { mode: 0o755 });
  let rejectedCommit;
  try {
    const rejected = await publish();
    assert.equal(rejected.status, 1, rejected.stderr);
    assert.match(rejected.stderr, /remote rejected.*pre-receive hook declined/);
    assert.doesNotMatch(rejected.stdout, /publication_result=True/);
    rejectedCommit = await git(checkout, 'rev-parse', 'HEAD');
    assert.notEqual(rejectedCommit, initial);
    const localDocument = JSON.parse(await git(checkout, 'show', `${rejectedCommit}:config/route-weights.json`));
    assert.equal(localDocument.exploration.challenger_filter, 'hosted-runnable-alternatives/v1');
    assert.equal(await git(remote, 'rev-parse', branch), initial);
    fs.writeFileSync(audit, JSON.stringify({ ...provenance, published_commit: rejectedCommit }));
    const unpublished = await record();
    assert.equal(unpublished.status, 1, unpublished.stderr);
    assert.equal(unpublished.stdout, '');
    assert.match(unpublished.stderr, /not the first verified challenger-filter publication/);
    assert.doesNotMatch(unpublished.stderr, /Traceback/);
    assert.equal(fs.existsSync(receipt), false);
    assert.equal(fs.existsSync(database), false);
    assert.deepEqual(fs.readdirSync(home), []);
  } finally {
    fs.unlinkSync(rejectPush);
  }
  // Retry the rejected Git push explicitly; recording itself cannot publish.
  await git(checkout, 'push', 'origin', `HEAD:${branch}`);
  assert.equal(await git(remote, 'rev-parse', branch), rejectedCommit);
  const published = await publish();
  assert.equal(published.status, 0, published.stderr);
  assert.match(published.stdout, /publication_result=False/);
  provenance.published_commit = await git(remote, 'rev-parse', branch);
  assert.notEqual(provenance.published_commit, initial);
  fs.writeFileSync(audit, JSON.stringify(provenance));

  async function refuseHistoryReadFailures() {
    const retained = fs.existsSync(receipt) ? fs.readFileSync(receipt) : null;
    const modified = retained === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
    // Fail only the recorder's tree/blob read, after a real fetch and history
    // listing. A failed read must not be mistaken for an absent/invalid artifact.
    const script = `
import subprocess
import sys
import route_weights_export as export

operation, encoding, audit, state = sys.argv[1:]
original_run = subprocess.run

def fail_history_read(command, **kwargs):
    if command[:1] == ["git"] and command[3:4] == [operation]:
        diagnostic = "fatal: fixture publication history read failed"
        if encoding == "bytes":
            diagnostic = diagnostic.encode() + b"\\xff"
        result = subprocess.CompletedProcess(command, 128, stdout="", stderr=diagnostic)
        if kwargs.get("check"):
            result.check_returncode()
        return result
    return original_run(command, **kwargs)

subprocess.run = fail_history_read
sys.argv = ["export", "--record-challenger-deployment", audit, "--state-dir", state]
raise SystemExit(export.main())
`;
    for (const operation of ['ls-tree', 'show']) {
      for (const encoding of ['text', 'bytes']) {
        await t.test(`refuses ${operation} ${encoding} failure ${retained === null ? 'without' : 'with'} receipt`, async () => {
          const refused = await run(python, ['-c', script, operation, encoding, audit, state],
            { cwd: root, env });
          assert.equal(refused.status, 1, refused.stderr);
          assert.equal(refused.stdout, '');
          assert.match(refused.stderr, /deployment provenance refused:/);
          assert.match(refused.stderr, /stderr: fatal: fixture publication history read failed/);
          assert.doesNotMatch(refused.stderr, /Traceback|not the first verified/);
          if (retained === null) {
            assert.equal(fs.existsSync(receipt), false);
          } else {
            assert.deepEqual(fs.readFileSync(receipt), retained);
            assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
          }
          // Removing the fault must recover using the original audit, without
          // publishing a new artifact or replacing its deployment instant.
          const recovered = await record();
          assert.equal(recovered.status, 0, recovered.stderr);
          const recoveredReceipt = JSON.parse(recovered.stdout);
          assert.equal(recoveredReceipt.published_commit, provenance.published_commit);
          assert.equal(recoveredReceipt.deployed_at, provenance.deployed_at);
          assert.equal(recoveredReceipt.deployed_epoch, Date.parse(provenance.deployed_at) / 1000);
          assert.equal(recoveredReceipt.deployment_evidence, provenance.deployment_evidence);
          if (retained === null) {
            fs.unlinkSync(receipt);
          } else {
            assert.deepEqual(fs.readFileSync(receipt), retained);
            assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
          }
          assert.equal(await git(remote, 'rev-parse', branch), provenance.published_commit);
          assert.equal(fs.existsSync(database), false);
          assert.deepEqual(fs.readdirSync(home), []);
        });
      }
    }
  }
  await refuseHistoryReadFailures();

  async function refuseBeforeGeneration() {
    const before = fs.existsSync(receipt) ? fs.readFileSync(receipt) : null;
    const modified = before === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
    const early = new Date(Date.parse(document.generated_at) - 1000)
      .toISOString().replace(/\.\d{3}Z$/, 'Z');
    fs.writeFileSync(audit, JSON.stringify({ ...provenance, deployed_at: early }));
    try {
      const refused = await record();
      assert.equal(refused.status, 1, refused.stderr);
      assert.equal(refused.stdout, '');
      assert.match(refused.stderr, /deployed_at precedes the published artifact's generated_at/);
      assert.doesNotMatch(refused.stderr, /Traceback/);
      if (before === null) {
        assert.equal(fs.existsSync(receipt), false);
      } else {
        assert.deepEqual(fs.readFileSync(receipt), before);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
      }
      assert.equal(await git(remote, 'rev-parse', branch), provenance.published_commit);
    } finally {
      fs.writeFileSync(audit, JSON.stringify(provenance));
    }
  }
  await refuseBeforeGeneration();

  // Publication can occur within the same whole second as artifact generation.
  // Use separate receipt directories so these boundary checks cannot replace
  // the original audit used by the failure/recovery scenario below.
  for (const [name, timestamp] of [
    ['zulu', document.generated_at],
    ['utc-offset', document.generated_at.replace(/Z$/, '+00:00')],
  ]) {
    await t.test(`accepts deployment at generation instant with ${name}`, async () => {
      const boundaryState = path.join(root, `boundary-${name}`);
      fs.writeFileSync(audit, JSON.stringify({ ...provenance, deployed_at: timestamp }));
      try {
        const accepted = await run(python, [
          path.join(modules, 'route_weights_export.py'),
          '--record-challenger-deployment', audit, '--state-dir', boundaryState,
        ], { cwd: root, env });
        assert.equal(accepted.status, 0, accepted.stderr);
        const boundaryReceipt = JSON.parse(accepted.stdout);
        assert.equal(boundaryReceipt.deployed_at, timestamp);
        assert.equal(boundaryReceipt.deployed_epoch, Date.parse(document.generated_at) / 1000);
        assert.equal(boundaryReceipt.published_commit, provenance.published_commit);
        assert.deepEqual(JSON.parse(fs.readFileSync(
          path.join(boundaryState, path.basename(receipt)), 'utf8')), boundaryReceipt);
        assert.equal(fs.existsSync(receipt), false);
        assert.equal(await git(remote, 'rev-parse', branch), provenance.published_commit);
      } finally {
        fs.writeFileSync(audit, JSON.stringify(provenance));
      }
    });
  }

  // Cached origin refs cannot establish that a publication is still remote.
  // Exercise real fetch failure both before and after a receipt is retained.
  async function refuseUnavailableRemote() {
    const offline = path.join(root, 'offline.git');
    const before = fs.existsSync(receipt) ? fs.readFileSync(receipt) : null;
    const modified = before === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
    assert.equal(await git(checkout, 'rev-parse', `origin/${branch}`), provenance.published_commit);
    fs.renameSync(remote, offline);
    try {
      const refused = await record();
      assert.equal(refused.status, 1, refused.stderr);
      assert.equal(refused.stdout, '', 'a failed fetch must not print a successful receipt');
      assert.match(refused.stderr, /deployment provenance refused:.*fetch/);
      assert.doesNotMatch(refused.stderr, /Traceback/);
      if (before === null) {
        assert.equal(fs.existsSync(receipt), false, 'cached refs must not create provenance');
      } else {
        assert.deepEqual(fs.readFileSync(receipt), before);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
      }
      assert.equal(await git(checkout, 'rev-parse', `origin/${branch}`), provenance.published_commit);
    } finally {
      fs.renameSync(offline, remote);
    }
  }
  await refuseUnavailableRemote();

  async function refuseDeletedExportBranch() {
    const retained = fs.existsSync(receipt) ? fs.readFileSync(receipt) : null;
    const modified = retained === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
    await t.test(`refuses deleted export branch ${retained === null ? 'without' : 'with'} receipt`, async () => {
      const remoteRef = `refs/remotes/origin/${branch}`;
      assert.equal(await git(checkout, 'rev-parse', remoteRef), provenance.published_commit);
      // Delete on the server so the verifier still has a stale origin ref and
      // the valid artifact's objects. The fetch succeeds and must prune that ref.
      await git(remote, 'update-ref', '-d', `refs/heads/${branch}`);
      try {
        assert.equal(await git(checkout, 'rev-parse', remoteRef), provenance.published_commit);
        const refused = await record();
        assert.equal(refused.status, 1, refused.stderr);
        assert.equal(refused.stdout, '');
        assert.match(refused.stderr, /deployment provenance refused:/);
        assert.match(refused.stderr, /rev-list/);
        assert.doesNotMatch(refused.stderr, /Traceback/);
        const pruned = await run('git', ['-C', checkout, 'show-ref', '--verify', '--quiet', remoteRef], { env });
        assert.equal(pruned.status, 1, pruned.stderr);
        assert.equal(await git(checkout, 'cat-file', '-t', provenance.published_commit), 'commit');
        if (retained === null) {
          assert.equal(fs.existsSync(receipt), false);
        } else {
          assert.deepEqual(fs.readFileSync(receipt), retained);
          assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
        }
        assert.equal(fs.existsSync(path.join(state, `.${path.basename(receipt)}.tmp`)), false);
      } finally {
        await git(remote, 'update-ref', `refs/heads/${branch}`, provenance.published_commit);
      }
      const recovered = await record();
      assert.equal(recovered.status, 0, recovered.stderr);
      const recoveredReceipt = JSON.parse(recovered.stdout);
      assert.equal(recoveredReceipt.published_commit, provenance.published_commit);
      assert.equal(recoveredReceipt.deployed_at, provenance.deployed_at);
      assert.equal(recoveredReceipt.deployed_epoch, Date.parse(provenance.deployed_at) / 1000);
      assert.equal(recoveredReceipt.deployment_evidence, provenance.deployment_evidence);
      if (retained === null) {
        fs.unlinkSync(receipt);
      } else {
        assert.deepEqual(fs.readFileSync(receipt), retained);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
      }
      assert.equal(await git(remote, 'rev-parse', branch), provenance.published_commit);
      assert.equal(fs.existsSync(database), false);
      assert.deepEqual(fs.readdirSync(home), []);
    });
  }
  await refuseDeletedExportBranch();

  async function refuseRewrittenExportHistory() {
    const retained = fs.existsSync(receipt) ? fs.readFileSync(receipt) : null;
    const modified = retained === null ? null : fs.statSync(receipt, { bigint: true }).mtimeNs;
    const first = provenance.published_commit;
    const tree = await git(remote, 'rev-parse', `${first}^{tree}`);
    // Same valid artifact, but a different first marked commit on a rewritten
    // branch. Both objects stay available locally throughout verification.
    const replacement = await git(remote, 'commit-tree', tree, '-p', initial,
      '-m', 'replacement publication fixture');
    assert.notEqual(replacement, first);
    for (const [name, tip] of [['rewound', initial], ['rewritten', replacement]]) {
      await t.test(`refuses ${name} export history ${retained === null ? 'without' : 'with'} receipt`, async () => {
        await git(remote, 'update-ref', `refs/heads/${branch}`, tip, first);
        try {
          assert.equal(await git(checkout, 'rev-parse', `origin/${branch}`), first);
          const refused = await record();
          assert.equal(refused.status, 1, refused.stderr);
          assert.equal(refused.stdout, '');
          assert.match(refused.stderr, /not the first verified challenger-filter publication/);
          assert.doesNotMatch(refused.stderr, /Traceback/);
          assert.equal(await git(checkout, 'rev-parse', `origin/${branch}`), tip,
            'verification must use fetched history rather than the cached branch');
          assert.equal(await git(checkout, 'cat-file', '-t', first), 'commit');
          if (retained === null) {
            assert.equal(fs.existsSync(receipt), false);
          } else {
            assert.deepEqual(fs.readFileSync(receipt), retained);
            assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
            if (name === 'rewritten') {
              // A new valid first artifact cannot replace an already retained
              // deployment boundary, even with an audit naming its exact SHA.
              fs.writeFileSync(audit, JSON.stringify({ ...provenance, published_commit: tip }));
              const conflicting = await record();
              assert.equal(conflicting.status, 1, conflicting.stderr);
              assert.equal(conflicting.stdout, '');
              assert.match(conflicting.stderr, /refusing to replace the first recorded deployment/);
              assert.doesNotMatch(conflicting.stderr, /Traceback/);
              assert.deepEqual(fs.readFileSync(receipt), retained);
              assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
            }
          }
          assert.equal(fs.existsSync(path.join(state, `.${path.basename(receipt)}.tmp`)), false);
          assert.equal(await git(remote, 'rev-parse', branch), tip);
        } finally {
          fs.writeFileSync(audit, JSON.stringify(provenance));
          await git(remote, 'update-ref', `refs/heads/${branch}`, first, tip);
        }
        const recovered = await record();
        assert.equal(recovered.status, 0, recovered.stderr);
        const recoveredReceipt = JSON.parse(recovered.stdout);
        assert.equal(recoveredReceipt.published_commit, first);
        assert.equal(recoveredReceipt.deployed_at, provenance.deployed_at);
        assert.equal(recoveredReceipt.deployed_epoch, Date.parse(provenance.deployed_at) / 1000);
        assert.equal(recoveredReceipt.deployment_evidence, provenance.deployment_evidence);
        if (retained === null) {
          assert.deepEqual(JSON.parse(fs.readFileSync(receipt, 'utf8')), recoveredReceipt);
          fs.unlinkSync(receipt);
        } else {
          assert.deepEqual(fs.readFileSync(receipt), retained);
          assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
        }
        assert.equal(await git(remote, 'rev-parse', branch), first);
        assert.equal(fs.existsSync(database), false);
        assert.deepEqual(fs.readdirSync(home), []);
      });
    }
  }
  await refuseRewrittenExportHistory();

  // A directory at the atomic-write path causes an actual filesystem failure,
  // including when running as root, without changing permissions or mocking I/O.
  const blockedWrite = path.join(state, `.${path.basename(receipt)}.tmp`);
  fs.mkdirSync(blockedWrite);
  const failed = await record();
  assert.equal(failed.status, 1, failed.stderr);
  assert.equal(failed.stdout, '');
  assert.match(failed.stderr, /deployment provenance refused:.*[Ii]s a directory/);
  assert.doesNotMatch(failed.stderr, /Traceback/);
  assert.equal(fs.existsSync(receipt), false);
  assert.equal(await git(remote, 'rev-parse', branch), provenance.published_commit);
  fs.rmdirSync(blockedWrite);

  const unchanged = await publish();
  assert.equal(unchanged.status, 0, unchanged.stderr);
  assert.match(unchanged.stdout, /publication_result=False/);
  const recovered = await record();
  assert.equal(recovered.status, 0, recovered.stderr);
  const recorded = JSON.parse(recovered.stdout);
  assert.equal(recorded.published_commit, provenance.published_commit);
  assert.equal(recorded.deployed_at, provenance.deployed_at);
  assert.equal(recorded.deployed_epoch, Date.parse(provenance.deployed_at) / 1000);
  assert.equal(recorded.deployment_evidence, provenance.deployment_evidence);
  assert.equal(recorded.generated_at, document.generated_at);
  assert.deepEqual(JSON.parse(fs.readFileSync(receipt, 'utf8')), recorded);
  const before = fs.readFileSync(receipt);
  const modified = fs.statSync(receipt, { bigint: true }).mtimeNs;
  await refuseHistoryReadFailures();
  await refuseBeforeGeneration();
  await refuseUnavailableRemote();
  await refuseDeletedExportBranch();
  await refuseRewrittenExportHistory();
  const repeated = await record();
  assert.equal(repeated.status, 0, repeated.stderr);
  assert.deepEqual(JSON.parse(repeated.stdout), recorded);
  assert.deepEqual(fs.readFileSync(receipt), before);
  assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
  assert.equal(await git(remote, 'rev-parse', branch), provenance.published_commit);
  assert.equal(fs.existsSync(database), false, 'publication must not create a feedback database');
  assert.deepEqual(fs.readdirSync(home), [], 'the live state fallback must stay unused');

  // A new publication must not move the observation boundary to the latest
  // artifact. Exercise the CLI against multi-commit remote history, including
  // an attempted replacement of the retained receipt with that later commit.
  document.source_version += 1;
  const laterPublication = await publish();
  assert.equal(laterPublication.status, 0, laterPublication.stderr);
  assert.match(laterPublication.stdout, /publication_result=True/);
  const laterCommit = await git(remote, 'rev-parse', branch);
  assert.notEqual(laterCommit, provenance.published_commit);
  const original = await record();
  assert.equal(original.status, 0, original.stderr);
  assert.deepEqual(JSON.parse(original.stdout), recorded);
  assert.deepEqual(fs.readFileSync(receipt), before);
  assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);

  fs.writeFileSync(audit, JSON.stringify({ ...provenance, published_commit: laterCommit }));
  const replacement = await record();
  assert.equal(replacement.status, 1, replacement.stderr);
  assert.equal(replacement.stdout, '');
  assert.match(replacement.stderr, /not the first verified challenger-filter publication/);
  assert.doesNotMatch(replacement.stderr, /Traceback/);
  assert.deepEqual(fs.readFileSync(receipt), before);
  assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);

  // An absent receipt must not make the latest commit acceptable either.
  fs.unlinkSync(receipt);
  const missingReceipt = await record();
  assert.equal(missingReceipt.status, 1, missingReceipt.stderr);
  assert.match(missingReceipt.stderr, /not the first verified challenger-filter publication/);
  assert.equal(missingReceipt.stdout, '');
  assert.equal(fs.existsSync(receipt), false);
  fs.writeFileSync(audit, JSON.stringify(provenance));
  const firstReceipt = await record();
  assert.equal(firstReceipt.status, 0, firstReceipt.stderr);
  assert.deepEqual(JSON.parse(firstReceipt.stdout), recorded);
  assert.deepEqual(fs.readFileSync(receipt), before);
  assert.equal(await git(remote, 'rev-parse', branch), laterCommit,
    'recording the original deployment must not publish or rewind the export branch');

  // A depth-one clone hides the actual first publication. Even though its tip
  // carries a valid contract, it cannot establish a deployment boundary.
  const shallowRepositories = path.join(root, 'shallow-repos');
  const shallowCheckout = path.join(shallowRepositories, 'stranske__Orchestrator');
  fs.mkdirSync(shallowRepositories);
  await git(root, 'clone', '--depth=1', '--branch', branch,
    pathToFileURL(remote).href, shallowCheckout);
  const shallowEnv = { ...env, ORCH_REPOS_DIR: shallowRepositories };
  assert.equal(await git(shallowCheckout, 'rev-parse', '--is-shallow-repository'), 'true');
  assert.equal(await git(shallowCheckout, 'rev-list', '--count', `origin/${branch}`), '1');
  assert.equal(await git(shallowCheckout, 'rev-parse', `origin/${branch}`), laterCommit);

  const retained = fs.readFileSync(receipt);
  const retainedModified = fs.statSync(receipt, { bigint: true }).mtimeNs;
  for (const existing of [true, false]) {
    if (!existing) fs.unlinkSync(receipt);
    // Neither an accurate audit nor one falsely naming the visible tip can be
    // verified from truncated history. Preserve any receipt already retained.
    for (const commit of [provenance.published_commit, laterCommit]) {
      fs.writeFileSync(audit, JSON.stringify({ ...provenance, published_commit: commit }));
      const refused = await record(shallowEnv);
      assert.equal(refused.status, 1, refused.stderr);
      assert.equal(refused.stdout, '');
      assert.match(refused.stderr, /complete export history is required/);
      assert.doesNotMatch(refused.stderr, /Traceback/);
      if (existing) {
        assert.deepEqual(fs.readFileSync(receipt), retained);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, retainedModified);
      } else {
        assert.equal(fs.existsSync(receipt), false);
      }
    }
  }

  // Restoring history must uncover the original publication, reject the later
  // audit, and recover the original receipt without publishing another commit.
  await git(shallowCheckout, 'fetch', '--unshallow', 'origin');
  assert.equal(await git(shallowCheckout, 'rev-parse', '--is-shallow-repository'), 'false');
  const laterAfterRecovery = await record(shallowEnv);
  assert.equal(laterAfterRecovery.status, 1, laterAfterRecovery.stderr);
  assert.equal(laterAfterRecovery.stdout, '');
  assert.match(laterAfterRecovery.stderr, /not the first verified challenger-filter publication/);
  assert.equal(fs.existsSync(receipt), false);
  fs.writeFileSync(audit, JSON.stringify(provenance));
  const originalAfterRecovery = await record(shallowEnv);
  assert.equal(originalAfterRecovery.status, 0, originalAfterRecovery.stderr);
  assert.deepEqual(JSON.parse(originalAfterRecovery.stdout), recorded);
  assert.deepEqual(fs.readFileSync(receipt), retained);
  assert.equal(await git(remote, 'rev-parse', branch), laterCommit);

  // The same verified commit does not authorize replacing its retained audit
  // or silently repairing a damaged receipt. Either could reset observation.
  const conflicts = [
    ['changed audit timestamp', { ...provenance, deployed_at: document.generated_at }, retained],
    ['changed audit evidence', {
      ...provenance, deployment_evidence: 'different-publication-audit/successful-push',
    }, retained],
    ['conflicting receipt epoch', provenance,
      JSON.stringify({ ...recorded, deployed_epoch: recorded.deployed_epoch + 1 })],
    ['missing receipt evidence', provenance,
      JSON.stringify({ ...recorded, deployment_evidence: undefined })],
    ['null receipt', provenance, 'null\n'],
    ['truncated receipt', provenance, '{'],
  ];
  for (const [name, attemptedAudit, retainedReceipt] of conflicts) {
    await t.test(`preserves deployment boundary with ${name}`, async () => {
      fs.writeFileSync(audit, JSON.stringify(attemptedAudit));
      fs.writeFileSync(receipt, retainedReceipt);
      const saved = fs.readFileSync(receipt);
      const savedModified = fs.statSync(receipt, { bigint: true }).mtimeNs;
      try {
        const refused = await record();
        assert.equal(refused.status, 1, refused.stderr);
        assert.equal(refused.stdout, '');
        assert.match(refused.stderr, /deployment provenance refused:/);
        assert.match(refused.stderr, name === 'truncated receipt'
          ? /Expecting property name/ : /refusing to replace the first recorded deployment/);
        assert.doesNotMatch(refused.stderr, /Traceback/);
        assert.deepEqual(fs.readFileSync(receipt), saved);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, savedModified);
        assert.equal(fs.existsSync(blockedWrite), false, 'refusal must precede atomic writes');
        assert.equal(await git(remote, 'rev-parse', branch), laterCommit);
      } finally {
        // Recovery requires restoring the original evidence, not another push.
        fs.writeFileSync(audit, JSON.stringify(provenance));
        fs.writeFileSync(receipt, retained);
      }
      const restoredModified = fs.statSync(receipt, { bigint: true }).mtimeNs;
      const recoveredReceipt = await record();
      assert.equal(recoveredReceipt.status, 0, recoveredReceipt.stderr);
      assert.deepEqual(JSON.parse(recoveredReceipt.stdout), recorded);
      assert.deepEqual(fs.readFileSync(receipt), retained);
      assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, restoredModified);
      assert.equal(await git(remote, 'rev-parse', branch), laterCommit);
      assert.equal(fs.existsSync(database), false);
      assert.deepEqual(fs.readdirSync(home), []);
    });
  }

  // Removing and later restoring the artifact must not erase the first
  // publication or let a byte-identical restoration restart observation.
  const originalReceiptModified = fs.statSync(receipt, { bigint: true }).mtimeNs;
  await git(checkout, 'rm', 'config/route-weights.json');
  await git(checkout, 'commit', '-m', 'remove publication fixture');
  await git(checkout, 'push', 'origin', `HEAD:${branch}`);
  const deletion = await git(remote, 'rev-parse', branch);
  for (const stage of ['deleted', 'restored']) {
    if (stage === 'restored') {
      await git(checkout, 'restore', `--source=${provenance.published_commit}`,
        '--', 'config/route-weights.json');
      await git(checkout, 'add', 'config/route-weights.json');
      await git(checkout, 'commit', '-m', 'restore original publication fixture');
      await git(checkout, 'push', 'origin', `HEAD:${branch}`);
      assert.equal(await git(checkout, 'rev-parse', 'HEAD:config/route-weights.json'),
        await git(checkout, 'rev-parse', `${provenance.published_commit}:config/route-weights.json`));
    }
    const tip = await git(remote, 'rev-parse', branch);
    assert.notEqual(tip, provenance.published_commit);
    if (stage === 'restored') assert.notEqual(tip, deletion);
    for (const hasReceipt of [false, true]) {
      await t.test(`preserves first publication with artifact ${stage} and receipt ${hasReceipt}`, async () => {
        const historyState = path.join(root, `history-${stage}-${hasReceipt}`);
        const historyReceipt = path.join(historyState, path.basename(receipt));
        fs.mkdirSync(historyState);
        if (hasReceipt) fs.writeFileSync(historyReceipt, retained);
        const savedModified = hasReceipt
          ? fs.statSync(historyReceipt, { bigint: true }).mtimeNs : null;
        const recordHistory = () => run(python, [
          path.join(modules, 'route_weights_export.py'),
          '--record-challenger-deployment', audit, '--state-dir', historyState,
        ], { cwd: root, env });
        try {
          fs.writeFileSync(audit, JSON.stringify({ ...provenance, published_commit: tip }));
          const refused = await recordHistory();
          assert.equal(refused.status, 1, refused.stderr);
          assert.equal(refused.stdout, '');
          assert.match(refused.stderr, /not the first verified challenger-filter publication/);
          assert.doesNotMatch(refused.stderr, /Traceback/);
          assert.equal(fs.existsSync(historyReceipt), hasReceipt);
          assert.equal(fs.existsSync(path.join(historyState, `.${path.basename(receipt)}.tmp`)), false);
          if (hasReceipt) {
            assert.deepEqual(fs.readFileSync(historyReceipt), retained);
            assert.equal(fs.statSync(historyReceipt, { bigint: true }).mtimeNs, savedModified);
          }
          fs.writeFileSync(audit, JSON.stringify(provenance));
          const recovered = await recordHistory();
          assert.equal(recovered.status, 0, recovered.stderr);
          assert.deepEqual(JSON.parse(recovered.stdout), recorded);
          assert.deepEqual(fs.readFileSync(historyReceipt), retained);
          if (hasReceipt) {
            assert.equal(fs.statSync(historyReceipt, { bigint: true }).mtimeNs, savedModified);
          }
          assert.equal(await git(remote, 'rev-parse', branch), tip);
          assert.deepEqual(fs.readFileSync(receipt), retained);
          assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, originalReceiptModified);
        } finally {
          fs.writeFileSync(audit, JSON.stringify(provenance));
        }
      });
    }
  }
  assert.equal(fs.existsSync(database), false);
  assert.deepEqual(fs.readdirSync(home), []);
});

for (const [name, provenance, diagnostic] of invalidProvenance) {
  test(`deployment provenance CLI refuses ${name} without side effects`, async (t) => {
    const interpreter = await run(process.env.PYTHON || 'python3', ['-c',
      'import sys; print(sys.executable)']);
    assert.equal(interpreter.status, 0, interpreter.stderr);
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-deployment-cli-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const bin = path.join(root, 'bin');
    const home = path.join(root, 'home');
    const state = path.join(root, 'state');
    const audit = path.join(root, 'audit.json');
    const gitLog = path.join(root, 'git.log');
    const receipt = path.join(state, 'route-weights-challenger-deployment.json');
    for (const directory of [bin, home, state]) fs.mkdirSync(directory);
    fs.writeFileSync(path.join(bin, 'git'),
      '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ROUTE_EXPORT_GIT_LOG"\nexit 99\n',
      { mode: 0o755 });
    if (provenance !== undefined) {
      fs.writeFileSync(audit, typeof provenance === 'string'
        ? provenance : JSON.stringify(provenance));
    }
    const env = {
      PATH: bin,
      HOME: home,
      PYTHONPATH: modules,
      PYTHONDONTWRITEBYTECODE: '1',
      ORCH_STATE_DIR: state,
      ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
      ORCH_FEEDBACK_DB: path.join(root, 'absent.db'),
      ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
      HANDOFF_DIR: path.join(root, 'handoff'),
      ROUTE_EXPORT_GIT_LOG: gitLog,
    };
    // Invalid input must neither create a receipt nor replace an earlier one.
    for (const existing of [false, true]) {
      const before = `${JSON.stringify(validProvenance)}\n`;
      if (existing) fs.writeFileSync(receipt, before);
      const modified = existing ? fs.statSync(receipt, { bigint: true }).mtimeNs : null;
      const result = await run(interpreter.stdout.trim(), [
        path.join(modules, 'route_weights_export.py'),
        '--record-challenger-deployment', audit, '--state-dir', state,
      ], { cwd: repo, env });
      assert.equal(result.signal, null, result.stderr);
      assert.equal(result.status, 1, result.stderr);
      assert.equal(result.stdout, '', 'refusal must not print a successful receipt');
      assert.match(result.stderr, /deployment provenance refused:/);
      assert.match(result.stderr, diagnostic);
      assert.doesNotMatch(result.stderr, /Traceback/);
      assert.equal(fs.existsSync(gitLog), false, 'invalid provenance must stop before Git');
      assert.deepEqual(fs.readdirSync(state), existing ? [path.basename(receipt)] : []);
      if (existing) {
        assert.equal(fs.readFileSync(receipt, 'utf8'), before);
        assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, modified);
      }
      assert.equal(fs.existsSync(env.ORCH_FEEDBACK_DB), false);
      assert.deepEqual(fs.readdirSync(home), [], 'the live state fallback must stay unused');
    }
  });
}

// Exercise argument parsing, the real process exit, and stderr without pytest
// fixtures. A Git tripwire proves that refusal precedes publication activity.
for (const source of ['missing', 'empty', 'wrong-db', 'empty-table', 'below-threshold']) {
  for (const override of [false, true]) {
    test(`publish_guard CLI refuses ${source}, override=${override}`, async (t) => {
      const interpreter = await run(process.env.PYTHON || 'python3', ['-c',
        'import sys; print(sys.executable)']);
      assert.equal(interpreter.status, 0, interpreter.stderr);
      const python = interpreter.stdout.trim();
      const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-export-cli-'));
      t.after(() => fs.rmSync(root, { recursive: true, force: true }));
      const bin = path.join(root, 'bin');
      const home = path.join(root, 'home');
      const state = path.join(root, 'state');
      const database = path.join(state, 'feedback', 'weights #?.db');
      const fallbackDatabase = path.join(state, 'feedback', 'orchestrator.db');
      const gitLog = path.join(root, 'git.log');
      for (const directory of [bin, home, path.dirname(database)]) {
        fs.mkdirSync(directory, { recursive: true });
      }
      fs.writeFileSync(path.join(bin, 'git'),
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ROUTE_EXPORT_GIT_LOG"\nexit 99\n',
        { mode: 0o755 });
      const env = {
        PATH: bin,
        HOME: home,
        PYTHONPATH: modules,
        PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: state,
        ORCH_LOCAL_RUNTIME: state,
        ORCH_FEEDBACK_DB: database,
        ORCH_CAPABILITIES_PATH: path.join(state, 'capabilities.json'),
        HANDOFF_DIR: path.join(root, 'handoff'),
        ORCH_ROUTE_WEIGHTS_PUBLISH: '1',
        ROUTE_EXPORT_GIT_LOG: gitLog,
      };
      if (source === 'empty') {
        fs.writeFileSync(database, '');
      } else if (source !== 'missing') {
        const initialized = await run(python, ['-c', `
import sqlite3
import sys
from pathlib import Path
import route_weights_export as export
database = Path(sys.argv[1])
if sys.argv[2] == "wrong-db":
    # A valid default Brain must not hide an explicitly selected unrelated DB.
    export._fixture_db(database.with_name("orchestrator.db"))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value INTEGER)")
        connection.execute("INSERT INTO unrelated VALUES (68)")
    sys.exit(0)
export._fixture_db(database)
with sqlite3.connect(database) as connection:
    if sys.argv[2] == "empty-table":
        connection.execute("DELETE FROM route_weights")
    else:
        connection.execute("UPDATE route_weights SET n_obs=0")
`, database, source], { cwd: repo, env });
        assert.equal(initialized.status, 0, initialized.stderr);
      }
      const before = source === 'missing' ? null : fs.readFileSync(database);
      const fallbackBefore = source === 'wrong-db' ? fs.readFileSync(fallbackDatabase) : null;
      const args = [path.join(modules, 'route_weights_export.py'), '--publish'];
      if (override) args.push('--allow-evidence-regression');
      const shadowPath = path.join(state, 'route-weights-export.json');
      const result = await run(python, args, { cwd: repo, env });

      assert.equal(result.status, 1, `publish_guard must refuse: ${result.stderr}`);
      const version = source === 'below-threshold' ? 2 : 0;
      assert.ok(result.stderr.includes(`db=${database} source_version=${version}`),
        result.stderr);
      assert.match(result.stderr, /publish_guard refused:/);
      assert.match(result.stderr, source === 'below-threshold'
        ? /all task types have evidence_ok false/ : /source_version == 0/);
      assert.match(result.stderr, /drainable_in=infiniteh/);
      assert.equal(fs.existsSync(gitLog), false, 'publish_guard must refuse before Git');
      if (before === null) {
        assert.equal(fs.existsSync(database), false, 'a missing DB must stay missing');
      } else {
        assert.deepEqual(fs.readFileSync(database), before, 'the source DB must stay read-only');
      }
      const shadowBytes = fs.readFileSync(shadowPath);
      const shadow = JSON.parse(shadowBytes);
      assert.equal(shadow.source_version, version);
      assert.deepEqual(shadow.task_types.implement.ranking, []);

      // An earlier refused run already wrote this local shadow. Its semantic
      // equality must never short-circuit the next publication guard.
      const repeated = await run(python, args, { cwd: repo, env });
      assert.equal(repeated.status, 1,
        `publish_guard must also refuse an unchanged local export: ${repeated.stderr}`);
      assert.ok(repeated.stdout.includes(`unchanged ${shadowPath}`), repeated.stdout);
      assert.match(repeated.stderr, /publish_guard refused:/);
      assert.match(repeated.stderr, source === 'below-threshold'
        ? /all task types have evidence_ok false/ : /source_version == 0/);
      assert.ok(repeated.stderr.includes(`db=${database} source_version=${version}`),
        repeated.stderr);
      assert.deepEqual(fs.readFileSync(shadowPath), shadowBytes,
        'an unchanged refused export must retain the earlier shadow bytes');
      assert.equal(fs.existsSync(gitLog), false,
        'an unchanged refused export must still stop before Git');
      if (before === null) {
        assert.equal(fs.existsSync(database), false);
      } else {
        assert.deepEqual(fs.readFileSync(database), before);
      }
      if (fallbackBefore !== null) {
        assert.deepEqual(fs.readFileSync(fallbackDatabase), fallbackBefore,
          'refusal must preserve the neighboring evidenced Brain');
      }
    });
  }
}

function timeoutInjectScript(mode) {
  // mode: 'fetch' | 'after_fetch'
  return `
import json
import sys
from pathlib import Path
from unittest.mock import patch
import subprocess

modules = Path(sys.argv[1])
sys.path.insert(0, str(modules))
import route_weights_export as export

audit = Path(sys.argv[2])
state = Path(sys.argv[3])
real_run = subprocess.run

def run(*a, **k):
    cmd = a[0] if a else k.get("args", ())
    if not isinstance(cmd, (list, tuple)):
        return real_run(*a, **k)
    to = k.get("timeout", 60)
    if "${mode}" == "fetch" and "fetch" in cmd:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=to)
    if "${mode}" == "after_fetch":
        if "fetch" in cmd:
            return real_run(*a, **k)
        if cmd and cmd[0] == "git":
            raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=to)
    return real_run(*a, **k)

with patch.object(export.subprocess, "run", side_effect=run):
    export.sys.argv = [
        "route_weights_export",
        "--record-challenger-deployment",
        str(audit),
        "--state-dir",
        str(state),
    ]
    raise SystemExit(export.main())
`;
}

async function runRecorderWithTimeoutInject(mode, audit, state, env, cwd) {
  const python = process.env.PYTHON || 'python3';
  const { stdout, stderr } = await execFileAsync(python, [
    '-c', timeoutInjectScript(mode), modules, audit, state,
  ], { env: { ...process.env, ...env }, cwd, maxBuffer: 2 * 1024 * 1024 });
  return { status: 0, stdout, stderr };
}

test('deployment CLI refuses subprocess.TimeoutExpired at fetch without receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-timeout-fetch-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const state = path.join(root, 'state');
  const reposDir = path.join(root, 'repos');
  const canon = path.join(reposDir, 'stranske__Orchestrator');
  fs.mkdirSync(canon, { recursive: true });
  const init = await run('git', ['init', canon], { env: { PATH: process.env.PATH } });
  assert.equal(init.status, 0, init.stderr);
  const audit = path.join(root, 'audit.json');
  const receipt = path.join(state, 'route-weights-challenger-deployment.json');
  fs.mkdirSync(state, { recursive: true });
  fs.writeFileSync(audit, JSON.stringify({
    published_commit: 'a'.repeat(40),
    deployed_at: '2026-11-05T00:00:00Z',
    deployment_evidence: 'publication-audit/run-123/successful-push',
  }), 'utf8');
  const env = {
    PATH: process.env.PATH,
    HOME: root,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: state,
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_REPOS_DIR: reposDir,
    ORCH_FEEDBACK_DB: path.join(root, 'absent.db'),
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
    HANDOFF_DIR: path.join(root, 'handoff'),
    GIT_CONFIG_NOSYSTEM: '1',
  };
  let err;
  try {
    await runRecorderWithTimeoutInject('fetch', audit, state, env, root);
  } catch (e) {
    err = e;
  }
  assert.ok(err && err.code === 1, err && err.stderr);
  assert.equal(err.stdout || '', '');
  assert.match(err.stderr, /deployment provenance refused: git subprocess timed out/);
  assert.doesNotMatch(err.stderr, /Traceback/);
  assert.equal(fs.existsSync(receipt), false);
});

test('deployment CLI refuses TimeoutExpired after fetch and preserves receipt', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-timeout-after-fetch-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home');
  const state = path.join(root, 'state');
  const remote = path.join(root, 'remote.git');
  const repositories = path.join(root, 'repos');
  const checkout = path.join(repositories, 'stranske__Orchestrator');
  const database = path.join(root, 'absent.db');
  const audit = path.join(root, 'audit.json');
  const receipt = path.join(state, 'route-weights-challenger-deployment.json');
  for (const directory of [home, state, repositories]) fs.mkdirSync(directory);
  const env = {
    PATH: process.env.PATH,
    HOME: home,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: state,
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_REPOS_DIR: repositories,
    ORCH_FEEDBACK_DB: database,
    ORCH_CAPABILITIES_PATH: path.join(root, 'capabilities.json'),
    HANDOFF_DIR: path.join(root, 'handoff'),
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_AUTHOR_NAME: 'Deployment Test',
    GIT_AUTHOR_EMAIL: 'deploy@example.invalid',
    GIT_COMMITTER_NAME: 'Deployment Test',
    GIT_COMMITTER_EMAIL: 'deploy@example.invalid',
  };
  async function git(directory, ...args) {
    const result = await run('git', ['-C', directory, ...args], { env });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  const branch = 'exports/route-weights';
  const artifact = path.join(checkout, 'config', 'route-weights.json');
  await git(root, 'init', '--bare', '--initial-branch=main', remote);
  await git(root, 'clone', remote, checkout);
  const document = {
    generated_at: '2026-11-04T10:00:00Z',
    exploration: {
      challenger_filter: 'hosted-runnable-alternatives/v1',
      challengers: Object.fromEntries(
        ['implement', 'review', 'testgen', 'mechanical', 'codemod', 'cross_repo']
          .map((task) => [task, 'codex'])),
    },
  };
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  fs.writeFileSync(artifact, JSON.stringify(document));
  await git(checkout, 'add', '.');
  await git(checkout, 'commit', '-m', 'publication fixture');
  await git(checkout, 'push', 'origin', `HEAD:${branch}`);
  const commit = await git(remote, 'rev-parse', branch);
  fs.writeFileSync(audit, JSON.stringify({
    published_commit: commit,
    deployed_at: '2026-11-04T11:00:00Z',
    deployment_evidence: 'fixture-publication-audit/successful-push',
  }));
  const ok = await run(process.env.PYTHON || 'python3', [
    path.join(modules, 'route_weights_export.py'),
    '--record-challenger-deployment', audit, '--state-dir', state,
  ], { cwd: root, env });
  assert.equal(ok.status, 0, ok.stderr);
  const before = fs.readFileSync(receipt, 'utf8');
  const beforeStat = fs.statSync(receipt, { bigint: true });
  fs.writeFileSync(audit, JSON.stringify({
    published_commit: 'b'.repeat(40),
    deployed_at: '2026-11-05T00:00:00Z',
    deployment_evidence: 'different-audit',
  }));
  let err;
  try {
    await runRecorderWithTimeoutInject('after_fetch', audit, state, env, root);
  } catch (e) {
    err = e;
  }
  assert.ok(err && err.code === 1, err && err.stderr);
  assert.equal(err.stdout || '', '');
  assert.match(err.stderr, /deployment provenance refused: git subprocess timed out/);
  assert.doesNotMatch(err.stderr, /Traceback/);
  assert.equal(fs.readFileSync(receipt, 'utf8'), before);
  assert.equal(fs.statSync(receipt, { bigint: true }).mtimeNs, beforeStat.mtimeNs);
});
