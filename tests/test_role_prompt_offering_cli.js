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

// The fixture uses the production schema. The consult itself goes through the real
// CLI, so omitting its --surface forwarding cannot pass a direct-API-only test.
const initialize = `
import json
import capabilities
import capability_advisor as advisor

row = capabilities._blank_capability("role-prompt")
row["status"] = "generated"
capabilities.save({"role-prompt": row})
print(json.dumps({
    "consult_keys": sorted(advisor.consult_keys()),
    "caller": advisor.CONSULT_SITES.get("research-program", {}).get("caller"),
    "bindings": {surface: advisor.SURFACE_BINDINGS[surface]["role-prompt"]
                 for surface in ("research-program", "repo-audit:phase-4")},
}))
`;

function privateLedger(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'role-offering-cli-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const ledger = path.join(directory, 'capabilities.json');
  // Keep consult matches and runtime state in this test's temporary directory.
  const env = {
    PATH: process.env.PATH,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_LOCAL_RUNTIME: directory,
    ORCH_CAPABILITIES_PATH: ledger,
    ORCH_CAPABILITY_HEARTBEATS: '0',
  };
  function run(args) {
    const result = spawnSync(python, args, {
      cwd: repo, env, encoding: 'utf8', timeout: 10000,
    });
    assert.equal(result.status, 0, result.error?.message || result.stderr || result.stdout);
    return JSON.parse(result.stdout);
  }
  const declarations = run(['-c', initialize]);
  return {
    declarations,
    bytes: () => fs.readFileSync(ledger),
    events: () => JSON.parse(fs.readFileSync(ledger, 'utf8'))
      .capabilities['role-prompt'].event_history,
    consult: (surface, task) => run([
      path.join(modules, 'capability_advisor.py'), '--json', '--surface', surface, task,
    ]),
  };
}

for (const surface of ['research-program', 'repo-audit:phase-4']) {
  for (const task of ['Author a batch of three issue bodies from verified findings', 'qzxv']) {
    test(`advisor CLI binds and attributes ${surface}: ${task}`, (t) => {
      const fixture = privateLedger(t);
      assert.ok(fixture.declarations.consult_keys.includes('research-program'));
      assert.equal(fixture.declarations.caller,
        '~/.codex/automations/research-program/driver.py');
      const reason = fixture.declarations.bindings[surface];
      assert.match(reason, /batch/i);

      const advice = fixture.consult(surface, task);
      const offer = advice.capabilities.find((row) => row.capability_id === 'role-prompt');
      assert.ok(offer, 'CLI must offer role-prompt for the caller');
      assert.equal(offer.bound, true);
      assert.equal(offer.binding_reason, reason);
      assert.equal(advice.recorded_matches, 1);
      const matches = fixture.events().filter((event) => event.type === 'match');
      assert.equal(matches.length, 1, 'one consult must persist one match');
      assert.equal(matches[0].metadata.surface, surface);
      assert.equal(matches[0].ref, advice.experiment_id);

      // Independent CLI processes must still recognize a repeated consult. A
      // duplicate match would inflate the batch surface's demotion denominator.
      const before = fixture.bytes();
      const repeated = fixture.consult(surface, task);
      assert.equal(repeated.experiment_id, advice.experiment_id);
      assert.equal(repeated.recorded_matches, 0);
      assert.deepEqual(fixture.bytes(), before);
    });
  }
}
