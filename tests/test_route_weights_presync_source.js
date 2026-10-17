'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';

function run(command, args, env) {
  const result = spawnSync(command, args, { env, encoding: 'utf8', timeout: 30000 });
  assert.equal(result.status, 0, result.stdout + result.stderr + String(result.error || ''));
  return result.stdout.trim();
}

// The stand-in verifier inspects the private snapshot; it never runs the live
// verifier or publishes anything. The selected source comes from the REAL resolver.
for (const scenario of ['default', 'state-only', 'runtime-only', 'runtime-and-state',
  'explicit', 'missing-explicit', 'missing-state']) {
  test(`pre-sync snapshots the learner's Brain: ${scenario}`, (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'route-presync-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const home = path.join(root, 'home');
    const source = path.join(root, 'source');
    const state = path.join(root, 'state with spaces');
    const runtime = path.join(root, 'runtime');
    const defaultRuntime = path.join(home, '.codex', 'orchestrator');
    const explicit = path.join(root, 'explicit #?.db');
    const record = path.join(root, 'snapshot.json');
    const env = {
      PATH: process.env.PATH,
      HOME: home,
      TMPDIR: root,
      PYTHON: python,
      PYTHONPATH: modules,
      PYTHONDONTWRITEBYTECODE: '1',
      ORCH_SYNC_SCRIPT: path.join(root, 'sync.sh'),
      SNAPSHOT_RECORD: record,
      SNAPSHOT_INSTALLER: path.join(repo, 'scripts', 'install_verified_snapshot.py'),
    };
    for (const directory of [home, source, state, runtime]) {
      fs.mkdirSync(directory, { recursive: true });
    }
    if (['state-only', 'runtime-and-state', 'explicit', 'missing-explicit', 'missing-state']
      .includes(scenario)) env.ORCH_STATE_DIR = state;
    if (['runtime-only', 'runtime-and-state', 'explicit', 'missing-explicit']
      .includes(scenario)) env.ORCH_LOCAL_RUNTIME = runtime;
    if (['explicit', 'missing-explicit'].includes(scenario)) env.ORCH_FEEDBACK_DB = explicit;

    // Different versions expose any accidental selection of a neighboring store.
    const databases = [
      [path.join(defaultRuntime, 'feedback', 'orchestrator.db'), 11],
      [path.join(runtime, 'feedback', 'orchestrator.db'), 42],
      [path.join(state, 'feedback', 'orchestrator.db'), 68],
      [explicit, 99],
    ];
    for (const [database, version] of databases) {
      if ((scenario === 'missing-explicit' && database === explicit)
        || (scenario === 'missing-state' && database.startsWith(state + path.sep))) continue;
      fs.mkdirSync(path.dirname(database), { recursive: true });
      run(python, ['-c',
        'import sqlite3, sys\nfrom pathlib import Path\n'
          + 'import route_weights_export as export\n'
          + 'database = Path(sys.argv[1])\nexport._fixture_db(database)\n'
          + 'with sqlite3.connect(database) as conn:\n'
          + '    conn.execute("UPDATE route_weights SET version=?", (int(sys.argv[2]),))\n',
        database, String(version)], env);
    }
    const before = new Map(databases.filter(([database]) => fs.existsSync(database))
      .map(([database]) => [database, fs.readFileSync(database)]));
    const selected = run(python, ['-c',
      'import feedback; print(feedback.resolve_db_path())'], env);
    const expectedVersion = before.has(selected)
      ? databases.find(([database]) => database === selected)[1] : null;

    fs.writeFileSync(path.join(source, 'orchestrate.sh'), '#!/bin/sh\n');
    fs.writeFileSync(path.join(source, 'verify.py'),
      'import json, os, sqlite3\nfrom pathlib import Path\n'
        + 'database = Path(os.environ["ORCH_FEEDBACK_DB"])\n'
        + 'version = None\n'
        + 'if database.exists():\n'
        + '    with sqlite3.connect(database) as conn:\n'
        + '        version = conn.execute("SELECT MAX(version) FROM route_weights").fetchone()[0]\n'
        + '        conn.execute("UPDATE route_weights SET version=777")\n'
        + 'Path(os.environ["SNAPSHOT_RECORD"]).write_text(json.dumps({\n'
        + '    "database": str(database), "source_version": version}))\n'
        + 'print("  tree:       EXEC MIRROR — snapshot test stand-in")\n');
    fs.writeFileSync(env.ORCH_SYNC_SCRIPT,
      '#!/bin/bash\nset -euo pipefail\n'
        + 'mkdir -p "$ORCH_MIRROR/scripts"\n'
        + 'cp "$1/verify.py" "$ORCH_MIRROR/verify.py"\n'
        + 'cp "$1/orchestrate.sh" "$ORCH_MIRROR/orchestrate.sh"\n'
        + 'cp "$SNAPSHOT_INSTALLER" "$ORCH_MIRROR/scripts/install_verified_snapshot.py"\n');
    run('git', ['-C', source, 'init', '-q'], env);
    run('git', ['-C', source, 'add', 'verify.py', 'orchestrate.sh'], env);
    run('git', ['-C', source, '-c', 'user.name=Snapshot Test',
      '-c', 'user.email=snapshot@example.invalid', '-c', 'commit.gpgsign=false',
      'commit', '-qm', 'isolated snapshot source'], env);

    const output = run('bash', [path.join(repo, 'scripts', 'verify_before_sync.sh'), source], env);
    const snapshot = JSON.parse(fs.readFileSync(record, 'utf8'));
    assert.equal(snapshot.source_version, expectedVersion,
      `snapshot must use feedback.resolve_db_path()=${selected}`);
    assert.notEqual(snapshot.database, selected, 'verification must use a private copy');
    assert.ok(snapshot.database.startsWith(root + path.sep), snapshot.database);
    assert.equal(fs.existsSync(snapshot.database), false, 'private snapshot must be cleaned up');
    for (const [database, bytes] of before) {
      assert.deepEqual(fs.readFileSync(database), bytes, 'verification wrote the live source');
    }
    if (!before.has(selected)) assert.equal(fs.existsSync(selected), false);
    assert.ok(output.includes(`selected live Brain: ${selected}`), output);
    assert.match(output, /verify-before-sync: VERIFIED/);
  });
}
