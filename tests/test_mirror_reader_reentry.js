'use strict';

// Pause the production reader after it unlocks its selected generation, but
// before exec opens the shell script. Publication must not redirect that open.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const modules = fs.existsSync(path.join(repo, 'src')) ? path.join(repo, 'src') : repo;
const installer = path.join(repo, 'scripts/install_verified_snapshot.py');
const python = process.env.PYTHON || 'python3';
const parentPin = 'ORCH_REPO="$(cd "$(dirname "$ORCH_REPO")" && pwd -P)/$(basename "$ORCH_REPO")"\n';

function checkedPython(args) {
  const result = spawnSync(python, args, { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr || String(result.error));
  return result.stdout.trim();
}

const scenarios = [
  { unanchored: true, incumbent: true },
  { unanchored: true, incumbent: false },
  { unanchored: false, incumbent: false },
].flatMap((scenario) =>
  ['absolute-alias', 'relative-alias', 'self-locating-alias'].map((entry) => ({ ...scenario, entry })));

for (const { unanchored, incumbent, entry } of scenarios) {
  const mode = incumbent ? 'incumbent negative control' :
    unanchored ? 'repaired reader with unanchored entry' : 'production reentry';
  test(`${mode} through ${entry}`, { timeout: 15000 }, async (t) => {
    const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'orch-reentry-')));
    let reader;
    let done;
    let lines;
    t.after(async () => {
      if (reader && reader.exitCode === null && reader.signalCode === null) reader.kill('SIGKILL');
      if (done) await done;
      if (lines) lines.close();
      fs.rmSync(root, { recursive: true, force: true });
    });
    const snapshot = path.join(root, 'snapshot');
    const mirror = path.join(root, 'live mirror');
    const alias = path.join(root, 'parent alias');
    fs.symlinkSync(root, alias, 'dir');
    fs.mkdirSync(path.join(snapshot, 'scripts'), { recursive: true });
    fs.copyFileSync(installer, path.join(snapshot, 'scripts', path.basename(installer)));
    fs.copyFileSync(path.join(modules, 'paths.py'), path.join(snapshot, 'paths.py'));
    let helper = fs.readFileSync(path.join(modules, 'mirror_reader.py'), 'utf8');
    if (incumbent) {
      // Freeze the old absolute-only mapper as well as removing shell anchoring.
      // The repaired helper now protects the aliased shell open independently.
      const commandPin = '        command = [pin_argument(argument) for argument in command]';
      assert.ok(helper.includes(commandPin), 'reader command pin boundary moved');
      helper = helper.replace(commandPin, [
        '        def incumbent_argument(argument):',
        '            path = Path(argument)',
        '            if path.is_absolute():',
        '                path = Path(os.path.abspath(path))',
        '            if path.is_absolute() and path.is_relative_to(root):',
        '                return str(pinned / path.relative_to(root))',
        '            return argument',
        '        command = [incumbent_argument(argument) for argument in command]',
      ].join('\n'));
    }
    const exec = '        os.execvpe(command[0], command, env)';
    assert.ok(helper.includes(exec), 'reader exec boundary moved');
    fs.writeFileSync(path.join(snapshot, 'mirror_reader.py'), helper.replace(exec,
      '        print("SELECTED", flush=True)\n' +
      '        assert sys.stdin.readline() == "resume\\n"\n' + exec));
    let prologue = fs.readFileSync(path.join(repo, 'orchestrate.sh'), 'utf8')
      .split('# gh auth for the launchd/cron context:')[0];
    assert.ok(prologue.includes(parentPin), 'shell parent anchoring moved');
    // Keep the publisher, rendezvous, and observer identical across all modes.
    // The unanchored positive case isolates the helper's protection.
    if (unanchored) prologue = prologue.replace(parentPin, '');
    fs.writeFileSync(path.join(snapshot, 'observer.py'),
      'import json, subprocess, sys\nimport module, paths\n' +
      'child = subprocess.run([sys.executable, "-c", "import module; print(module.VALUE)"], ' +
      'capture_output=True, text=True, check=True)\n' +
      'print(json.dumps([module.VALUE, child.stdout.strip(), str(paths.REPO_ROOT)]))\n');

    function publish(value) {
      fs.writeFileSync(path.join(snapshot, 'module.py'), `VALUE = '${value}'\n`);
      fs.writeFileSync(path.join(snapshot, 'orchestrate.sh'), prologue +
        `echo 'SHELL:${value}'\npython3 "$ORCH/observer.py"\n`);
      const digest = checkedPython([installer, snapshot, '--digest']);
      checkedPython([installer, snapshot, mirror, '--expected-digest', digest]);
      assert.equal(checkedPython([installer, mirror, '--digest']), digest);
    }

    publish('old');
    const pinned = fs.realpathSync(mirror);
    const aliasedMirror = path.join(alias, path.basename(mirror));
    const env = { ...process.env, HOME: root, PYTHONPATH: aliasedMirror };
    for (const name of ['ORCH_PUBLICATION_READER_FD', 'ORCH_PUBLICATION_ROOT',
      'ORCH_PUBLICATION_GENERATION', 'ORCH_DIR']) delete env[name];
    if (entry !== 'self-locating-alias') {
      env.ORCH_DIR = entry === 'relative-alias' ? path.relative(root, aliasedMirror) : aliasedMirror;
    }
    reader = spawn('/bin/bash', [path.join(aliasedMirror, 'orchestrate.sh')],
      { cwd: root, env, stdio: ['pipe', 'pipe', 'pipe'] });
    let errors = '';
    reader.stderr.setEncoding('utf8').on('data', (chunk) => { errors += chunk; });
    lines = readline.createInterface({ input: reader.stdout });
    const output = [];
    let readyResolve;
    let readyReject;
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
    lines.on('line', (line) => {
      if (line === 'SELECTED') readyResolve();
      else output.push(line);
    });
    done = new Promise((resolve, reject) => {
      reader.on('error', (error) => { readyReject(error); reject(error); });
      reader.on('close', (code) => {
        readyReject(new Error(`reader exited before selection: ${errors}`));
        resolve(code);
      });
    });
    await ready;
    publish('new');
    assert.notEqual(fs.realpathSync(mirror), pinned);
    reader.stdin.end('resume\n');
    assert.equal(await done, 0, errors);
    assert.equal(output[0], incumbent ? 'SHELL:new' : 'SHELL:old',
      incumbent ? 'observer did not detect the before-code defect' :
        'shell reopened through the publication link');
    assert.deepEqual(JSON.parse(output[1]), ['old', 'old', pinned]);
  });
}
