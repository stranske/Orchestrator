'use strict';

// Read-only comparison with the owner's private corpus. This does not authenticate
// that corpus, recover an old replay receipt, or turn fixture replay into live action.
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { isDeepStrictEqual } = require('node:util');

const fixtureNames = Array.from({ length: 12 }, (_, i) =>
  `historical-${String(i + 1).padStart(2, '0')}.json`);
const defaultFixtures = path.resolve(__dirname, '../tests/fixtures/redirect_stalls');

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function retained(report, fixture = false) {
  return Object.fromEntries(Object.entries(report).filter(([key]) =>
    key !== 'target' && !(fixture && key === 'policy_decision')));
}

function audit(corpus, fixtures = defaultFixtures) {
  const result = {
    observed_at: new Date().toISOString(),
    status: 'unavailable',
    historical_replay_receipt: 'not_established',
    source_authenticity: 'requires_owner_receipt',
    expected_bindings: 12,
    verified_bindings: 0,
    bindings: [],
    limitations: [],
  };
  let corpusBytes;
  try {
    corpusBytes = fs.readFileSync(corpus);
  } catch {
    result.limitations.push('private_source_corpus_unavailable');
    return result;
  }
  result.corpus_sha256 = sha256(corpusBytes);
  const sources = new Map();
  let stalled = 0;
  const lines = corpusBytes.toString('utf8').split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trim()) continue;
    let entry;
    try {
      entry = JSON.parse(lines[i]);
    } catch {
      result.limitations.push('invalid_source_corpus_json');
      return result;
    }
    if (!object(entry)) {
      result.limitations.push('invalid_source_corpus_entry');
      return result;
    }
    if (entry.kind !== 'redirect_proposal' || entry.report?.state !== 'stalled') continue;
    stalled++;
    const fingerprint = entry.report.sweep_fingerprint;
    const rows = sources.get(fingerprint) || [];
    rows.push({ report: entry.report, line: i + 1, bytes: lines[i] });
    sources.set(fingerprint, rows);
  }
  result.stalled_source_reports = stalled;
  let names;
  try {
    names = fs.readdirSync(fixtures).filter((name) => name.endsWith('.json')).sort();
  } catch {
    result.limitations.push('fixture_directory_unavailable');
    return result;
  }
  result.status = 'mismatch';
  if (!isDeepStrictEqual(names, fixtureNames)) {
    result.limitations.push('expected_twelve_fixture_files');
    return result;
  }
  const sourceTargets = new Map();
  const fixtureTargets = new Map();
  const used = new Set();
  for (const name of fixtureNames) {
    let bytes;
    let fixture;
    try {
      bytes = fs.readFileSync(path.join(fixtures, name));
      fixture = JSON.parse(bytes.toString('utf8'));
    } catch {
      result.limitations.push(`${name}: fixture_unreadable`);
      continue;
    }
    if (!object(fixture) || fixture.state !== 'stalled' || fixture.lane !== 'experiment'
      || typeof fixture.sweep_fingerprint !== 'string' || !fixture.sweep_fingerprint) {
      result.limitations.push(`${name}: invalid_stall_fixture`);
      continue;
    }
    const matches = sources.get(fixture.sweep_fingerprint) || [];
    if (matches.length !== 1 || used.has(fixture.sweep_fingerprint)) {
      result.limitations.push(`${name}: source_binding_missing_or_ambiguous`);
      continue;
    }
    used.add(fixture.sweep_fingerprint);
    const source = matches[0];
    if (!isDeepStrictEqual(retained(source.report), retained(fixture, true))) {
      result.limitations.push(`${name}: retained_source_facts_differ`);
      continue;
    }
    if (!fixture.policy_action || !fixture.policy_confidence
      || !isDeepStrictEqual(fixture.policy_decision, {
        action: fixture.policy_action, confidence: fixture.policy_confidence,
      })) {
      result.limitations.push(`${name}: policy_mapping_differs`);
      continue;
    }
    const original = source.report.target;
    const anonymous = fixture.target;
    if (typeof original !== 'string' || !original || typeof anonymous !== 'string' || !anonymous
      || (sourceTargets.has(original) && sourceTargets.get(original) !== anonymous)
      || (fixtureTargets.has(anonymous) && fixtureTargets.get(anonymous) !== original)) {
      result.limitations.push(`${name}: target_anonymization_not_one_to_one`);
      continue;
    }
    if (original === anonymous) {
      result.limitations.push(`${name}: target_not_anonymized`);
      continue;
    }
    sourceTargets.set(original, anonymous);
    fixtureTargets.set(anonymous, original);
    result.bindings.push({
      fixture: name,
      fixture_sha256: sha256(bytes),
      source_line: source.line,
      source_entry_sha256: sha256(source.bytes),
    });
  }
  result.verified_bindings = result.bindings.length;
  if (!result.limitations.length && result.verified_bindings === 12) result.status = 'verified';
  return result;
}

function main(args = process.argv.slice(2)) {
  if (args.length < 1 || args.length > 2 || args.some((arg) => arg.startsWith('--'))) {
    console.error('Usage: node scripts/audit_redirect_stall_provenance.js <private-corpus.jsonl> [fixtures-dir]');
    return 2;
  }
  // Never print exception messages or source values: they can contain private targets.
  try {
    const result = audit(args[0], args[1]);
    console.log(JSON.stringify(result, null, 2));
    return result.status === 'verified' ? 0 : 1;
  } catch {
    console.error('Provenance audit failed; historical provenance remains unverified.');
    return 1;
  }
}

if (require.main === module) process.exitCode = main();
module.exports = { audit, fixtureNames, main };
