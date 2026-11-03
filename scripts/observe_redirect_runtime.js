'use strict';

// Observe the existing free screen; never dispatch, link outcomes, or run --status
// (its default link preview can create/migrate the Brain). Run on the owner host.
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const digest = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const identity = (value) => typeof value === 'string' && value ? digest(value) : null;

function read(file) {
  try {
    const bytes = fs.readFileSync(file);
    return { bytes, status: 'read', sha256: digest(bytes), mtime: fs.statSync(file).mtime.toISOString() };
  } catch (error) {
    return { status: error.code === 'ENOENT' ? 'missing' : 'unreadable' };
  }
}

function jsonLines(bytes) {
  return bytes.toString('utf8').split('\n').flatMap((line, index) => {
    if (!line.trim()) return [];
    const entry = JSON.parse(line);
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) throw new Error('invalid entry');
    return [{ entry, line: index + 1, sha256: digest(line) }];
  });
}

function loggedRuns(bytes) {
  const runs = [];
  let current;
  for (const [index, line] of bytes.toString('utf8').split('\n').entries()) {
    const header = line.match(/^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\] redirect apply\/link$/);
    if (header) {
      current = { recorded_at: header[1], line: index + 1, summaries: [] };
      runs.push(current);
    }
    // Keep only known numeric summaries, never raw targets, paths, or refusals.
    if (current && /^(?:candidates: supervisor \d+, sweep \d+, eligible (?:\d+|UNKNOWN)|offloads_spent=\d+ authorized=\d+ applied=\d+ \(flag ORCH_REDIRECT_APPLY_BOOTSTRAP=[01]\))$/.test(line)) {
      current.summaries.push({ line: index + 1, sha256: digest(line), text: line });
    }
  }
  return { total: runs.length, latest: runs.slice(-20) };
}

function recordedSwitchReview(input) {
  if (!input.bytes) return { status: input.status };
  try {
    const report = JSON.parse(input.bytes.toString('utf8'));
    const monitor = report.value_chain;
    const timestamp = (value) => Number.isSafeInteger(value) && value > 0;
    if (!timestamp(report.generated_at)) throw new Error('invalid timestamp');
    if (monitor?.disabled === true) return { status: 'monitor_disabled', recorded_at: report.generated_at };
    if (!timestamp(monitor?.generated_at) || !Number.isSafeInteger(monitor.window_days)
      || monitor.window_days <= 0 || !Array.isArray(monitor.rows)) throw new Error('invalid monitor');
    const rows = monitor.rows.filter((row) => row?.capability === 'redirect-apply-bootstrap');
    if (!rows.length) return { status: 'row_unavailable', recorded_at: report.generated_at };
    if (rows.length !== 1) throw new Error('ambiguous row');
    const fields = ['situation_count', 'offered', 'invocation_count', 'usable', 'acted_on',
      'outcome', 'outcome_success'];
    const counts = Object.fromEntries(fields.map((key) => [key, rows[0][key] ?? null]));
    if (Object.values(counts).some((value) => value !== null
      && (!Number.isSafeInteger(value) || value < 0))) throw new Error('invalid count');
    const firstBreak = rows[0].first_break ?? null;
    if (firstBreak !== null && !['no_situation', 'not_offered', 'not_invoked', 'unusable',
      'not_acted_on', 'no_outcome', 'no_success', 'outcome_excluded',
      'outcome_unscoreable', 'works'].includes(firstBreak)) throw new Error('invalid break');
    // These are a retained report's rolling counts, not current eligibility or
    // attribution of an individual apply. Never copy targets or free-form errors.
    return { status: 'recorded', recorded_at: report.generated_at,
      monitor_generated_at: monitor.generated_at, window_days: monitor.window_days,
      counts, first_break: firstBreak };
  } catch {
    return { status: 'invalid' };
  }
}

function observe({ runtime, stateDir = runtime, python = 'python3', env = process.env, run = spawnSync }) {
  const result = {
    observed_at: new Date().toISOString(),
    status: 'unavailable',
    operational_acceptance: 'not_established',
    historical_replay_receipt: 'not_established',
    blocking: null, drainable: null, candidates: null,
    process_flag: ['0', '1'].includes(env.ORCH_REDIRECT_APPLY_BOOTSTRAP)
      ? env.ORCH_REDIRECT_APPLY_BOOTSTRAP : null,
    flag_scope: 'invoking_process_only; deployed_tick_flag_not_established',
    artifacts: {}, limitations: [],
  };
  const files = {
    corpus: env.ORCH_REDIRECT_SHADOW_CORPUS || path.join(runtime, 'redirect-shadow/shadow.jsonl'),
    plan: env.ORCH_KEEPALIVE_STAGE2_PLAN_JSON || path.join(stateDir, 'keepalive-supervisor-stage2-plan.json'),
    sweep: path.join(stateDir, 'redirect-sweep.json'),
    success: path.join(stateDir, '.last-redirect-apply-link'),
    failure: path.join(stateDir, '.fail-redirect-apply-link'),
    log: path.join(stateDir, 'redirect-apply.log'),
    switch_review: path.join(stateDir, 'switch-review.json'),
  };
  const inputs = Object.fromEntries(Object.entries(files).map(([key, file]) => [key, read(file)]));
  for (const [key, { bytes, ...metadata }] of Object.entries(inputs)) result.artifacts[key] = metadata;
  if (inputs.log.bytes) result.logged_runs = loggedRuns(inputs.log.bytes);
  result.recorded_switch_review = recordedSwitchReview(inputs.switch_review);
  let events;
  try {
    if (!inputs.corpus.bytes) throw new Error('unavailable');
    events = jsonLines(inputs.corpus.bytes);
  } catch {
    result.limitations.push(inputs.corpus.bytes ? 'corpus_invalid' : 'owner_corpus_unavailable');
    return result;
  }
  // These are recorded events, not proof of influence edges or resolved stalls.
  result.recorded_lineage = events.filter(({ entry }) =>
    ['redirect_apply', 'redirect_outcome_link', 'redirect_outcome_link_failed'].includes(entry.kind))
    .map(({ entry, line, sha256 }) => ({
      line, sha256, kind: entry.kind, recorded_at: Number.isFinite(entry.ts) ? entry.ts : null,
      role_run_sha256: identity(entry.role_run_id), target_sha256: identity(entry.target),
      influenced_run_sha256: identity(entry.influenced_run_id),
      applied: entry.applied === true, dry_run: entry.dry_run === true,
      authorized: entry.authorized === true, synced: entry.link_result?.synced === true,
    }));
  const source = path.resolve(__dirname, '../src/redirect_apply.py');
  result.screen_source_sha256 = read(source).sha256 || null;
  const childEnv = { ...env, ORCH_LOCAL_RUNTIME: runtime, ORCH_STATE_DIR: stateDir,
    ORCH_REDIRECT_SHADOW_CORPUS: files.corpus, ORCH_CAPABILITY_HEARTBEATS: '0' };
  delete childEnv.PYTHONPATH;
  delete childEnv.PYTHONHOME;
  const execution = run(python, ['-B', source, '--screen', '--json', '--corpus', files.corpus,
    '--stage2-plan', files.plan, '--report-dir', env.ORCH_KEEPALIVE_STAGE2_REPORT_DIR
      || path.join(stateDir, 'keepalive-supervisor-stage2')], {
    env: childEnv, encoding: 'utf8', timeout: 30000, maxBuffer: 4 * 1024 * 1024,
  });
  result.screen_execution = { exit_code: execution.status, signal: execution.signal || null,
    error_code: execution.error?.code || null };
  // A growing append-only corpus or refreshed plan must not be attributed to an
  // earlier hash. Retry capture after the bounded cadence finishes; never lock it.
  const changed = Object.entries(files).filter(([key, file]) => {
    const after = read(file);
    return after.status !== inputs[key].status || after.sha256 !== inputs[key].sha256
      || after.mtime !== inputs[key].mtime;
  }).map(([key]) => key);
  if (changed.length) {
    result.limitations.push('artifacts_changed_during_capture');
    result.changed_artifacts = changed;
    if (changed.includes('switch_review')) result.recorded_switch_review = { status: 'changed_during_capture' };
    return result;
  }
  if (execution.status !== 0 || execution.signal || execution.error) {
    result.limitations.push('screen_execution_failed');
    return result;
  }
  try {
    const screen = JSON.parse(execution.stdout);
    const nonnegative = (value) => Number.isInteger(value) && value >= 0;
    if (!nonnegative(screen.gate?.synced_needed) || !nonnegative(screen.gate?.disagreements_needed)
      || !Array.isArray(screen.candidates)
      || !(screen.passing_screen === null || nonnegative(screen.passing_screen))
      || !(screen.current_candidates === null || nonnegative(screen.current_candidates))
      || ((screen.current_candidates === null) !== (screen.passing_screen === null))
      || (screen.current_candidates !== null && (screen.current_candidates !== screen.candidates.length
        || screen.passing_screen !== screen.candidates.filter((row) => row.passes_screen === true).length))
      || screen.candidates.some((row) => typeof row.passes_screen !== 'boolean'
        || !Array.isArray(row.blocks) || row.blocks.some((block) => typeof block !== 'string'))) throw new Error('invalid screen');
    result.blocking = { synced_outcomes_needed: screen.gate.synced_needed,
      disagreements_needed: screen.gate.disagreements_needed };
    result.drainable = screen.passing_screen;
    result.candidates = screen.current_candidates;
    result.population = { supervisor: screen.population?.status ?? 'unknown',
      sweep: screen.sweep_population?.status ?? 'unknown' };
    result.refusals = screen.candidates.filter((row) => row.passes_screen === false).map((row) => ({
      target_sha256: identity(row.target),
      block_sha256: row.blocks.map((block) => digest(block)),
    }));
    result.status = result.candidates === null ? 'unavailable' : 'observed';
    if (result.candidates === null) result.limitations.push('current_population_unavailable');
  } catch {
    result.blocking = result.drainable = result.candidates = null;
    result.limitations.push('screen_output_invalid');
  }
  return result;
}

function main(args = process.argv.slice(2)) {
  if (args.length < 1 || args.length > 3 || args.some((arg) => arg.startsWith('--'))) {
    console.error('Usage: node scripts/observe_redirect_runtime.js <owner-runtime> [state-dir] [python-path]');
    return 2;
  }
  const result = observe({ runtime: path.resolve(args[0]),
    stateDir: path.resolve(args[1] || args[0]), python: args[2] });
  console.log(JSON.stringify(result, null, 2));
  return result.status === 'observed' ? 0 : 1;
}

if (require.main === module) process.exitCode = main();
module.exports = { observe, main };
