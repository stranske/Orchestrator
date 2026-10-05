'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python3';

test('shell sweep corpus recording defaults on and preserves the operator kill switch', () => {
  // Include the existing shell regression in the Node suite: it replays only
  // the sweep settings and checks an exported value in a child for all four cases.
  const result = spawnSync('bash', [path.join(repo, 'tests/check_redirect_sweep_defaults.sh')], {
    encoding: 'utf8', timeout: 10000,
  });
  assert.equal(result.status, 0, result.error?.message || result.stderr || result.stdout);
});

// Exercise the real Python normalization and role boundary without dispatching,
// writing a corpus, or touching the Brain. Only the backend picker is substituted.
const routeBackend = `
import json
from pathlib import Path
import sys
from unittest.mock import patch

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src" if (root / "src").is_dir() else root))
import redirect_sweep
import roles

report = json.loads((root / "tests/fixtures/redirect_stalls/auth.json").read_text())
with patch.object(roles, "route_role", return_value={"agent": "gemini"}) as pick:
    result = roles.run_redirect_agent(
        report,
        "focused gate",
        backend=redirect_sweep._selected_backend(None),
        proposal_json={"action": "inspect", "reason": "synthetic check", "confidence": "high"},
    )
    assert not result["errors"], result["errors"]
    assert result["mutates_state"] is False
    print(json.dumps({"backend": result["backend"], "roles": [c.args[0] for c in pick.call_args_list]}))
`;

for (const backend of [undefined, '', 'auto', 'AUTO', 'cursor', 'codex']) {
  const routed = backend === undefined || backend === '' || backend.toLowerCase() === 'auto';
  test(`shell sweep backend ${JSON.stringify(backend) ?? 'unset'} ${routed ? 'routes' : 'overrides'}`, () => {
    // Replay only the tracked export, so this cannot start an orchestrator tick.
    const exports = fs.readFileSync(path.join(repo, 'orchestrate.sh'), 'utf8')
      .split('\n').filter((line) => line.startsWith('export ORCH_REDIRECT_SWEEP_BACKEND='));
    assert.equal(exports.length, 1, 'expected one sweep backend export');
    const shell = spawnSync('bash', [
      '-c', exports[0] + '\nprintf "%s" "$ORCH_REDIRECT_SWEEP_BACKEND"',
    ], {
      env: backend === undefined ? {} : { ORCH_REDIRECT_SWEEP_BACKEND: backend },
      encoding: 'utf8', timeout: 10000,
    });
    assert.equal(shell.status, 0, shell.error?.message || shell.stderr);
    if (!routed) assert.equal(shell.stdout, backend);

    const result = spawnSync(python, ['-c', routeBackend, repo], {
      env: {
        PATH: process.env.PATH,
        ORCH_CAPABILITY_HEARTBEATS: '0',
        ORCH_REDIRECT_SWEEP_BACKEND: shell.stdout,
      },
      encoding: 'utf8', timeout: 10000,
    });
    assert.equal(result.status, 0, result.error?.message || result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {
      backend: routed ? 'gemini' : backend,
      roles: routed ? ['redirect'] : [],
    });
  });
}
