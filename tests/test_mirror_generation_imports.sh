#!/usr/bin/env bash
# Standalone reader witness; needs only Bash and standard-library Python.
# Run from any directory: bash tests/test_mirror_generation_imports.sh
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo"
exec python3 - "$repo" <<'PY'
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "src" if (repo / "src").is_dir() else repo))
import paths
modules = paths.MODULE_DIR
from scripts import install_verified_snapshot as installer

# Both modes use this exact observer and the production reader wrapper. Pause
# before a late import and before spawning a fresh child, allowing publication
# to complete while the reader is still running.
observer = """
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import module
import paths

print('READY:' + module.VALUE, flush=True)
assert sys.stdin.readline() == 'resume\\n'
import peer
import peer_alias
child = subprocess.run(
    [sys.executable, '-c', 'import peer; print(peer.VALUE)'],
    capture_output=True, text=True, check=True,
)
print(json.dumps({
    'first': module.VALUE,
    'late': peer.VALUE,
    'symlink_import': peer_alias.VALUE,
    'child': child.stdout.strip(),
    'file': (paths.MODULE_DIR / 'module.py').read_text().strip(),
    'added': importlib.util.find_spec('later_only') is not None,
    'removed': importlib.util.find_spec('old_only') is not None,
    'checkout': str(paths.checkout_root(Path(paths.__file__).parent)),
    'root': str(paths.REPO_ROOT),
}), flush=True)
"""


def incumbent_publish(payload, mirror, entries, expected_digest, prior_docs):
    # Replace only the publication primitive. install(), receipt validation,
    # private payload construction, and the runtime registry update remain real.
    for pattern in ('*.py', '*.sh'):
        for leaf in mirror.glob(pattern):
            installer._remove_owned_file(leaf)
    for tree in installer.REPLACED_TREES:
        installer._remove(mirror / tree)
    for relative in [*prior_docs, *map(Path, installer.OWNED_FILES)]:
        installer._remove_owned_file(mirror / relative)
    installer._copy_payload(payload, mirror, entries)


def witness(incumbent):
    with tempfile.TemporaryDirectory(prefix='orch-imports-') as temporary:
        root = Path(temporary).resolve()
        snapshot = root / 'snapshot'
        mirror = root / 'live mirror'
        registry = root / 'runtime' / 'registry.json'
        (snapshot / 'scripts').mkdir(parents=True)
        shutil.copy2(installer.__file__, snapshot / 'scripts/install_verified_snapshot.py')
        for name in ('paths.py', 'mirror_reader.py'):
            shutil.copy2(modules / name, snapshot / name)
        (snapshot / 'orchestrate.sh').write_text('#!/bin/sh\n')
        (snapshot / 'observer.py').write_text(observer)
        (snapshot / 'old_only.py').write_text("VALUE = 'old'\n")
        (snapshot / 'peer_alias.py').symlink_to('peer.py')
        report = mirror / 'docs/reports/runtime.md'
        report.parent.mkdir(parents=True)
        report.write_text('runtime report\n')

        def publish(value):
            for name in ('module.py', 'peer.py'):
                (snapshot / name).write_text(f'VALUE = {value!r}\n')
            (snapshot / 'module.py').chmod(0o751)
            (snapshot / 'repo_review_registry.json').write_text(
                json.dumps({'generation': value}) + '\n'
            )
            expected = installer.snapshot_digest(snapshot)
            installer.install(snapshot, mirror, expected, registry)
            assert installer.snapshot_digest(mirror) == expected
            assert registry.read_bytes() == (snapshot / 'repo_review_registry.json').read_bytes()
            assert report.read_text() == 'runtime report\n'
            return expected

        old_digest = publish('old')
        pinned = mirror.resolve()
        env = dict(os.environ, PYTHONPATH=str(mirror))
        for name in (
            'ORCH_PUBLICATION_READER_FD',
            'ORCH_PUBLICATION_ROOT',
            'ORCH_PUBLICATION_GENERATION',
        ):
            env.pop(name, None)
        reader = subprocess.Popen(
            [sys.executable, str(modules / 'mirror_reader.py'), 'run', str(mirror),
             sys.executable, str(mirror / 'observer.py')],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(reader.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=10), 'reader did not reach the rendezvous'
            line = reader.stdout.readline().strip()
            if line != 'READY:old':
                if reader.poll() is None:
                    reader.kill()
                _, error = reader.communicate(timeout=10)
                raise AssertionError(f'unexpected rendezvous {line!r}: {error}')
            (snapshot / 'old_only.py').unlink()
            (snapshot / 'later_only.py').write_text("VALUE = 'new only'\n")
            # A retained-generation reader must allow both publications to
            # complete before resuming. The alarm bounds a lock regression
            # without substituting a fake publication or observer.
            if incumbent:
                with patch.object(installer, '_publish_generation', incumbent_publish):
                    publish('new')
                    publish('newer')
            else:
                publish('new')
                publish('newer')
            output, error = reader.communicate('resume\n', timeout=10)
            assert reader.returncode == 0, error
            observed = json.loads(output)
            expected = {
                'first': 'old', 'late': 'old', 'symlink_import': 'old', 'child': 'old',
                'file': "VALUE = 'old'", 'added': False, 'removed': True,
                'checkout': str(pinned), 'root': str(pinned),
            }
            if incumbent:
                assert observed != expected, 'observer failed to detect the incumbent defect'
                assert observed['first'] == 'old'
                assert observed['late'] == observed['symlink_import'] == observed['child'] == 'newer'
                assert observed['file'] == "VALUE = 'newer'"
                assert observed['added'] is True and observed['removed'] is False
                print('PASS: identical wrapped observer detects incumbent mixed generations')
            else:
                assert observed == expected, f'reader crossed generations: {observed}'
                assert mirror.resolve() != pinned
                assert installer.snapshot_digest(pinned) == old_digest
                assert os.readlink(pinned / 'peer_alias.py') == 'peer.py'
                assert (pinned / 'module.py').stat().st_mode & 0o777 == 0o751
                print('PASS: production publisher retains reader and child imports across two publications')
        finally:
            if reader.poll() is None:
                reader.kill()
            reader.communicate(timeout=10)


# Alarm applies to both cases, including a publication blocked by a reader lock.
# Raising allows normal cleanup of the waiting reader and temporary directories.
def timed_out(signum, frame):
    raise TimeoutError('publication/import witness exceeded 30 seconds')


signal.signal(signal.SIGALRM, timed_out)
signal.alarm(30)
try:
    witness(incumbent=True)
    witness(incumbent=False)
finally:
    signal.alarm(0)
PY
