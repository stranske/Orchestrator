'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const python = process.env.PYTHON || 'python3';
const profiles = ['codex-6-astra-high', 'codex-5.6-terra-high'];
const quality = Object.fromEntries(profiles.map((profile, index) => [profile, 0.8 + index / 10]));

// Synthetic evidence exercises the operator's prepare/finalize commands in separate
// processes. It never dispatches workers or writes a live trial receipt.
function trialWorld(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'profile-trial-cli-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sourceFiles = ['orchestrator', 'workflows'].map((name) => {
    const directory = path.join(root, name);
    fs.mkdirSync(directory);
    const file = path.join(directory, 'sample.py');
    fs.writeFileSync(file, 'VALUE = 1\n');
    return file;
  });
  const env = {
    PATH: process.env.PATH,
    PYTHONPATH: modules,
    PYTHONDONTWRITEBYTECODE: '1',
    ORCH_STATE_DIR: path.join(root, 'state'),
    ORCH_LOCAL_RUNTIME: path.join(root, 'runtime'),
    ORCH_FEEDBACK_DB: path.join(root, 'quarantine-feedback.db'),
    ORCH_CAPABILITY_HEARTBEATS: '0',
  };
  function run(args, failure) {
    const output = path.join(root, 'stdout');
    const errors = path.join(root, 'stderr');
    const stdout = fs.openSync(output, 'w');
    const stderr = fs.openSync(errors, 'w');
    try {
      const result = spawnSync(python, args, {
        cwd: root, env, stdio: ['ignore', stdout, stderr], timeout: 10000,
      });
      assert.ifError(result.error);
      const errorText = fs.readFileSync(errors, 'utf8');
      assert.equal(result.status, failure ? 1 : 0, errorText);
      if (failure) {
        assert.match(errorText, failure);
        return;
      }
      return JSON.parse(fs.readFileSync(output, 'utf8'));
    } finally {
      fs.closeSync(stdout);
      fs.closeSync(stderr);
      fs.rmSync(output);
      fs.rmSync(errors);
    }
  }
  const cli = path.join(modules, 'model_profile_trial.py');
  const manifestPath = path.join(root, 'manifest.json');
  const packet = { instruction: 'Review the synthetic frozen recurring-work specification.' };
  const packetPath = path.join(root, 'packet.json');
  fs.writeFileSync(packetPath, JSON.stringify(packet));
  const manifest = run([
    cli, 'prepare', '--orchestrator-root', path.dirname(sourceFiles[0]),
    '--workflows-root', path.dirname(sourceFiles[1]), '--output', manifestPath,
    '--packet', packetPath, '--capacity-state', 'ok', '--seed', '14',
    ...profiles.flatMap((profile) => ['--profile-id', profile]), '--instances-per-profile', '3',
  ]);
  assert.deepEqual(JSON.parse(fs.readFileSync(manifestPath, 'utf8')), manifest);
  assert.deepEqual(manifest.frozen_packet, packet);
  assert.equal(manifest.requests.length, 6);
  assert.equal(new Set(manifest.requests.map((request) => request.run_id)).size, 6);
  for (const profile of profiles) {
    assert.equal(manifest.requests.filter((request) => request.profile_id === profile).length, 3);
  }
  const attempts = manifest.requests.map((request) => {
    const identity = {
      profile_id: request.profile_id,
      provider_resolved_provider: request.provider,
      provider_resolved_model: request.requested_model,
    };
    const artifact = path.join(root, `identity-${request.launch_ordinal}.json`);
    const bytes = JSON.stringify(identity);
    fs.writeFileSync(artifact, bytes);
    return {
      ...identity,
      run_id: request.run_id,
      operation_role: 'worker',
      requested_model: request.requested_model,
      selected_model: request.requested_model,
      reported_model: request.requested_model,
      runner_version: 'synthetic-cli-test',
      cli_version: 'synthetic-cli-test',
      status: 'success',
      packet_hash: manifest.packet_hash,
      acknowledged: true,
      tokens_in: request.launch_ordinal * 10,
      tokens_out: request.launch_ordinal,
      identity_evidence: {
        schema: 'orchestrator.model-identity-evidence',
        version: 1,
        authority: 'openai-response-metadata/v1',
        artifact_ref: artifact,
        artifact_sha256: `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`,
      },
    };
  });
  const results = {
    schema: 'orchestrator.model-profile-trial-results',
    version: 1,
    trial_id: manifest.trial_id,
    packet_hash: manifest.packet_hash,
    acknowledged: true,
    identity_verified: true,
    quality_by_profile: quality,
    attempts,
  };
  const statePath = path.join(env.ORCH_STATE_DIR, 'trial-state.json');
  const summaryPath = path.join(env.ORCH_STATE_DIR, 'capability-program', 'profile-trial.json');
  return {
    manifest, results, sourceFiles, statePath, summaryPath,
    finalize(failure) {
      const resultsPath = path.join(root, 'results.json');
      fs.writeFileSync(resultsPath, JSON.stringify(results));
      return run([
        cli, 'finalize', '--manifest', manifestPath, '--results', resultsPath,
        '--state', statePath, '--confirm-instrumentation', '--ingest',
      ], failure);
    },
    summary: () => JSON.parse(fs.readFileSync(summaryPath, 'utf8')),
    counts: () => run(['-c', `
import json
import feedback
with feedback._conn() as conn:
    print(json.dumps({table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("runs", "execution_attempts", "outcomes",
                                    "profile_trial_ingests", "route_weights", "route_weights_v2")}))
`]),
    reviewLine: () => run(['-c', `
import json
import switch_review
print(json.dumps(switch_review.profile_trial_summary_line()))
`]),
  };
}

test('six-instance CLI trial writes the program receipt and remains idempotent on replay', (t) => {
  const w = trialWorld(t);
  const state = w.finalize();
  assert.deepEqual(JSON.parse(fs.readFileSync(w.statePath, 'utf8')), state);
  assert.equal(state.brain_ingest_enabled, true);
  assert.equal(state.source_integrity.unchanged, true);
  assert.deepEqual(state.shared_pool_debit, { 'codex-subscription': 6 });
  const summary = w.summary();
  assert.equal(summary.trial_id, w.manifest.trial_id);
  assert.equal(summary.profile_count, 2);
  assert.equal(summary.instance_count, 6);
  assert.equal(summary.identity_verified, true);
  assert.equal(summary.brain_ingest_enabled, true);
  assert.deepEqual(summary.quality_by_profile, quality);
  assert.equal(summary.aggregate_tokens_in, 210);
  assert.equal(summary.aggregate_tokens_out, 21);
  for (const profile of profiles) {
    const attempts = w.results.attempts.filter((attempt) => attempt.profile_id === profile);
    assert.deepEqual(summary.cost_tokens_by_profile[profile], {
      tokens_in: attempts.reduce((total, attempt) => total + attempt.tokens_in, 0),
      tokens_out: attempts.reduce((total, attempt) => total + attempt.tokens_out, 0),
    });
    assert.deepEqual(summary.provider_resolved_identity_by_profile[profile], {
      provider: attempts[0].provider_resolved_provider,
      model: attempts[0].provider_resolved_model,
    });
  }
  assert.match(w.reviewLine(), /^profile trial: profiles 2, instances 6, identity verified Y, /);
  const counts = w.counts();
  assert.deepEqual(counts, {
    runs: 6, execution_attempts: 6, outcomes: 6, profile_trial_ingests: 1,
    route_weights: 0, route_weights_v2: 0,
  });
  assert.equal(w.finalize().brain_ingest_enabled, true);
  assert.deepEqual(w.counts(), counts);
  assert.deepEqual(w.summary(), summary);
  for (const file of w.sourceFiles) assert.equal(fs.readFileSync(file, 'utf8'), 'VALUE = 1\n');
});

for (const reason of [
  'unverified', 'provider-unavailable', 'artifact-changed', 'source-changed', 'missing-instance',
]) {
  test(`six-instance CLI refuses ${reason} evidence before recording any trial rows`, (t) => {
    const w = trialWorld(t);
    let message;
    if (reason === 'unverified') {
      w.results.identity_verified = false;
      message = /worker identity is unverified/;
    } else if (reason === 'provider-unavailable') {
      // Exact CLI-reported models alone cannot stand in for provider identity.
      w.results.attempts[5].provider_resolved_provider = null;
      w.results.attempts[5].provider_resolved_model = null;
      message = /missing exact provider-resolved provider/;
    } else if (reason === 'artifact-changed') {
      fs.writeFileSync(w.results.attempts[5].identity_evidence.artifact_ref, '{}');
      message = /identity evidence is not authoritative/;
    } else if (reason === 'source-changed') {
      fs.writeFileSync(w.sourceFiles[1], 'VALUE = 2\n');
      message = /source integrity changed/;
    } else {
      w.results.attempts.pop();
      message = /exactly one attempt per instance/;
    }
    w.finalize(message);
    assert.equal(fs.existsSync(w.statePath), false);
    assert.equal(fs.existsSync(w.summaryPath), false);
    assert.deepEqual(w.counts(), {
      runs: 0, execution_attempts: 0, outcomes: 0, profile_trial_ingests: 0,
      route_weights: 0, route_weights_v2: 0,
    });
  });
}
