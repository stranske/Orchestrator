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
