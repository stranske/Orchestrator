'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const fixtures = path.join(__dirname, 'fixtures', 'redirect_stalls');

test('all twelve historical stalls explicitly map their flattened policy facts', () => {
  const names = fs.readdirSync(fixtures).filter((name) => name.endsWith('.json')).sort();
  assert.deepEqual(names, Array.from({ length: 12 }, (_, index) =>
    `historical-${String(index + 1).padStart(2, '0')}.json`));

  for (const name of names) {
    const report = JSON.parse(fs.readFileSync(path.join(fixtures, name), 'utf8'));
    assert.equal(report.state, 'stalled', `${name}: replay input must be a real stall`);
    assert.equal(report.lane, 'experiment', `${name}: retain the historical lane`);
    assert.equal(typeof report.policy_action, 'string', `${name}: retain the source action`);
    assert.ok(report.policy_action, `${name}: source action must not be empty`);
    // Replay defaults a missing policy_decision.action to redirect. A passing
    // authorization count alone therefore cannot prove the fixture was mapped.
    assert.equal(report.policy_decision?.action, report.policy_action,
      `${name}: map policy_action in the fixture before replay`);
    assert.equal(typeof report.policy_confidence, 'string',
      `${name}: retain the source confidence`);
    assert.ok(report.policy_confidence, `${name}: source confidence must not be empty`);
    assert.equal(report.policy_decision?.confidence, report.policy_confidence,
      `${name}: map policy_confidence in the fixture before replay`);
  }
});

test('historical replay and both CLI outputs stay dry even with live apply armed', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'redirect-stall-replay-'));
  try {
    // Use the real Python chain and CLI, with standard-library write guards.
    // This also runs on workers without pytest; it does not replace the named
    // pytest acceptance checks or establish a live influence edge.
    const replay = `
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

root, fixtures, scratch = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src" if (root / "src").is_dir() else root))
import capabilities
import feedback
import redirect_apply
import redirect_shadow
import roles

attempts = []
def forbidden(*args, **kwargs):
    attempts.append(True)
    raise AssertionError("historical replay must not dispatch, claim or learn")

with ExitStack() as guards:
    for owner, name in [
        (roles.dispatcher, "offload"),
        (roles.dispatcher, "delegate"),
        (redirect_apply.claims, "claim"),
        (redirect_apply.claims, "release"),
        (redirect_apply.redirect_plan, "apply_plan"),
        (feedback, "record_role_run"),
        (feedback, "record_run"),
        (feedback, "record_influence_edge"),
        (redirect_shadow, "_append_event"),
        (capabilities, "heartbeat"),
    ]:
        guards.enter_context(patch.object(owner, name, forbidden))
    guards.enter_context(patch.object(roles.router, "load_capacity", return_value={}))
    guards.enter_context(patch.object(roles.router, "select_agent", return_value={"agent": "codex"}))

    result = redirect_apply.replay_stalls(fixtures)
    assert result["total"] == 12, result
    assert result["authorized"] >= 9, result["results"]
    assert result["dry_run"] is True
    assert {row["fixture"] for row in result["results"]} == {
        f"historical-{n:02d}.json" for n in range(1, 13)
    }
    for row in result["results"]:
        if row["authorization"]["allowed"]:
            assert row["agent"], row
        assert row["authorization"]["would_mutate"] is False, row

    # Exercise the public CLI under the same guards, including JSON serialization.
    expected_exit = 0 if result["authorized"] == result["total"] else 1
    output = StringIO()
    with redirect_stdout(output):
        exit_code = redirect_apply.main(["--replay-stalls", str(fixtures), "--json"])
    assert exit_code == expected_exit
    assert json.loads(output.getvalue()) == result
    output = StringIO()
    with redirect_stdout(output):
        exit_code = redirect_apply.main(["--replay-stalls", str(fixtures)])
    assert exit_code == expected_exit
    expected = f"authorized {result['authorized']}/12 with named agents (dry-run)"
    assert output.getvalue().strip() == expected

assert os.environ["ORCH_CAPABILITY_HEARTBEATS"] == "1"
assert os.environ[redirect_apply.BOOTSTRAP_FLAG] == "1"
assert not attempts, "a swallowed write attempt still violates dry replay"
assert not list(scratch.rglob("*")), "dry replay created state files"
print(expected)
`;
    const result = spawnSync(process.env.PYTHON || 'python3', [
      '-B', '-c', replay, path.resolve(__dirname, '..'), fixtures, temporary,
    ], {
      env: {
        PATH: process.env.PATH,
        ORCH_LOCAL_RUNTIME: temporary,
        ORCH_FEEDBACK_DB: path.join(temporary, 'brain.db'),
        ORCH_REDIRECT_SHADOW_CORPUS: path.join(temporary, 'corpus.jsonl'),
        ORCH_STATE_DIR: temporary,
        HANDOFF_DIR: path.join(temporary, 'handoff'),
        ORCH_REDIRECT_APPLY_BOOTSTRAP: '1',
        ORCH_CAPABILITY_HEARTBEATS: '1',
      },
      encoding: 'utf8', timeout: 30000,
    });
    assert.equal(result.status, 0,
      [result.error?.message, result.stderr, result.stdout].filter(Boolean).join('\n'));
    assert.match(result.stdout.trim(), /^authorized (9|1[0-2])\/12 with named agents \(dry-run\)$/);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
