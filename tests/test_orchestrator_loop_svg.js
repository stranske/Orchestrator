'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

// A snapshot override lets the same assertions check a historical diagram without
// changing checkout bytes. XML parsing is verified separately with ElementTree.
const diagram = process.env.ORCH_TEST_LOOP_SVG || path.resolve(__dirname, '../orchestrator-loop.svg');
const source = fs.readFileSync(diagram, 'utf8');

test('loop SVG contains no unresolved conflict markers', () => {
  assert.doesNotMatch(source, /^(?:<<<<<<<|=======|>>>>>>>)/m);
});

test('loop SVG retains one batch and value-chain description', () => {
  const descriptions = [...source.matchAll(/<desc>([\s\S]*?)<\/desc>/g)];
  assert.equal(descriptions.length, 1);
  const description = descriptions[0][1];
  assert.equal(description.split('RedirectAgent, PromptAgent').length - 1, 1,
    'the role description must not repeat the conflict block');
  for (const required of [
    'research-program and repo-audit:phase-4 batch authoring',
    'one backend per batch with per-item role-run evidence',
    'read-only value-chain rail comparing independent situation counts with production invocations',
    'graded influence outcomes, naming the first broken step and input-off switches without changing gates',
    'system-of-record is the Workflows repo',
  ]) {
    assert.ok(description.includes(required), `missing diagram description: ${required}`);
  }
});
