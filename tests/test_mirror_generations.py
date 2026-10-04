"""Executable generation witnesses, runnable with Python's standard unittest runner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_verified_snapshot as installer

REPO = Path(__file__).resolve().parents[1]
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
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orch-generations-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.snapshot = self.root / "snapshot"
        self.mirror = self.root / "mirror"
        (self.snapshot / "scripts").mkdir(parents=True)
        for name in ("paths.py", "mirror_reader.py"):
            shutil.copy2(REPO / "src" / name, self.snapshot / name)
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
                str(REPO / "src/mirror_reader.py"),
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
