"""Hermetic coverage for issue #1189's ranked calibration, registry, and CLI gaps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import claims
import features
import feedback
import human_calibration
import research_scheduler


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every mutable registry and claim under this test's temporary runtime."""
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_FEATURES_PATH", str(runtime / "features.json"))
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback.db")
    monkeypatch.setattr(features, "REG", runtime / "features.json")
    monkeypatch.setattr("capabilities.production_heartbeat", lambda *args, **kwargs: None)


def test_calibration_rejects_bad_anchors_and_handles_every_readiness_state() -> None:
    for kwargs, message in (
        ({"experiment_id": "", "implementer": "codex", "score": 4}, "experiment_id"),
        ({"experiment_id": "e", "implementer": "", "score": 4}, "implementer"),
        ({"experiment_id": "e", "implementer": "codex", "score": 11}, "score"),
        (
            {"experiment_id": "e", "implementer": "codex", "score": 4, "member_id": "other"},
            "member_id",
        ),
    ):
        with pytest.raises(human_calibration.CalibrationInputError, match=message):
            human_calibration.build_anchor_payload(**kwargs)

    rows = [
        (1, "ignored", None, None),
        (1, "bad", "{", None),
        (1, "exp:legacy", "7", "legacy"),
        {
            "ts": 2,
            "ref": "structured",
            "human_verdict": {
                "experiment_id": "exp",
                "implementer": "codex",
                "score": 8,
                "agent": "codex",
            },
            "note": "structured",
        },
        (3, "multi", json.dumps({"scores": {"cursor": 6, "invalid": 99}}), None),
    ]
    anchors = human_calibration.parse_human_anchors(rows)
    assert {(row["experiment_id"], row["implementer"]) for row in anchors} == {
        ("exp", "legacy"),
        ("exp", "codex"),
        ("multi", "cursor"),
    }

    assert human_calibration.compute([], [], generated_at=1)["status"] == "no_human_anchors"
    no_match = human_calibration.compute([], [(1, "exp:codex", "8", None)], generated_at=1)
    assert no_match["status"] == "no_matched_proxy_scores"
    same_proxy = human_calibration.compute(
        [(f"e{i}", "codex", "judge", 5, 1) for i in range(2)],
        [(1, f"e{i}:codex", "6", None) for i in range(2)],
        min_pairs=2,
        generated_at=1,
    )
    assert same_proxy["status"] == "insufficient_score_variance"
    ready = human_calibration.compute(
        [("e1", "codex", "judge", 2, 1), ("e2", "codex", "judge", 8, 1)],
        [(1, "e1:codex", "3", None), (1, "e2:codex", "9", None)],
        min_pairs=2,
        generated_at=1,
    )
    assert ready["ready"] is True
    assert human_calibration.calibrate_score(5, None) == 5


def test_calibration_cli_queue_record_and_summary_paths(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        human_calibration,
        "pending_queue",
        lambda **kwargs: {
            "items": [
                {
                    "experiment_id": "e",
                    "implementer": "codex",
                    "target": "",
                    "judge_scores": {"j": 7},
                    "judge_spread": 0,
                    "next_transition": "wait",
                }
            ],
            "pending_total": 1,
            "window_days": kwargs["window_days"],
            "anchored_pairs": 0,
        },
    )
    assert human_calibration.main(["--queue", "--window-days", "2", "--limit", "1"]) == 0
    assert "objective-anchor state" in capsys.readouterr().out
    assert (
        human_calibration.main(
            ["--record-anchor", "--experiment-id", "e", "--implementer", "codex", "--score", "7"]
        )
        == 0
    )
    assert "dry_run" in capsys.readouterr().out
    monkeypatch.setattr(
        human_calibration,
        "summarize",
        lambda **kwargs: {
            "status": "ready",
            "ready": True,
            "structured_anchor_count": 2,
            "matched_pair_count": 2,
            "window_days": 2,
            "recommendation": "use it",
        },
    )
    assert human_calibration.main(["--window-days", "2"]) == 0
    assert "status=ready" in capsys.readouterr().out


def test_feature_registry_cli_and_research_demo_are_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    registry = tmp_path / "features.json"
    for invalid, message in (
        ([], "object"),
        ({1: {}}, "named"),
        ({"bad": {"maturity": "broken"}}, "unsupported"),
        ({"bad": {"uses": [1]}}, "uses"),
        ({"bad": {"count": -1}}, "count"),
    ):
        with pytest.raises(ValueError, match=message):
            features._validate_registry(invalid)
    assert features._seed(initialize=False)["offload"]["first_seen"] == 0
    assert features._normalized_seed({"x": {"uses": ["one"], "first_seen": 1}}) == {
        "x": {"uses": ["one"], "count": 1}
    }
    first = features.record_use("new", "one", "problem", registry)
    second = features.record_use("new", "two", path=registry)
    features.record_use("new", "three", path=registry)
    assert first["maturity"] == "ad-hoc" and second["maturity"] == "reused"
    assert features.promotion_candidates(registry) == [
        {"name": "new", "count": 3, "problem": "problem"}
    ]
    features.mark_hardened("new", "new.py", registry)
    assert features.summary(registry)["counts_by_maturity"]["hardened"] >= 1
    assert features.promotion_candidates(registry) == []

    monkeypatch.setattr(features, "_capability_heartbeat", lambda: None)
    monkeypatch.setattr(
        features, "record_use", lambda *args, **kwargs: {"maturity": "reused", "count": 2}
    )
    assert features.main(["record", "--name", "x", "--where", "here", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "x"
    monkeypatch.setattr(features, "mark_hardened", lambda *args, **kwargs: None)
    assert features.main(["harden", "--name", "x", "--module", "x.py", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["maturity"] == "hardened"
    monkeypatch.setattr(
        features, "promotion_candidates", lambda: [{"name": "x", "count": 3, "problem": "p"}]
    )
    assert features.main(["candidates"]) == 0
    assert "used 3x" in capsys.readouterr().out
    monkeypatch.setattr(
        features,
        "summary",
        lambda: {
            "total": 1,
            "counts_by_maturity": {"ad-hoc": 0, "reused": 1, "hardened": 0},
            "promotion_candidates": [{"name": "x", "count": 3, "problem": "p"}],
        },
    )
    assert features.main(["summary"]) == 0
    assert "promotion_candidates=1" in capsys.readouterr().out
    monkeypatch.setattr(
        features,
        "load",
        lambda: {"x": {"maturity": "reused", "count": 2, "module": "x.py", "problem": "p"}},
    )
    assert features.main(["list"]) == 0
    assert "reused" in capsys.readouterr().out
    research_scheduler._demo()
    assert any(
        line.startswith("demo select_arms recruited=")
        for line in capsys.readouterr().out.splitlines()
    )


def test_claims_cli_routes_to_isolated_claim_directory(capsys) -> None:
    assert claims.main(["claim", "owner/repo#1", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["claimed"] is True
    assert claims.main(["release", "owner/repo#1", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["released"] is True
    assert claims.main(["reap"]) == 0
    assert json.loads(capsys.readouterr().out)["reaped"] == []
    assert claims.main([]) == 0
    assert json.loads(capsys.readouterr().out)["active_claims"] == {}
