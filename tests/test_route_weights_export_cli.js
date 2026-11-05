'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { test } = require('node:test');

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

async function publicationFixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-pass-publish-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const checkout = path.join(root, 'checkout');
  const remote = path.join(root, 'remote.git');
  const state = path.join(root, 'state');
  fs.mkdirSync(checkout);
  const env = { ...process.env, PYTHONPATH: modules, PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: state, ORCH_LOCAL_RUNTIME: state,
    ORCH_FEEDBACK_DB: path.join(root, 'weights.db') };
  async function git(args) {
    const result = await run('git', args, { cwd: checkout, env });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  }
  await git(['init', '--bare', remote]);
  await git(['init', '--initial-branch=main']);
  await git(['config', 'user.name', 'Route Export Test']);
  await git(['config', 'user.email', 'route-export-test@example.invalid']);
  fs.writeFileSync(path.join(checkout, 'README.md'), 'Local publication fixture\n');
  await git(['add', 'README.md']);
  await git(['commit', '-m', 'Initialize local publication fixture']);
  await git(['remote', 'add', 'origin', remote]);
  await git(['push', 'origin', 'main']);

  async function publish(rate, stampTime) {
    return run(process.env.PYTHON || 'python3', ['-c', `
import json
import sys
from pathlib import Path
from unittest.mock import patch
import route_weights_export as export
checkout, database, state = map(Path, sys.argv[1:4])
if not database.exists():
    export._fixture_db(database)
document = export.build_document(database, include_capacity=False)
document["exploration"]["rate"] = float(sys.argv[4])
record = export.record_hosted_exploration_deploy
# Control only the stamp's clock; retain the real writer and Git publication.
def record_at(document, state_dir):
    return record(document, state_dir, now=int(sys.argv[5]))
with patch.object(export.provision, "ensure_canonical", return_value=checkout), \\
     patch.object(export, "record_hosted_exploration_deploy", side_effect=record_at):
    changed = export.publish_document(document, state_dir=state, db_path=database)
print("RESULT=" + json.dumps({"published": changed}))
`, checkout, env.ORCH_FEEDBACK_DB, state, String(rate), String(stampTime)],
    { cwd: repo, env });
  }
  return { checkout, remote, state, git, publish, env };
}

for (const mode of ['preview', 'local-export', 'publish-disabled']) {
  test(`${mode} does not start the hosted acceptance clock before a real push`, async (t) => {
    const { remote, state, git, publish, env } = await publicationFixture(t);
    const python = process.env.PYTHON || 'python3';
    const initialized = await run(python, ['-c', `
import sys
from pathlib import Path
import route_weights_export as export
export._fixture_db(Path(sys.argv[1]))
print(sys.executable)
`, env.ORCH_FEEDBACK_DB], { cwd: repo, env });
    assert.equal(initialized.status, 0, initialized.stderr);
    const brainBefore = fs.readFileSync(env.ORCH_FEEDBACK_DB);
    const stampPath = path.join(state, 'hosted-exploration-deploy.json');
    const args = [path.join(modules, 'route_weights_export.py')];
    if (mode === 'preview') args.push('--preview-exploration-window');
    if (mode === 'publish-disabled') args.push('--publish');
    // Keep live capacity probes from invoking installed provider clients.
    const result = await run(initialized.stdout.trim(), args,
      { cwd: repo, env: { ...env, PATH: state, ORCH_ROUTE_WEIGHTS_PUBLISH: '0',
        ORCH_CAPABILITY_HEARTBEATS: '0' } });
    assert.equal(result.status, 0, result.stderr);
    if (mode === 'preview') {
      assert.equal(fs.existsSync(path.join(state, 'route-weights-export.json')), false);
    } else {
      assert.equal(fs.existsSync(path.join(state, 'route-weights-export.json')), true);
    }
    if (mode === 'publish-disabled') assert.match(result.stdout, /publish blocked/);
    assert.deepEqual(fs.readFileSync(env.ORCH_FEEDBACK_DB), brainBefore);
    assert.equal(fs.existsSync(stampPath), false,
      'local export activity must not supply a deployment date to the acceptance query');
    assert.equal(await git(['--git-dir', remote, 'for-each-ref', '--format=%(refname)',
      'refs/heads/exports/route-weights']), '');

    const pushed = await publish(0.2, 1700000200);
    assert.equal(pushed.status, 0, pushed.stderr);
    assert.match(pushed.stdout, /RESULT=\{"published": true\}/);
    assert.equal(JSON.parse(fs.readFileSync(stampPath, 'utf8')).deploy_ts, 1700000200,
      'only the successful remote publication starts the acceptance query window');
  });
}

test('real export pushes start the hosted pass clock once; unchanged exports retain it',
  async (t) => {
    const { remote, state, git, publish } = await publicationFixture(t);
    const stampPath = path.join(state, 'hosted-exploration-deploy.json');
    assert.equal(fs.existsSync(stampPath), false);
    const first = await publish(0.2, 1700000000);
    assert.equal(first.status, 0, first.stderr);
    assert.match(first.stdout, /RESULT=\{"published": true\}/);
    const stamp = fs.readFileSync(stampPath);
    assert.equal(JSON.parse(stamp).deploy_ts, 1700000000);
    const firstHead = await git(['--git-dir', remote, 'rev-parse', 'refs/heads/exports/route-weights']);
    const firstExport = JSON.parse(await git(['--git-dir', remote, 'show',
      'refs/heads/exports/route-weights:config/route-weights.json']));
    assert.equal(firstExport.exploration.rate, 0.2);
    assert.ok(Object.values(firstExport.exploration.challengers)
      .every((agent) => agent === null || agent === 'cursor' || agent === 'codex'));

    const unchanged = await publish(0.2, 1700000100);
    assert.equal(unchanged.status, 0, unchanged.stderr);
    assert.match(unchanged.stdout, /RESULT=\{"published": false\}/);
    assert.equal(await git(['--git-dir', remote, 'rev-parse',
      'refs/heads/exports/route-weights']), firstHead);
    assert.deepEqual(fs.readFileSync(stampPath), stamp);

    const changed = await publish(0.3, 1700000200);
    assert.equal(changed.status, 0, changed.stderr);
    assert.match(changed.stdout, /RESULT=\{"published": true\}/);
    assert.notEqual(await git(['--git-dir', remote, 'rev-parse',
      'refs/heads/exports/route-weights']), firstHead);
    const changedExport = JSON.parse(await git(['--git-dir', remote, 'show',
      'refs/heads/exports/route-weights:config/route-weights.json']));
    assert.equal(changedExport.exploration.rate, 0.3);
    assert.deepEqual(fs.readFileSync(stampPath), stamp,
      'later deployments must not reset the acceptance query lower bound');
  });

test('a rejected real export push leaves the hosted deployment unmeasured', async (t) => {
  const { checkout, remote, state, git, publish } = await publicationFixture(t);
  fs.writeFileSync(path.join(remote, 'hooks', 'pre-receive'), '#!/bin/sh\nexit 1\n',
    { mode: 0o755 });
  const result = await publish(0.2, 1700000000);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /pre-receive hook declined/);
  assert.equal(fs.existsSync(path.join(state, 'hosted-exploration-deploy.json')), false,
    'a local commit must not count as deployment when the remote rejects it');
  assert.equal(fs.existsSync(path.join(checkout, 'config', 'route-weights.json')), true);
  assert.equal(await git(['--git-dir', remote, 'for-each-ref', '--format=%(refname)',
    'refs/heads/exports/route-weights']), '');
});

test('hosted pass number counts only switches strictly after deployment without writing the Brain',
  async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-pass-number-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const database = path.join(root, 'Brain #?.db');
    const state = path.join(root, 'state');
    const env = { ...process.env, PYTHONPATH: modules, PYTHONDONTWRITEBYTECODE: '1',
      ORCH_STATE_DIR: state, ORCH_FEEDBACK_DB: database };
    const initialized = await run(process.env.PYTHON || 'python3', ['-c', `
import sqlite3
import sys
from pathlib import Path
import route_weights_export as export
deploy = export.HOSTED_EXPLORATION_PASS_CHECK_TS - 14 * 86400
export.record_hosted_exploration_deploy(
    {"exploration": {"challengers": {"implement": "cursor"}}},
    Path(sys.argv[2]), now=deploy)
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE agent_switches "
                       "(switched_ts INTEGER, source TEXT, delegation_source TEXT)")
    connection.executemany("INSERT INTO agent_switches VALUES (?, ?, ?)", [
        (deploy - 1, "policy", "route_weights_exploration"),
        (deploy, "policy", "route_weights_exploration"),
        (deploy + 1, "policy", "route_weights_exploration"),
        (deploy + 2, "label", "route_weights_exploration"),
        (deploy + 3, "policy", "route_weights_exploration"),
        (deploy + 4, "route_weights_exploration", "route_weights"),
    ])
`, database, state], { cwd: repo, env });
    assert.equal(initialized.status, 0, initialized.stderr);

    async function grade(offset, expectedStatus, expectedCount) {
      const before = fs.readFileSync(database);
      const stampPath = path.join(state, 'hosted-exploration-deploy.json');
      const stampBefore = fs.readFileSync(stampPath);
      const result = await run(process.env.PYTHON || 'python3', ['-c', `
import json
import sys
import route_weights_export as export
print(json.dumps(export.hosted_exploration_pass_number(
    now=export.HOSTED_EXPLORATION_PASS_CHECK_TS + int(sys.argv[1]))))
`, String(offset)], { cwd: repo, env });
      assert.equal(result.status, 0, result.stderr);
      const report = JSON.parse(result.stdout);
      assert.equal(report.status, expectedStatus);
      assert.equal(report.switches, expectedCount);
      assert.equal(report.min_switches, 4);
      assert.equal(report.deploy_ts, JSON.parse(stampBefore).deploy_ts);
      assert.deepEqual(fs.readFileSync(database), before, 'grading must keep the Brain read-only');
      assert.deepEqual(fs.readFileSync(stampPath), stampBefore, 'grading must retain deployment time');
      assert.deepEqual(fs.readdirSync(root).sort(), ['Brain #?.db', 'state']);
      assert.deepEqual(fs.readdirSync(state), ['hosted-exploration-deploy.json']);
    }

    await grade(-1, 'pending', 3);
    await grade(0, 'fail', 3);
    const switched = await run(process.env.PYTHON || 'python3', ['-c', `
import sqlite3
import sys
import route_weights_export as export
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("INSERT INTO agent_switches VALUES (?, ?, ?)",
                       (export.HOSTED_EXPLORATION_PASS_CHECK_TS - 1,
                        "policy", "route_weights_exploration"))
`, database], { cwd: repo, env });
    assert.equal(switched.status, 0, switched.stderr);
    await grade(-1, 'pending', 4);
    await grade(0, 'pass', 4);
  });

test('hosted pass number reads committed WAL switches while the Brain writer stays open',
  async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-pass-live-brain-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const database = path.join(root, 'Brain #?.db');
    const env = { ...process.env, PYTHONPATH: modules, PYTHONDONTWRITEBYTECODE: '1',
      ORCH_STATE_DIR: path.join(root, 'state'), ORCH_FEEDBACK_DB: database };
    const result = await run(process.env.PYTHON || 'python3', ['-c', `
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
import route_weights_export as export

database = Path(sys.argv[1])
deploy = export.HOSTED_EXPLORATION_PASS_CHECK_TS - 14 * 86400
export.record_hosted_exploration_deploy(
    {"exploration": {"challengers": {"implement": "cursor"}}}, now=deploy)
stamp_path = Path(export._default_state_dir()) / export.HOSTED_EXPLORATION_DEPLOY_FILE
stamp_before = stamp_path.read_bytes()
reader = """
import json
import route_weights_export as export
print(json.dumps(export.hosted_exploration_pass_number(
    now=export.HOSTED_EXPLORATION_PASS_CHECK_TS)))
"""
reports = []
writer = sqlite3.connect(database)
try:
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE agent_switches "
                   "(switched_ts INTEGER, delegation_source TEXT)")
    writer.executemany("INSERT INTO agent_switches VALUES (?, ?)", [
        (deploy + offset, "route_weights_exploration") for offset in range(1, 4)
    ])
    writer.commit()
    assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    checkpointed = database.read_bytes()
    wal_path = database.with_name(database.name + "-wal")

    def grade(phase):
        before = database.read_bytes(), wal_path.read_bytes()
        child = subprocess.run([sys.executable, "-c", reader],
                               check=True, capture_output=True, text=True, timeout=5)
        report = json.loads(child.stdout)
        assert before == (database.read_bytes(), wal_path.read_bytes()), phase
        assert stamp_path.read_bytes() == stamp_before, phase
        assert report["deploy_ts"] == deploy, report
        reports.append([phase, report["status"], report["switches"]])

    grade("checkpointed")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO agent_switches VALUES (?, ?)",
                   (deploy + 4, "route_weights_exploration"))
    grade("fourth switch uncommitted")
    writer.rollback()
    grade("fourth switch rolled back")
    writer.execute("INSERT INTO agent_switches VALUES (?, ?)",
                   (deploy + 4, "route_weights_exploration"))
    writer.commit()
    assert wal_path.stat().st_size > 0
    assert database.read_bytes() == checkpointed, "fourth switch must exist only in WAL"
    grade("fourth switch committed in WAL")
    writer.execute("DELETE FROM agent_switches WHERE switched_ts=?", (deploy + 4,))
    grade("fourth switch deletion uncommitted")
    writer.commit()
    grade("fourth switch deletion committed")
finally:
    writer.close()
print(json.dumps(reports))
`, database], { cwd: repo, env });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), [
      ['checkpointed', 'fail', 3],
      ['fourth switch uncommitted', 'fail', 3],
      ['fourth switch rolled back', 'fail', 3],
      ['fourth switch committed in WAL', 'pass', 4],
      ['fourth switch deletion uncommitted', 'pass', 4],
      ['fourth switch deletion committed', 'fail', 3],
    ]);
  });

test('invalid deployment stamps cannot pass against an otherwise passing Brain; repair recovers',
  async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-pass-deploy-stamp-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const database = path.join(root, 'brain.db');
    const state = path.join(root, 'state');
    fs.mkdirSync(state);
    const stampPath = path.join(state, 'hosted-exploration-deploy.json');
    const env = { ...process.env, PYTHONPATH: modules, PYTHONDONTWRITEBYTECODE: '1',
      ORCH_STATE_DIR: state, ORCH_FEEDBACK_DB: database };
    const initialized = await run(process.env.PYTHON || 'python3', ['-c', `
import sqlite3
import sys
import route_weights_export as export
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE agent_switches "
                       "(switched_ts INTEGER, delegation_source TEXT)")
    connection.executemany("INSERT INTO agent_switches VALUES (?, ?)", [
        (export.HOSTED_EXPLORATION_PASS_CHECK_TS - offset, "route_weights_exploration")
        for offset in range(1, 5)
    ])
`, database], { cwd: repo, env });
    assert.equal(initialized.status, 0, initialized.stderr);
    const brainBefore = fs.readFileSync(database);

    async function grades() {
      const result = await run(process.env.PYTHON || 'python3', ['-c', `
import json
import route_weights_export as export
print(json.dumps([export.hosted_exploration_pass_number(
    now=export.HOSTED_EXPLORATION_PASS_CHECK_TS + offset) for offset in (-1, 0)]))
`], { cwd: repo, env });
      assert.equal(result.status, 0, result.stderr);
      assert.deepEqual(fs.readFileSync(database), brainBefore, 'grading must not write the Brain');
      return JSON.parse(result.stdout);
    }

    const invalid = [
      ['missing', null],
      ['invalid-json', '{broken'],
      ['null-document', 'null'],
      ['array-document', '[]'],
      ['absent-timestamp', '{}'],
      ['null-timestamp', '{"deploy_ts":null}'],
      ['boolean-timestamp', '{"deploy_ts":true}'],
      ['string-timestamp', '{"deploy_ts":"1700000000"}'],
      ['fractional-timestamp', '{"deploy_ts":1700000000.5}'],
      ['negative-timestamp', '{"deploy_ts":-1}'],
    ];
    for (const [name, content] of invalid) {
      if (content !== null) fs.writeFileSync(stampPath, content);
      const [before, due] = await grades();
      assert.equal(before.status, 'pending', name);
      assert.equal(due.status, 'unmeasured', name);
      for (const report of [before, due]) {
        assert.equal(report.deploy_ts, null, name);
        assert.equal(report.switches, null, name);
        assert.match(report.reason, /deploy stamp absent/, name);
      }
      assert.deepEqual(fs.readdirSync(root).sort(), ['brain.db', 'state']);
      if (content === null) assert.deepEqual(fs.readdirSync(state), []);
      else {
        assert.equal(fs.readFileSync(stampPath, 'utf8'), content,
          `${name}: read-only grading must preserve invalid deployment evidence`);
        assert.deepEqual(fs.readdirSync(state), ['hosted-exploration-deploy.json']);
      }
    }

    const validStamp = '{"deploy_ts":1700000000}';
    fs.writeFileSync(stampPath, validStamp);
    const [before, due] = await grades();
    assert.equal(before.status, 'pending');
    assert.equal(due.status, 'pass');
    for (const report of [before, due]) {
      assert.equal(report.deploy_ts, 1700000000);
      assert.equal(report.switches, 4);
    }
    assert.equal(fs.readFileSync(stampPath, 'utf8'), validStamp,
      'recovery must use the supplied deployment time without rewriting it');
  });

for (const source of ['missing', 'corrupt', 'missing-switches-table']) {
  test(`hosted pass number reports ${source} Brain as unmeasured without creating or repairing it`,
    async (t) => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-pass-unmeasured-'));
      t.after(() => fs.rmSync(root, { recursive: true, force: true }));
      const database = path.join(root, 'Brain #?.db');
      const env = { ...process.env, PYTHONPATH: modules, PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: path.join(root, 'absent-state'), ORCH_FEEDBACK_DB: database };
      if (source === 'corrupt') fs.writeFileSync(database, 'not a SQLite database\n');
      if (source === 'missing-switches-table') {
        const initialized = await run(process.env.PYTHON || 'python3', ['-c', `
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE unrelated (value INTEGER)")
`, database], { cwd: repo, env });
        assert.equal(initialized.status, 0, initialized.stderr);
      }
      const before = source === 'missing' ? null : fs.readFileSync(database);
      const result = await run(process.env.PYTHON || 'python3', ['-c', `
import json
import route_weights_export as export
print(json.dumps(export.hosted_exploration_pass_number(
    deploy_ts=export.HOSTED_EXPLORATION_PASS_CHECK_TS - 14 * 86400,
    now=export.HOSTED_EXPLORATION_PASS_CHECK_TS)))
`], { cwd: repo, env });
      assert.equal(result.status, 0, result.stderr);
      const report = JSON.parse(result.stdout);
      assert.equal(report.status, 'unmeasured');
      assert.equal(report.switches, null);
      assert.match(report.reason, /unreadable/);
      if (before === null) assert.equal(fs.existsSync(database), false);
      else assert.deepEqual(fs.readFileSync(database), before);
      assert.deepEqual(fs.readdirSync(root), before === null ? [] : ['Brain #?.db']);
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
