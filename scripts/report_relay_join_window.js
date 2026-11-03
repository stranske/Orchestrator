'use strict';

// Read existing Brain state and optional raw relay receipts only.
// A fresh switch-review report remains separate evidence.
// Requires Node >=22.13, as does export_adjudicator_inventory.js.
const path = require('node:path');
const fs = require('node:fs');
const { createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { parseArgs } = require('node:util');

const capability = 'windowed-capacity-policy';
const version = 'relay-pacing-v1';

function utcSecond(text) {
  const millis = Date.parse(text);
  if (!Number.isFinite(millis) || new Date(millis).toISOString() !== text.replace('Z', '.000Z')) {
    throw new Error('invalid UTC timestamp');
  }
  return millis / 1000;
}

// Optional raw receipts extend the population beyond decisions already imported
// into Brain. Neither file is imported or rewritten by this audit.
function correlateRawLogs(report, pacingLog, exitLog, explicitRounds) {
  const audit = {
    status: 'unavailable',
    files: {},
    window_decisions: null,
    matching_completed_exit_decisions: null,
    accepted_window_joins: null,
    unattributed_completed_exit_decisions: null,
    decisions: null,
    errors: [],
  };
  try {
    if (report.status !== 'available') throw new Error('Brain snapshot unavailable');
    function readLines(file, parse) {
      const bytes = fs.readFileSync(file);
      audit.files[path.resolve(file)] = {
        sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length,
      };
      const rows = [];
      for (const [index, text] of bytes.toString('utf8').split(/\r?\n/).entries()) {
        if (!text.trim()) continue;
        try {
          rows.push({ ...parse(text), line_number: index + 1, text });
        } catch (error) {
          audit.errors.push(`${file}:${index + 1}: ${error.message}`);
        }
      }
      return rows;
    }
    const agents = new Set(['codex', 'claude', 'claude_code', 'cursor', 'muse']);
    const pacing = readLines(pacingLog, (line) => {
      const [stamp, ...parts] = line.trim().split(/\s+/);
      const entries = parts.map((part) => {
        const equals = part.indexOf('=');
        if (equals < 1) throw new Error('invalid pacing field');
        return [part.slice(0, equals), part.slice(equals + 1)];
      });
      const fields = Object.fromEntries(entries);
      if (Object.keys(fields).length !== entries.length) throw new Error('duplicate pacing field');
      for (const key of ['source', 'lane', 'candidates', 'chosen', 'weights', 'states', 'fallback']) {
        if (!fields[key]) throw new Error(`missing pacing field: ${key}`);
      }
      const candidates = fields.candidates.split(',');
      if (!agents.has(fields.source) || !candidates.includes(fields.chosen)
          || !candidates.every((agent) => agents.has(agent))
          || !['opener', 'closer'].includes(fields.lane)
          || !['yes', 'no'].includes(fields.fallback)
          || (fields.decision_id !== undefined && (!fields.decision_id
            || Array.from(fields.decision_id).length > 192 - 'relay-pacing:id:'.length))) {
        throw new Error('invalid pacing facts');
      }
      return { ts: utcSecond(stamp), fields };
    });
    const exits = readLines(exitLog, (line) => {
      const match = /^=== (\S+) RELAY exit=(-?\d+) agent=(\S+) lane=(opener|closer) duration_s=\d+ decision_id=(\S+) ===$/.exec(line.trim());
      if (!match || !agents.has(match[3]) || !Number.isSafeInteger(Number(match[2]))) {
        throw new Error('invalid completed-exit receipt');
      }
      return {
        ts: utcSecond(match[1]), exit_status: Number(match[2]),
        agent: match[3].replace('claude_code', 'claude'), lane: match[4], raw_decision_id: match[5],
      };
    });
    const { start_inclusive: start, end_inclusive: end } = report.window;
    const byId = new Map();
    for (const row of pacing) {
      const id = row.fields.decision_id;
      if (!id || row.ts < start || row.ts > end) continue; // legacy remains outside this audit
      if (!byId.has(id)) byId.set(id, []);
      byId.get(id).push(row);
    }
    const persisted = new Map(report.decisions.map((row) => [row.raw_decision_id, row]));
    const decisions = [];
    let matchingExits = 0;
    for (const [id, rawDecisions] of byId) {
      const first = rawDecisions[0];
      const source = persisted.get(id);
      const receipts = exits.filter((row) => row.raw_decision_id === id && row.ts <= end);
      // Identical repeated receipts describe the same facts; conflicting facts do not.
      const variants = new Set(rawDecisions.map((row) => JSON.stringify([
        row.ts, ...['source', 'lane', 'candidates', 'chosen', 'weights', 'states', 'fallback']
          .map((key) => row.fields[key]),
      ])));
      const exitVariants = new Set(receipts.map((row) =>
        JSON.stringify([row.ts, row.agent, row.lane, row.exit_status])));
      let status = 'no_completed_exit_receipt';
      if (variants.size > 1) status = 'conflicting_raw_decisions';
      else if (exitVariants.size > 1) status = 'ambiguous_completed_exit_receipts';
      else if (receipts.length) {
        const exit = receipts[0];
        if (exit.ts < first.ts || exit.lane !== first.fields.lane
            || exit.agent !== first.fields.chosen.replace('claude_code', 'claude')) {
          status = 'completed_exit_receipt_mismatch';
        } else {
          matchingExits++;
          if (!source) status = 'missing_persisted_decision';
          else if (source.decision_ts !== first.ts
              || ['source', 'lane', 'chosen', 'weights', 'states'].some((key) =>
                source.pacing_facts[key] !== first.fields[key])
              || source.pacing_facts.fallback !== (first.fields.fallback === 'yes')
              || JSON.stringify(source.pacing_facts.candidates)
                !== JSON.stringify(first.fields.candidates.split(','))) {
            status = 'persisted_decision_mismatch';
          }
          else if (source.completed_rounds.length !== 1) status = source.completed_rounds.length
            ? 'ambiguous_completed_exits' : 'missing_persisted_round';
          else {
            const round = source.completed_rounds[0];
            status = round.ts === exit.ts && round.agent === exit.agent
              && round.lane === exit.lane && round.exit_status === exit.exit_status
              ? source.status : 'persisted_round_receipt_mismatch';
          }
        }
      }
      decisions.push({
        raw_decision_id: id, decision_ts: first.ts,
        raw_pacing_lines: rawDecisions, raw_completed_exits: receipts,
        persisted_rounds: explicitRounds.get(id) || [],
        persisted_decision: source || null, status,
        accepted_edge_ids: status === 'accepted_join' ? source.accepted_edge_ids : [],
      });
    }
    const accepted = decisions.filter((row) => row.status === 'accepted_join').length;
    Object.assign(audit, {
      status: audit.errors.length ? 'incomplete' : 'available',
      window_decisions: decisions.length,
      matching_completed_exit_decisions: matchingExits,
      accepted_window_joins: accepted,
      unattributed_completed_exit_decisions: matchingExits - accepted,
      decisions,
    });
  } catch (error) {
    audit.errors.push(error.message);
  }
  return audit;
}

function reportWindow({ db, start, end, pacingLog, exitLog }) {
  if (typeof db !== 'string' || !db.trim()) throw new Error('db is required');
  if (![start, end].every((n) => Number.isSafeInteger(n) && n >= 0) || end < start) {
    throw new Error('start and end must be ordered nonnegative integer Unix seconds');
  }
  if (Boolean(pacingLog) !== Boolean(exitLog)) throw new Error('pacing-log and exit-log are required together');
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
    limitations: [
      'The cumulative count describes bindings present now, not joins newly observed in this window.',
      'Decisions absent from Brain and legacy adjacency are outside this report population.',
      'Raw pacing/exit logs and fresh switch-review counts must be verified separately.',
    ],
    errors: [],
  };
  let connection;
  const explicitRounds = new Map();
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
    const byId = explicitRounds;
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
            capability, version, start, end, round.ts, facts.decision_id, round.exit_status)
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
        pacing_facts: facts,
        status,
        completed_rounds: matches,
        accepted_edge_ids: edges,
      });
    }
    Object.assign(report, {
      status: 'available',
      cumulative_accepted_bindings_present: bindings,
      window_decisions: decisions.length,
      matching_completed_exit_decisions: decisions.filter((d) =>
        ['accepted_join', 'missing_accepted_join'].includes(d.status)).length,
      accepted_window_joins: decisions.filter((d) => d.status === 'accepted_join').length,
      missing_join_decisions: decisions.filter((d) => d.status === 'missing_accepted_join').length,
      decisions,
    });
  } catch (error) {
    report.errors.push(error.message);
  } finally {
    if (connection) connection.close();
  }
  if (pacingLog && exitLog) {
    report.raw_log_correlation = correlateRawLogs(report, pacingLog, exitLog, explicitRounds);
    report.limitations.push('Brain and raw log reads are separate snapshots; concurrent host writes may require another observation.');
  }
  return report;
}

function main() {
  try {
    const { values } = parseArgs({ options: {
      db: { type: 'string' }, start: { type: 'string' }, end: { type: 'string' },
      'pacing-log': { type: 'string' }, 'exit-log': { type: 'string' },
    } });
    const report = reportWindow({ db: values.db, start: Number(values.start), end: Number(values.end),
      pacingLog: values['pacing-log'], exitLog: values['exit-log'] });
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    if (report.status !== 'available'
        || (report.raw_log_correlation && report.raw_log_correlation.status !== 'available')) {
      process.exitCode = 1;
    }
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

if (require.main === module) main();
module.exports = { reportWindow };
