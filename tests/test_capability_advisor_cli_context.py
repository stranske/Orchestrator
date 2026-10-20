"""Hermetic CLI-context coverage for the capability advisor."""

from __future__ import annotations

import json

import capability_advisor as advisor


def test_cli_preserves_explicit_context_and_overrides_its_pr_with_the_cli_value(
    monkeypatch, capsys
):
    """A CLI PR is the authoritative PR fact, while the rest of context remains intact."""
    seen = {}

    def fake_advise(task, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        return {"useful": False, "capabilities": []}

    monkeypatch.setattr(advisor, "advise", fake_advise)

    assert (
        advisor.main(
            [
                "--repository",
                "stranske/Orchestrator",
                "--lane",
                "closer",
                "--surface",
                "closer-lane",
                "--repo-path",
                "/tmp/consumer",
                "--context",
                json.dumps({"labels": ["agent:retry"], "pr": 41}),
                "--pr",
                "42",
                "--json",
                "review",
                "this",
                "PR",
            ]
        )
        == 0
    )

    assert seen == {
        "task": "review this PR",
        "repository": "stranske/Orchestrator",
        "lane": "closer",
        "surface": "closer-lane",
        "repo_path": "/tmp/consumer",
        "context": {"labels": ["agent:retry"], "pr": 42},
    }
    assert json.loads(capsys.readouterr().out) == {"useful": False, "capabilities": []}
