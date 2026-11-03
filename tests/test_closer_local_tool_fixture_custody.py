"""Fixture-only hosted layout simulation; the actual account-home guard stays real."""

import os
import pwd
from pathlib import Path

import pytest
from test_closer_local_tool_confinement import box as box

import closer_local_tool_confinement as boundary


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Model the CI temp layout without creating anything in the real account home.
    root = tmp_path_factory.mktemp("hosted-layout") / "synthetic-home/work/_temp/pytest-case"
    root.mkdir(parents=True)
    return root


def test_runtime_fixture_is_outside_ci_like_home_parent(box, tmp_path):
    args, source, tool = box
    simulated_home = tmp_path.parents[2]
    runtime = Path(args["runtime_read_roots"][0])
    assert not runtime.is_relative_to(simulated_home)
    assert runtime.resolve() == runtime
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert source.parent == tmp_path / "workspace"
    assert Path(args["scratch"]) == tmp_path / "scratch"
    assert tool.parent == runtime
    assert boundary.prepare_boundary(**args).runtime_read_roots == (str(runtime),)


def test_real_account_home_still_refused_with_outside_fixture_runtime(box):
    args, _, _ = box
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    args["runtime_read_roots"] = [account_home]
    with pytest.raises(boundary.BoundaryRefused, match="account home"):
        boundary.prepare_boundary(**args)
