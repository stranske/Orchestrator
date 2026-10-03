'use strict';

// Negative control for #389: this exercises the incumbent publisher, whose live
// copy is still in place. Passing these tests proves the bootstrap finding, not
// atomic publication. Reuse the observer against a future generation publisher.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const installer = path.join(repo, 'scripts', 'install_verified_snapshot.py');
const python = process.env.PYTHON || 'python3';
const oldValue = "VALUE = 'old'\n";
const newValue = "VALUE = 'verified'\n";

// Only the rendezvous is injected; all preparation, deletion, live copying,
// digest validation, and registry updates run through the production installer.
const publisher = `
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import install_verified_snapshot as installer

snapshot, mirror, registry = map(Path, sys.argv[2:5])
expected, phase = sys.argv[5:7]
copy_payload = installer._copy_payload

def rendezvous():
    print("READY", flush=True)
    if sys.stdin.readline() != "resume\\n":
        raise RuntimeError("reader did not release publisher")

def copy_with_reader(source, destination, entries):
    if destination == mirror and phase == "before-copy":
        rendezvous()
    copy_payload(source, destination, entries)
    if destination == mirror and phase == "after-copy":
        rendezvous()

installer._copy_payload = copy_with_reader
if phase == "control":
    rendezvous()
else:
    installer.install(snapshot, mirror, expected, registry)
`;

function observe(root, relative) {
  try {
    return fs.readFileSync(path.join(root, relative), 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return 'MISSING';
    throw error;
  }
}

function world(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-reader-'));
  const snapshot = path.join(root, 'verified snapshot');
  const mirror = path.join(root, 'live mirror');
  const registry = path.join(root, 'runtime-registry.json');
  fs.mkdirSync(path.join(snapshot, 'scripts'), { recursive: true });
  fs.copyFileSync(installer, path.join(snapshot, 'scripts', path.basename(installer)));
  fs.writeFileSync(path.join(snapshot, 'orchestrate.sh'), '#!/bin/sh\n');
  fs.writeFileSync(path.join(snapshot, 'module.py'), newValue);
  fs.writeFileSync(path.join(snapshot, 'peer.py'), newValue);
  fs.writeFileSync(path.join(snapshot, 'repo_review_registry.json'), '{"repos": []}\n');
  fs.cpSync(snapshot, mirror, { recursive: true });
  fs.writeFileSync(path.join(mirror, 'module.py'), oldValue);
  fs.writeFileSync(path.join(mirror, 'peer.py'), oldValue);
  fs.mkdirSync(path.join(mirror, 'docs', 'reports'), { recursive: true });
  fs.writeFileSync(path.join(mirror, 'docs', 'reports', 'runtime.md'), 'runtime report\n');
  fs.writeFileSync(registry, '{"old": true}\n');
  const result = spawnSync(python, [installer, snapshot, '--digest'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { snapshot, mirror, registry, digest: result.stdout.trim() };
}

function startPublisher(t, w, phase) {
  const child = spawn(python, [
    '-c', publisher, repo, w.snapshot, w.mirror, w.registry, w.digest, phase,
  ], { stdio: ['pipe', 'pipe', 'pipe'] });
  let errors = '';
  child.stderr.setEncoding('utf8').on('data', (chunk) => { errors += chunk; });
  const lines = readline.createInterface({ input: child.stdout });
  let reached = false;
  let readyResolve;
  let readyReject;
  const ready = new Promise((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  lines.on('line', (line) => {
    if (line === 'READY') {
      reached = true;
      readyResolve();
    }
  });
  const done = new Promise((resolve, reject) => {
    child.on('error', (error) => { readyReject(error); reject(error); });
    child.on('exit', (code, signal) => {
      if (!reached) readyReject(new Error(`publisher exited before rendezvous: ${errors}`));
      resolve({ code, signal, errors });
    });
  });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    await done;
    lines.close();
  });
  return { child, ready, done };
}

test('reader control observes one complete generation without publication', { timeout: 10000 }, async (t) => {
  const w = world(t);
  const pinned = fs.realpathSync(w.mirror);
  const first = observe(pinned, 'module.py');
  const p = startPublisher(t, w, 'control');
  await p.ready;
  assert.deepEqual([first, observe(pinned, 'peer.py')], [oldValue, oldValue]);
  p.child.stdin.end('resume\n');
  const result = await p.done;
  assert.equal(result.code, 0, result.errors);
});

for (const phase of ['before-copy', 'after-copy']) {
  test(`incumbent publisher exposes ${phase === 'before-copy' ? 'missing files' : 'mixed generations'} to a pinned reader`, { timeout: 10000 }, async (t) => {
    const w = world(t);
    // Resolving once cannot pin a generation when that same directory is mutated.
    const pinned = fs.realpathSync(w.mirror);
    const first = observe(pinned, 'module.py');
    const p = startPublisher(t, w, phase);
    await p.ready;
    const second = observe(pinned, 'peer.py');
    assert.deepEqual([first, second], [oldValue, phase === 'before-copy' ? 'MISSING' : newValue]);
    assert.equal(observe(pinned, 'docs/reports/runtime.md'), 'runtime report\n');
    assert.equal(fs.readFileSync(w.registry, 'utf8'), '{"old": true}\n');
    p.child.stdin.end('resume\n');
    const result = await p.done;
    assert.equal(result.code, 0, result.errors);
    assert.equal(observe(pinned, 'module.py'), newValue);
    assert.equal(observe(pinned, 'peer.py'), newValue);
    const digest = spawnSync(python, [installer, w.mirror, '--digest'], { encoding: 'utf8' });
    assert.equal(digest.status, 0, digest.stderr);
    assert.equal(digest.stdout.trim(), w.digest);
  });
}

test('interrupting incumbent live copy leaves an incomplete mirror until retry', { timeout: 10000 }, async (t) => {
  const w = world(t);
  const p = startPublisher(t, w, 'before-copy');
  await p.ready;
  p.child.kill('SIGKILL');
  assert.equal((await p.done).signal, 'SIGKILL');
  assert.equal(observe(w.mirror, 'module.py'), 'MISSING');
  assert.equal(observe(w.mirror, 'peer.py'), 'MISSING');
  assert.equal(observe(w.mirror, 'orchestrate.sh'), 'MISSING');
  assert.equal(observe(w.mirror, 'docs/reports/runtime.md'), 'runtime report\n');
  assert.equal(fs.readFileSync(w.registry, 'utf8'), '{"old": true}\n');
  const result = spawnSync(python, [
    installer, w.snapshot, w.mirror, '--expected-digest', w.digest,
    '--runtime-registry', w.registry,
  ], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(observe(w.mirror, 'module.py'), newValue);
  assert.equal(observe(w.mirror, 'peer.py'), newValue);
  assert.equal(fs.readFileSync(w.registry, 'utf8'), '{"repos": []}\n');
});
