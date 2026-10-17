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

// Exercise argument parsing, the real process exit, and stderr without pytest
// fixtures. A Git tripwire proves that refusal precedes publication activity.
for (const source of ['missing', 'empty', 'empty-table', 'below-threshold']) {
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
      const args = [path.join(modules, 'route_weights_export.py'), '--publish'];
      if (override) args.push('--allow-evidence-regression');
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
      const shadow = JSON.parse(fs.readFileSync(path.join(state, 'route-weights-export.json')));
      assert.equal(shadow.source_version, version);
      assert.deepEqual(shadow.task_types.implement.ranking, []);
    });
  }
}
