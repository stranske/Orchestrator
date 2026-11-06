'use strict';

// Run on the target machine with PYTHON set only for an explicitly selected interpreter.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');

const names = {
  gate_cancellations: 'Gate cancellation share (48 h, labeled versus synchronize)',
  merge_to_close: 'Merge to issue closure (48 h)',
  oldest_canary: 'Oldest open canary sync PR',
  opener_cap: 'Opener-owned PR count against cap',
  released_flow: 'Released-issue agent PRs per day (48 h)',
};

function validateReport(report, text, started, finished) {
  const delivery = report.fleet_delivery_measures;
  assert.ok(delivery, 'missing fleet delivery snapshot');
  assert.equal(delivery.window_hours, 48);
  assert.ok(Number.isInteger(delivery.as_of), 'missing integer check timestamp');
  assert.equal(delivery.as_of, report.generated_at, 'JSON snapshots disagree');
  assert.ok(delivery.as_of >= Math.floor(started / 1000)
    && delivery.as_of <= Math.ceil(finished / 1000), 'check timestamp is outside this live run');
  assert.deepEqual(Object.keys(delivery.rows).sort(), Object.keys(names).sort(),
    'must retain exactly the five fleet rows');
  const sections = text.split('## Fleet delivery measures\n');
  assert.equal(sections.length, 2, 'must print the fleet section exactly once');
  const section = sections[1].split(/^## /m)[0];
  // Python's UTC isoformat uses +00:00 rather than JavaScript's Z.
  const stamp = new Date(delivery.as_of * 1000).toISOString().replace('.000Z', '+00:00');
  assert.ok(section.includes(`  As of ${stamp}\n`), 'text check date disagrees with JSON');
  const lines = section.split('\n');
  for (const [key, name] of Object.entries(names)) {
    const row = delivery.rows[key];
    assert.equal(row.name, name);
    let value;
    if (row.value === null) {
      assert.equal(row.evidence_class, 'unmeasured');
      assert.match(row.reason, /^(?:gh |would need )\S[^\r\n]*$/,
        `${key}: missing explicit read error or required query`);
      value = `unmeasured — ${row.reason}`;
    } else {
      assert.equal(typeof row.value, 'string');
      assert.match(row.value, /\S/);
      assert.equal(typeof row.evidence_class, 'string');
      assert.match(row.evidence_class, /\S/);
      assert.notEqual(row.evidence_class, 'unmeasured');
      assert.equal(row.reason, '');
      value = `${row.value} [${row.evidence_class}]`;
    }
    const matches = lines.filter((line) => line.trimStart().startsWith(`${name}:`));
    assert.deepEqual(matches, [`  ${name}: ${value}`], `${key}: text and JSON disagree`);
  }
  return delivery;
}

function runCommand(python, args, options) {
  return new Promise((resolve, reject) => {
    execFile(python, args, options, (error, stdout) => {
      if (error) {
        // Keep subprocess output out of the error; it can contain unrelated host diagnostics.
        reject(new Error(`report command failed (${error.code || error.signal || 'unknown'})`));
      } else {
        resolve(stdout);
      }
    });
  });
}

async function verifyLive(outputDir, {
  python = process.env.PYTHON || '/opt/anaconda3/bin/python3',
  run = runCommand,
  now = Date.now,
} = {}) {
  const root = path.resolve(__dirname, '..');
  const destination = path.resolve(outputDir);
  // Refuse reused evidence directories, so an old successful report cannot survive a failed run.
  fs.mkdirSync(destination);
  const textPath = path.join(destination, 'report.txt');
  const args = [path.join(root, 'src/switch_review.py'), '--json', '--text-out', textPath];
  const started = now();
  const stdout = await run(python, args, {
    cwd: root,
    env: {
      ...process.env,
      ORCH_LOCAL_RUNTIME: path.join(destination, 'runtime'),
      ORCH_STATE_DIR: path.join(destination, 'state'),
    },
    encoding: 'utf8', timeout: 300000, maxBuffer: 16 * 1024 * 1024,
  });
  const finished = now();
  const report = JSON.parse(stdout);
  const delivery = validateReport(report, fs.readFileSync(textPath, 'utf8'), started, finished);
  fs.writeFileSync(path.join(destination, 'report.json'), stdout);
  const receipt = {
    checked_at: new Date(finished).toISOString(),
    command: [python, ...args],
    as_of: delivery.as_of,
    rows: delivery.rows,
  };
  fs.writeFileSync(path.join(destination, 'check.json'), JSON.stringify(receipt, null, 2) + '\n');
  return receipt;
}

if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length !== 1) {
    console.error('usage: node scripts/verify_fleet_delivery_live.js NEW_OUTPUT_DIRECTORY');
    process.exitCode = 1;
  } else {
    verifyLive(args[0]).then((receipt) => {
      console.log(`Five fleet rows verified at ${receipt.checked_at}; evidence: ${path.resolve(args[0])}`);
    }).catch((error) => {
      console.error(`Fleet live verification blocked or failed: ${error.message}`);
      process.exitCode = 1;
    });
  }
}

module.exports = { names, validateReport, verifyLive };
