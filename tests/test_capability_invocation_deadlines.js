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

// Use the real registrar and both callable entrypoints. Change persisted deadlines
// directly so a writing ledger load cannot retire the row before the guard sees it.
const exercise = `
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import capabilities
import capability_compiler as compiler
import capability_lifecycle as lifecycle
import capability_targets as targets
import feedback

root = Path(sys.argv[1])
case = json.loads(sys.argv[2])
ledger, registry = root / "capabilities.json", root / "targets.json"
feedback.DB_PATH = root / "brain.db"
capabilities.save({}, ledger)
artifact = compiler.compile_workflow_rail(compiler.reference_workflow_source())
registered = lifecycle.register_compiled_target(
    "workflow", artifact,
    lifecycle_policy={"risk_level": "low_reversible", "side_effect_policy": "read_only"},
    matcher={"repository": "owner/repo", "task_types": ["implement"]},
    predecessor="baseline:workflow", ledger_path=ledger, target_registry_path=registry,
)
cap_id = registered["capability"]["capability_id"]
version_id = registered["capability"]["capability_version_id"]
deadline = int(time.time()) + 3600
ledger_deadline = deadline + (60 if case["earlier"] == "artifact" else 0)
artifact_deadline = deadline + (60 if case["earlier"] == "ledger" else 0)
rows = json.loads(ledger.read_text())
rows["capabilities"][cap_id]["expiry"] = ledger_deadline
ledger.write_text(json.dumps(rows))
bindings = json.loads(registry.read_text())
bindings["targets"][version_id]["artifact"]["lifecycle"]["expires_at"] = artifact_deadline
registry.write_text(json.dumps(bindings))
binding = targets.get_binding(version_id, registry_path=registry)
run_id = "workflow:deadline-boundary"
feedback.record_run(run_id, "deadline-subject", "implement", "codex")
paths = (ledger, registry, Path(binding["binding_path"]))
before = [p.read_bytes() for p in paths]
kwargs = dict(
    trigger={"task_type": artifact["selector"]["value"][0], "target": "owner/repo#23"},
    target_run_id=run_id, ledger_path=ledger, timestamp=deadline + case["offset"],
)
guard_reason = lifecycle._invocation_ineligible(
    rows["capabilities"][cap_id], binding, kwargs["timestamp"], {},
)

def invoke():
    if case["entrypoint"] == "compiled":
        return lifecycle.invoke_compiled_target(cap_id, target_registry_path=registry, **kwargs)
    return targets.invoke_target(binding, registry_path=registry, **kwargs)

if case["offset"] >= 0:
    def forbidden(*args, **kwargs):
        raise AssertionError("expired invocation reached producer, consumer or causal writer")

    with patch.object(compiler, "dry_run_workflow_rail", forbidden), \\
         patch.object(compiler, "consume_workflow_output", forbidden), \\
         patch.object(feedback, "record_capability_consumption", forbidden):
        result = invoke()
else:
    result = invoke()

print(json.dumps({
    "result": result,
    "guard_reason": guard_reason,
    "unchanged": before == [p.read_bytes() for p in paths],
    "invocations": len(targets.get_binding(version_id, registry_path=registry)["invocations"]),
    "edges": feedback.capability_causal_evidence(cap_id, version_id),
}))
`;

for (const entrypoint of ['compiled', 'direct']) {
  for (const earlier of ['ledger', 'artifact', 'equal']) {
    for (const offset of [-1, 0, 1]) {
      test(`${entrypoint}: ${earlier} deadline, offset ${offset}`, (t) => {
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'invocation-deadlines-'));
        t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
        const child = spawnSync(python, [
          '-c', exercise, directory, JSON.stringify({ entrypoint, earlier, offset }),
        ], {
          cwd: repo,
          env: {
            PATH: process.env.PATH,
            PYTHONPATH: modules,
            PYTHONDONTWRITEBYTECODE: '1',
            ORCH_LOCAL_RUNTIME: directory,
            ORCH_CAPABILITIES_PATH: path.join(directory, 'capabilities.json'),
            ORCH_CAPABILITY_HEARTBEATS: '0',
          },
          encoding: 'utf8',
          timeout: 10000,
        });
        assert.equal(child.status, 0, child.error?.message || child.stderr || child.stdout);
        const observed = JSON.parse(child.stdout);
        assert.equal(observed.guard_reason, offset < 0 ? null : 'expired');
        if (offset < 0) {
          assert.equal(observed.result.invoked, true);
          assert.equal(observed.result.accepted, true);
          assert.equal(observed.invocations, 1);
          if (entrypoint === 'compiled') {
            assert.ok(observed.result.causal_edge);
            assert.equal(observed.edges.length, 1);
          }
        } else {
          assert.deepEqual(observed.result, { matched: true, invoked: false, reason: 'expired' });
          assert.equal(observed.unchanged, true);
          assert.equal(observed.invocations, 0);
          assert.deepEqual(observed.edges, []);
        }
      });
    }
  }
}
