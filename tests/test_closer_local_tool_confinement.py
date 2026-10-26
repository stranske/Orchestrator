"""Synthetic local-tool controls; profile simulation never qualifies an executor."""

import dataclasses
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

import closer_local_tool_confinement as boundary
import dispatcher


@pytest.fixture
def box(tmp_path):
    root = tmp_path.resolve()
    workspace = root / "workspace"
    scratch = root / "scratch"
    # Hosted pytest tmp may be beneath the real account home. The fake runtime
    # must satisfy the same production read-root guard, not weaken/mock it.
    with tempfile.TemporaryDirectory(prefix="closer-inert-runtime-", dir="/tmp") as private:
        runtime = Path(private).resolve()
        workspace.mkdir(mode=0o700)
        scratch.mkdir(mode=0o700)
        source = workspace / "input.py"
        source.write_text("x=1\n")
        tool = runtime / "inert"
        tool.write_text("#!/bin/sh\nprintf 'inert\\n'\n")
        tool.chmod(0o700)
        args = dict(
            workspace=workspace,
            write_paths=[source],
            scratch=scratch,
            runtime_read_roots=[runtime],
            argv=[str(tool)],
            timeout_seconds=2,
            max_output_bytes=1024,
            operation_id="original-operation",
        )
        yield args, source, tool


def callbacks():
    return dict(
        starting_callback=lambda _: None,
        started_callback=lambda _: None,
        finished_callback=lambda _: None,
    )


def test_exact_data_writes_private_cache_and_deny_default_no_authority(box):
    args, source, _ = box
    value = boundary.prepare_boundary(**args)
    assert "(deny default)" in value.profile and "(deny network*)" in value.profile
    assert f'(allow file-write-data (literal "{source}"))' in value.profile
    assert f'(allow file-write* (subpath "{source.parent}"))' not in value.profile
    assert "(allow default)" not in value.profile
    assert value.operation_id == "original-operation"
    assert len(value.descriptor_sha256) == 64


@pytest.mark.parametrize("mutation", ["outside", "duplicate", "symlink", "hardlink", "git", "dir"])
def test_nonexact_write_objects_fail_before_any_tool(box, mutation):
    args, source, _ = box
    other = source.parent.parent / "other.py"
    other.write_text("outside")
    if mutation == "outside":
        args["write_paths"] = [other]
    elif mutation == "duplicate":
        args["write_paths"] = [source, source]
    elif mutation == "symlink":
        link = source.parent / "link.py"
        link.symlink_to(source)
        args["write_paths"] = [link]
    elif mutation == "hardlink":
        link = source.parent / "link.py"
        link.hardlink_to(source)
        args["write_paths"] = [source]
    elif mutation == "git":
        git = source.parent / ".git"
        git.mkdir()
        tracked = git / "config"
        tracked.write_text("x")
        args["write_paths"] = [tracked]
    else:
        args["write_paths"] = [source.parent]
    with pytest.raises(boundary.BoundaryRefused):
        boundary.prepare_boundary(**args)


@pytest.mark.parametrize(
    "key,value",
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
        ("max_output_bytes", 65537),
        ("max_output_bytes", float("nan")),
        ("operation_id", ""),
        ("argv", ["relative-command"]),
    ],
)
def test_explicit_bounded_original_inputs_required(box, key, value):
    args, _, _ = box
    args[key] = value
    with pytest.raises((boundary.BoundaryRefused, FileNotFoundError)):
        boundary.prepare_boundary(**args)


def test_ambient_home_runtime_and_nonempty_private_scratch_refused(box):
    args, _, _ = box
    import os
    import pwd

    args["runtime_read_roots"] = [Path(pwd.getpwuid(os.getuid()).pw_dir)]
    with pytest.raises(boundary.BoundaryRefused):
        boundary.prepare_boundary(**args)
    args, _, _ = box
    Path(args["scratch"], "credential-context").write_text("synthetic")
    with pytest.raises(boundary.BoundaryRefused):
        boundary.prepare_boundary(**args)


def test_unsupported_nested_never_reaches_observed_process(box, monkeypatch):
    args, _, _ = box
    value = boundary.prepare_boundary(**args)
    monkeypatch.setattr(boundary.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("CODEX_SANDBOX", "synthetic-outer")
    monkeypatch.setattr(
        dispatcher, "run_observed_offload", lambda *a, **k: pytest.fail("unconfined fallback")
    )
    got = boundary.run_confined_tool(value, **callbacks())
    assert got["status"] == "refused" and got["process_started"] is False
    assert got["qualified"] is False and got["authority"] is False


@pytest.mark.parametrize("change", ["tool", "target", "descriptor"])
def test_fresh_source_binary_and_file_identity_revalidation(box, monkeypatch, change):
    args, source, tool = box
    value = boundary.prepare_boundary(**args)
    monkeypatch.setattr(boundary, "unsupported_reason", lambda: None)
    monkeypatch.setattr(
        dispatcher, "run_observed_offload", lambda *a, **k: pytest.fail("drift reached process")
    )
    if change == "tool":
        tool.write_text("#!/bin/sh\nexit 0\n")
    elif change == "target":
        source.write_text("x = 2\n")
    else:
        value = dataclasses.replace(value, timeout_seconds=3)
    got = boundary.run_confined_tool(value, **callbacks())
    assert got["status"] == "refused" and got["process_started"] is False


def test_simulated_broker_receives_explicit_sandbox_env_and_bounds_no_effect_claim(
    box, monkeypatch
):
    args, _, _ = box
    value = boundary.prepare_boundary(**args)
    monkeypatch.setattr(boundary, "unsupported_reason", lambda: None)
    monkeypatch.setenv("SYNTHETIC_AMBIENT_SECRET", "must-not-forward")
    seen = {}

    def fake(command, **kwargs):
        seen.update(kwargs)
        seen["command"] = command
        row = {
            **kwargs["observation"],
            "pid": 321,
            "process_group": 321,
            "os_started": "synthetic",
            "cleanup_terminal": True,
            "source": "trusted-popen-transport/v1",
        }
        kwargs["starting_callback"](kwargs["observation"])
        kwargs["callback"](row)
        kwargs["finished_callback"](row)
        result = subprocess.CompletedProcess(command, 0, "inert", "")
        result.observation = row
        return result

    monkeypatch.setattr(dispatcher, "run_observed_offload", fake)
    got = boundary.run_confined_tool(value, **callbacks())
    assert seen["command"] == [boundary.SANDBOX_EXEC, "-p", value.profile, *value.argv]
    assert seen["env"]["HOME"] == value.scratch and "SYNTHETIC_AMBIENT_SECRET" not in seen["env"]
    assert seen["output_limit"] == 1024 and seen["timeout"] == 2
    assert seen["stdin"] == subprocess.DEVNULL
    assert got["status"] == "observed" and got["authority"] is False and got["qualified"] is False
    assert got["effect_outcome"] == "unknown_requires_original_operation_reconciliation"
    assert "must-not-forward" not in json.dumps(got)


def test_broker_failure_remains_original_unknown(box, monkeypatch):
    args, _, _ = box
    value = boundary.prepare_boundary(**args)
    monkeypatch.setattr(boundary, "unsupported_reason", lambda: None)

    def failed(*a, **k):
        raise dispatcher.ObservedOffloadFailure(
            "inert unknown",
            {
                "pid": 321,
                "process_group": 321,
                "cleanup_terminal": False,
                "source": "trusted-popen-transport/v1",
            },
            None,
            "",
            "",
        )

    monkeypatch.setattr(dispatcher, "run_observed_offload", failed)
    got = boundary.run_confined_tool(value, **callbacks())
    assert got["status"] == "held" and got["effect_outcome"] == "unknown"
    assert got["process_observation"]["cleanup_terminal"] is False


@pytest.mark.parametrize("mode", ["success", "overflow", "timeout"])
def test_actual_inert_observer_join_is_not_os_sandbox_qualification(box, monkeypatch, mode):
    """Fake wrapper ONLY validates broker integration, never OS confinement."""
    args, _, tool = box
    wrapper = tool.parent / "inert-sandbox-wrapper"
    wrapper.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    wrapper.chmod(0o700)
    if mode == "overflow":
        tool.write_text("#!/bin/sh\nwhile :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done\n")
    elif mode == "timeout":
        tool.write_text("#!/bin/sh\nsleep 10\n")
    monkeypatch.setattr(boundary, "SANDBOX_EXEC", str(wrapper))
    monkeypatch.setattr(boundary, "unsupported_reason", lambda: None)
    value = boundary.prepare_boundary(**args)
    rows = []
    hooks = {key: lambda row: rows.append(dict(row)) for key in callbacks()}
    got = boundary.run_confined_tool(value, **hooks)
    assert got["qualified"] is False and got["authority"] is False
    assert got["process_started"] is True
    assert got["process_observation"]["cleanup_terminal"] is True
    assert rows[0]["operation_id"] == rows[1]["operation_id"] == "original-operation"
    if mode == "success":
        assert got["status"] == "observed" and got["stdout"] == "inert\n"
        assert got["effect_outcome"] == "unknown_requires_original_operation_reconciliation"
    else:
        assert got["status"] == "held" and got["effect_outcome"] == "unknown"
