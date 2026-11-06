'use strict';

// Exercise the real Python renderer without requiring optional hosted-pass
// instrumentation or reading an operator's runtime state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;

function run(args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.PYTHON || 'python3', args, { timeout: 15000, ...options });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (data) => { stdout += data; });
    child.stderr.on('data', (data) => { stderr += data; });
    child.on('error', reject);
    child.on('close', (status) => resolve({ status, stdout, stderr }));
  });
}

const render = `
import json
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(sys.argv[1])
with patch.object(Path, "home", return_value=root):
    import switch_review
    import strategy_experiment
    import triage_shadow

    window, days, suspect = json.loads(sys.argv[2])
    gate = {
        "window": window,
        "window_days": days,
        "suspect": suspect,
        "status": "unmeasured",
        "evidence_error": "no verified production observation",
        "drainable": 0,
        "arms": [
            {"mode": "epsilon-greedy", "runs": 9, "graded_runs": 4,
             "pass_rate": 0.75, "durable_rate": None, "durability_runs": 0},
            {"mode": "thompson-hybrid", "runs": 8, "graded_runs": 2,
             "pass_rate": 0.5, "durable_rate": None, "durability_runs": 0},
        ],
    }
    rep = {
        "review_days": 7,
        "raise_count": 0,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "mirror_drift": {"status": "ok"},
        "comparative": {"status": "unmeasured", "lambda": {},
                        "window_days": 7, "reasons": ["fixture"]},
        "exploration_gate": gate,
    }
    with (
        patch.object(switch_review, "experiment_producer_line", return_value=""),
        patch.object(strategy_experiment, "strategy_result_path",
                     return_value=root / "absent-strategy.json"),
        patch.object(triage_shadow, "summary_line", return_value=""),
    ):
        print(switch_review.format_report(rep))
`;

const measuredWindow = {
  rate: 0.05,
  env: 'default',
  start: '2026-10-25T00:00:00Z',
  switches: 3,
  end: '2026-11-08T00:00:00Z',
  extended: false,
  n_by_policy: { 'epsilon-greedy': 1, 'thompson-hybrid': 2 },
  errors: [],
};

for (const [name, window, days, suspect] of [
  ['no window or arm horizon', null, null, false],
  ['no window and suspect arms', null, 14, true],
  ['window without arm horizon', measuredWindow, null, false],
  ['window with suspect arms', measuredWindow, 14, true],
  ['arms without window', null, 14, false],
]) {
  test(`switch review renders ${name}`, async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'switch-exploration-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const result = await run(
      ['-c', render, root, JSON.stringify([window, days, suspect])], {
        cwd: root,
        env: {
          PATH: process.env.PATH,
          HOME: root,
          PYTHONPATH: modules,
          PYTHONDONTWRITEBYTECODE: '1',
          ORCH_STATE_DIR: root,
          ORCH_LOCAL_RUNTIME: root,
          ORCH_FEEDBACK_DB: path.join(root, 'absent.db'),
          ORCH_CAPABILITIES_PATH: path.join(root, 'absent-capabilities.json'),
        },
      });
    assert.equal(result.status, 0, result.stderr);
    const text = result.stdout;
    assert.match(text, /# Switch review/);
    assert.match(text, /review window: 7d/);
    assert.equal(text.includes('## Exploration arms'), Boolean(window || (days && !suspect)));
    assert.equal(text.includes('exploration window: rate'), Boolean(window));
    assert.equal(text.includes('exploration decisions N='), Boolean(days && !suspect));
    assert.equal(text.includes('## Exploration evidence gate'), suspect);
    if (suspect) {
      assert.match(text, /SUSPECT — unmeasured: no verified production observation/);
      assert.doesNotMatch(text, /Nothing due/);
    }
    if (window) {
      assert.match(text, /exploration switches since 2026-10-25T00:00:00Z 3/);
      assert.match(text, /epsilon-greedy 1 \/ thompson-hybrid 2/);
    }
    if (days && !suspect) {
      assert.match(text, /epsilon-greedy: exploration decisions N=9 graded G=4 PASS=75.0%/);
      assert.match(text, /thompson-hybrid: exploration decisions N=8 graded G=2 PASS=50.0%/);
    }
    assert.deepEqual(fs.readdirSync(root), [], 'rendering must leave temporary state untouched');
  });
}
