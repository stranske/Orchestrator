'use strict';

// Check retained native evidence against this candidate without launching an agent or
// rerunning sandbox-exec. Integrity checks are not a new native acceptance receipt.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const receiptPath = 'docs/verification/issue-532-seatbelt-regex/native-recovery-20261011';
const sourcePath = 'src/adapters.py';
const testPath = 'tests/test_seatbelt_regex_quoting.py';
const expectedFailures = {
  test_backslash: 'AssertionError: write allowed:',
  test_combined: 'sandbox-exec: Error reading string',
  test_quote: 'sandbox-exec: Error reading string',
  test_regex_metacharacters: 'sandbox-exec: unexpected ^ operator',
};

function digest(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function run(command, args, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { timeout: 30000 });
    const output = [];
    const errors = [];
    child.stdout.on('data', (chunk) => output.push(chunk));
    child.stderr.on('data', (chunk) => errors.push(chunk));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) reject(new Error(`${command} failed: ${Buffer.concat(errors)}`));
      else resolve(Buffer.concat(output));
    });
    child.stdin.on('error', reject);
    child.stdin.end(input);
  });
}

async function cases(xml) {
  // Use the standard XML parser rather than interpreting XML with a regex.
  const output = await run(process.env.PYTHON || 'python3', ['-I', '-c', `
import json, sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.buffer.read())
print(json.dumps([
    {"name": c.attrib["name"], "classname": c.attrib.get("classname"),
     "outcomes": [e.tag for e in c],
     "failure": "".join(c.find("failure").itertext()) if c.find("failure") is not None else ""}
    for c in root.iter("testcase")
]))
`], xml);
  return JSON.parse(output.toString('utf8'));
}

async function verifyProof(root = path.resolve(__dirname, '..')) {
  const receiptDir = path.join(root, receiptPath);
  const hashes = JSON.parse(fs.readFileSync(path.join(receiptDir, 'sha256.json'), 'utf8'));
  const archive = path.join(receiptDir, 'proof.tar.gz');
  assert.equal(digest(fs.readFileSync(archive)), hashes['proof.tar.gz'], 'archive digest mismatch');
  const listing = await run('tar', ['-tzf', archive]);
  const names = listing.toString('utf8').trim().split('\n');
  assert.equal(new Set(names).size, names.length, 'duplicate archive member');
  const members = new Map();
  for (const name of names) {
    assert.match(name, /^533-[a-z-]+\.(?:log|xml|json|py|md)$/, 'unexpected archive member');
    const bytes = await run('tar', ['-xOzf', archive, '--', name]);
    assert.equal(digest(bytes), hashes[name], `member digest mismatch: ${name}`);
    members.set(name, bytes);
  }
  const member = (name) => {
    assert.ok(members.has(name), `missing receipt: ${name}`);
    return members.get(name);
  };
  for (const file of [sourcePath, testPath]) {
    assert.equal(digest(fs.readFileSync(path.join(root, file))), hashes[file],
      `native evidence does not cover current ${file}`);
  }
  const results = JSON.parse(member('533-proof-results.json').toString('utf8'));
  assert.equal(results.original_restored_equal, true, 'byte-identical restoration not recorded');
  assert.equal(results.green.sha256, hashes[sourcePath], 'GREEN source digest mismatch');
  const source = fs.readFileSync(path.join(root, sourcePath), 'utf8');
  const anchor = '(regex {_seatbelt_quote(pattern)})';
  assert.equal(source.split(anchor).length - 1, 1, 'production reversal anchor must occur once');
  assert.equal(digest(source.replace(anchor, '(regex #{_seatbelt_quote(pattern)})')),
    results.red.sha256, 'RED did not use the recorded production reversal');
  assert.equal(results.red.exit, 1);
  assert.equal(results.red.cases, 4);
  assert.equal(results.red.failure, 4);
  assert.equal(results.green.exit, 0);
  assert.equal(results.green.cases, 4);
  assert.equal(results.green.pass, 4);
  assert.equal(results.green.native_probes, 5);
  for (const phase of ['red', 'green']) {
    const recorded = await cases(member(`533-proof-${phase}.xml`));
    assert.deepEqual(recorded.map((c) => c.name).sort(), Object.keys(expectedFailures).sort(),
      `${phase}: wrong test cases`);
    for (const c of recorded) {
      assert.equal(c.classname, 'tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests');
      assert.deepEqual(c.outcomes, phase === 'red' ? ['failure'] : [],
        `${phase}: skipped/error/nonpassing case ${c.name}`);
      if (phase === 'red') {
        assert.ok(c.failure.includes(expectedFailures[c.name]),
          `RED lacks the native failure for ${c.name}`);
      }
    }
  }
  const greenLog = member('533-proof-green.log').toString('utf8');
  assert.equal(greenLog.split('native Seatbelt probe: OK').length - 1, 5);
  assert.match(greenLog, /\b4 passed\b/);
  assert.doesNotMatch(greenLog, /UNKNOWN|\b\d+ (?:failed|skipped|errors?)\b/);
  return { source_sha256: results.green.sha256, red_failures: 4, green_passes: 4, native_probes: 5 };
}

module.exports = { verifyProof };
if (require.main === module) {
  verifyProof().then((result) => {
    console.log(JSON.stringify({ retained_native_evidence: 'VERIFIED', ...result }));
  }).catch((error) => {
    console.error(`retained native evidence: NOT VERIFIED: ${error.message}`);
    process.exitCode = 1;
  });
}
