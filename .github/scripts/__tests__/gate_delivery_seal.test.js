'use strict';

// #1262 linked a summary failure caused by an unsealed generated delivery.
// Exercise the consumer-owned renderer through its real environment and output
// files, so source text printed elsewhere in the log is not mistaken for an error.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const renderer = path.resolve(__dirname, '..', 'gate_summary.py');
const head = '7ba7006dc3243a7bf0221d709e4de5562c89f042';
const repository = 'stranske/Orchestrator';

function summarize(t, record, extraEnv = {}, prepareArtifacts = () => {}, syncState = null) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-delivery-1262-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const eventPath = path.join(root, 'event.json');
  const outputPath = path.join(root, 'outputs');
  const summaryPath = path.join(root, 'summary.md');
  const artifactsRoot = path.join(root, 'artifacts');
  prepareArtifacts(artifactsRoot);
  const syncMetadata = syncState === null ? ''
    : `<!-- workflows-consumer-sync:v1 ${JSON.stringify({
      schema: 'workflows-consumer-sync-pr/v1',
      consumer_repo: repository,
      lifecycle_state: syncState,
    })} -->\n`;
  fs.writeFileSync(eventPath, JSON.stringify({
    pull_request: {
      head: { ref: 'sync/workflows-delivery', sha: head, repo: { full_name: repository } },
      base: { repo: { full_name: repository } },
      body: `${syncMetadata}<!-- sync-pr-delivery-record:v1 ${JSON.stringify(record)} -->`,
    },
  }));
  // Fix every renderer input explicitly; the outer CI job may supply its own.
  const result = spawnSync(process.env.PYTHON || 'python3', [renderer], {
    cwd: root,
    stdio: 'inherit',
    timeout: 30000,
    env: {
      ...process.env,
      GITHUB_EVENT_PATH: eventPath,
      GITHUB_OUTPUT: outputPath,
      GITHUB_STEP_SUMMARY: summaryPath,
      GATE_ARTIFACTS_ROOT: artifactsRoot,
      DOC_ONLY: 'true',
      RUN_CORE: 'false',
      REASON: 'docs_only',
      PYTHON_REQUIRED: 'false',
      PYTHON_RESULT: 'skipped',
      DOCS_GUARD_RESULT: 'success',
      DOCKER_RESULT: 'skipped',
      DOCKER_CHANGED: 'false',
      TEST_QUALITY_RESULT: 'skipped',
      ORCHESTRATOR_NODE_TESTS_RESULT: 'skipped',
      COMBINED_COVERAGE_STATUS: 'not-requested',
      COMBINED_COVERAGE_PERCENT: '',
      DELIVERY_SEAL_RESULT: 'success',
      ...extraEnv,
    },
  });
  assert.ifError(result.error);
  // Rendering succeeds; the workflow enforces the emitted state in a later step.
  assert.equal(result.status, 0, 'summary renderer failed');
  return {
    outputs: fs.readFileSync(outputPath, 'utf8'),
    summary: fs.readFileSync(summaryPath, 'utf8'),
  };
}

function deliveryRecord(overrides = {}) {
  return {
    schema: 'sync-pr-delivery-record/v1',
    repository,
    delivery_state: 'sealed',
    sealed_head_sha: head,
    lease_expires_at: new Date(Date.now() + 86400000).toISOString(),
    ...overrides,
  };
}

test('staging delivery reproduces the linked summary failure before any fast pass', (t) => {
  const result = summarize(t, deliveryRecord({ delivery_state: 'staging', sealed_head_sha: '' }));
  assert.match(result.outputs, /^state=failure$/m);
  assert.match(result.outputs,
    /^description=Generated delivery is not sealed: delivery state is staging\.$/m);
  assert.match(result.summary, /Maint 71 must complete bounded review settlement/);
  assert.doesNotMatch(result.outputs, /identity differs|rate.limit/i);
});

test('valid exact-head seal clears the delivery hold', (t) => {
  const result = summarize(t, deliveryRecord());
  assert.match(result.outputs, /^state=success$/m);
  assert.doesNotMatch(result.summary, /Generated delivery hold/);
});

test('delivery record controls the summary when sync metadata has a different lifecycle state', (t) => {
  // Regenerated PR #1247 retained staging sync metadata after its delivery record
  // was sealed. Check that envelope with active checks, plus the inverse case:
  // sync metadata claiming sealed must never clear a staging delivery hold.
  const activeChecks = {
    DOC_ONLY: 'false',
    RUN_CORE: 'true',
    REASON: '',
    ORCHESTRATOR_NODE_TESTS_RESULT: 'success',
  };
  for (const [syncState, deliveryState, expectedState] of [
    ['staging', 'sealed', 'success'],
    ['sealed', 'staging', 'failure'],
  ]) {
    const result = summarize(t, deliveryRecord({ delivery_state: deliveryState }),
      activeChecks, undefined, syncState);
    assert.match(result.outputs, new RegExp(`^state=${expectedState}$`, 'm'));
    if (expectedState === 'success') {
      assert.match(result.outputs, /^description=All Gate checks succeeded\.$/m);
      assert.doesNotMatch(result.summary, /Generated delivery hold|fast-pass/);
    } else {
      assert.match(result.outputs,
        /^description=Generated delivery is not sealed: delivery state is staging\.$/m);
      assert.match(result.summary, /Generated delivery hold/);
    }
  }
});

test('seal for a different head preserves the delivery hold', (t) => {
  const result = summarize(t, deliveryRecord({ sealed_head_sha: 'a'.repeat(40) }));
  assert.match(result.outputs, /^state=failure$/m);
  assert.match(result.outputs, /sealed head does not match the PR head/);
});

test('failed seal job overrides a valid event seal', (t) => {
  const result = summarize(t, deliveryRecord(), { DELIVERY_SEAL_RESULT: 'failure' });
  assert.match(result.outputs, /^state=failure$/m);
  assert.match(result.summary, /Generated delivery hold/);
});

test('staging hold takes precedence over skipped Python jobs in the linked run', (t) => {
  const result = summarize(t,
    deliveryRecord({ delivery_state: 'staging', sealed_head_sha: '' }), {
      DOC_ONLY: 'false',
      RUN_CORE: 'true',
      REASON: '',
      PYTHON_REQUIRED: 'true',
      PYTHON_RESULT: 'skipped',
      DOCS_GUARD_RESULT: 'skipped',
      DELIVERY_SEAL_RESULT: 'failure',
    });
  assert.match(result.outputs, /^state=failure$/m);
  assert.match(result.outputs,
    /^description=Generated delivery is not sealed: delivery state is staging\.$/m);
  assert.doesNotMatch(result.outputs, /waiting for rerun|Python CI artifacts missing/);
});

test('sealing a delivery still requires Python shard receipts for active checks', (t) => {
  const result = summarize(t, deliveryRecord(), {
    DOC_ONLY: 'false',
    RUN_CORE: 'true',
    REASON: '',
    PYTHON_REQUIRED: 'true',
    PYTHON_RESULT: 'success',
  });
  assert.match(result.outputs, /^state=failure$/m);
  assert.match(result.outputs, /^description=Python CI artifacts missing:/m);
  assert.match(result.summary, /Python shard artifacts: .*missing/);
  assert.doesNotMatch(result.summary, /Generated delivery hold/);
});

test('sealed delivery preserves the Node job verdict for changes without Python', (t) => {
  for (const [jobResult, state, description] of [
    ['success', 'success', 'All Gate checks succeeded.'],
    ['failure', 'failure', 'Orchestrator node tests result: failure.'],
    ['cancelled', 'pending', 'Orchestrator node tests cancelled; waiting for rerun.'],
  ]) {
    const result = summarize(t, deliveryRecord(), {
      DOC_ONLY: 'false',
      RUN_CORE: 'true',
      REASON: '',
      ORCHESTRATOR_NODE_TESTS_RESULT: jobResult,
    });
    assert.match(result.outputs, new RegExp(`^state=${state}$`, 'm'), jobResult);
    assert.ok(result.outputs.split('\n').includes(`description=${description}`), jobResult);
    assert.doesNotMatch(result.summary, /Generated delivery hold/);
  }
});

test('exact-head sealing clears the staging hold with all required Python receipts', (t) => {
  const activeChecks = {
    DOC_ONLY: 'false',
    RUN_CORE: 'true',
    REASON: '',
    PYTHON_REQUIRED: 'true',
    PYTHON_RESULT: 'success',
    TEST_QUALITY_RESULT: 'success',
    ORCHESTRATOR_NODE_TESTS_RESULT: 'success',
  };
  const prepareArtifacts = (artifactsRoot) => {
    for (const runtime of ['3.13', '3.14']) {
      for (let shard = 1; shard <= 6; shard++) {
        const receiptDir = path.join(artifactsRoot, 'downloads',
          `gate-shard-${shard}-coverage-${runtime}-1`);
        fs.mkdirSync(receiptDir, { recursive: true });
        fs.writeFileSync(path.join(receiptDir, 'summary.json'), JSON.stringify({
          python_version: runtime,
          checks: {
            lint: { outcome: 'success' },
            type_check: { outcome: 'success' },
            tests: { outcome: 'success' },
            coverage_minimum: { outcome: 'success' },
          },
        }));
      }
    }
  };
  const held = summarize(t,
    deliveryRecord({ delivery_state: 'staging', sealed_head_sha: '' }),
    activeChecks, prepareArtifacts);
  assert.match(held.outputs, /^state=failure$/m);
  assert.match(held.outputs,
    /^description=Generated delivery is not sealed: delivery state is staging\.$/m);

  const sealed = summarize(t, deliveryRecord(), activeChecks, prepareArtifacts);
  assert.match(sealed.outputs, /^state=success$/m);
  assert.match(sealed.outputs, /^description=All Gate checks succeeded\.$/m);
  assert.match(sealed.summary, /all 6 shards reported for Python 3\.13 and 3\.14/);
  assert.equal((sealed.summary.match(/\| 3\.(?:13|14) shard \d\/6 \|/g) || []).length, 12);
  assert.doesNotMatch(sealed.summary, /Generated delivery hold|fast-pass|artifacts: .*missing/);
});
