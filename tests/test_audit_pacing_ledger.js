'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { auditLedger, captureLedger, main } = require('../scripts/audit_pacing_ledger');

const start = 1794283200;
const end = start + 48 * 3600;
const window = { start, end };
const ndjson = (rows) => rows.map((row) => JSON.stringify(row)).join('\n') + '\n';

function snapshot(ts, changes = {}) {
  const agents = {};
  for (const pool of ['codex', 'claude']) {
    agents[pool] = {
      state: 'warn',
      pacing: { used_fraction: 0.15, routing_weight: 0.5, measurement: 'measured',
        stale: false, projected_fraction: 4.2, state: 'warn', ...changes },
      pacing_diagnostics: { span_h: 2, previous_routing_weight: 0.75 },
    };
  }
  return { event: 'pacing_snapshot', ts, agents };
}

test('orders numeric timestamps, filters events and retains inclusive boundaries', () => {
  const result = auditLedger(ndjson([
    snapshot(end), snapshot(start - 1), snapshot(start + 900), snapshot(start),
    snapshot(end + 1), { event: 'dispatch', ts: start + 1 },
  ]), window);
  assert.equal(result.snapshots, 3);
  assert.equal(result.first_snapshot, start);
  assert.equal(result.last_snapshot, end);
  assert.equal(result.measured_pool_rows, 6);
  assert.equal(result.unknown_pool_rows, 0);
  assert.equal(result.max_snapshot_gap_seconds, end - start - 900);
  assert.deepEqual(result.raw_below_threshold_rows, []);
  assert.equal(result.window_at_least_48_hours, true);
  assert.equal(result.runtime_acceptance, 'not_established');
});

test('empty and sparse windows expose missing boundary coverage', () => {
  const empty = auditLedger('', window);
  assert.equal(empty.snapshots, 0);
  assert.equal(empty.first_snapshot, null);
  assert.equal(empty.max_snapshot_gap_seconds, 48 * 3600);
  const sparse = auditLedger(ndjson([snapshot(end - 900)]), window);
  assert.equal(sparse.max_snapshot_gap_seconds, 48 * 3600 - 900);
  assert.equal(auditLedger('', { start, end: start + 3600 }).window_at_least_48_hours, false);
});

test('retains raw short-history failures and capped recovery without silently exempting shed', () => {
  const rows = [snapshot(start, { routing_weight: 0.416 }),
    snapshot(start + 900, { routing_weight: 0.25, state: 'ok' }),
    snapshot(start + 1800, { routing_weight: 0, state: 'shed', used_fraction: 0 })];
  rows[1].agents.codex.pacing_diagnostics.previous_routing_weight = 0;
  const result = auditLedger(ndjson(rows), window);
  assert.equal(result.raw_below_threshold_rows.length, 6);
  assert.equal(result.raw_below_threshold_rows[0].routing_weight, 0.416);
  assert.equal(result.raw_below_threshold_rows[2].previous_routing_weight, 0);
  assert.equal(result.raw_below_threshold_rows[4].pacing_state, 'shed');
});

test('only mature history projecting above one releases the raw threshold', () => {
  for (const [span, projection, usage, expected] of [
    [5.75, 4.2, 0.15, 2], [6, 1, 0.15, 2], [6, null, 0.15, 2],
    [6, 1.001, 0.15, 0], [2, 4.2, 0.5, 0], [2, 4.2, 1, 0],
  ]) {
    const row = snapshot(start, { routing_weight: 0.25, projected_fraction: projection,
      used_fraction: usage });
    for (const agent of Object.values(row.agents)) agent.pacing_diagnostics.span_h = span;
    assert.equal(auditLedger(ndjson([row]), window).raw_below_threshold_rows.length, expected);
  }
});

test('malformed, stale, missing and nonnumeric observations are unknown', () => {
  for (const changes of [
    { stale: true }, { measurement: 'unknown' }, { used_fraction: null },
    { used_fraction: '0.15' }, { routing_weight: null }, { routing_weight: -1 },
    { routing_weight: 2 },
  ]) {
    assert.equal(auditLedger(ndjson([snapshot(start, changes)]), window).unknown_pool_rows, 2);
  }
  const row = snapshot(start);
  delete row.agents.claude;
  row.agents.codex.pacing_diagnostics.span_h = null;
  const result = auditLedger('broken\nnull\n[]\n' + ndjson([
    row, snapshot('1794283200'), snapshot(null), snapshot(-1),
  ]), window);
  assert.equal(result.malformed_rows, 3);
  assert.equal(result.invalid_snapshot_timestamps, 3);
  assert.equal(result.unknown_pool_rows, 2);
  assert.equal(result.measured_pool_rows, 0);
});

test('reads historical diagnostic placement without treating zero span as absent', () => {
  const row = snapshot(start, { routing_weight: 0.25 });
  for (const agent of Object.values(row.agents)) {
    Object.assign(agent.pacing, agent.pacing_diagnostics);
    delete agent.pacing_diagnostics;
  }
  row.agents.codex.pacing_diagnostics = { span_h: 0 };
  const result = auditLedger(ndjson([row]), window);
  assert.equal(result.measured_pool_rows, 2);
  assert.equal(result.raw_below_threshold_rows[0].span_h, 0);
  assert.equal(result.raw_below_threshold_rows[1].span_h, 2);
});

test('captures exact input digest and CLI observations without modifying the ledger', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pacing-ledger-audit-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'ledger.ndjson');
  const bytes = Buffer.from(ndjson([snapshot(start)]));
  fs.writeFileSync(filename, bytes);
  const result = captureLedger(filename, window);
  assert.equal(result.ledger.sha256, createHash('sha256').update(bytes).digest('hex'));
  assert.equal(result.ledger.bytes, bytes.length);
  assert.equal(result.ledger.path, filename);
  assert.ok(Number.isFinite(Date.parse(result.checked_at)));
  const cli = main([filename, String(start), String(end)]);
  assert.equal(cli.ledger.sha256, result.ledger.sha256);
  assert.equal(cli.snapshots, 1);
  assert.deepEqual(fs.readFileSync(filename), bytes);
  assert.deepEqual(fs.readdirSync(directory), ['ledger.ndjson']);
  assert.throws(() => captureLedger(path.join(directory, 'absent'), window), /ENOENT/);
});

test('rejects invalid windows and CLI arguments', () => {
  for (const bounds of [{ start, end: start }, { start: -1, end },
    { start: '1', end }, { start, end: Infinity }]) {
    assert.throws(() => auditLedger('', bounds), /ordered nonnegative/);
  }
  assert.throws(() => main([]), /usage:/);
  assert.throws(() => main(['absent', '', '10']), /usage:/);
});
