'use strict';

// Execute the exact #428 safety contract. Mutations live only in a private
// import overlay; the repository and operational Brain are never mutated.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { parseArgs } = require('node:util');

const testFile = 'tests/test_adjudicator_retro.py';
const names = [
  'test_packet_builder_needs_target_finding_and_ground_truth',
  'test_retro_records_verdicts_without_changing_outcomes',
  'test_closer_lane_offers_the_adjudicator_only_on_a_contested_verdict',
];
const controls = [
  { name: 'outcomes', file: 'src/adjudicator_retro.py', index: 1,
    before: '    population = disputes(db=db)\n',
    after: '    with sqlite3.connect(db) as corruption:\n' +
      '        corruption.execute("UPDATE outcomes SET merged=0")\n' +
      '    population = disputes(db=db)\n',
    failures: [names[1]], assertion: 'outcomes() == before' },
  { name: 'unconditional-offer', file: 'src/capability_advisor.py', index: 2,
    before: '        and entry.get("pr_requirement_met") is not True\n',
    after: '        and False\n',
    failures: [1, 2, 3].map((n) => `${names[2]}[context${n}-False]`),
    assertion: 'assert ("role-adjudicator" in ids) is offered' },
];
const inputs = ['src', 'config', testFile, 'tests/conftest.py', 'pyproject.toml'];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

function execute(command, args, options) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-command-'));
  const stdout = fs.openSync(path.join(temporary, 'stdout'), 'w');
  const stderr = fs.openSync(path.join(temporary, 'stderr'), 'w');
  try {
    const result = spawnSync(command, args, { timeout: 60000, ...options,
      stdio: ['ignore', stdout, stderr] });
    return { status: result.status, signal: result.signal,
      error: result.error ? result.error.message : null,
      stdout: fs.readFileSync(path.join(temporary, 'stdout'), 'utf8'),
      stderr: fs.readFileSync(path.join(temporary, 'stderr'), 'utf8') };
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

function git(root, args) {
  const result = execute('git', args, { cwd: root,
    env: { PATH: process.env.PATH, GIT_NO_REPLACE_OBJECTS: '1' } });
  assert.equal(result.status, 0, `git ${args[0]} failed: ${result.stderr}`);
  return result.stdout;
}

function sourceState(root, revision) {
  assert.equal(git(root, ['status', '--porcelain', '--untracked-files=all', '--', ...inputs]),
    '', 'acceptance inputs must be clean, including untracked source files');
  git(root, ['diff', '--exit-code', revision, '--', ...inputs]);
  const files = git(root, ['ls-files', '-z', '--', ...inputs]).split('\0').filter(Boolean).sort();
  const hashes = Object.fromEntries(files.map((file) =>
    [file, digest(fs.readFileSync(path.join(root, file)))]));
  return { sha256: digest(JSON.stringify(hashes)), files: hashes };
}

const parseXml = `
import json, sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
print(json.dumps({"suites": [dict(s.attrib) for s in root.iter("testsuite")],
    "cases": [{"name": c.attrib.get("name"), "classname": c.attrib.get("classname"),
        "outcomes": [e.tag for e in c if e.tag in ("failure", "error", "skipped")],
        "failure": "".join(c.find("failure").itertext()) if c.find("failure") is not None else ""}
        for c in root.iter("testcase")]}))
`;

function validatePhase(phase, xml, index, control) {
  const expectedNames = index === 2
    ? [0, 1, 2, 3, 4].map((n) => `${names[2]}[context${n}-${n === 4 ? 'True' : 'False'}]`)
    : [names[index]];
  const failures = control ? control.failures : [];
  assert.equal(phase.error, null, 'test process failed to execute');
  assert.equal(phase.signal, null, 'test process was terminated');
  assert.equal(phase.status, failures.length ? 1 : 0, 'wrong pytest exit');
  assert.equal(xml.suites.length, 1, 'expected one native JUnit suite');
  for (const [key, count] of Object.entries({ tests: expectedNames.length,
    failures: failures.length, errors: 0, skipped: 0 })) {
    assert.equal(xml.suites[0][key], String(count), `JUnit ${key}`);
  }
  assert.deepEqual(xml.cases.map((c) => c.name).sort(), expectedNames.sort(), 'wrong named cases');
  for (const entry of xml.cases) {
    assert.equal(entry.classname, 'tests.test_adjudicator_retro', 'wrong test module');
    const failed = failures.includes(entry.name);
    assert.deepEqual(entry.outcomes, failed ? ['failure'] : [], 'error, skip or wrong failure');
    if (failed) {
      assert.match(entry.failure, /AssertionError/, 'not an assertion failure');
      assert.ok(entry.failure.includes(control.assertion), 'wrong mutation failure assertion');
    }
  }
}

// executeTest is a test seam; CLI always uses the real subprocess executor.
function runAcceptance({ root = path.resolve(__dirname, '..'), python, output,
  executeTest = execute }) {
  assert.ok(python && path.isAbsolute(python), '--python must select an absolute interpreter path');
  // Invoke the selected spelling: resolving a virtualenv's interpreter symlink
  // would silently run its base interpreter without the selected environment.
  const interpreter = python;
  const resolvedInterpreter = fs.realpathSync(python);
  const revision = git(root, ['rev-parse', 'HEAD']).trim();
  const original = sourceState(root, revision);
  const originals = new Map(controls.map((c) => [c.file, fs.readFileSync(path.join(root, c.file))]));
  fs.mkdirSync(output); // Never overwrite an earlier receipt or its native logs.
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'adjudicator-acceptance-'));
  const overlay = path.join(temporary, 'overlay');
  fs.mkdirSync(overlay);
  for (const [file, bytes] of originals) fs.writeFileSync(path.join(overlay, path.basename(file)), bytes);
  const receipt = { schema_version: 1, recorded_at: new Date().toISOString(),
    runner_sha256: digest(fs.readFileSync(__filename)),
    contract_source: 'https://github.com/stranske/Orchestrator/issues/428',
    mutation_source: 'docs/verification/issue-428-named-acceptance-20261007/validation.json',
    evaluated_revision: revision, interpreter: { requested: python, resolved: resolvedInterpreter },
    input_sha256: original.sha256, input_files: original.files,
    named_nodes: names.map((name) => `${testFile}::${name}`),
    phases: [], mutations: [], status: 'blocked', evidence_gaps: [],
    named_safety_acceptance_complete: false, operational_acceptance_complete: false,
    scope: 'named safety tests only; private fixture state, no real cohort or closer observation' };
  const env = { PATH: process.env.PATH,
    PYTHONPATH: [overlay, path.join(root, 'src'), root].join(path.delimiter),
    PYTHONDONTWRITEBYTECODE: '1', ORCH_STATE_DIR: path.join(temporary, 'state'),
    ORCH_LOCAL_RUNTIME: path.join(temporary, 'runtime'),
    ORCH_FEEDBACK_DB: path.join(temporary, 'brain.db'),
    ORCH_CAPABILITIES_PATH: path.join(temporary, 'capabilities.json') };
  function command(label, args) {
    const result = executeTest(interpreter, args, { cwd: root, env });
    for (const stream of ['stdout', 'stderr']) {
      fs.writeFileSync(path.join(output, `${label}.${stream}.log`), result[stream]);
    }
    const phase = { label, argv: [interpreter, ...args], cwd: root,
      status: result.status, signal: result.signal, error: result.error,
      stdout: `${label}.stdout.log`, stdout_sha256: digest(result.stdout),
      stderr: `${label}.stderr.log`, stderr_sha256: digest(result.stderr) };
    receipt.phases.push(phase);
    return { phase, result };
  }
  function testPhase(label, index, control) {
    const xmlFile = path.join(output, `${label}.xml`);
    const { phase } = command(label, ['-m', 'pytest', receipt.named_nodes[index],
      '-q', '-m', 'not slow', '-o', 'addopts=', '-o', 'pythonpath=',
      '-o', `cache_dir=${path.join(temporary, 'pytest-cache')}`, `--junitxml=${xmlFile}`]);
    // Missing XML, collection errors and skipped tests cannot be mutation witnesses.
    phase.junit_sha256 = digest(fs.readFileSync(xmlFile));
    const parsed = execute(interpreter, ['-I', '-c', parseXml, xmlFile], { cwd: root, env });
    assert.equal(parsed.status, 0, `invalid native JUnit: ${parsed.stderr}`);
    validatePhase(phase, JSON.parse(parsed.stdout), index, control);
    phase.verified = true;
  }
  try {
    const probe = command('interpreter', ['-I', '-c',
      'import json, sys; print(json.dumps({"executable": sys.executable, "version": sys.version}))']);
    assert.equal(probe.phase.status, 0, 'selected interpreter cannot run');
    receipt.interpreter.observed = JSON.parse(probe.result.stdout);
    const prerequisite = command('pytest-prerequisite', ['-c', 'import pytest; print(pytest.__version__)']);
    if (prerequisite.phase.status !== 0 || prerequisite.phase.error || prerequisite.phase.signal) {
      receipt.evidence_gaps.push('selected_interpreter_pytest_unavailable');
      return receipt;
    }
    receipt.status = 'failed';
    const imports = command('import-origins', ['-c',
      'import json, adjudicator_retro, capability_advisor; ' +
      'print(json.dumps([adjudicator_retro.__file__, capability_advisor.__file__]))']);
    assert.equal(imports.phase.status, 0, 'acceptance modules cannot import');
    assert.deepEqual(JSON.parse(imports.result.stdout).map((file) => fs.realpathSync(file)),
      controls.map((control) => path.join(overlay, path.basename(control.file))),
      'mutations must load from the private overlay');
    for (let index = 0; index < names.length; index++) testPhase(`baseline-${index + 1}`, index);
    for (const control of controls) {
      const bytes = originals.get(control.file);
      const source = bytes.toString('utf8');
      assert.equal(source.split(control.before).length - 1, 1, 'mutation anchor must occur once');
      const changed = Buffer.from(source.replace(control.before, control.after));
      assert.notDeepEqual(changed, bytes, 'mutation must change bytes');
      const file = path.join(overlay, path.basename(control.file));
      const record = { name: control.name, source: control.file, node: receipt.named_nodes[control.index],
        before: control.before, after: control.after,
        original_sha256: digest(bytes), mutated_sha256: digest(changed) };
      receipt.mutations.push(record);
      let redError;
      try {
        fs.writeFileSync(file, changed);
        testPhase(`${control.name}-red`, control.index, control);
      } catch (error) {
        redError = error;
      } finally {
        fs.writeFileSync(file, bytes);
        assert.deepEqual(fs.readFileSync(file), bytes, 'restoration must be byte-identical');
        record.restored_sha256 = digest(fs.readFileSync(file));
      }
      testPhase(`${control.name}-restored`, control.index);
      if (redError) throw redError;
    }
    for (let index = 0; index < names.length; index++) testPhase(`final-${index + 1}`, index);
    receipt.status = 'passed';
    receipt.named_safety_acceptance_complete = true;
  } catch (error) {
    receipt.status = 'failed';
    receipt.evidence_gaps.push(error.message);
  } finally {
    try {
      assert.deepEqual(sourceState(root, revision), original, 'repository acceptance inputs changed');
      receipt.repository_inputs_unchanged = true;
    } catch (error) {
      receipt.status = 'failed';
      receipt.named_safety_acceptance_complete = false;
      receipt.evidence_gaps.push(error.message);
      receipt.repository_inputs_unchanged = false;
    }
    fs.rmSync(temporary, { recursive: true, force: true });
    fs.writeFileSync(path.join(output, 'receipt.json'), `${JSON.stringify(receipt, null, 2)}\n`);
  }
  return receipt;
}

function main() {
  try {
    const { values } = parseArgs({ options: {
      python: { type: 'string' }, output: { type: 'string' },
    } });
    assert.ok(values.output, '--output must be a new receipt directory');
    const receipt = runAcceptance({ python: values.python, output: path.resolve(values.output) });
    process.stdout.write(`${JSON.stringify({ status: receipt.status,
      named_safety_acceptance_complete: receipt.named_safety_acceptance_complete,
      operational_acceptance_complete: false, evidence_gaps: receipt.evidence_gaps })}\n`);
    process.exitCode = { passed: 0, failed: 1, blocked: 2 }[receipt.status];
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { runAcceptance, validatePhase, execute, names, controls };
if (require.main === module) main();
