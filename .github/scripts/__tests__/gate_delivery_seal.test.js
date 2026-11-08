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

function summarize(t, record, extraEnv = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-delivery-1262-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const eventPath = path.join(root, 'event.json');
  const outputPath = path.join(root, 'outputs');
  const summaryPath = path.join(root, 'summary.md');
  fs.writeFileSync(eventPath, JSON.stringify({
    pull_request: {
      head: { ref: 'sync/workflows-delivery', sha: head, repo: { full_name: repository } },
      base: { repo: { full_name: repository } },
      body: `<!-- sync-pr-delivery-record:v1 ${JSON.stringify(record)} -->`,
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
      GATE_ARTIFACTS_ROOT: path.join(root, 'artifacts'),
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
