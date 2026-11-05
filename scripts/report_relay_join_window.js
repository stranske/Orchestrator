'use strict';

// Read existing Brain state only. An optional retained pacing log exposes
// explicit raw decisions omitted from Brain. Exit logs and switch-review remain separate evidence.
// Requires Node >=22.13, as does export_adjudicator_inventory.js.
const fs = require('node:fs');
const { createHash } = require('node:crypto');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const { parseArgs } = require('node:util');

const capability = 'windowed-capacity-policy';
const version = 'relay-pacing-v1';
const pacingFields = ['source', 'lane', 'candidates', 'chosen', 'weights', 'states', 'fallback'];
const maxDecisionIdChars = 192 - 'relay-pacing:id:'.length;

function reconcileRawLog(pacingLog, start, end, sources, decisions, byId) {
  const bytes = fs.readFileSync(pacingLog);
  const byRawId = new Map();
  const agents = new Set(['codex', 'claude', 'claude_code', 'cursor', 'muse']);
  let excludedLines = 0;
  for (const line of bytes.toString('utf8').split(/\r?\n/).filter((s) => s.trim())) {
    const [stamp, ...parts] = line.trim().split(/\s+/);
    const fields = Object.create(null);
    if (parts.some((part) => !part.includes('='))) { excludedLines++; continue; }
    for (const part of parts) {
      const split = part.indexOf('=');
      fields[part.slice(0, split)] = part.slice(split + 1);
    }
    const ts = Date.parse(stamp) / 1000;
    const id = fields.decision_id;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(stamp)
        || !Number.isSafeInteger(ts)
        || new Date(ts * 1000).toISOString().replace('.000Z', 'Z') !== stamp
        || !pacingFields.every((key) => Object.hasOwn(fields, key))
        || !agents.has(fields.source) || !['opener', 'closer'].includes(fields.lane)
        || !fields.candidates.split(',').every((agent) => agents.has(agent))
        || !fields.candidates.split(',').includes(fields.chosen)
        || !['yes', 'no'].includes(fields.fallback)
        || !id || [...id].length > maxDecisionIdChars || /\s/.test(id)) {
      excludedLines++;
      continue;
    }
    if (ts > end) continue;
    const facts = Object.fromEntries(pacingFields.map((key) => [key, fields[key]]));
    facts.candidates = fields.candidates.split(',');
    facts.fallback = fields.fallback === 'yes';
    const record = { decision_ts: ts, facts };
    if (!byRawId.has(id)) byRawId.set(id, new Map());
    byRawId.get(id).set(JSON.stringify(record), record);
  }
  const persisted = new Map(decisions.map((d) => [d.raw_decision_id, d]));
  const events = new Map(sources.map((s) => [s.event_id, s]));
  const rows = [];
  for (const [id, records] of byRawId) {
    const rawRecords = [...records.values()];
    const raw = rawRecords.find((r) => r.decision_ts >= start);
    if (!raw) continue;
    const saved = persisted.get(id);
    const rounds = byId.get(id) || [];
    let status = 'missing_persisted_decision';
    if (rawRecords.length > 1) {
      status = 'ambiguous_raw_decisions';
    } else if (saved) {
      const facts = JSON.parse(events.get(saved.source_event_id).payload_json).relay_pacing;
      status = saved.decision_ts === raw.decision_ts
        && pacingFields.every((key) => JSON.stringify(facts[key]) === JSON.stringify(raw.facts[key]))
        ? saved.status : 'persisted_decision_mismatch';
    }
    rows.push({
      raw_decision_id: id,
      decision_ts: raw.decision_ts,
      raw_records: rawRecords,
      source_event_id: saved?.source_event_id ?? null,
      source_run_id: `relay-pacing:id:${id}`,
      persisted_decision_ts: saved?.decision_ts ?? null,
      status,
      matching_completed_exit: rawRecords.length === 1 && rounds.length === 1
        && Number.isSafeInteger(rounds[0].exit_status) && rounds[0].ts >= raw.decision_ts
        && rounds[0].lane === raw.facts.lane
        && rounds[0].agent === raw.facts.chosen.replace('claude_code', 'claude'),
      completed_rounds: rounds,
      accepted_edge_ids: status === 'accepted_join' ? saved.accepted_edge_ids : [],
    });
  }
  rows.sort((a, b) => a.decision_ts - b.decision_ts
    || a.raw_decision_id.localeCompare(b.raw_decision_id));
  return {
    scope: 'raw_explicit_relay_decisions',
    pacing_log: path.resolve(pacingLog),
    pacing_log_sha256: createHash('sha256').update(bytes).digest('hex'),
    excluded_lines: excludedLines,
    window_decisions: rows.length,
    matching_completed_exit_decisions: rows.filter((r) => r.matching_completed_exit).length,
    accepted_window_joins: rows.filter((r) => r.status === 'accepted_join').length,
    unaccounted_completed_exit_decisions: rows.filter((r) =>
      r.matching_completed_exit && r.status !== 'accepted_join').length,
    decisions: rows,
    limitations: [
      'Legacy, malformed and ID-less lines are excluded; conflicting raw IDs cannot earn joins.',
      'Use a retained pacing log captured with the Brain snapshot; separate live files are not atomic.',
      'Completed rounds are persisted Brain records; raw exit logs and deployment provenance require separate verification.',
    ],
  };
}

function reportWindow({ db, start, end, pacingLog }) {
  if (typeof db !== 'string' || !db.trim()) throw new Error('db is required');
  if (![start, end].every((n) => Number.isSafeInteger(n) && n >= 0) || end < start) {
    throw new Error('start and end must be ordered nonnegative integer Unix seconds');
  }
  const report = {
    schema_version: 1,
    scope: 'persisted_explicit_relay_decisions',
    database: path.resolve(db),
    window: { start_inclusive: start, end_inclusive: end },
    status: 'unavailable',
    cumulative_accepted_bindings_present: null,
    window_decisions: null,
    matching_completed_exit_decisions: null,
    accepted_window_joins: null,
    missing_join_decisions: null,
    decisions: null,
    raw_reconciliation: null,
    limitations: [
      'The cumulative count describes bindings present now, not joins newly observed in this window.',
      'Top-level counts cover persisted decisions; optional raw_reconciliation also exposes decisions absent from Brain.',
      'Raw pacing/exit logs and fresh switch-review counts must be verified separately.',
    ],
    errors: [],
  };
  let connection;
  try {
    connection = new DatabaseSync(report.database, { readOnly: true });
    connection.exec('PRAGMA busy_timeout=5000; BEGIN');
    const bindings = connection.prepare(`
      SELECT COUNT(DISTINCT source_run_id) AS count FROM influence_edges
      WHERE capability_id=? AND capability_version_id=? AND accepted=1
    `).get(capability, version).count;
    const sources = connection.prepare(`
      SELECT event_id,run_id,created_ts,payload_json FROM completion_events
      WHERE capability_id=? AND capability_version_id=?
        AND producer='feedback.relay_pacing' AND phase='decision'
        AND validation_status='accepted' AND created_ts BETWEEN ? AND ?
      ORDER BY created_ts,run_id
    `).all(capability, version, start, end);
    const rounds = connection.prepare(`
      SELECT decision_id,ts,agent,lane,exit_status,error_class,causal_context_json
      FROM routing_decisions_v2 WHERE record_kind='lane_round' AND ts<=?
      ORDER BY ts,decision_id
    `).all(end);
    const byId = new Map();
    for (const round of rounds) {
      const id = JSON.parse(round.causal_context_json || '{}').relay_decision_id;
      if (!id) continue;
      if (!byId.has(id)) byId.set(id, []);
      const { causal_context_json, ...facts } = round;
      byId.get(id).push(facts);
    }
    const joined = connection.prepare(`
      SELECT ie.edge_id FROM influence_edges ie
      JOIN completion_events target ON target.event_id=ie.target_event_id
      WHERE ie.source_event_id=? AND ie.source_run_id=? AND ie.target_run_id=?
        AND ie.capability_id=? AND ie.capability_version_id=?
        AND ie.accepted=1 AND ie.counterfactual=0 AND ie.created_ts BETWEEN ? AND ?
        AND target.run_id=ie.target_run_id AND target.created_ts=?
        AND target.capability_id=ie.capability_id
        AND target.capability_version_id=ie.capability_version_id
        AND target.producer='feedback.relay_pacing' AND target.phase='outcome'
        AND target.validation_status='accepted'
        AND json_extract(target.payload_json,'$.relay_pacing.round_id')=ie.target_run_id
        AND json_extract(target.payload_json,'$.relay_pacing.decision_id')=?
        AND json_extract(target.payload_json,'$.relay_pacing.exit_status')=?
        AND json_extract(target.payload_json,'$.relay_pacing.lane')=?
        AND json_extract(target.payload_json,'$.relay_pacing.chosen')=?
        AND json_extract(target.payload_json,'$.relay_pacing.status')
          IN ('completed','failed','rate-limited')
      ORDER BY ie.edge_id
    `);
    const decisions = [];
    for (const source of sources) {
      const facts = JSON.parse(source.payload_json).relay_pacing;
      if (!facts || !facts.decision_id) continue;
      const matches = byId.get(facts.decision_id) || [];
      let status = 'no_completed_exit';
      let edges = [];
      if (source.run_id !== `relay-pacing:id:${facts.decision_id}`) {
        status = 'decision_identity_mismatch';
      } else if (matches.length > 1) {
        status = 'ambiguous_completed_exits';
      } else if (matches.length === 1) {
        const round = matches[0];
        if (!Number.isSafeInteger(round.exit_status)
            || round.ts < source.created_ts || round.lane !== facts.lane
            || round.agent !== facts.chosen.replace('claude_code', 'claude')) {
          status = 'completed_exit_mismatch';
        } else {
          edges = joined.all(source.event_id, source.run_id, round.decision_id,
            capability, version, start, end, round.ts, facts.decision_id, round.exit_status,
            facts.lane, facts.chosen)
            .map((row) => row.edge_id);
          status = edges.length ? 'accepted_join' : 'missing_accepted_join';
        }
      }
      decisions.push({
        raw_decision_id: facts.decision_id,
        source_run_id: source.run_id,
        source_event_id: source.event_id,
        decision_ts: source.created_ts,
        lane: facts.lane,
        chosen: facts.chosen,
        status,
        completed_rounds: matches,
        accepted_edge_ids: edges,
      });
    }
    const rawReconciliation = pacingLog === undefined ? null
      : reconcileRawLog(pacingLog, start, end, sources, decisions, byId);
    Object.assign(report, {
      status: 'available',
      cumulative_accepted_bindings_present: bindings,
      window_decisions: decisions.length,
      matching_completed_exit_decisions: decisions.filter((d) =>
        ['accepted_join', 'missing_accepted_join'].includes(d.status)).length,
      accepted_window_joins: decisions.filter((d) => d.status === 'accepted_join').length,
      missing_join_decisions: decisions.filter((d) => d.status === 'missing_accepted_join').length,
      decisions,
      raw_reconciliation: rawReconciliation,
    });
  } catch (error) {
    report.errors.push(error.message);
  } finally {
    if (connection) connection.close();
  }
  return report;
}

function main() {
  try {
    const { values } = parseArgs({ options: {
      db: { type: 'string' }, start: { type: 'string' }, end: { type: 'string' },
      'pacing-log': { type: 'string' },
    } });
    const report = reportWindow({ db: values.db, start: Number(values.start), end: Number(values.end),
      pacingLog: values['pacing-log'] });
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    if (report.status !== 'available') process.exitCode = 1;
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

if (require.main === module) main();
module.exports = { reportWindow };
