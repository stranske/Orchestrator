"""Hermetic regression probes for issue 1173's ranked coverage targets."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import execution_profiles as profiles
import issue_quality
import keepalive_shadow as shadow


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every exercised helper receives a fake transport and temp-only storage."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_profile_identity_rejects_each_unsafe_identity_shape() -> None:
    profile = dict(next(iter(profiles.PROFILE_REGISTRY.values())))
    profile["transport_support"] = ["offload", "local"]
    profile["capacity_pool_ids"] = ["normal", "priority"]

    identity = profiles.profile_identity(profile)
    assert identity["transport_support"] == ["local", "offload"]
    assert identity["capacity_pool_ids"] == ["normal", "priority"]

    cases = [
        (None, "must be an object"),
        ({**profile, "agent": " "}, "missing or invalid"),
        ({**profile, "agent": "aider"}, "not an eligible"),
        ({**profile, "lifecycle_status": "retired"}, "not active"),
        ({**profile, "permission_mode": "unsafe"}, "unsupported"),
        ({**profile, "transport_support": ["offload", "offload"]}, "missing or invalid"),
        ({**profile, "transport_support": ["local"]}, "no supported offload"),
        ({**profile, "requires_explicit_selection": 1}, "flag is invalid"),
    ]
    for candidate, message in cases:
        with pytest.raises(ValueError, match=message):
            profiles.profile_identity(candidate)


def test_issue_quality_link_and_payload_helpers_preserve_only_valid_issue_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int]] = []

    def fake_json(args: list[str], *, timeout: int) -> object:
        calls.append((args, timeout))
        if args[1:3] == ["pr", "view"]:
            return {
                "closingIssuesReferences": [
                    {"number": "3"},
                    {"number": 3},
                    {"number": None},
                    {"number": "bad"},
                    None,
                ]
            }
        return {"number": 3, "title": "fixture", "body": "body", "url": "https://example"}

    monkeypatch.setattr(issue_quality.keepalive_outcomes, "_run_json", fake_json)
    assert issue_quality.fetch_linked_issue_numbers("owner/repo", 7) == [3]
    assert issue_quality.fetch_issue_body("owner/repo", 3) == {
        "number": 3,
        "title": "fixture",
        "body": "body",
        "url": "https://example",
    }
    assert issue_quality._coerce_issue_payload("body") == ("body", None, None)
    assert issue_quality._coerce_issue_payload({"body": 7, "title": 8, "url": 9}) == ("7", 8, 9)
    assert issue_quality._coerce_issue_payload({"title": "missing"}) is None
    assert issue_quality._coerce_issue_payload([]) is None
    assert all(timeout == issue_quality.GH_TIMEOUT_SECONDS for _, timeout in calls)


def test_shadow_live_and_backfill_failures_are_hermetic_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="owner/repo#N"):
        shadow.gather_signals("owner/repo", runner=lambda *_a, **_k: None)

    failed = SimpleNamespace(returncode=1, stdout="", stderr="not found")
    with pytest.raises(RuntimeError, match="not found"):
        shadow.gather_signals("owner/repo#7", runner=lambda *_a, **_k: failed)

    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout=json.dumps({"state": "MERGED", "labels": []})),
            SimpleNamespace(returncode=0, stdout='<!-- keepalive-state:v1 {"iteration":2} -->'),
        ]
    )
    signals = shadow.gather_signals("owner/repo#7", runner=lambda *_a, **_k: next(responses))
    assert signals["pr_state"] == "merged" and signals["iteration"] == 2
    assert shadow.extract_state_payload("<!-- keepalive-state:v1 malformed -->") is None
    assert (
        shadow.normalize_signals("o/r#1", {}, pr_state="closed", labels=[])["outcome"]
        == "closed_unmerged"
    )

    monkeypatch.setitem(
        sys.modules,
        "durability_sweep",
        SimpleNamespace(
            classify_durability=lambda _row, pr, **_kw: {
                "durability": "durable" if pr["repo"] else None
            }
        ),
    )
    assert (
        shadow._durability_outcome(
            "owner/repo#7",
            runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        )
        == "durable"
    )
    assert (
        shadow._durability_outcome(
            "owner/repo#7",
            runner=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="denied"),
        )
        is None
    )

    monkeypatch.setitem(
        sys.modules,
        "gh_capacity",
        SimpleNamespace(throttle_if_enabled=lambda _resource: (_ for _ in ()).throw(OSError())),
    )
    # _gh_throttle itself is intentionally fail-open for an optional pacing dependency.
    shadow._gh_throttle("core")

    corpus = tmp_path / "shadow.jsonl"
    corpus.write_text("\nnot-json\n" + json.dumps({"source": "backfill", "target": "o/r#1"}) + "\n")
    assert shadow._backfilled_targets(corpus) == {"o/r#1"}

    result = shadow.backfill(
        since="2026-01-01",
        until="2026-01-02",
        runner=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="search denied"),
        corpus_path=corpus,
    )
    assert result == {"since": "2026-01-01", "error": "search denied", "recorded": 0}
