'use strict';

// Audit the recovery ledger's attribution and local artifact integrity. External
// references remain references: this does not authenticate a native receipt,
// establish a deployed revision, regrade a report, or dispatch a retry.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseArgs } = require('node:util');

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function auditLedger(file) {
  const ledgerPath = fs.realpathSync(file);
  const base = path.dirname(ledgerPath);
  const bytes = fs.readFileSync(ledgerPath);
  const ledger = JSON.parse(bytes);
  const artifacts = new Map();
  const external = [];
  const errors = [];
  function requireValue(condition, message) {
    if (!condition) errors.push(message);
  }
  const nonblank = (value) => typeof value === 'string' && Boolean(value.trim());
  function local(reference, directory = base) {
    if (!nonblank(reference) || path.isAbsolute(reference)) {
      throw new Error('Artifact reference must be relative to the ledger');
    }
    const target = fs.realpathSync(path.resolve(directory, reference));
    const relative = path.relative(base, target);
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new Error('Artifact reference escapes the ledger directory');
    }
    const content = fs.readFileSync(target);
    artifacts.set(relative, { path: relative, sha256: sha256(content) });
    return { target, content };
  }
  function checkHash(reference, expected, directory) {
    requireValue(typeof expected === 'string' && /^[a-f0-9]{64}$/.test(expected),
      `Missing or invalid SHA-256 for ${reference}`);
    const artifact = local(reference, directory);
    requireValue(sha256(artifact.content) === expected, `SHA-256 mismatch for ${reference}`);
  }
  function receipt(reference) {
    const { target, content } = local(reference);
    const directory = path.dirname(target);
    const saved = JSON.parse(content);
    // Each historical receipt keeps its own source identity. Do not compare an
    // older source manifest to today's checkout or treat it as a deployment.
    function visit(value) {
      if (!value || typeof value !== 'object') return;
      if (Object.hasOwn(value, 'evidence_sha256')) {
        const hashes = value.evidence_sha256;
        requireValue(hashes && typeof hashes === 'object' && !Array.isArray(hashes),
          'Evidence SHA-256 map must be an object');
        if (hashes && typeof hashes === 'object' && !Array.isArray(hashes)) {
          for (const [reference, expected] of Object.entries(hashes)) {
            checkHash(reference, expected, directory);
          }
        }
      }
      for (const key of ['log', 'junit', 'real_input_observation']) {
        if (Object.hasOwn(value, key) &&
            (key !== 'junit' || value[key] !== null || value[`${key}_sha256`] !== null)) {
          checkHash(value[key], value[`${key}_sha256`], directory);
        }
      }
      for (const [key, item] of Object.entries(value)) {
        if (!['log_sha256', 'junit_sha256', 'real_input_observation_sha256'].includes(key) &&
            key.endsWith('_sha256') &&
            Object.hasOwn(value, key.slice(0, -7))) {
          const referenceKey = key.slice(0, -7);
          if (item !== null || value[referenceKey] !== null) {
            checkHash(value[referenceKey], item, directory);
          }
        }
        if (key !== 'source_sha256') visit(item);
      }
    }
    visit(saved);
    for (const [log, observation] of Object.entries(saved.regression_logs || {})) {
      checkHash(log, observation.sha256, directory);
    }
  }
  function referenceOnly(category, item) {
    requireValue(nonblank(item.reference), `${category} requires an external reference`);
    external.push({ category, reference: item.reference, authenticated_by_audit: false });
  }

  requireValue(ledger.schema_version === 1, 'Unsupported ledger schema');
  requireValue(ledger.acceptance_complete === false,
    'Recovery artifacts alone cannot establish acceptance_complete');
  requireValue(nonblank(ledger.checked_date), 'Ledger check date is required');
  requireValue(Array.isArray(ledger.missing_evidence) && ledger.missing_evidence.length > 0 &&
    ledger.missing_evidence.every(nonblank), 'Missing historical proof must be disclosed');
  requireValue(ledger.missing_evidence?.some((item) =>
    nonblank(item) && item.includes('October 18')),
  'Missing raw October 18 proof must remain explicitly disclosed');
  requireValue(ledger.grade_thresholds?.intermediate_graded_25 === 'not_verified' &&
    ledger.grade_thresholds?.canonical_graded_29 === 'not_verified' &&
    ledger.grade_thresholds?.advice_graded_is_substitute === false,
  'Recovery ledger must keep unverified canonical thresholds separate from advice_graded');
  requireValue(ledger.fixed_next_20?.status === 'unavailable' &&
    ledger.fixed_next_20.manifest === null &&
    ledger.fixed_next_20.verified_members_with_decisions === null,
  'Recovery ledger cannot substitute aggregate observations for fixed membership');
  const identities = ledger.revision_identity;
  requireValue(identities?.accepted_source?.status === 'unverified' &&
    identities?.current_deployed_revision?.status === 'unavailable' &&
    identities?.current_mirror_revision?.status === 'unavailable',
  'Recovery ledger must disclose unverified accepted/deployed/mirror bindings');

  requireValue(Array.isArray(ledger.historical_observations) &&
    ledger.historical_observations.length > 0, 'Historical observations must remain separate');
  for (const item of ledger.historical_observations || []) {
    referenceOnly('historical_observations', item);
    requireValue(nonblank(item.date) && nonblank(item.class) && nonblank(item.limitation),
      'Historical observations need dates, classes and limitations');
    if (item.inventory) {
      const saved = JSON.parse(local(item.inventory).content);
      requireValue(saved.evidence_class === 'historical_snapshot' &&
        saved.evidence_reference === item.reference &&
        saved.retained_population === item.retained_population &&
        saved.case_count === item.retained_rows &&
        saved.retained_summary?.graded === item.retained_graded &&
        saved.receipt_authority === 'not_authenticated_by_inventory',
      'Historical inventory attribution or retained counts disagree with the ledger');
    }
  }
  const current = ledger.current_observations;
  requireValue(current && !Array.isArray(current) && typeof current === 'object' &&
    nonblank(current.checks), 'Current observations need their own check receipt');
  const receipts = new Set();
  function collect(value) {
    if (!value || typeof value !== 'object') return;
    for (const [key, item] of Object.entries(value)) {
      if (['checks', 'validation', 'runner_validation'].includes(key)) {
        requireValue(nonblank(item), `Current ${key} needs a receipt path`);
        if (nonblank(item)) receipts.add(item);
      } else collect(item);
    }
  }
  collect(current);
  for (const reference of receipts) receipt(reference);

  requireValue(Array.isArray(ledger.mirror_synchronization) &&
    ledger.mirror_synchronization.length > 0, 'Mirror synchronization must remain separate');
  for (const item of ledger.mirror_synchronization || []) {
    if (item.class === 'historical_sync_observation') {
      referenceOnly('mirror_synchronization', item);
      requireValue(nonblank(item.date), 'Historical mirror synchronization needs a date');
    } else requireValue(item.class === 'current_sync_observation' &&
      item.status === 'unavailable' && item.writes_performed === 0,
    'Current mirror synchronization must disclose unavailable proof');
  }
  const dispatch = ledger.dispatch_receipts;
  requireValue(dispatch && Array.isArray(dispatch.historical_receipt_index),
    'Dispatch references must remain separate');
  for (const item of dispatch?.historical_receipt_index || []) {
    referenceOnly('dispatch_receipts', item);
  }
  requireValue(['provider_calls', 'production_dispatches', 'canonical_publications']
    .every((key) => dispatch?.current_run?.[key] === 0),
  'Recovery audit does not support a current dispatch/publication claim');
  requireValue(fs.readFileSync(ledgerPath).equals(bytes), 'Ledger changed during audit');
  if (errors.length) throw new Error(errors.join('\n'));
  return { schema_version: 1, ledger_sha256: sha256(bytes),
    audit_scope: 'recovery_attribution_and_local_artifact_integrity',
    attribution_and_artifact_checks_passed: true, operational_acceptance_verified: false,
    external_references_authenticated: false, source_revision_bindings_verified: false,
    artifacts: [...artifacts.values()], external_references: external,
    missing_evidence: ledger.missing_evidence };
}

if (require.main === module) {
  try {
    const { values } = parseArgs({ options: { ledger: { type: 'string' } } });
    if (!values.ledger) throw new Error('Select a ledger with --ledger');
    process.stdout.write(`${JSON.stringify(auditLedger(values.ledger), null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { auditLedger };
