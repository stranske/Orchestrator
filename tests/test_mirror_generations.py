"""Executable generation witnesses, runnable with Python's standard unittest runner."""

from __future__ import annotations

import fcntl
import json
import os
import py_compile
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext, suppress
from itertools import product
from pathlib import Path
from unittest.mock import patch

from scripts import install_verified_snapshot as installer

import paths

REPO = Path(__file__).resolve().parents[1]
# The MODULES, wherever this tree keeps them: src/ in a checkout, the root of the flat exec mirror.
# `REPO / "src"` raised FileNotFoundError in the mirror, which CI's exec-mirror job caught on its
# first run (2026-10-04), the same class #408 fixed in test_install_verified_snapshot.py.
MODULES = paths.MODULE_DIR
OBSERVER = """
import json
import os
import subprocess
import sys
from pathlib import Path
import module
import paths

print(module.VALUE, flush=True)
sys.stdin.readline()
import peer
child = subprocess.run(
    [sys.executable, '-c', 'import peer; print(peer.VALUE)'],
    capture_output=True, text=True, check=True,
)
print(json.dumps([
    module.VALUE,
    peer.VALUE,
    (paths.MODULE_DIR / 'module.py').read_text().strip(),
    child.stdout.strip(),
    str(paths.checkout_root(Path(paths.__file__).parent)),
    str(paths.REPO_ROOT),
]), flush=True)
"""


class MirrorGenerationTests(unittest.TestCase):
    def test_contended_publisher_expires_reader_before_command_execution(self):
        import mirror_reader

        marker = self.root / "command-ran"
        with mirror_reader.lock_path(self.mirror).open("a") as publisher:
            fcntl.flock(publisher.fileno(), fcntl.LOCK_EX)
            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULES / "mirror_reader.py"),
                    "--lock-timeout",
                    "0.15",
                    "run",
                    str(self.mirror),
                    sys.executable,
                    "-c",
                    f"open({str(marker)!r}, 'w').close()",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("publication reader lock deadline exceeded", result.stderr)
        self.assertFalse(marker.exists(), "blocked reader executed the tick")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orch-generations-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.snapshot = self.root / "snapshot"
        self.mirror = self.root / "mirror"
        (self.snapshot / "scripts").mkdir(parents=True)
        for name in ("paths.py", "mirror_reader.py"):
            shutil.copy2(MODULES / name, self.snapshot / name)
        shutil.copy2(
            Path(installer.__file__), self.snapshot / "scripts" / "install_verified_snapshot.py"
        )
        (self.snapshot / "orchestrate.sh").write_text("#!/bin/sh\n")
        (self.snapshot / "observer.py").write_text(OBSERVER)
        (self.snapshot / "docs").mkdir()
        (self.snapshot / "docs/guide.md").write_text("shipped guide\n")
        (self.snapshot / ".docs-shipped.txt").write_text("docs/guide.md\n")
        (self.snapshot / "peer_alias.py").symlink_to("peer.py")
        (self.mirror / "docs/reports").mkdir(parents=True)
        (self.mirror / "docs/reports/runtime.md").write_text("runtime\n")
        self.set_value("old")

    def set_value(self, value):
        for name in ("module.py", "peer.py"):
            (self.snapshot / name).write_text(f"VALUE = {value!r}\n")
        (self.snapshot / "module.py").chmod(0o751)

    def publish(self):
        installer.install(self.snapshot, self.mirror, installer.snapshot_digest(self.snapshot))

    def publish_with_deadline(self, timeout=10):
        """Kill and reap publication if a paused reader keeps the exclusive lock blocked."""
        publisher_code = """
import fcntl
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install_verified_snapshot as installer
flock = fcntl.flock
def observed_flock(fd, operation):
    if operation == fcntl.LOCK_EX:
        print('PUBLICATION-ATTEMPTED', file=sys.stderr, flush=True)
    return flock(fd, operation)
installer.fcntl.flock = observed_flock
installer.install(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
"""
        publisher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                publisher_code,
                str(Path(installer.__file__).parent),
                str(self.snapshot),
                str(self.mirror),
                installer.snapshot_digest(self.snapshot),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            ready, _, _ = select.select([publisher.stderr], [], [], 10)
            self.assertTrue(ready, "publisher did not reach the exclusive lock")
            self.assertEqual(publisher.stderr.readline().strip(), "PUBLICATION-ATTEMPTED")
            _, error = publisher.communicate(timeout=timeout)
            self.assertEqual(publisher.returncode, 0, error)
        finally:
            with suppress(ProcessLookupError):
                os.killpg(publisher.pid, signal.SIGKILL)
            publisher.communicate(timeout=10)

    def test_paused_reader_lock_bounds_publication_and_allows_retry(self):
        self.publish()
        pinned = self.mirror.resolve()
        self.set_value("new")
        publishers = []
        popen = subprocess.Popen

        def record_publisher(*args, **kwargs):
            publisher = popen(*args, **kwargs)
            publishers.append(publisher)
            return publisher

        # Negative control: emulate a reader retaining shared mode while paused.
        # The publisher must reach flock before the short deadline starts.
        with (self.root / ".mirror.publish.lock").open("a") as reader_lock:
            fcntl.flock(reader_lock.fileno(), fcntl.LOCK_SH)
            with patch.object(subprocess, "Popen", side_effect=record_publisher):
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.publish_with_deadline(timeout=0.5)
            self.assertEqual(len(publishers), 1)
            self.assertEqual(publishers[0].returncode, -signal.SIGKILL)
            # Check reaping before releasing the lock: a surviving publisher could
            # otherwise publish later and make the retry appear to have succeeded.
            with self.assertRaises(ChildProcessError):
                os.waitpid(publishers[0].pid, os.WNOHANG)
            self.assertEqual(self.mirror.resolve(), pinned)
            self.assertEqual((pinned / "module.py").read_text(), "VALUE = 'old'\n")
        self.publish_with_deadline()
        self.assertEqual((self.mirror / "module.py").read_text(), "VALUE = 'new'\n")
        self.assertEqual(
            installer.snapshot_digest(self.mirror), installer.snapshot_digest(self.snapshot)
        )

    @staticmethod
    def incumbent_publish(payload, mirror, entries, expected_digest, prior_docs):
        """Negative control: deployment deletion/copy at the incumbent live pathname."""
        for pattern in ("*.py", "*.sh"):
            for path in mirror.glob(pattern):
                installer._remove_owned_file(path)
        for tree in installer.REPLACED_TREES:
            installer._remove(mirror / tree)
        for relative in [*prior_docs, *map(Path, installer.OWNED_FILES)]:
            installer._remove_owned_file(mirror / relative)
        installer._copy_payload(payload, mirror, entries)

    def observe_across_publications(self, guarded, tick_mode=None, extra_import_paths=()):
        self.publish()
        pinned = self.mirror.resolve()
        command = [sys.executable, str(self.mirror / "observer.py")]
        if tick_mode is not None:
            command = ["/bin/bash", str(self.mirror / "orchestrate.sh")]
            if tick_mode == "active":
                command.append("--active")
        elif guarded:
            command = [
                sys.executable,
                str(MODULES / "mirror_reader.py"),
                "run",
                str(self.mirror),
                *command,
            ]
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join([str(self.mirror), *map(str, extra_import_paths)]),
            ORCH_DIR=str(self.mirror),
            HOME=str(self.root),
            ORCH_STATE_DIR=str(self.root / "state"),
        )
        for name in (
            "ORCH_PUBLICATION_READER_FD",
            "ORCH_PUBLICATION_ROOT",
            "ORCH_PUBLICATION_GENERATION",
            "ORCH_WATCHDOG_TICK_PID",
        ):
            env.pop(name, None)
        reader = subprocess.Popen(
            command,
            cwd=self.root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        errors = []

        def publish_twice():
            try:
                self.set_value("new")
                self.publish()
                self.set_value("newer")
                self.publish()
            except BaseException as error:
                errors.append(error)

        publisher = threading.Thread(target=publish_twice, daemon=True)
        try:
            while True:
                line = reader.stdout.readline()
                self.assertTrue(line, "reader exited before its first observation")
                if line.strip() == "old":
                    break
            publisher.start()
            publisher.join(15)
            self.assertFalse(publisher.is_alive(), "publication blocked on a pinned reader")
            self.assertFalse(errors, errors)
            output, error = reader.communicate("resume\n", timeout=15)
            self.assertEqual(reader.returncode, 0, error)
            self.assertEqual((self.mirror / "module.py").read_text(), "VALUE = 'newer'\n")
            self.assertEqual((self.mirror / "docs/reports/runtime.md").read_text(), "runtime\n")
            return json.loads(output), pinned
        finally:
            if reader.poll() is None:
                reader.kill()
                reader.communicate(timeout=10)
            if publisher.ident is not None:
                publisher.join(15)

    def test_inherited_pythonpath_cannot_import_a_later_generation_module(self):
        observer = """
import importlib.util
import json
import os
import subprocess
import sys
print(json.dumps(os.environ['PYTHONPATH'].split(os.pathsep)), flush=True)
sys.stdin.readline()
child = subprocess.run(
    [sys.executable, '-c',
     'import importlib.util; print(importlib.util.find_spec("later_only") is not None)'],
    capture_output=True, text=True, check=True,
)
print(json.dumps([
    importlib.util.find_spec('later_only') is not None,
    child.stdout.strip() == 'True',
]), flush=True)
"""
        (self.snapshot / "observer.py").write_text(observer)
        self.publish()
        pinned = self.mirror.resolve()
        outside = self.root / "external"
        outside.mkdir()
        sibling = self.root / "mirror-other"
        sibling.mkdir()
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        inherited = [
            str(outside),
            str(self.mirror / "plugins"),
            "mirror/scripts",
            "./mirror/scripts/../scripts",
            str(parent_alias / "mirror/scripts"),
            "parent-alias/mirror/scripts",
            str(self.mirror / "plugins" / ".."),
            str(self.mirror / ".." / "mirror"),
            str(self.mirror / ".." / "external"),
            str(sibling),
            "relative-entry",
            str(self.mirror),
            str(outside),
        ]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(inherited))
        reader = subprocess.Popen(
            [
                sys.executable,
                str(MODULES / "mirror_reader.py"),
                "run",
                str(self.mirror),
                sys.executable,
                str(self.mirror / "observer.py"),
            ],
            cwd=self.root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            ready, _, _ = select.select([reader.stdout], [], [], 15)
            self.assertTrue(ready, "reader did not report its inherited import paths")
            paths = json.loads(reader.stdout.readline())
            (self.snapshot / "later_only.py").write_text("VALUE = 'new generation only'\n")
            # Root imports are already pinned. A relative subdirectory must not
            # provide a second route into a later executable generation, in
            # either the paused reader or a child started after publication.
            (self.snapshot / "scripts/later_only.py").write_text("VALUE = 'new generation only'\n")
            self.set_value("new")
            self.publish_with_deadline()
            output, error = reader.communicate("resume\n", timeout=15)
            self.assertEqual(reader.returncode, 0, error)
            self.assertTrue((self.mirror / "later_only.py").is_file())
            self.assertFalse((pinned / "later_only.py").exists())
            self.assertEqual(json.loads(output), [False, False])
            self.assertEqual(
                paths,
                [
                    str(pinned),
                    str(outside),
                    str(pinned / "plugins"),
                    str(pinned / "scripts"),
                    str(pinned / "scripts"),
                    str(pinned / "scripts"),
                    str(pinned / "scripts"),
                    str(pinned),
                    str(pinned),
                    str(self.mirror / ".." / "external"),
                    str(sibling),
                    "relative-entry",
                    str(pinned),
                    str(outside),
                ],
            )
        finally:
            with suppress(ProcessLookupError):
                os.killpg(reader.pid, signal.SIGKILL)
            reader.communicate(timeout=10)

    def test_inherited_import_paths_exclude_later_generation_modules(self):
        external = self.root / "external"
        external.mkdir()
        (external / "unrelated.py").write_text("VALUE = 'external'\n")
        observer = OBSERVER.replace(
            "import peer\n",
            "import importlib.util\n"
            "import unrelated\n"
            "assert unrelated.VALUE == 'external'\n"
            "assert importlib.util.find_spec('later_only') is None, 'crossed generations'\n"
            "import peer\n",
        )
        (self.snapshot / "observer.py").write_text(observer)
        original = self.set_value

        def add_later_module(value):
            original(value)
            (self.snapshot / "scripts/later_only.py").write_text("VALUE = 'new'\n")

        self.set_value = add_later_module
        observed, pinned = self.observe_across_publications(
            guarded=True, extra_import_paths=(self.mirror / "scripts", external)
        )
        self.assertTrue((self.mirror / "scripts/later_only.py").is_file())
        self.assertEqual(observed[0], "old")
        self.assertNotEqual(pinned, self.mirror.resolve())

    def test_command_aliases_stay_pinned_when_publication_precedes_exec(self):
        """Pause after selection and unlock, before the real exec reopens the script."""
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        harness = """
import os
import sys
from pathlib import Path
import mirror_reader
execvpe = os.execvpe
def paused_exec(executable, command, env):
    print('SELECTED', file=sys.stderr, flush=True)
    assert sys.stdin.readline() == 'resume\\n'
    execvpe(executable, command, env)
mirror_reader.os.execvpe = paused_exec
mirror_reader.run(Path(sys.argv[1]), sys.argv[2:])
"""
        observer = (
            f"#!{sys.executable}\n"
            "import json, subprocess, sys\nfrom pathlib import Path\n"
            "import module, paths\n"
            "child = subprocess.run([sys.executable, '-c', "
            "'import module; print(module.VALUE)'], capture_output=True, text=True, check=True)\n"
            "Path(sys.argv[-1]).write_text('created')\n"
            "print(json.dumps([SCRIPT_VALUE, module.VALUE, child.stdout.strip(), "
            "str(paths.REPO_ROOT), sys.argv[1:]]))\n"
        )
        unrelated = ["relative-entry", str(self.root / "external"), "--flag"]
        commands = (
            [sys.executable, str(parent_alias / "mirror/observer.py")],
            [sys.executable, "parent-alias/mirror/observer.py"],
            [sys.executable, "./mirror/scripts/../observer.py"],
            [str(parent_alias / "mirror/observer.py")],
        )
        for index, command in enumerate(commands):
            output_argument = f"mirror/new-output-{index}.txt"
            with self.subTest(command=command):
                self.set_value("old")
                script = self.snapshot / "observer.py"
                script.write_text(observer.replace("SCRIPT_VALUE", "'old'"))
                script.chmod(0o751)
                self.publish()
                pinned = self.mirror.resolve()
                reader = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        harness,
                        str(self.mirror),
                        *command,
                        *unrelated,
                        output_argument,
                    ],
                    cwd=self.root,
                    env=dict(os.environ, PYTHONPATH=str(MODULES)),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    ready, _, _ = select.select([reader.stderr], [], [], 10)
                    self.assertTrue(ready, "reader did not select a generation")
                    self.assertEqual(reader.stderr.readline().strip(), "SELECTED")
                    self.set_value("new")
                    script.write_text(observer.replace("SCRIPT_VALUE", "'new'"))
                    self.publish_with_deadline()
                    output, error = reader.communicate("resume\n", timeout=10)
                    self.assertEqual(reader.returncode, 0, error)
                    self.assertEqual(
                        json.loads(output),
                        ["old", "old", "old", str(pinned), [*unrelated, output_argument]],
                    )
                    self.assertNotEqual(self.mirror.resolve(), pinned)
                finally:
                    with suppress(ProcessLookupError):
                        os.killpg(reader.pid, signal.SIGKILL)
                    reader.communicate(timeout=10)

    def test_tick_and_child_imports_remain_pinned_while_publication_completes(self):
        text = (REPO / "orchestrate.sh").read_text()
        prefix = text.split("# --- Log rotation (every tick, cheap, fail-open)", 1)[0]
        preflight = "# ORCH-ANCHOR: gh-auth-preflight"
        tail = (
            preflight + text.split(preflight, 1)[1].split("# ORCH-ANCHOR: heartbeat-export", 1)[0]
        )
        (self.snapshot / "orchestrate.sh").write_text(
            prefix + tail + 'python3 "$ORCH/observer.py"\n'
        )
        (self.snapshot / "gh_capacity.py").write_text("print('fixture authenticated')\n")
        watchdog = self.root / "watchdog-pids"
        (self.snapshot / "tick_watchdog.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            f"with Path({str(watchdog)!r}).open('a') as output:\n"
            "    output.write(sys.argv[sys.argv.index('--tick-pid') + 1] + '\\n')\n"
        )
        for mode in ("shadow", "active"):
            with self.subTest(mode=mode):
                self.set_value("old")
                observation, pinned = self.observe_across_publications(False, tick_mode=mode)
                self.assertEqual(observation[:4], ["old", "old", "VALUE = 'old'", "old"])
                self.assertEqual(observation[4:], [str(pinned), str(pinned)])
        self.assertEqual(len(watchdog.read_text().splitlines()), 1)

    def test_startup_modules_share_the_tick_generation(self):
        """Publication during the first startup module cannot change later tick reads."""
        text = (REPO / "orchestrate.sh").read_text()
        prologue = text.split("# ORCH-ANCHOR: heartbeat-export", 1)[0]
        record = self.root / "startup-observations.jsonl"
        observe = (
            "import json, sys\nfrom pathlib import Path\nimport module, paths\n"
            f"record = Path({str(record)!r})\n"
            "def observe():\n"
            "    with record.open('a') as output:\n"
            "        output.write(json.dumps([module.VALUE, str(paths.REPO_ROOT)]) + '\\n')\n"
            "observe()\n"
        )
        pause = (
            "print('STARTUP-READY', file=sys.stderr, flush=True)\n"
            "assert sys.stdin.readline() == 'resume\\n'\n"
            "observe()\n"
        )
        footer = (
            "import json\nfrom pathlib import Path\n"
            f"print(json.dumps([json.loads(line) for line in Path({str(record)!r})"
            ".read_text().splitlines()]))\n"
        )
        (self.snapshot / "observer.py").write_text(observe + footer)
        (self.snapshot / "gh_capacity.py").write_text(observe + "print('fixture authenticated')\n")
        for mode, (retain_lock, exit_shell) in product(
            ("active", "shadow"), ((False, False), (True, False), (True, True))
        ):
            with self.subTest(mode=mode, retain_lock=retain_lock, exit_shell=exit_shell):
                record.unlink(missing_ok=True)
                startup_pause = pause
                if retain_lock:
                    # Negative control: reproduce a reader that forgets to release
                    # shared mode before pausing in a retained generation. Ignore
                    # stdin EOF so cleanup must terminate the child, not just its shell.
                    startup_pause = (
                        "import fcntl, threading\n"
                        f"reader_lock = open({str(self.root / '.mirror.publish.lock')!r}, 'a')\n"
                        "fcntl.flock(reader_lock.fileno(), fcntl.LOCK_SH)\n"
                        "print('STARTUP-READY', file=sys.stderr, flush=True)\n"
                        "threading.Event().wait()\n"
                    )
                (self.snapshot / "tick_watchdog.py").write_text(observe + startup_pause)
                (self.snapshot / "cadence_registry.py").write_text(
                    observe + (startup_pause if mode == "shadow" else "") + "print(':')\n"
                )
                (self.snapshot / "orchestrate.sh").write_text(
                    prologue + 'python3 "$ORCH/observer.py"\n'
                )
                self.set_value("old")
                self.publish()
                pinned = self.mirror.resolve()
                env = dict(
                    os.environ,
                    ORCH_DIR=str(self.mirror),
                    HOME=str(self.root),
                    ORCH_STATE_DIR=str(self.root / "state"),
                    PYTHONPATH=str(self.mirror),
                )
                for name in (
                    "ORCH_PUBLICATION_READER_FD",
                    "ORCH_PUBLICATION_ROOT",
                    "ORCH_PUBLICATION_GENERATION",
                    "ORCH_WATCHDOG_TICK_PID",
                ):
                    env.pop(name, None)
                reader = subprocess.Popen(
                    [
                        "/bin/bash",
                        str(self.mirror / "orchestrate.sh"),
                        *(["--active"] if mode == "active" else []),
                    ],
                    cwd=self.root,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                # Isolate the publisher so a regression cannot leave a blocked
                # daemon thread (or its flock patch) alive in the test runner.
                publisher_code = """
import fcntl
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install_verified_snapshot as installer
flock = fcntl.flock
def observed_flock(fd, operation):
    if operation == fcntl.LOCK_EX:
        print('PUBLICATION-ATTEMPTED', file=sys.stderr, flush=True)
    return flock(fd, operation)
installer.fcntl.flock = observed_flock
installer.install(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
"""
                publisher = None
                try:
                    ready, _, _ = select.select([reader.stderr], [], [], 15)
                    self.assertTrue(ready, "first startup module did not rendezvous")
                    self.assertEqual(reader.stderr.readline().strip(), "STARTUP-READY")
                    self.set_value("new")
                    publisher = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            publisher_code,
                            str(Path(installer.__file__).parent),
                            str(self.snapshot),
                            str(self.mirror),
                            installer.snapshot_digest(self.snapshot),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    ready, _, _ = select.select([publisher.stderr], [], [], 10)
                    self.assertTrue(ready, "publisher did not reach the exclusive lock")
                    self.assertEqual(publisher.stderr.readline().strip(), "PUBLICATION-ATTEMPTED")
                    if exit_shell:
                        # A dead tick shell can leave its startup child holding the
                        # lock. Cleanup must still terminate the child's process group.
                        reader.kill()
                        self.assertEqual(reader.wait(timeout=10), -signal.SIGKILL)
                    expected_failure = (
                        self.assertRaises(subprocess.TimeoutExpired)
                        if retain_lock
                        else nullcontext()
                    )
                    with expected_failure:
                        _, error = publisher.communicate(timeout=0.5 if retain_lock else 10)
                        self.assertEqual(publisher.returncode, 0, error)
                    if retain_lock:
                        self.assertEqual(self.mirror.resolve(), pinned)
                    else:
                        output, error = reader.communicate("resume\n", timeout=15)
                        self.assertEqual(reader.returncode, 0, error)
                        observations = json.loads(output.splitlines()[-1])
                        self.assertEqual(len(observations), 5 if mode == "active" else 3)
                        self.assertEqual(
                            observations,
                            [["old", str(pinned)]] * len(observations),
                            "startup crossed executable generations",
                        )
                finally:
                    try:
                        # Startup modules are children of the shell. Kill the whole
                        # session to release their locks and pipes before joining.
                        with suppress(ProcessLookupError):
                            os.killpg(reader.pid, signal.SIGKILL)
                        reader.communicate(timeout=10)
                    finally:
                        if publisher is not None:
                            try:
                                _, error = publisher.communicate(timeout=10)
                                self.assertEqual(publisher.returncode, 0, error)
                            finally:
                                # Even a publisher that stays stuck after reader cleanup
                                # must be killed and reaped before the temporary tree goes.
                                with suppress(ProcessLookupError):
                                    os.killpg(publisher.pid, signal.SIGKILL)
                                publisher.communicate(timeout=10)
                self.assertEqual((self.mirror / "module.py").read_text(), "VALUE = 'new'\n")
                self.assertEqual(
                    installer.snapshot_digest(self.mirror), installer.snapshot_digest(self.snapshot)
                )

    def test_bytecode_cache_cannot_override_a_new_generation(self):
        """Timestamp/size-valid old bytecode must not be shared with verified new code."""
        timestamp = 1700000000
        os.utime(self.snapshot / "module.py", (timestamp, timestamp))
        self.publish()
        pinned = self.mirror.resolve()

        def imported_value(root):
            result = subprocess.run(
                [sys.executable, "-c", "import module; print(module.VALUE)"],
                cwd=self.root,
                env=dict(os.environ, PYTHONPATH=str(root)),
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()

        self.assertEqual(imported_value(pinned), "old")
        self.assertTrue(list((pinned / "__pycache__").glob("module.*.pyc")))
        py_compile.compile(str(pinned / "module.py"), cfile=str(pinned / "removed.pyc"))
        (pinned / "removed.pyo").write_bytes(b"retired bytecode")
        self.set_value("new")
        os.utime(self.snapshot / "module.py", (timestamp, timestamp))
        self.publish()
        self.assertEqual(imported_value(self.mirror), "new")
        self.assertEqual(imported_value(pinned), "old")
        self.assertFalse((self.mirror / "__pycache__").is_symlink())
        self.assertFalse((self.mirror / "removed.pyc").exists())
        self.assertFalse((self.mirror / "removed.pyo").exists())
        self.assertTrue((pinned / "removed.pyc").is_file())
        self.assertEqual((self.mirror / "docs/reports/runtime.md").read_text(), "runtime\n")

    def test_pinned_tick_still_aborts_on_refused_authentication(self):
        text = (REPO / "orchestrate.sh").read_text()
        prologue = text.split("# ORCH-ANCHOR: heartbeat-export", 1)[0]
        (self.snapshot / "orchestrate.sh").write_text(
            prologue + 'echo "UNEXPECTED: tick ran past authentication"\n'
        )
        (self.snapshot / "tick_watchdog.py").write_text("print('fixture watchdog armed')\n")
        (self.snapshot / "cadence_registry.py").write_text("print(':')\n")
        (self.snapshot / "gh_capacity.py").write_text(
            "print('fixture refused authentication')\nraise SystemExit(77)\n"
        )
        self.publish()
        env = dict(
            os.environ,
            ORCH_DIR=str(self.mirror),
            HOME=str(self.root),
            ORCH_STATE_DIR=str(self.root / "state"),
        )
        for name in (
            "ORCH_PUBLICATION_READER_FD",
            "ORCH_PUBLICATION_ROOT",
            "ORCH_PUBLICATION_GENERATION",
            "ORCH_WATCHDOG_TICK_PID",
        ):
            env.pop(name, None)
        result = subprocess.run(
            ["/bin/bash", str(self.mirror / "orchestrate.sh"), "--active"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ABORT: fixture refused authentication", result.stderr)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertEqual(result.stdout.count("fixture watchdog armed"), 1)

    def test_same_observer_fails_on_incumbent_and_passes_on_generation_publisher(self):
        with patch.object(installer, "_publish_generation", self.incumbent_publish):
            observation, _ = self.observe_across_publications(guarded=False)
        self.assertEqual(observation[:2], ["old", "newer"])
        self.assertEqual(observation[2:4], ["VALUE = 'newer'", "newer"])

        self.set_value("old")
        observation, pinned = self.observe_across_publications(guarded=True)
        self.assertEqual(observation[:4], ["old", "old", "VALUE = 'old'", "old"])
        self.assertEqual(observation[4:], [str(pinned), str(pinned)])
        self.assertTrue(self.mirror.is_symlink())
        self.assertNotEqual(self.mirror.resolve(), pinned)
        self.assertEqual((pinned / "peer.py").read_text(), "VALUE = 'old'\n")
        self.assertEqual(os.readlink(self.mirror / "peer_alias.py"), "peer.py")
        self.assertEqual(
            installer.snapshot_digest(self.mirror), installer.snapshot_digest(self.snapshot)
        )

    def test_direct_python_reader_pins_child_imports_across_publications(self):
        # Direct Python entry points also import paths, but do not pass through
        # mirror_reader.run(). A child inherits PYTHONPATH rather than sys.path.
        observation, pinned = self.observe_across_publications(guarded=False)
        self.assertEqual(observation[:4], ["old", "old", "VALUE = 'old'", "old"])
        self.assertEqual(observation[4:], [str(pinned), str(pinned)])
        self.assertNotEqual(self.mirror.resolve(), pinned)

    def test_direct_python_reader_pins_nested_import_roots_for_parent_and_child(self):
        external = self.root / "external"
        external.mkdir()
        (external / "unrelated.py").write_text("VALUE = 'external'\n")
        checks = (
            "import importlib.util; import unrelated; "
            "assert unrelated.VALUE == 'external'; "
            "assert importlib.util.find_spec('later_only') is None; "
        )
        observer = OBSERVER.replace("import peer\n", checks + "import peer\n").replace(
            "'import peer; print(peer.VALUE)'", repr(checks + "import peer; print(peer.VALUE)")
        )
        (self.snapshot / "observer.py").write_text(observer)
        original = self.set_value

        def add_later_module(value):
            original(value)
            (self.snapshot / "scripts/later_only.py").write_text("VALUE = 'new'\n")

        self.set_value = add_later_module
        observation, pinned = self.observe_across_publications(
            guarded=False,
            extra_import_paths=(
                self.mirror / "scripts",
                "mirror/scripts",
                "mirror/scripts/../scripts",
                external,
                "",
            ),
        )
        self.assertTrue((self.mirror / "scripts/later_only.py").is_file())
        self.assertEqual(observation[:4], ["old", "old", "VALUE = 'old'", "old"])
        self.assertEqual(observation[4:], [str(pinned), str(pinned)])

    def test_interrupting_link_switch_preserves_pinned_generation_and_runtime_on_retry(self):
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                self.set_value("old")
                self.publish()
                pinned = self.mirror.resolve()
                report = self.mirror / "docs/reports/runtime.md"
                original = Path.replace

                def interrupted_replace(path, destination):
                    if path.name.startswith(self.mirror.name + ".retired-"):
                        if phase == "after":
                            original(path, destination)
                        raise OSError("interrupted link publication")
                    return original(path, destination)

                self.set_value("new")
                with report.open("a") as writer:
                    with patch.object(Path, "replace", interrupted_replace):
                        with self.assertRaisesRegex(OSError, "interrupted link publication"):
                            self.publish()
                    self.assertEqual(
                        (self.mirror / "module.py").read_text(),
                        "VALUE = 'old'\n" if phase == "before" else "VALUE = 'new'\n",
                    )
                    writer.write(phase + "\n")
                    writer.flush()
                    self.publish()
                self.assertEqual((pinned / "module.py").read_text(), "VALUE = 'old'\n")
                self.assertIn(phase + "\n", report.read_text())
                self.assertEqual(
                    installer.snapshot_digest(self.mirror), installer.snapshot_digest(self.snapshot)
                )
                self.assertFalse(list(self.root.glob("mirror.next-*")))

    def test_registry_cannot_mutate_a_retained_generation(self):
        self.publish()
        old = self.mirror.resolve()
        self.set_value("new")
        self.publish()
        with self.assertRaisesRegex(ValueError, "runtime registry must be outside"):
            installer.install(
                self.snapshot,
                self.mirror,
                installer.snapshot_digest(self.snapshot),
                old / "module.py",
            )
        self.assertEqual((old / "module.py").read_text(), "VALUE = 'old'\n")


if __name__ == "__main__":
    unittest.main()
