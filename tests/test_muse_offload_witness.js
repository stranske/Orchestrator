'use strict';

// Standalone Muse acceptance witness: node --test tests/test_muse_offload_witness.js.
// Only the provider process and unrelated telemetry are replaced. The dispatcher,
// native stream parser, retry loop, and joined SQLite writers execute together.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src', 'dispatcher.py'))
  ? path.join(repo, 'src') : repo;

// Sanitized native framing, matching the retained Muse receipt in test_muse_backend.py.
function nativeResult() {
  const run = { kind: 'run', id: 'c1' };
  return [
    ['runtime.command.accepted',
      { kind: 'command_accepted', command_id: 'c1', command_kind: 'turn.submit' }],
    ['session.run.linked',
      { kind: 'session_run_linked', command_id: 'c1', run_stream: run }],
    ['run.lifecycle.started',
      { kind: 'run_started', command_id: 'c1', run_stream: run }],
    ['run.terminal.completed',
      { kind: 'run_terminal', command_id: 'c1', run_stream: run,
        terminal: 'completed', text: 'Reviewed the bounded task.', reason: null }],
  ].map(([kind, payload], index) => JSON.stringify({
    schema_version: 1, payload_schema_version: 1,
    stream: { kind: 'session', id: 's1' },
    record_type: index ? 'event' : 'reconciliation', durability: 'durable',
    causation_id: 'c1', payload_type: kind, payload,
  })).join('\n');
}

const observer = String.raw`
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
import adapters
import dispatcher
import feedback
import paths
import rate_incidents

root = Path(sys.argv[2])
responses = iter(json.loads(sys.argv[3]))
clock = [36000.0]
prompts = []
ledger = []

def provider(argv, **kwargs):
    assert argv[:2] == ['bash', '-lc']
    assert kwargs['stdin'] == subprocess.DEVNULL
    words = shlex.split(argv[-1])
    assert '--json' in words and '--max-model-steps' in words
    prompt = Path(words[words.index('--prompt-file') + 1])
    assert prompt.read_text() == 'Review the bounded task.'
    assert prompt.stat().st_mode & 0o777 == 0o600
    prompts.append(prompt)
    clock[0] += 2.5
    code, stdout, stderr = next(responses)
    return subprocess.CompletedProcess(argv, code, stdout, stderr)

with ExitStack() as stack:
    def replace(obj, name, value):
        stack.enter_context(patch.object(obj, name, value))

    replace(feedback, 'DB_PATH', root / 'brain.db')
    replace(feedback, '_capability_daily_heartbeat', lambda *a, **k: None)
    replace(dispatcher, 'DISPATCH_LOG_DIR', root / 'logs')
    replace(dispatcher, 'REAL_HOME', root / 'home')
    replace(dispatcher, 'AGENT_RUNTIME_DIR', root / 'runtime')
    replace(dispatcher, '_capability_heartbeat', lambda *a, **k: None)
    replace(dispatcher, '_offload_prompt', lambda prompt, *a: prompt)
    replace(dispatcher, '_select_offload_profile', lambda *a: None)
    replace(adapters, 'can_report_cli_identity', lambda *a: (False, 'witness'))
    replace(adapters, 'record_ledger', lambda agent, **row: ledger.append(row))
    replace(dispatcher, 'time', SimpleNamespace(
        time=lambda: clock[0], time_ns=time.time_ns, strftime=time.strftime, gmtime=time.gmtime))
    replace(dispatcher.subprocess, 'run', provider)
    for name, leaf in [('HANDOFF', '.'), ('INCIDENT_FILE', 'incidents.ndjson'),
                       ('LOCK_FILE', 'incidents.lock'), ('SHED_DIR', 'shed')]:
        replace(rate_incidents, name, root / leaf)
    out = dispatcher.offload('muse', 'Review the bounded task.', cwd=str(root))
    with feedback._conn() as conn:
        rows = conn.execute(
            'SELECT r.agent,c.cost_usd,c.source,c.tokens_in,c.tokens_out,'
            'u.muse_invocation_count,u.muse_wall_seconds,u.muse_raw_exit_codes,'
            'u.muse_window_id FROM runs r JOIN costs c USING(run_id) '
            'JOIN muse_subscription_usage u USING(run_id) WHERE r.run_id=?',
            (out['run_id'],),
        ).fetchall()
    usage = [line.split('\t')[1:3] for line in
             Path(os.environ['ORCH_MUSE_USAGE_LOG']).read_text().splitlines()]
    assert all(not prompt.exists() for prompt in prompts), 'private prompt was not removed'
    print(json.dumps({'out': out, 'rows': rows, 'usage': usage,
                      'ledger': ledger, 'calls': len(prompts),
                      'flat': paths.MODULE_DIR == paths.REPO_ROOT}))
`;

const cases = [
  { name: 'completed native run', responses: [[0, nativeResult(), '']],
    exit: 0, raw: [0] },
  { name: 'progress with a zero CLI exit',
    responses: [[0, nativeResult().split('\n').slice(0, -1).join('\n'), '']],
    exit: 70, raw: [0] },
  { name: 'network retry followed by completion',
    responses: [[75, '', 'connection reset by peer'], [0, nativeResult(), '']],
    exit: 0, raw: [75, 0] },
];

for (const flat of [false, true]) {
  for (const scenario of cases) {
    test(`${flat ? 'flat' : 'src'} modules: ${scenario.name}`, (t) => {
      const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'muse-offload-')));
      t.after(() => fs.rmSync(root, { recursive: true, force: true }));
      // Copy the real module bytes into each layout without any live state or login.
      const moduleDir = flat ? path.join(root, 'flat') : path.join(root, 'checkout', 'src');
      fs.mkdirSync(moduleDir, { recursive: true });
      for (const name of fs.readdirSync(modules).filter((name) => name.endsWith('.py'))) {
        fs.copyFileSync(path.join(modules, name), path.join(moduleDir, name));
      }
      const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
        !key.startsWith('ORCH_') && !key.startsWith('PYTHON')));
      Object.assign(env, {
        HOME: path.join(root, 'home'), TMPDIR: root, PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: path.join(root, 'state'), ORCH_LOCAL_RUNTIME: path.join(root, 'local'),
        ORCH_OFFLOAD_NETWORK_RETRIES: '1', ORCH_OFFLOAD_RETRY_BACKOFF_S: '0',
        ORCH_MUSE_USAGE_LOG: path.join(root, 'usage.log'),
      });
      const result = spawnSync(process.env.PYTHON || 'python3',
        ['-I', '-c', observer, moduleDir, root, JSON.stringify(scenario.responses)],
        { encoding: 'utf8', env, timeout: 30000 });
      assert.equal(result.status, 0, result.error?.message || result.stderr || result.stdout);
      const observed = JSON.parse(result.stdout);
      const { out } = observed;
      assert.equal(observed.flat, flat);
      assert.equal(out.exit, scenario.exit);
      assert.equal(out.output, scenario.exit === 0 ? 'Reviewed the bounded task.' : '');
      assert.deepEqual(out.usage, {});
      assert.equal(out.attempts, scenario.responses.length);
      assert.equal(observed.calls, scenario.responses.length);
      assert.deepEqual(observed.rows, [[
        'muse', 0, 'imputed:subscription', null, null, scenario.raw.length,
        scenario.raw.length * 2.5, JSON.stringify(scenario.raw),
        `observation:rolling-5h:18000-36000;observed:36000-${Math.floor(36000 + scenario.raw.length * 2.5)};provider-reset:unknown`,
      ]]);
      assert.deepEqual(observed.usage, scenario.raw.map((code) => ['offload', String(code)]));
      assert.deepEqual(observed.ledger.map((row) => row.event), ['start', 'complete']);
      assert.equal(observed.ledger[1].exit, scenario.exit);
      if (scenario.exit === 70) assert.equal(out.raw_exit, 0);
      if (scenario.raw.length > 1) assert.equal(out.retried, true);
    });
  }
}
