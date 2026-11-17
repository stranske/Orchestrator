'use strict';

// Replay the cross-provider incident through the real Python reader and renderer.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const now = 1800000000;
const target = 'stranske/Manager-Mosaic#1402';
const error = 'repair dispatch: provision failed: cannot inspect holder';
const providers = ['codex', 'claude', 'cursor'];
const inspect = `
import json
import sys
import switch_review

report = switch_review.stuck_lane_targets(now=float(sys.argv[1]))
print(json.dumps({
    "report": report,
    "rendered": "\\n".join(switch_review.format_stuck_lane_targets(report)),
}))
`;

for (const identical of [true, false]) {
  test(`provider rotation ${identical ? 'preserves identical' : 'breaks differing'} errors`, (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'switch-stuck-targets-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const rounds = Array.from({ length: 21 }, (_, index) => {
      const agent = providers[index % providers.length];
      return {
        ts: new Date((now - 86400 + index * 900) * 1000).toISOString(),
        items: [{ target, action: 'error', agent, error: identical ? error : `${error}: ${agent}` }],
      };
    });
    const journals = ['opener-core', 'closer-core'].map((lane) => {
      const file = path.join(root, lane, 'rounds.jsonl');
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, lane === 'closer-core'
        ? rounds.map((round) => JSON.stringify(round)).join('\n') + '\n' : '');
      return file;
    });
    const before = journals.map((file) => fs.readFileSync(file));
    const result = spawnSync(process.env.PYTHON || 'python3', ['-c', inspect, String(now)], {
      cwd: root,
      env: {
        PATH: process.env.PATH,
        HOME: root,
        PYTHONPATH: modules,
        PYTHONDONTWRITEBYTECODE: '1',
        ORCH_STATE_DIR: root,
        ORCH_LOCAL_RUNTIME: root,
        ORCH_CAPABILITIES_PATH: path.join(root, 'absent-capabilities.json'),
      },
      encoding: 'utf8',
      timeout: 15000,
    });
    assert.equal(result.status, 0, result.error?.message || result.stderr);
    const { report, rendered } = JSON.parse(result.stdout);
    assert.equal(report.status, 'ok');
    assert.deepEqual(report.rounds_read, { 'opener-core': 0, 'closer-core': 21 });
    assert.equal(report.suspect, identical ? 1 : 0);
    if (identical) {
      assert.deepEqual(report.rows, [{
        lane: 'closer-core', target, action: 'error', reason: error, rounds: 21,
        since: rounds[0].ts.replace('.000Z', '+00:00'),
      }]);
      assert.match(rendered, /stuck lane targets: 1 SUSPECT/);
      assert.ok(rendered.includes(`${target}: error — ${error} (21 rounds, since `));
    } else {
      assert.deepEqual(report.rows, []);
      assert.match(rendered, /stuck lane targets: none \(opener 0 rounds, closer 21 rounds read\)/);
    }
    assert.deepEqual(journals.map((file) => fs.readFileSync(file)), before,
      'the FYI report must leave both lane journals unchanged');
    assert.deepEqual(fs.readdirSync(root).sort(), ['closer-core', 'opener-core'],
      'the FYI report must not create runtime state');
  });
}
