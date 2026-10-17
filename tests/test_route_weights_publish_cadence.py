"""The route-weights export publishes on the daily cadence, behind the owner's switch.

Until 2026-09-21 orchestrate.sh never passed --publish, so the export the fleet's delegation policy
fetches was the one-off publication of 2026-09-04 while relearn kept producing new versions: a learner
whose output had no receiver. The owner asked for cadence publication. Two guards stay: the script
refuses without ORCH_ROUTE_WEIGHTS_PUBLISH=1, and it pushes only when the remote artifact changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import paths
import route_weights_export as rwe

ORCHESTRATE = Path(paths.REPO_ROOT) / "orchestrate.sh"


class CadenceRefusalTests(unittest.TestCase):
    def test_publish_refusal_is_visible_in_tick_log_and_marked_failed(self):
        source = ORCHESTRATE.read_text()
        block = source.split("if _cadence_due route-weights-export", 1)[1]
        block = "if _cadence_due route-weights-export" + block.split(
            "if _cadence_due periodic-report", 1
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "route_weights_export.py").write_text(
                "import sys\nprint('publish_guard refused: source_version must be > 0', "
                "file=sys.stderr)\nsys.exit(1)\n"
            )
            harness = """set -euo pipefail
_cadence_due() { return 0; }
_attempt_ok() { return 0; }
_mark_success() { echo SUCCESS; }
_mark_fail() { echo "FAILED: $*"; }
"""
            result = subprocess.run(
                ["bash", "-c", harness + block],
                env={**os.environ, "ORCH": directory, "STAMP_DIR": directory},
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertIn("publish_guard refused: source_version must be > 0", result.stderr)
        self.assertIn("FAILED: route-weights-export", result.stdout)
        self.assertNotIn("SUCCESS", result.stdout)


def test_the_cadence_invocation_passes_publish_exactly_once():
    src = ORCHESTRATE.read_text()
    # WIRING PIN (smallest fragment, count == 1): the daily export step must carry --publish.
    needle = 'route_weights_export.py" ' + "--publish"
    assert (
        src.count(needle) == 1
    ), "the daily export publishes on cadence (owner decision 2026-09-21)"
    assert (
        src.count("ORCH_ROUTE_WEIGHTS_PUBLISH:-0") == 1
    ), "the step announces which mode it runs in"


def _wire(monkeypatch, tmp_path, *, changed: bool):
    calls: list = []
    monkeypatch.setattr(
        sys, "argv", ["route_weights_export.py", "--publish", "--state-dir", str(tmp_path)]
    )
    monkeypatch.setattr(
        rwe, "build_document", lambda db_path, minimum: {"source_version": 63, "weights": {}}
    )
    monkeypatch.setattr(rwe, "write_document", lambda target, doc: changed)
    monkeypatch.setattr(rwe.capabilities, "daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(rwe, "publish_document", lambda doc, **kwargs: calls.append(doc) or True)
    return calls


def test_publish_is_refused_without_the_owner_switch(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ORCH_ROUTE_WEIGHTS_PUBLISH", raising=False)
    calls = _wire(monkeypatch, tmp_path, changed=True)
    assert rwe.main() == 0
    assert "publish blocked" in capsys.readouterr().out and calls == []


def test_publish_runs_with_the_owner_switch(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    calls = _wire(monkeypatch, tmp_path, changed=True)
    assert rwe.main() == 0
    assert len(calls) == 1 and calls[0]["source_version"] == 63
    assert "publish blocked" not in capsys.readouterr().out
