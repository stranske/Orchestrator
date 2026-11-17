"""Repeated lane decisions are FYI; absent evidence must never print a drain."""

import json
from datetime import datetime, timezone

import pytest

import switch_review

NOW = 1_800_000_000
TARGET = "stranske/Manager-Mosaic#1402"


def _round(index, items, *, step=900):
    return {
        "ts": datetime.fromtimestamp(NOW - 86400 + index * step, timezone.utc).isoformat(),
        "items": items,
    }


def _journal(root, lane, rounds):
    path = root / lane / "rounds.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rounds), encoding="utf-8")
    return path


@pytest.fixture
def journals(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    for lane in ("opener-core", "closer-core"):
        _journal(tmp_path, lane, [])
    return tmp_path


def test_repeated_identical_error_is_suspect(journals):
    error = "repair dispatch: provision failed: cannot inspect holder"
    rounds = [_round(i, [{"target": TARGET, "action": "error", "error": error}]) for i in range(21)]
    _journal(journals, "closer-core", rounds)

    report = switch_review.stuck_lane_targets(now=NOW)

    assert report["status"] == "ok"
    assert report["suspect"] == 1
    assert report["rows"] == [
        {
            "lane": "closer-core",
            "target": TARGET,
            "action": "error",
            "reason": error,
            "rounds": 21,
            "since": rounds[0]["ts"],
        }
    ]


def test_changing_reason_and_holds_are_not_suspect(journals):
    rounds = []
    for i in range(21):
        items = [{"target": TARGET, "action": "error", "reason": f"round {i}"}]
        for action, reason in (
            ("already-parked", "parked"),
            ("complete", "done"),
            ("skip", "held: agents:paused"),
            ("skip-held", "held: owner"),
        ):
            items.append({"target": f"o/r#{action}", "action": action, "reason": reason})
        rounds.append(_round(i, items))
    _journal(journals, "opener-core", rounds)

    report = switch_review.stuck_lane_targets(now=NOW)
    assert report["status"] == "ok"
    assert report["rows"] == []
    assert report["suspect"] == 0


def test_fully_drained_prints_none_and_unreadable_prints_unknown(journals):
    _journal(journals, "opener-core", [_round(0, [])])
    report = switch_review.stuck_lane_targets(now=NOW)
    assert "stuck lane targets: none (opener 1 rounds, closer 0 rounds read)" in (
        "\n".join(switch_review.format_stuck_lane_targets(report))
    )
    (journals / "closer-core" / "rounds.jsonl").unlink()
    report = switch_review.stuck_lane_targets(now=NOW)
    rendered = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert report["status"] == "unknown"
    assert report["rounds_read"]["closer-core"] is None
    assert "closer-core/rounds.jsonl" in report["measurement"]
    assert "stuck lane targets: unknown" in rendered
    assert "stuck lane targets: none" not in rendered


@pytest.mark.parametrize("count,step,expected", [(5, 1800, 0), (6, 719, 0), (6, 720, 1)])
def test_both_round_and_span_thresholds(journals, count, step, expected):
    _journal(
        journals,
        "opener-core",
        [
            _round(
                i, [{"target": TARGET, "action": "skip", "reason": "experiment lock"}], step=step
            )
            for i in range(count)
        ],
    )
    assert switch_review.stuck_lane_targets(now=NOW)["suspect"] == expected


@pytest.mark.parametrize("interruption", ["reason", "action", "absent"])
def test_only_newest_consecutive_run_counts(journals, interruption):
    item = {"target": TARGET, "action": "error", "error": "unchanged"}
    rounds = [_round(i, [item]) for i in range(10)]
    changed = (
        {**item, "reason": "new reason"}
        if interruption == "reason"
        else {**item, "action": "retry"}
    )
    rounds.append(_round(10, [] if interruption == "absent" else [changed]))
    rounds.extend(_round(i, [item]) for i in range(11, 16))
    _journal(journals, "closer-core", rounds)
    assert switch_review.stuck_lane_targets(now=NOW)["suspect"] == 0


def test_tail_limit_and_duplicate_items_count_rounds_once(journals):
    item = {"target": TARGET, "action": "error", "error": "same"}
    rounds = [_round(i, [item, item], step=60) for i in range(221)]
    path = _journal(journals, "closer-core", rounds)
    # A malformed entry outside the bounded tail must not poison current evidence.
    path.write_text("bad old line\n" + path.read_text(), encoding="utf-8")
    report = switch_review.stuck_lane_targets(now=NOW)
    assert report["status"] == "ok"
    assert report["rounds_read"]["closer-core"] == 200
    assert report["rows"][0]["rounds"] == 200
    assert report["rows"][0]["since"] == rounds[21]["ts"]


@pytest.mark.parametrize(
    "bad",
    [
        "bad json",
        "{}",
        '{"ts":"bad","items":[]}',
        '{"ts":"2026-01-01T00:00:00Z","items":{}}',
    ],
)
def test_malformed_journal_is_unknown_but_other_lane_is_reported(journals, bad):
    item = {"target": TARGET, "action": "error", "reason": "same"}
    _journal(journals, "opener-core", [_round(i, [item]) for i in range(6)])
    path = _journal(journals, "closer-core", [_round(i, [item]) for i in range(6)])
    with path.open("a") as stream:
        stream.write(bad + "\n")
    report = switch_review.stuck_lane_targets(now=NOW)
    assert report["status"] == "unknown"
    assert report["suspect"] == 1
    assert report["rows"][0]["lane"] == "opener-core"
    assert report["rounds_read"]["closer-core"] is None
    rendered = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert "unknown" in rendered and "1 SUSPECT" in rendered


def test_roots_override_and_top_five_rendering(journals):
    roots = {lane: journals / "override" / lane for lane in ("opener-core", "closer-core")}
    _journal(journals / "override", "closer-core", [])
    items = [{"target": f"o/r#{i}", "action": "error", "reason": "same"} for i in range(7)]
    _journal(journals / "override", "opener-core", [_round(i, items) for i in range(6)])
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    rendered = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert "stuck lane targets: 7 SUSPECT" in rendered
    assert rendered.count("  opener-core o/r#") == 5


def test_future_rounds_do_not_extend_a_run(journals):
    item = {"target": TARGET, "action": "error", "reason": "same"}
    rounds = [_round(i, [item], step=720) for i in range(6)]
    _journal(journals, "closer-core", rounds)
    assert switch_review.stuck_lane_targets(now=NOW - 86400 + 5 * 720 - 1)["suspect"] == 0


def test_weekly_review_includes_and_formats_section(journals, monkeypatch):
    for name in (
        "_capability_heartbeat",
        "stale_runners",
        "adversarial_shape_population",
        "mirror_drift",
        "fleet_gates",
        "firing_regressions",
        "_exploration_gate",
        "gate_expiry",
        "capacity_shed",
        "worktree_git_refusals",
    ):
        monkeypatch.setattr(switch_review, name, lambda *a, **kw: {})
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **kw: {"held_off": [], "on_but_idle": []}
    )
    item = {"target": TARGET, "action": "error", "reason": "same"}
    _journal(journals, "closer-core", [_round(i, [item]) for i in range(6)])
    report = switch_review.review(
        now=NOW,
        sources={},
        env={"ORCH_VALUE_CHAIN_MONITOR": "0"},
        feature_goals_fn=lambda stamp: {},
    )
    assert report["stuck_lane_targets"]["suspect"] == 1
    assert report["raise_count"] == 0
    rendered = switch_review.format_report(report)
    assert "stuck lane targets: 1 SUSPECT" in rendered
    assert TARGET in rendered
    assert "Nothing due." not in rendered


def test_live_hold_wording_is_not_suspect_but_a_repeating_failure_is(journals):
    """2026-11-17 live journals: 44 of 45 first-run suspects were each lane's own deliberate holds."""
    opener, closer = [], []
    for i in range(21):
        opener.append(
            _round(
                i,
                [
                    {"target": "o/r#1", "action": "already-pr_linked"},
                    {
                        "target": "o/r#2",
                        "action": "format-unfixed",
                        "reason": "rewrite did not pass the validator; next round",
                    },
                ],
            )
        )
        closer.append(
            _round(
                i,
                [
                    {"target": "o/r#3", "action": "probe"},
                    {
                        "target": "o/r#4",
                        "action": "skip-held",
                        "reason": "hold label agents:paused",
                    },
                    {
                        "target": "o/r#5",
                        "action": "wait-unmerged",
                        "reason": "dependency-bot PR opened under 3 days ago: its own automerge has it",
                    },
                ],
            )
        )
    _journal(journals, "opener-core", opener)
    _journal(journals, "closer-core", closer)
    report = switch_review.stuck_lane_targets(now=NOW)
    assert [(r["lane"], r["target"]) for r in report["rows"]] == [("opener-core", "o/r#2")]


def test_a_run_broken_by_the_targets_absence_is_not_suspect(journals):
    item = {"target": TARGET, "action": "error", "reason": "same"}
    rounds = [_round(i, [item]) for i in range(6)] + [_round(6, [])]
    _journal(journals, "closer-core", rounds)
    assert switch_review.stuck_lane_targets(now=NOW)["suspect"] == 0


@pytest.mark.parametrize(
    "closer_rounds,nothing_due", [("clean", True), ("suspect", False), ("missing", False)]
)
def test_nothing_due_depends_on_the_stuck_target_section(
    journals, monkeypatch, closer_rounds, nothing_due
):
    for name in (
        "_capability_heartbeat",
        "stale_runners",
        "adversarial_shape_population",
        "fleet_gates",
        "firing_regressions",
        "_exploration_gate",
        "worktree_git_refusals",
    ):
        monkeypatch.setattr(switch_review, name, lambda *a, **kw: {})
    monkeypatch.setattr(switch_review, "mirror_drift", lambda *a, **kw: {"status": "ok"})
    monkeypatch.setattr(
        switch_review,
        "gate_expiry",
        lambda *a, **kw: {"status": "ok", "expiring": [], "lapsed": [], "window_days": 14},
    )
    monkeypatch.setattr(
        switch_review,
        "capacity_shed",
        lambda *a, **kw: {
            "status": "ok",
            "suspect": 0,
            "markers": [],
            "dir": "shed",
            "horizon_days": 14,
        },
    )
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **kw: {"held_off": [], "on_but_idle": []}
    )
    _journal(journals, "opener-core", [_round(0, [])])
    item = {"target": TARGET, "action": "error", "reason": "same"}
    if closer_rounds == "clean":
        _journal(journals, "closer-core", [_round(0, [])])
    elif closer_rounds == "suspect":
        _journal(journals, "closer-core", [_round(i, [item]) for i in range(6)])
    else:
        (journals / "closer-core" / "rounds.jsonl").unlink(missing_ok=True)
    report = switch_review.review(
        now=NOW,
        sources={},
        env={"ORCH_VALUE_CHAIN_MONITOR": "0"},
        feature_goals_fn=lambda stamp: {},
    )
    assert ("Nothing due." in switch_review.format_report(report)) is nothing_due
