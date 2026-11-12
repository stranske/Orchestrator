'use strict';

// Read-only provisioning acceptance and reporting over the hourly collector.
// An old event timestamp never establishes a continuous observation baseline.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');

const week = 7n * 24n * 3600n * 1000000n;
const maxGap = 2n * 3600n * 1000000n;
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
// Match followup_evidence.eligible_lines: the existing owner exclusion applies
// before parsing, hashing or reporting. Every other row retains its multiplicity.
const excluded = Buffer.from('stranske/' + 'Collab-Admin'.toLowerCase());

function instant(value) {
  const match = typeof value === 'string'
    && /^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match) throw new Error('timezone-qualified timestamp required');
  const local = Date.parse(`${match[1]}T${match[2]}Z`);
  if (!Number.isFinite(local)
      || new Date(local).toISOString().slice(0, 19) !== `${match[1]}T${match[2]}`) {
    throw new Error('invalid calendar timestamp');
  }
  const milliseconds = Date.parse(`${match[1]}T${match[2]}${match[4]}`);
  if (!Number.isFinite(milliseconds)) throw new Error('invalid timestamp');
  return BigInt(milliseconds) * 1000n + BigInt((match[3] || '').padEnd(6, '0'));
}

function readRegular(filename) {
  if (!fs.lstatSync(filename).isFile()) throw new Error('regular file required');
  return fs.readFileSync(filename);
}

function segmentPath(filename) {
  // Runtime directories may have aliases (for example /var and /private/var).
  // Resolve only the parent: final-component symlinks must still be rejected by
  // readRegular, and missing receipt paths must fail comparison without hiding counts.
  try {
    const absolute = path.resolve(filename);
    return path.join(fs.realpathSync(path.dirname(absolute)), path.basename(absolute));
  } catch {
    return null;
  }
}

function eligibleLines(bytes) {
  const lines = [];
  let start = 0;
  while (start < bytes.length) {
    const end = bytes.indexOf(10, start);
    const line = bytes.subarray(start, end < 0 ? bytes.length : end + 1);
    const lower = line.map((byte) => byte >= 65 && byte <= 90 ? byte + 32 : byte);
    if (!lower.includes(excluded)) lines.push(line);
    start += line.length;
  }
  return lines;
}

function deploymentIdentity(root, sourceDirectory, revision) {
  if (!/^[0-9a-f]{40}$/.test(revision)) throw new Error('full delivered SHA required');
  const generation = fs.realpathSync(sourceDirectory);
  const name = path.basename(generation);
  const latest = readRegular(path.join(root, 'mirror-sync-history.jsonl')).toString('utf8')
    .trimEnd().split('\n').map((line) => JSON.parse(line)).reverse()
    .find((row) => row.outcome === 'deployed');
  if (!/^orchestrator-mirror\.generation-[0-9a-f]{32}$/.test(name)
      || latest?.detail !== name || latest.mode !== 'ci-verdict'
      || !/^[0-9a-f]{7,40}$/.test(latest.sha) || !revision.startsWith(latest.sha)) {
    throw new Error('latest normal deployment does not identify the installed generation and SHA');
  }
  instant(latest.ts);
  const installedRevision = execFileSync('git', ['rev-parse', '--verify', `${latest.sha}^{commit}`])
    .toString().trim();
  if (installedRevision !== revision) throw new Error('installer SHA resolves to a different commit');
  // The installer history uses an abbreviated SHA. Bind the actual acceptance
  // inputs to the full delivered commit in the calling checkout.
  for (const file of ['src/provision.py', 'src/followup_evidence.py',
    'scripts/audit_provision_ledger.js']) {
    const installed = file.startsWith('src/') ? path.basename(file) : file;
    const committed = execFileSync('git', ['show', `${revision}:${file}`]);
    if (!committed.equals(readRegular(path.join(generation, installed)))
        || (file.startsWith('scripts/') && !committed.equals(readRegular(__filename)))) {
      throw new Error(`installed or running acceptance input differs from delivered SHA: ${file}`);
    }
  }
  return { generation, deployed_at: latest.ts, revision_prefix: latest.sha };
}

function auditProvisionLedger(root, sourceDirectory, { now = new Date().toISOString(),
  observedAfter = null, revision = null,
  writerPath = process.env.ORCH_PROVISION_EVENTS_PATH ?? path.join(
    process.env.ORCH_LOCAL_RUNTIME ?? path.join(os.homedir(), '.codex', 'orchestrator'),
    'provision-events.ndjson') } = {}) {
  root = fs.realpathSync(root);
  const result = { schema_version: 1, deployment_acceptance_verified: false,
    reporter_sha256: sha256(readRegular(__filename)),
    coverage: 'UNKNOWN', continuous_since: null, checked_at: null, observed_after: observedAfter,
    counts: {}, branch_refusals: 0, malformed_lines: 0, segments: [],
    continuity_failures: [], evidence_ready: false };
  const fail = (message) => result.continuity_failures.push(message);
  let packet;
  try {
    const raw = readRegular(path.join(root, 'followup-evidence', 'latest.json'));
    result.receipt_sha256 = sha256(raw);
    packet = JSON.parse(raw).provision;
    if (!packet || packet.issue !== 1098) throw new Error('issue 1098 packet required');
    result.checked_at = packet.checked_at;
    result.continuous_since = packet.continuous_since;
  } catch {
    fail('collector receipt missing or malformed; collect with the existing hourly tick');
  }
  let deploymentStart = null;
  const postdeployment = [];
  if (revision !== null) {
    result.cohort = { revision, deployment: null, available: 0, evaluated: 0,
      sha256: null, counts: {}, branch_refusals: 0, accepted: false };
    try {
      const identity = deploymentIdentity(root, sourceDirectory, revision);
      result.cohort.deployment = identity;
      deploymentStart = instant(identity.deployed_at);
      if (!Object.entries(identity).every(([key, value]) => packet?.deployment?.identity?.[key] === value)
          || packet.deployment.failure !== null) {
        fail(`continuous deployment observation unavailable: ${packet?.deployment?.failure ?? 'collector provenance differs'}`);
      }
      if (!packet || instant(packet.checked_at) <= deploymentStart) {
        fail('collector has not observed after deployment');
      }
    } catch (error) {
      fail(`deployment provenance unavailable: ${error.message}`);
    }
  }
  // A missing receipt loses continuity, never the visible failures. Recount the
  // current window in that case, explicitly naming its endpoint in the report.
  let end = instant(now);
  let cutoff = end - week;
  result.counted_through = now;
  try {
    const observedEnd = instant(packet.checked_at);
    if (instant(packet.window_start) !== observedEnd - week) throw new Error('window');
    end = observedEnd;
    cutoff = end - week;
    result.counted_through = packet.checked_at;
    const age = instant(now) - end;
    if (age < 0n || age > maxGap) fail('collector receipt is future-dated or older than two hours');
    if (revision === null && instant(packet.continuous_since) > cutoff) {
      fail('seven continuous observed days not established');
    }
    if (packet.collection_max_gap_seconds !== 7200
        || !Array.isArray(packet.continuity_failures)
        || (revision === null && (packet.coverage !== 'continuous' || packet.continuity_failures.length))) {
      fail('collector has not established intact continuous coverage');
    }
  } catch {
    fail('collector window or baseline missing or invalid');
  }
  // Recency alone can accept a receipt from before publication of the same
  // source bytes. Let the caller require the existing collector's next tick;
  // this boundary never advances the receipt or changes its counting window.
  if (observedAfter !== null) {
    try {
      const boundary = instant(observedAfter);
      if (!packet || instant(packet.checked_at) <= boundary) {
        fail('collector receipt is not after the required observation boundary');
      }
    } catch {
      fail('required observation boundary invalid or collector timestamp unavailable');
    }
  }
  const counts = new Map();
  // Discover all segments, including archives when the active filename is absent.
  const names = fs.readdirSync(root).filter((name) => name.startsWith('provision-events.ndjson')).sort();
  if (!names.length) fail('no ledger');
  // Use the writer's effective configured path, not an unrelated zero ledger.
  // A collector that does not observe that path must establish a new baseline.
  if (!names.some((name) => path.join(root, name) === segmentPath(writerPath))) {
    fail('writer ledger is not included in the collector directory and observation');
  }
  const observed = [];
  for (const name of names) {
    try {
      const raw = readRegular(path.join(root, name));
      const lines = eligibleLines(raw);
      const segment = { path: path.join(root, name), lines: lines.length,
        sha256: sha256(Buffer.concat(lines)) };
      observed.push(segment);
      result.segments.push({ ...segment, bytes: raw.length, raw_sha256: sha256(raw) });
      for (const line of lines) {
        try {
          const event = JSON.parse(line.toString('utf8'));
          const ts = instant(event.ts);
          if (line.at(-1) !== 10 || typeof event.outcome !== 'string'
              || typeof event.cause !== 'string') throw new Error('incomplete event');
          if (cutoff <= ts && ts <= end) {
            const key = `${event.outcome}/${event.cause}`;
            counts.set(key, (counts.get(key) || 0) + 1);
          }
          if (deploymentStart !== null && deploymentStart < ts && ts <= end) {
            postdeployment.push({ ts, line, key: `${event.outcome}/${event.cause}` });
          }
        } catch {
          result.malformed_lines += 1;
        }
      }
    } catch {
      fail(`ledger segment unreadable or not a regular file: ${name}`);
    }
  }
  result.counts = Object.fromEntries([...counts].sort(([a], [b]) => a.localeCompare(b)));
  result.branch_refusals = (counts.get('failed/branch-held') || 0)
    + (counts.get('failed/branch-exists') || 0);
  if (result.malformed_lines) fail('malformed or partial ledger data');
  // Exact segment equality binds the counts to this receipt. Appended/rotated
  // data requires a fresh existing-collector observation, never a fabricated zero.
  if (!Array.isArray(packet?.segments) || packet.segments.length !== observed.length
      || observed.some((row) => !packet.segments.some((old) => typeof old?.path === 'string'
        && segmentPath(old.path) === row.path
        && old.lines === row.lines && old.sha256 === row.sha256))) {
    fail('active/archive segments differ from the collector observation');
  }
  if (!packet?.counts || typeof packet.counts !== 'object' || Array.isArray(packet.counts)
      || Object.keys(packet.counts).length !== counts.size
      || [...counts].some(([key, count]) => packet.counts[key] !== count)
      || packet.branch_refusals !== result.branch_refusals) {
    fail('recount differs from the collector observation');
  }
  for (const [name, field] of [['provision.py', 'instrument_sha256'],
    ['followup_evidence.py', 'collector_sha256']]) {
    try {
      const digest = sha256(readRegular(path.join(sourceDirectory, name)));
      result[field] = digest;
      if (packet?.[field] !== digest) fail(`${name} differs from the observed implementation`);
    } catch {
      fail(`${name} source unavailable`);
    }
  }
  if (!result.continuity_failures.length) result.coverage = 'continuous';
  if (result.cohort) {
    // Writer timestamps order operations. Break exact ties with their original
    // bytes so renaming a segment cannot move a refusal across the cutoff.
    // Equal rows stay separate and make identical contributions to the cohort.
    postdeployment.sort((a, b) => a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : Buffer.compare(a.line, b.line));
    const first = postdeployment.slice(0, 100);
    const cohortCounts = new Map();
    for (const { key } of first) cohortCounts.set(key, (cohortCounts.get(key) || 0) + 1);
    const refusals = (cohortCounts.get('failed/branch-held') || 0)
      + (cohortCounts.get('failed/branch-exists') || 0);
    const accepted = result.coverage === 'continuous' && first.length === 100 && refusals === 0;
    Object.assign(result.cohort, { available: postdeployment.length, evaluated: first.length,
      sha256: sha256(Buffer.concat(first.map(({ line }) => line))),
      counts: Object.fromEntries(cohortCounts), branch_refusals: refusals, accepted });
    result.evidence_ready = accepted;
    return result;
  }
  result.evidence_ready = result.coverage === 'continuous' && result.branch_refusals === 0;
  if (packet?.evidence_ready !== result.evidence_ready && !result.continuity_failures.length) {
    fail('collector readiness differs from the recounted evidence');
    result.coverage = 'UNKNOWN';
    result.evidence_ready = false;
  }
  return result;
}

function formatReport(report) {
  const count = report.segments.length ? report.branch_refusals : 'unmeasured: no ledger';
  return [`provision: baseline = ${report.continuous_since || 'UNKNOWN'}`,
    `provision: checked_at = ${report.checked_at || 'UNKNOWN'}`,
    ...(report.observed_after !== null
      ? [`provision: observed_after = ${report.observed_after}`] : []),
    `provision: counted_through = ${report.counted_through}`,
    `provision: coverage = ${report.coverage}`,
    `provision: segments = ${report.segments.length}; malformed_lines=${report.malformed_lines}`,
    ...Object.entries(report.counts).map(([key, n]) => `provision: ${key} = ${n}`),
    `provision: failed(branch-held|branch-exists) = ${count} over 7d`,
    ...(report.cohort ? [
      `provision: first-100 = ${report.cohort.evaluated}/100; failed(branch-held|branch-exists) = ${report.cohort.branch_refusals}; accepted = ${report.cohort.accepted}`,
    ] : []),
    `provision: ledger evidence = ${report.evidence_ready ? 'ready' : 'unmet acceptance'}`,
    ...report.continuity_failures.map((reason) => `provision: UNKNOWN: ${reason}`),
    'provision: native/full-verifier/mirror acceptance requires separate receipts'].join('\n');
}

function main(args) {
  const mode = args[2];
  if (args.length !== 2 && !(args.length === 3 && mode === '--trailing-week')
      && !(args.length === 4 && ['--observed-after', '--revision'].includes(mode))) {
    throw new Error('usage: node scripts/audit_provision_ledger.js RUNTIME SOURCE_DIRECTORY [--revision DELIVERED_SHA | --trailing-week | --observed-after TIMESTAMP]');
  }
  const revision = args.length === 2 ? execFileSync('git', ['rev-parse', 'HEAD']).toString().trim()
    : mode === '--revision' ? args[3] : null;
  const report = auditProvisionLedger(args[0], args[1],
    { observedAfter: mode === '--observed-after' ? args[3] : null, revision });
  console.log(formatReport(report));
  console.log(JSON.stringify(report, null, 2));
  return report.evidence_ready ? 0 : 1;
}

if (require.main === module) {
  try { process.exitCode = main(process.argv.slice(2)); }
  catch (error) {
    console.error(`Provision ledger audit unavailable: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { auditProvisionLedger, eligibleLines, formatReport, instant, main };
