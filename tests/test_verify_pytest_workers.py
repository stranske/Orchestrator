"""verify.py opts into xdist only when explicitly given a positive worker count."""

import sys
from types import SimpleNamespace

import pytest

import verify


@pytest.fixture
def commands(monkeypatch):
    captured = []

    def run(cmd, **kwargs):
        captured.append(cmd)
        return SimpleNamespace(stdout="2 passed, 1 skipped in 0.1s", stderr="", returncode=0)

    monkeypatch.setattr(verify.subprocess, "run", run)
    monkeypatch.setattr(verify, "COVERAGE", False)
    return captured


@pytest.mark.parametrize("workers", ["4", "1", "0", "-1", "abc", "", "1.5", None])
def test_pytest_command_gets_n_from_env(monkeypatch, commands, workers):
    if workers is None:
        monkeypatch.delenv("VERIFY_PYTEST_WORKERS", raising=False)
    else:
        monkeypatch.setenv("VERIFY_PYTEST_WORKERS", workers)

    result = verify.run_pytest()

    if workers in ("4", "1"):
        assert commands[0][-2:] == ["-n", workers]
    else:
        assert "-n" not in commands[0]
    assert (result["collected"], result["passed"], result["skipped"], result["failed"]) == (
        3,
        2,
        1,
        0,
    )


def test_unset_env_keeps_the_serial_command_byte_identical(monkeypatch, commands):
    monkeypatch.delenv("VERIFY_PYTEST_WORKERS", raising=False)

    verify.run_pytest()

    assert commands == [
        [sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider", "--no-header"]
    ]
