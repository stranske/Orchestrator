'use strict';

// Complement the native zsh probes with Bash's opt-in startup alias expansion.
// This uses the production BASH_ENV file and never invokes the installed gh.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const repo = path.resolve(__dirname, '..');
const source = process.env.ORCH_GH_GUARD_TEST_SOURCE || repo;
const modules = fs.existsSync(path.join(source, 'src')) ? path.join(source, 'src') : source;
const python = process.env.PYTHON || 'python3';

for (const flags of ['-c', '-lc']) {
  for (const [args, expected] of [['auth logout', 77], ['auth status', 0]]) {
    test(`owner BASH_ENV alias preserves startup and refusal precedence: ${flags} ${args}`, (t) => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-gh-alias-'));
      t.after(() => fs.rmSync(root, { recursive: true, force: true }));
      const home = path.join(root, 'home');
      const real = path.join(root, 'real gh');
      const guard = path.join(root, 'guard');
      const owner = path.join(root, 'owner env.sh');
      const log = path.join(root, 'gh.log');
      fs.mkdirSync(home);
      fs.mkdirSync(real);
      fs.writeFileSync(path.join(real, 'gh'), '#!/bin/sh\nprintf "REAL-GH %s\\n" "$*" >> "$GH_LOG"\n',
        { mode: 0o755 });
      fs.writeFileSync(owner, [
        'shopt -s expand_aliases',
        'export PATH="$REAL_GH_DIR:$PATH"',
        'alias gh="command gh"',
        'alias owner_marker="printf owner-startup"',
        'export ORCH_TEST_OWNER_STARTUP=ran',
        '',
      ].join('\n'));
      // bash -lc reads the system profile, which may reset PATH. The owner's
      // BASH_ENV must still run afterwards and put fake gh ahead of the wrapper.
      const env = {
        PATH: process.env.PATH,
        HOME: home,
        // A dispatched shell is a child of the launcher's shell. Bash treats
        // level-one shells specially when detecting remote startup.
        SHLVL: '1',
        ORCH_STATE_DIR: path.join(root, 'state'),
        PYTHONPATH: modules,
        PYTHONDONTWRITEBYTECODE: '1',
        GH_LOG: log,
        REAL_GH_DIR: real,
        BASH_ENV: path.join(guard, 'bashenv.sh'),
        ORCH_GH_GUARD_PREV_BASH_ENV: owner,
      };
      const written = spawnSync(python, ['-c', [
        'import sys',
        'from pathlib import Path',
        'import dispatcher',
        'for file, content in dispatcher._gh_guard_files(Path(sys.argv[1])).items():',
        '    file.parent.mkdir(parents=True, exist_ok=True)',
        '    file.write_text(content)',
      ].join('\n'), guard], { cwd: root, env, encoding: 'utf8', timeout: 10000 });
      assert.equal(written.status, 0, written.stderr || String(written.error));
      const command = [
        '[ "$ORCH_TEST_OWNER_STARTUP" = ran ] || exit 99',
        'owner_marker',
        'printf "\\n%s\\n" "$ORCH_TEST_OWNER_STARTUP"',
        `gh ${args}`,
      ].join('\n');
      const ran = spawnSync('bash', [flags, command],
        { cwd: root, env: { ...env, PATH: `${real}:/usr/bin:/bin` },
          encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'pipe'] });
      assert.equal(ran.status, expected, ran.stderr || String(ran.error));
      assert.equal(ran.stdout, 'owner-startup\nran\n');
      assert.doesNotMatch(ran.stderr, /syntax error|parse error/);
      if (expected) assert.match(ran.stderr, /orchestrator gh guard/);
      assert.equal(fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '',
        expected ? '' : `REAL-GH ${args}\n`);
    });
  }
}
