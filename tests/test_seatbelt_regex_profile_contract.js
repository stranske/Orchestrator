'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');

function generatedProfileFor(t, name) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'seatbelt-profile-contract-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const script = `
import json
import pathlib
import re
import sys

sys.path.insert(0, sys.argv[1])
import adapters

protected = pathlib.Path(sys.argv[2]) / "protected-tree"
allowed_root = protected / sys.argv[3]
allowed_root.mkdir(parents=True)
protect, allow = adapters.write_boundary_roots([protected], [], allowed_root, agent="vibe")
profile = adapters.seatbelt_write_profile(protect, allow)
print(json.dumps({
    "profile": profile,
    "protect": protect,
    "allow": allow,
    "expected_pattern": "^" + re.escape(str(allowed_root.resolve())) + r"/(.*/)?\\.git(/|$)",
}))
`;
  const proc = spawnSync(
    process.env.PYTHON || 'python3',
    ['-I', '-c', script, path.join(repo, 'src'), root, name],
    { encoding: 'utf8' },
  );
  assert.equal(proc.status, 0, proc.stderr);
  return JSON.parse(proc.stdout);
}

for (const name of [
  'odd"root',
  String.raw`back\slash`,
  'meta.[x]+(a){2}^$|?*',
  String.raw`mixed"back\slash.[x]+(a){2}^$|?* space`,
]) {
  test(`production profile quotes the nested .git regex for ${name}`, (t) => {
    const { profile, protect, allow, expected_pattern: expectedPattern } = generatedProfileFor(
      t,
      name,
    );
    const literals = [...profile.matchAll(/\(deny file-write\* \(regex ("(?:\\.|[^"\\])*")\)\)/g)];
    assert.equal(literals.length, 1, profile);
    const literal = literals[0][1];
    const pattern = JSON.parse(literal);
    assert.equal(pattern, expectedPattern);
    assert.doesNotMatch(profile, /\(regex #"/);
    assert.equal(
      profile,
      `(version 1)(allow default)`
        + `(deny file-write* (subpath ${JSON.stringify(protect[0])}))`
        + `(allow file-write* (subpath ${JSON.stringify(allow[0])}))`
        + `(deny file-write* (regex ${literal}))`,
    );

    const regex = new RegExp(pattern);
    for (const suffix of ['/.git', '/.git/config', '/nested/.git/hooks/pre-commit']) {
      assert.match(allow[0] + suffix, regex);
    }
    for (const candidate of [
      `${allow[0]}/source.py`,
      `${allow[0]}/nested/.gitignore`,
      `${allow[0]}/.git-other/config`,
      `${allow[0]}-sibling/.git/config`,
      `${allow[0].replace(name, 'different-root')}/.git/config`,
    ]) {
      assert.doesNotMatch(candidate, regex);
    }
  });
}
