'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const fixtures = path.join(__dirname, 'fixtures', 'redirect_stalls');

test('all twelve historical stalls explicitly map their flattened policy facts', () => {
  const names = fs.readdirSync(fixtures).filter((name) => name.endsWith('.json')).sort();
  assert.deepEqual(names, Array.from({ length: 12 }, (_, index) =>
    `historical-${String(index + 1).padStart(2, '0')}.json`));

  for (const name of names) {
    const report = JSON.parse(fs.readFileSync(path.join(fixtures, name), 'utf8'));
    assert.equal(report.state, 'stalled', `${name}: replay input must be a real stall`);
    assert.equal(report.lane, 'experiment', `${name}: retain the historical lane`);
    assert.equal(typeof report.policy_action, 'string', `${name}: retain the source action`);
    assert.ok(report.policy_action, `${name}: source action must not be empty`);
    // Replay defaults a missing policy_decision.action to redirect. A passing
    // authorization count alone therefore cannot prove the fixture was mapped.
    assert.equal(report.policy_decision?.action, report.policy_action,
      `${name}: map policy_action in the fixture before replay`);
    assert.equal(typeof report.policy_confidence, 'string',
      `${name}: retain the source confidence`);
    assert.ok(report.policy_confidence, `${name}: source confidence must not be empty`);
    assert.equal(report.policy_decision?.confidence, report.policy_confidence,
      `${name}: map policy_confidence in the fixture before replay`);
  }
});
