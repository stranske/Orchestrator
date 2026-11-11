'use strict';

// Read-only evidence for #698. Raw threshold matches include failure overrides
// and capped recovery; neither fixture rows nor this audit prove deployment.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function auditLedger(text, { start, end }) {
  if (![start, end].every((value) => finite(value) && value >= 0) || end <= start) {
    throw new Error('start and end must be ordered nonnegative Unix seconds');
  }
  const result = {
    schema_version: 1,
    runtime_acceptance: 'not_established',
    window: { start_inclusive: start, end_inclusive: end, hours: (end - start) / 3600 },
    window_at_least_48_hours: end - start >= 48 * 3600,
    malformed_rows: 0,
    invalid_snapshot_timestamps: 0,
    snapshots: 0,
    measured_pool_rows: 0,
    unknown_pool_rows: 0,
    first_snapshot: null,
    last_snapshot: null,
    max_snapshot_gap_seconds: end - start,
    raw_below_threshold_rows: [],
    limitations: [
      'Window timestamps do not bind these rows to a deployed repair revision.',
      'Snapshot gaps include both window boundaries; missing rows are not clean observations.',
      'Raw threshold matches retain exhaustion, latched failures and capped recovery for review.',
      'The post-sync live report and deployment receipt must be captured separately.',
    ],
  };
  const snapshots = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let row;
    try {
      row = JSON.parse(line);
      if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error('invalid row');
    } catch {
      result.malformed_rows += 1;
      continue;
    }
    if (row.event !== 'pacing_snapshot') continue;
    if (!finite(row.ts) || row.ts < 0) {
      result.invalid_snapshot_timestamps += 1;
      continue;
    }
    if (row.ts >= start && row.ts <= end) snapshots.push(row);
  }
  snapshots.sort((a, b) => a.ts - b.ts);
  result.snapshots = snapshots.length;
  if (snapshots.length) {
    result.first_snapshot = snapshots[0].ts;
    result.last_snapshot = snapshots.at(-1).ts;
    let previous = start;
    result.max_snapshot_gap_seconds = 0;
    for (const row of snapshots) {
      result.max_snapshot_gap_seconds = Math.max(result.max_snapshot_gap_seconds, row.ts - previous);
      previous = row.ts;
    }
    result.max_snapshot_gap_seconds = Math.max(result.max_snapshot_gap_seconds, end - previous);
  }
  for (const row of snapshots) {
    for (const pool of ['codex', 'claude']) {
      const agent = row.agents?.[pool];
      const pacing = agent?.pacing || {};
      const diagnostics = agent?.pacing_diagnostics || {};
      // Older snapshots kept diagnostics in the pacing block itself.
      const span = diagnostics.span_h ?? pacing.span_h;
      const usage = pacing.used_fraction;
      const weight = pacing.routing_weight;
      if (pacing.measurement !== 'measured' || pacing.stale !== false
          || !finite(usage) || usage < 0 || !finite(weight) || weight < 0 || weight > 1
          || !finite(span) || span < 0) {
        result.unknown_pool_rows += 1;
        continue;
      }
      result.measured_pool_rows += 1;
      const projection = pacing.projected_fraction;
      const matureExhaustion = span >= 6 && finite(projection) && projection > 1;
      if (usage < 0.5 && weight < 0.5 && !matureExhaustion) {
        result.raw_below_threshold_rows.push({
          ts: row.ts, pool, used_fraction: usage, routing_weight: weight, span_h: span,
          projected_fraction: finite(projection) ? projection : null,
          previous_routing_weight: diagnostics.previous_routing_weight
            ?? pacing.previous_routing_weight ?? null,
          pacing_state: pacing.state ?? null, agent_state: agent.state ?? null,
        });
      }
    }
  }
  return result;
}

function captureLedger(filename, window) {
  const bytes = fs.readFileSync(filename);
  return {
    ...auditLedger(bytes.toString('utf8'), window),
    checked_at: new Date().toISOString(),
    ledger: { path: path.resolve(filename), bytes: bytes.length,
      sha256: createHash('sha256').update(bytes).digest('hex') },
  };
}

function main(args) {
  if (args.length !== 3 || args.slice(1).some((arg) => !/^\d+(?:\.\d+)?$/.test(arg))) {
    throw new Error('usage: node scripts/audit_pacing_ledger.js LEDGER START_UNIX_SECONDS END_UNIX_SECONDS');
  }
  return captureLedger(args[0], { start: Number(args[1]), end: Number(args[2]) });
}

if (require.main === module) {
  try {
    console.log(JSON.stringify(main(process.argv.slice(2)), null, 2));
  } catch (error) {
    console.error(`Pacing ledger audit unavailable: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { auditLedger, captureLedger, main };
