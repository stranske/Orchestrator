"""Hermetic regression tests for advisor selftest cleanup and prerequisite gates."""

from __future__ import annotations

import inspect
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import capabilities
import capability_advisor as advisor
import repo_knowledge


def _redirect_bound_paths(monkeypatch, runtime):
    """Point the ledger, feature and repo-knowledge paths at `runtime`, wherever they were bound.

    Each is fixed when its module is imported, as a module constant AND as the default of every
    `path: Path = REG` parameter, so setting the environment variables inside a test moves none of them.
    On a machine whose runtime directory holds the real ledger and registry that meant these tests
    created the real ledger's lock file and seeded the real repo-knowledge registry.
    """
    targets = {
        capabilities.LOCAL_RUNTIME: runtime,
        capabilities.REG: runtime / "capabilities.json",
        capabilities.FEATURES_REG: runtime / "experiments" / "features.json",
        repo_knowledge.REG: runtime / "experiments" / "repo_knowledge.json",
    }
    src_dir = Path(capabilities.__file__).resolve().parent
    for module in list(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if not module_file or Path(module_file).resolve().parent != src_dir:
            continue
        for name, value in list(vars(module).items()):
            if isinstance(value, Path) and value in targets:
                monkeypatch.setattr(module, name, targets[value])
            elif inspect.isfunction(value) and value.__module__ == module.__name__:
                defaults = value.__defaults__ or ()
                if any(isinstance(d, Path) and d in targets for d in defaults):
                    monkeypatch.setattr(
                        value,
                        "__defaults__",
                        tuple(
                            targets[d] if isinstance(d, Path) and d in targets else d
                            for d in defaults
                        ),
                    )
                kwdefaults = value.__kwdefaults__ or {}
                if any(isinstance(d, Path) and d in targets for d in kwdefaults.values()):
                    monkeypatch.setattr(
                        value,
                        "__kwdefaults__",
                        {
                            k: targets[d] if isinstance(d, Path) and d in targets else d
                            for k, d in kwdefaults.items()
                        },
                    )


@pytest.fixture(autouse=True)
def isolated_runtime_and_external_calls(monkeypatch, tmp_path):
    """These selftests must never consult a live ledger or start a process."""
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("advisor selftest regression attempted external I/O")

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    _redirect_bound_paths(monkeypatch, tmp_path / "runtime")
    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


@pytest.mark.parametrize(
    ("surface", "selftest"),
    [
        ("t-howto", advisor._selftest_how_to_use),
        ("t-concept", advisor._selftest_how_to_use),
        ("t-contra", advisor._selftest_contraindications),
        ("t-surface", advisor._selftest_bindings),
        ("t-precond", advisor._selftest_preconditions),
    ],
)
def test_advisor_selftests_restore_existing_surface_bindings(monkeypatch, surface, selftest):
    """A selftest must restore a pre-existing binding instead of deleting it."""
    original = {"existing-capability": "must survive the isolated probe"}
    monkeypatch.setitem(advisor.SURFACE_BINDINGS, surface, original)

    selftest()

    assert advisor.SURFACE_BINDINGS[surface] == original

    # THE OPPOSITE STATE. `SURFACE_BINDINGS` is a module global, so a temporary binding that is
    # not removed again leaks into every later test in the process. Start from "no such binding"
    # and require that the selftest leaves none behind.
    monkeypatch.delitem(advisor.SURFACE_BINDINGS, surface)

    selftest()

    assert surface not in advisor.SURFACE_BINDINGS


def test_front_door_reports_missing_ledger_rows_and_returns(monkeypatch, tmp_path):
    """Missing prerequisites are reported, by name, instead of exercising a live ledger."""
    reported = []
    # `capabilities.REG` is fixed when the module is imported, so the fixture's environment
    # variables cannot move it. Point it at an empty ledger so the three rows the front door
    # needs are absent on every machine, whatever the machine's own ledger holds.
    monkeypatch.setattr(advisor.capabilities, "REG", tmp_path / "empty" / "capabilities.json")
    monkeypatch.setattr(
        advisor.env_prereq,
        "report_gaps",
        lambda name, gaps: reported.append((name, list(gaps))),
    )

    def ran_past_the_gate(*_args, **_kwargs):
        pytest.fail("the front door kept running after reporting its missing prerequisites")

    monkeypatch.setattr(advisor, "advise", ran_past_the_gate)

    assert advisor._selftest_front_door() is None

    assert [name for name, _gaps in reported] == ["capability_advisor.py front-door"]
    gaps = reported[0][1]
    assert len(gaps) == 1, gaps
    for row in ("offload", "codemod-campaign", "testgen-lane"):
        assert row in gaps[0], (row, gaps[0])
