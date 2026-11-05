'use strict';

// Validate retained #428 evidence without replay, dispatch, or outcome writes.
// Python is used only for its standard-library XML parser; pytest is not needed.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { gunzipSync } = require('node:zlib');

const receiptPath = 'docs/verification/issue-428-named-acceptance-20261007';
const evaluatedRevision = '6ed980924ec1d7d83a06f9b6a181dc76ba5eb910';
const manifestRevision = 'b9267a08939a53d6bbbff7d098dad5c5e0f04dc3';
const sourcePaths = ['src/adjudicator_retro.py', 'src/capability_advisor.py',
  'src/switch_review.py', 'tests/test_adjudicator_retro.py',
  'tests/test_adjudicator_collected_input.py'];
const controls = [
  { mutation: 'outcomes', source: sourcePaths[0],
    test: 'test_retro_records_verdicts_without_changing_outcomes', count: 1, failed: 1 },
  { mutation: 'unconditional-offer', source: sourcePaths[1],
    test: 'test_closer_lane_offers_the_adjudicator_only_on_a_contested_verdict', count: 5, failed: 3 },
];

function digest(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function execute(command, args, options = {}) {
  // File-backed IO also works in runners that prohibit child-process pipes.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-receipt-io-'));
  const { input, ...settings } = options;
  const fds = [];
  try {
    fs.writeFileSync(path.join(dir, 'stdin'), input || '');
    fds.push(fs.openSync(path.join(dir, 'stdin'), 'r'));
    fds.push(fs.openSync(path.join(dir, 'stdout'), 'w'));
    fds.push(fs.openSync(path.join(dir, 'stderr'), 'w'));
    const result = spawnSync(command, args, { timeout: 30000,
      env: { ...process.env, GIT_NO_REPLACE_OBJECTS: '1' }, ...settings, stdio: fds });
    assert.ifError(result.error);
    assert.equal(result.status, 0,
      `${command} failed: ${fs.readFileSync(path.join(dir, 'stderr'), 'utf8')}`);
    return fs.readFileSync(path.join(dir, 'stdout'));
  } finally {
    for (const fd of fds) fs.closeSync(fd);
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function blob(root, revision, file) {
  assert.match(revision, /^[a-f0-9]{40}$/);
  assert.ok(!file.startsWith('/') && !file.split('/').includes('..'));
  return execute('git', ['show', `${revision}:${file}`], { cwd: root });
}

function junit(xml) {
  return JSON.parse(execute(process.env.PYTHON || 'python3', ['-I', '-c', `
import json, sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.buffer.read())
print(json.dumps({"suites": [dict(s.attrib) for s in root.iter("testsuite")],
    "cases": [{"name": c.attrib["name"], "classname": c.attrib.get("classname"),
        "outcomes": [e.tag for e in c],
        "failure": "".join(c.find("failure").itertext()) if c.find("failure") is not None else ""}
        for c in root.iter("testcase")]}))
`], { input: xml }).toString('utf8'));
}

// Exported separately so adversarial receipt tests can check semantics after
// recomputing member hashes, without changing the immutable Git trust anchor.
function verifyPacket(validation, members, readBlob) {
  assert.equal(validation.evaluated_revision, evaluatedRevision, 'wrong evaluated revision');
  assert.deepEqual(Object.keys(validation.evaluated_blob_hashes).sort(), [...sourcePaths].sort());
  const sources = new Map(sourcePaths.map((file) => {
    const bytes = readBlob(evaluatedRevision, file);
    assert.equal(digest(bytes), validation.evaluated_blob_hashes[file], `evaluated source: ${file}`);
    return [file, bytes.toString('utf8')];
  }));
  const member = (name) => {
    assert.ok(members.has(name), `missing member: ${name}`);
    return members.get(name);
  };
  assert.deepEqual(JSON.parse(member('controls.json')), validation.required_mutations,
    'control declarations disagree');
  assert.equal(validation.required_mutations.length, controls.length);
  const results = [];
  for (const expected of controls) {
    const record = validation.required_mutations.find((r) => r.mutation === expected.mutation);
    assert.ok(record, `missing mutation: ${expected.mutation}`);
    assert.equal(record.source, expected.source);
    assert.equal(record.node, `tests/test_adjudicator_retro.py::${expected.test}`);
    const source = sources.get(record.source);
    assert.equal(typeof record.before, 'string');
    assert.ok(record.before.length > 0);
    assert.equal(source.split(record.before).length - 1, 1, 'mutation anchor must occur once');
    assert.equal(record.original_sha256, digest(source), 'original source digest');
    assert.equal(record.mutated_sha256, digest(source.replace(record.before, record.after)),
      'mutation source digest');
    assert.notEqual(record.mutated_sha256, record.original_sha256, 'mutation is a no-op');
    assert.equal(record.restored_sha256, record.original_sha256, 'restoration is not byte-identical');
    assert.deepEqual(record.phases.map((p) => p.phase), ['red', 'green']);
    for (const phase of record.phases) {
      const red = phase.phase === 'red';
      const failures = red ? expected.failed : 0;
      assert.equal(phase.exit, red ? 1 : 0, 'phase exit');
      assert.equal(phase.testcases, expected.count);
      assert.equal(phase.failed, failures);
      assert.equal(phase.errors, 0);
      assert.equal(phase.skipped, 0);
      assert.deepEqual(phase.argv.slice(1, 4), ['-m', 'pytest', record.node], 'wrong named command');
      const prefix = `${record.mutation}-${phase.phase}`;
      assert.equal(path.posix.basename(phase.argv.find((a) => a.startsWith('--junitxml=')).slice(11)),
        `${prefix}.xml`, 'wrong JUnit command target');
      const xml = junit(member(`${prefix}.xml`));
      assert.equal(xml.suites.length, 1);
      for (const [key, count] of Object.entries({ tests: expected.count,
        failures, errors: 0, skipped: 0 })) {
        assert.equal(xml.suites[0][key], String(count), `JUnit suite ${key}`);
      }
      const names = expected.count === 1 ? [expected.test] : Array.from({ length: 5 },
        (_, i) => `${expected.test}[context${i}-${i === 4 ? 'True' : 'False'}]`);
      assert.deepEqual(xml.cases.map((c) => c.name).sort(), names.sort(), 'wrong JUnit cases');
      for (const c of xml.cases) {
        assert.equal(c.classname, 'tests.test_adjudicator_retro');
        const failed = red && (expected.count === 1 || /\[context[123]-False\]$/.test(c.name));
        assert.deepEqual(c.outcomes, failed ? ['failure'] : [], 'skipped/error/wrong outcome');
        if (failed) {
          assert.match(c.failure, /AssertionError/, 'not an assertion failure');
          assert.ok(c.failure.includes(expected.count === 1 ? 'outcomes() == before' :
            'assert ("role-adjudicator" in ids) is offered'), 'wrong failure assertion');
        }
      }
      const consoleText = member(`${prefix}.txt`).toString('utf8');
      assert.ok(consoleText.includes(red ? `${expected.failed} failed` : `${expected.count} passed`));
      if (!red) assert.doesNotMatch(consoleText, /\b(?:failed|skipped|errors?)\b/i);
    }
    results.push({ mutation: record.mutation, red_failures: expected.failed,
      restored_passes: expected.count, byte_identical_restoration: true });
  }
  assert.deepEqual(validation.focused_tests.argv.slice(1), ['-m', 'pytest',
    'tests/test_adjudicator_retro.py', 'tests/test_adjudicator_collected_input.py', '-q', '-o', 'addopts=']);
  assert.equal(validation.focused_tests.actual_exit, 0);
  assert.equal(validation.focused_tests.passed, 39);
  const focused = member('509-current-main-acceptance.txt').toString('utf8');
  assert.match(focused, /\b39 passed\b/);
  assert.doesNotMatch(focused, /\b(?:failed|skipped|errors?)\b/i);
  assert.ok(sources.get('tests/test_adjudicator_retro.py').includes(
    'def test_packet_builder_needs_target_finding_and_ground_truth():'));
  return { evaluated_revision: evaluatedRevision, historical_receipts_valid: true,
    focused_suite_passes: 39, mutations: results, fresh_current_source_run: false,
    operational_acceptance_complete: false };
}

function verifyProof(root = path.resolve(__dirname, '..')) {
  const readBlob = (revision, file) => blob(root, revision, file);
  const manifestBytes = fs.readFileSync(path.join(root, receiptPath, 'manifest.json'));
  assert.deepEqual(manifestBytes, readBlob(manifestRevision, `${receiptPath}/manifest.json`),
    'manifest differs from merged #511');
  const manifest = JSON.parse(manifestBytes);
  assert.equal(manifest.schema, 'revision-bound-evidence/v1');
  assert.deepEqual(manifest.entries.map((e) => e.path).sort(),
    ['README.md', 'validation.json', 'phase-artifacts.json.gz'].map((f) => `${receiptPath}/${f}`).sort());
  const files = new Map();
  for (const entry of manifest.entries) {
    const bytes = readBlob(entry.revision, entry.path);
    assert.equal(bytes.length, entry.bytes, `manifest byte count: ${entry.path}`);
    assert.equal(digest(bytes), entry.sha256, `manifest digest: ${entry.path}`);
    // README intentionally gained an explanation after the bound revision.
    if (!entry.path.endsWith('/README.md')) {
      assert.deepEqual(fs.readFileSync(path.join(root, entry.path)), bytes,
        `retained bytes differ from revision: ${entry.path}`);
    }
    files.set(path.posix.basename(entry.path), bytes);
  }
  const packed = JSON.parse(gunzipSync(files.get('phase-artifacts.json.gz')));
  const members = new Map();
  for (const [name, item] of Object.entries(packed)) {
    const bytes = Buffer.from(item.base64, 'base64');
    assert.equal(digest(bytes), item.sha256, `member digest: ${name}`);
    members.set(name, bytes);
  }
  const validation = JSON.parse(files.get('validation.json'));
  const result = verifyPacket(validation, members, readBlob);
  result.changed_since_evaluation = sourcePaths.filter((file) =>
    digest(fs.readFileSync(path.join(root, file))) !== validation.evaluated_blob_hashes[file]);
  return result;
}

module.exports = { verifyProof, verifyPacket };
if (require.main === module) {
  try {
    process.stdout.write(`${JSON.stringify(verifyProof(), null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
