"""The rail-exercise cadence must run from the tree it executes in — including the flat exec mirror.

Its first run ever from that mirror (2026-09-14 00:40 UTC) resolved the contract tree one level
ABOVE the mirror (`Path(__file__).resolve().parents[1]`), reported `tree: absent` by name, exited 0,
and orchestrate.sh stamped six days of success on a zero. Three things are pinned here: the root
comes from `paths` (right on both layouts); a flat tree gets a view with `src/`, so the committed
contracts (33 of 49 say `src/`) can run at all; and zero contracts is a non-zero exit.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import paths
import rail_exercise

MINIMAL_CONTRACT = {"run": "true", "pass_check": "true", "break_case": {"run": "false"}}


def test_contract_root_follows_the_detected_checkout_root() -> None:
    assert rail_exercise.REPO_ROOT == paths.REPO_ROOT
    assert rail_exercise.CONTRACT_ROOT == paths.REPO_ROOT / "tests" / "rail_exercises"
    source = (paths.MODULE_DIR / "rail_exercise.py").read_text(encoding="utf-8")
    # The defect by its spelling: a hardcoded prefix is right in one tree and wrong in the other.
    assert source.count("resolve().parents[1]") == 0
    assert rail_exercise.CONTRACT_ROOT.is_dir(), rail_exercise.CONTRACT_ROOT


def _write_contract(root: Path, name: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "contract.json").write_text(json.dumps({"capability_id": name, **MINIMAL_CONTRACT}))
    (folder / "fixtures").mkdir()


@pytest.fixture
def isolated_main(monkeypatch, capsys):
    """Run rail_exercise.main() against a tree of our choosing, with no ledger writes."""
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(rail_exercise.capabilities, "daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["rail_exercise.py", "--json"])

    def run(root: Path) -> tuple[int, dict]:
        monkeypatch.setattr(rail_exercise, "CONTRACT_ROOT", root)
        rc = rail_exercise.main()
        return rc, json.loads(capsys.readouterr().out)

    return run


def test_absent_tree_is_a_failed_run(isolated_main, tmp_path) -> None:
    rc, report = isolated_main(tmp_path / "missing")
    assert rc == 1
    assert report["tree"].startswith("absent: ")
    assert report["totals"]["contracts"] == 0


def test_present_but_empty_tree_is_a_failed_run(isolated_main, tmp_path) -> None:
    # The mirror's shape after a sync that died inside rsync: directories, no contract.json.
    (tmp_path / "empty" / "some-capability").mkdir(parents=True)
    rc, report = isolated_main(tmp_path / "empty")
    assert rc == 1
    assert report["tree"] == "present"
    assert report["totals"]["contracts"] == 0


def test_one_passing_contract_is_a_successful_run(isolated_main, tmp_path) -> None:
    _write_contract(tmp_path / "tree", "good")
    rc, report = isolated_main(tmp_path / "tree")
    assert rc == 0, report
    assert report["tree"] == "present"
    assert report["totals"]["contracts"] == 1
    assert report["totals"]["passed"] == 1
    assert report["totals"]["failed"] == 0


def test_flat_tree_gets_a_view_with_src_for_the_contracts(monkeypatch, tmp_path) -> None:
    flat = tmp_path / "flat-mirror"
    flat.mkdir()
    (flat / "probe.py").write_text("print('ran from', __file__)\n")
    (flat / "tests").mkdir()
    (flat / "orchestrate.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(rail_exercise, "REPO_ROOT", flat)
    monkeypatch.setattr(rail_exercise, "_EXEC_ROOT", None)
    monkeypatch.setattr(paths, "MODULE_DIR", flat)
    view = rail_exercise.exec_root()
    try:
        assert view != flat
        assert (view / "src" / "probe.py").resolve() == (flat / "probe.py").resolve()
        assert (view / "tests").is_symlink()
        assert (view / "orchestrate.sh").is_symlink()
        # A contract written for a src/ checkout, run unchanged against a flat tree.
        out = rail_exercise._run(
            "cd $REPO_ROOT && PYTHONPATH=src python3 src/probe.py",
            contract_dir=tmp_path,
            fixture_dir=tmp_path,
            env=dict(os.environ),
        )
        assert out[0]["rc"] == 0, out
        assert "ran from" in out[0]["output"]
    finally:
        shutil.rmtree(view, ignore_errors=True)


def test_src_checkout_runs_in_place(monkeypatch, tmp_path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    monkeypatch.setattr(rail_exercise, "REPO_ROOT", checkout)
    monkeypatch.setattr(rail_exercise, "_EXEC_ROOT", None)
    assert rail_exercise.exec_root() == checkout


def test_contract_env_is_fully_sandboxed(monkeypatch, tmp_path) -> None:
    """A contract sees a sandbox for state, runtime AND handoff — never the live ~/.codex/handoff.

    HANDOFF_DIR was the one input still inherited from the tick: router.plan() reads capacity.json
    and the shed markers from it, and those move with the fleet. The first complete mirror run failed
    range-lane-rollout on exactly that while every isolated re-run passed.
    """
    seen: list[dict] = []

    def fake_run(commands_, *, contract_dir, fixture_dir, env):
        seen.append(dict(env))
        return [{"command": "true", "rc": 0, "output": ""}]

    monkeypatch.setattr(rail_exercise, "_run", fake_run)
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "live-handoff"))
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    _write_contract(tmp_path / "tree", "sandboxed")
    contract_path = tmp_path / "tree" / "sandboxed" / "contract.json"
    row = rail_exercise.run_contract(contract_path, json.loads(contract_path.read_text()))
    assert seen, row
    env = seen[0]
    sandbox = Path(env["ORCH_STATE_DIR"]).parent
    assert Path(env["ORCH_LOCAL_RUNTIME"]).parent == sandbox
    assert Path(env["HANDOFF_DIR"]).parent == sandbox
    assert env["HANDOFF_DIR"] != str(tmp_path / "live-handoff")
    assert "ORCH_CAPABILITY_HEARTBEATS" not in env
