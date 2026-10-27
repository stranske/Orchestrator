"""Experimental candidate preparation never grants production admission."""

import sys
from pathlib import Path

import pytest

import adapters
import non_claude_data_only_candidate as candidate


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent": "cursor"},
        {"agent": "gemini"},
        {"agent": "muse"},
        {"profile": "claude-sonnet-5-5-high"},
        {"mode": None},
        {"mode": "full"},
        {"transport": "local"},
    ],
)
def test_wrong_candidate_binding_refuses_before_manifest_or_planner(overrides, monkeypatch):
    def must_not_prepare(**_kwargs):
        raise AssertionError("invalid binding reached candidate planner")

    monkeypatch.setattr(candidate, "prepare_vibe_candidate", must_not_prepare)
    values = {"agent": "vibe", "manifest": object(), "private_root": Path("/not-created")}
    values.update(overrides)
    with pytest.raises(ValueError):
        adapters.prepare_non_claude_data_only_candidate(**values)


def test_pinned_candidate_plan_does_not_enable_production(tmp_path):
    imports = tmp_path / "imports"
    imports.mkdir()
    (imports / "stub.py").write_text("# inert code-root inventory\n")
    manifest = candidate.snapshot_runtime_manifest(
        interpreter=Path(sys.executable),
        import_roots=(imports,),
        support_files=(Path(candidate.__file__), Path(adapters.__file__)),
        origin="offline_fixture",
    )
    plan = adapters.prepare_non_claude_data_only_candidate(
        "vibe", manifest=manifest, private_root=tmp_path / "private"
    )
    assert plan["profile_identity"]["profile_id"] == "vibe-medium-3.5"
    assert plan["requested_model"] == "mistral-medium-3.5"
    assert plan["requested_effort"] == "high" and plan["observed_effort"] == "UNKNOWN"
    assert plan["production_admission"] is False and plan["activation"] is False
    assert not (tmp_path / "private").exists()
    assert adapters.data_only_problem("vibe") == "data-only tool boundary unsupported for vibe"
    with pytest.raises(ValueError, match="data-only tool boundary unsupported for vibe"):
        adapters.build_command(
            "vibe", "", mode="assess", transport="offload", prompt_via_stdin=True, data_only=True
        )
    (imports / "added.py").write_text("# unreviewed closure change\n")
    with pytest.raises(ValueError):
        adapters.prepare_non_claude_data_only_candidate(
            "vibe", manifest=manifest, private_root=tmp_path / "changed"
        )
    assert not (tmp_path / "changed").exists()
