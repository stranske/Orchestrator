"""Lane repetition is an FYI measurement; unknown journals never mean a drained lane."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import switch_review

NOW = 1_800_000_000
TARGET = "stranske/Manager-Mosaic#1402"
ERROR = "repair dispatch: provision failed: cannot inspect holder"


def _roots(tmp_path):
    roots = {lane: tmp_path / lane for lane in ("opener-core", "closer-core")}
    for root in roots.values():
        root.mkdir(exist_ok=True)
        (root / "rounds.jsonl").write_text("", encoding="utf-8")
    return roots


def _round(stamp, items):
    return {"ts": datetime.fromtimestamp(stamp, timezone.utc).isoformat(), "items": items}


def _write(root, rounds):
    (root / "rounds.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rounds), encoding="utf-8"
    )


def _repeated(count=21, *, action="error", reason=None, step=900, target=TARGET):
    item = {"target": target, "action": action}
    item["error" if reason is None else "reason"] = ERROR if reason is None else reason
    return [_round(NOW - (count - 1 - n) * step, [item.copy()]) for n in range(count)]


def test_repeated_identical_error_is_suspect(tmp_path):
    roots = _roots(tmp_path)
    rounds = _repeated()
    _write(roots["closer-core"], rounds)
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["status"] == "ok"
    assert report["suspect"] == 1
    assert report["rows"] == [
        {
            "lane": "closer-core",
            "target": TARGET,
            "action": "error",
            "reason": ERROR,
            "rounds": 21,
            "since": rounds[0]["ts"],
        }
    ]
    text = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert "stuck lane targets: 1 SUSPECT" in text
    assert TARGET in text and ERROR in text and "21 rounds" in text


def test_changing_reason_and_holds_are_not_suspect(tmp_path):
    roots = _roots(tmp_path)
    rounds = []
    for n in range(21):
        items = [
            {"target": "o/r#1", "action": "wait", "reason": f"round {n}"},
            {"target": "o/r#2", "action": "already-parked", "reason": "paused"},
            {"target": "o/r#3", "action": "complete"},
            {"target": "o/r#4", "action": "skip", "reason": "held: agents:paused"},
            {"target": "o/r#5", "action": "skip-held", "reason": "held: owner"},
        ]
        rounds.append(_round(NOW - (20 - n) * 900, items))
    for root in roots.values():
        _write(root, rounds)
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["status"] == "ok"
    assert report["rows"] == [] and report["suspect"] == 0
    assert report["rounds_read"] == {"opener-core": 21, "closer-core": 21}


def test_fully_drained_prints_none_and_unreadable_prints_unknown(tmp_path):
    roots = _roots(tmp_path)
    _write(roots["opener-core"], _repeated(3))
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    text = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert "stuck lane targets: none (opener 3 rounds, closer 0 rounds read)" in text
    journal = roots["closer-core"] / "rounds.jsonl"
    journal.unlink()
    journal.mkdir()  # portable unreadability, even when tests run as root
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    text = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert report["status"] == "unknown" and report["measurement"]
    assert report["rounds_read"]["closer-core"] is None
    assert "stuck lane targets: unknown" in text
    assert "stuck lane targets: none" not in text


def test_count_and_elapsed_span_must_both_reach_threshold(tmp_path):
    roots = _roots(tmp_path)
    for count, step, expected in ((5, 1800, 0), (6, 719, 0), (6, 720, 1)):
        _write(roots["opener-core"], _repeated(count, step=step))
        report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
        assert report["suspect"] == expected, (count, step, report)


def test_changed_action_or_reason_resets_the_newest_run(tmp_path):
    roots = _roots(tmp_path)
    for changed in ({"action": "repair"}, {"error": "a different failure"}):
        rounds = _repeated()
        rounds[-1]["items"][0].update(changed)
        _write(roots["closer-core"], rounds)
        report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
        assert report["suspect"] == 0
    # The newest run counts only the matching suffix, not an older matching run.
    rounds = _repeated()
    rounds[-7]["items"][0]["error"] = "temporary progress"
    _write(roots["closer-core"], rounds)
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["rows"][0]["rounds"] == 6
    assert report["rows"][0]["since"] == rounds[-6]["ts"]


def test_missing_target_round_breaks_or_clears_the_streak(tmp_path):
    roots = _roots(tmp_path)
    for missing_at in (-2, -1):
        rounds = _repeated()
        rounds[missing_at]["items"] = []
        _write(roots["closer-core"], rounds)
        assert switch_review.stuck_lane_targets(now=NOW, roots=roots)["suspect"] == 0


def test_only_last_200_physical_lines_are_measured(tmp_path):
    roots = _roots(tmp_path)
    _write(roots["closer-core"], _repeated(250))
    journal = roots["closer-core"] / "rounds.jsonl"
    recent = journal.read_bytes()
    # Neither invalid JSON nor invalid UTF-8 outside the retained window matters.
    for old_line in (b"invalid old line\n", b"\xff invalid old line\n"):
        journal.write_bytes(old_line + recent)
        report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
        assert report["status"] == "ok"
        assert report["rows"][0]["rounds"] == 200
        assert report["rows"][0]["since"] == _round(NOW - 199 * 900, [])["ts"]
        assert report["rounds_read"]["closer-core"] == 200


def test_corrupt_or_missing_journal_is_unknown_with_known_suspects_retained(tmp_path):
    roots = _roots(tmp_path)
    _write(roots["opener-core"], _repeated())
    bad_rows = (
        "not-json\n",
        '{"items": []}\n',
        json.dumps({"ts": "invalid", "items": []}),
        json.dumps({"ts": "2026-01-01T00:00:00", "items": []}),
        json.dumps(_round(NOW, {})),
        json.dumps(_round(NOW, [None])),
        json.dumps(_round(NOW, [{"target": TARGET, "action": "error", "error": 1}])),
        json.dumps(_round(NOW, [{"target": TARGET}])),
        json.dumps(_round(NOW, [{"target": "", "action": "error"}])),
    )
    journal = roots["closer-core"] / "rounds.jsonl"
    for text in bad_rows:
        journal.write_text(text, encoding="utf-8")
        report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
        assert report["status"] == "unknown", text
        assert "closer-core" in report["measurement"]
        assert report["suspect"] == 1
        rendered = "\n".join(switch_review.format_stuck_lane_targets(report))
        assert "unknown" in rendered and "1 SUSPECT" in rendered
        assert "stuck lane targets: none" not in rendered
    journal.write_bytes(b"\xff")
    assert switch_review.stuck_lane_targets(now=NOW, roots=roots)["status"] == "unknown"
    journal.unlink()
    assert switch_review.stuck_lane_targets(now=NOW, roots=roots)["status"] == "unknown"


def test_default_roots_follow_state_environment(tmp_path):
    _roots(tmp_path)
    _write(tmp_path / "opener-core", _repeated())
    with patch.dict(os.environ, ORCH_STATE_DIR=str(tmp_path)):
        with patch.object(switch_review.time, "time", return_value=NOW):
            assert switch_review.stuck_lane_targets()["rows"][0]["lane"] == "opener-core"


def test_future_round_does_not_supply_elapsed_time(tmp_path):
    roots = _roots(tmp_path)
    rounds = _repeated(6, step=60)
    rounds.append(_round(NOW + 86400, rounds[-1]["items"]))
    _write(roots["opener-core"], rounds)
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["suspect"] == 0 and report["rounds_read"]["opener-core"] == 6


def test_out_of_order_rounds_are_unknown(tmp_path):
    roots = _roots(tmp_path)
    _write(roots["opener-core"], list(reversed(_repeated())))
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["status"] == "unknown" and "out of order" in report["measurement"]


def test_multiple_items_count_once_per_round_and_last_decision_wins(tmp_path):
    roots = _roots(tmp_path)
    rounds = _repeated()
    for row in rounds:
        row["items"] *= 3
    _write(roots["opener-core"], rounds)
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["rows"][0]["rounds"] == 21
    rounds[-1]["items"].append({"target": TARGET, "action": "complete"})
    _write(roots["opener-core"], rounds)
    assert switch_review.stuck_lane_targets(now=NOW, roots=roots)["suspect"] == 0


def test_skip_classification_follows_live_hold_policy(tmp_path):
    roots = _roots(tmp_path)
    for action, expected in (("skip", 1), ("skip-held", 0), ("wait", 1)):
        _write(roots["opener-core"], _repeated(action=action, reason="experiment lock busy"))
        assert switch_review.stuck_lane_targets(now=NOW, roots=roots)["suspect"] == expected


def test_render_limits_to_five_rows_and_keeps_lanes_separate(tmp_path):
    roots = _roots(tmp_path)
    rounds = _repeated()
    for row in rounds:
        row["items"] = [{"target": f"o/r#{n}", "action": "error", "error": ERROR} for n in range(6)]
    for root in roots.values():
        _write(root, rounds)
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["suspect"] == 12
    text = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert "stuck lane targets: 12 SUSPECT" in text
    assert text.count("21 rounds") == 5
    assert {row["lane"] for row in report["rows"]} == {"opener-core", "closer-core"}


def test_incomplete_root_configuration_does_not_read_live_state(tmp_path):
    roots = _roots(tmp_path)
    del roots["closer-core"]
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["status"] == "unknown"
    assert "no journal root configured for closer-core" in report["measurement"]


def _indexed_round(index, items, *, step=900):
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


def test_repeated_identical_error_is_suspect_from_state(journals):
    error = "repair dispatch: provision failed: cannot inspect holder"
    rounds = [
        _indexed_round(i, [{"target": TARGET, "action": "error", "error": error}])
        for i in range(21)
    ]
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


def test_changing_reason_and_holds_are_not_suspect_from_state(journals):
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
        rounds.append(_indexed_round(i, items))
    _journal(journals, "opener-core", rounds)

    report = switch_review.stuck_lane_targets(now=NOW)
    assert report["status"] == "ok"
    assert report["rows"] == []
    assert report["suspect"] == 0


def test_fully_drained_prints_none_and_unreadable_prints_unknown_from_state(journals):
    _journal(journals, "opener-core", [_indexed_round(0, [])])
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
            _indexed_round(
                i, [{"target": TARGET, "action": "skip", "reason": "experiment lock"}], step=step
            )
            for i in range(count)
        ],
    )
    assert switch_review.stuck_lane_targets(now=NOW)["suspect"] == expected


@pytest.mark.parametrize("interruption", ["reason", "action", "absent"])
def test_only_newest_consecutive_run_counts(journals, interruption):
    item = {"target": TARGET, "action": "error", "error": "unchanged"}
    rounds = [_indexed_round(i, [item]) for i in range(10)]
    changed = (
        {**item, "reason": "new reason"}
        if interruption == "reason"
        else {**item, "action": "retry"}
    )
    rounds.append(_indexed_round(10, [] if interruption == "absent" else [changed]))
    rounds.extend(_indexed_round(i, [item]) for i in range(11, 16))
    _journal(journals, "closer-core", rounds)
    assert switch_review.stuck_lane_targets(now=NOW)["suspect"] == 0


def test_tail_limit_and_duplicate_items_count_rounds_once(journals):
    item = {"target": TARGET, "action": "error", "error": "same"}
    rounds = [_indexed_round(i, [item, item], step=60) for i in range(221)]
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
        *[
            json.dumps(
                {
                    "ts": "2026-01-01T00:00:00Z",
                    "items": [{"target": TARGET, "action": "error", field: value}],
                }
            )
            for field in ("reason", "error")
            for value in (0, False, [], {})
        ],
    ],
)
def test_malformed_journal_is_unknown_but_other_lane_is_reported(journals, bad):
    item = {"target": TARGET, "action": "error", "reason": "same"}
    _journal(journals, "opener-core", [_indexed_round(i, [item]) for i in range(6)])
    path = _journal(journals, "closer-core", [_indexed_round(i, [item]) for i in range(6)])
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
    _journal(journals / "override", "opener-core", [_indexed_round(i, items) for i in range(6)])
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    rendered = "\n".join(switch_review.format_stuck_lane_targets(report))
    assert "stuck lane targets: 7 SUSPECT" in rendered
    assert rendered.count("  opener-core o/r#") == 5


def test_future_rounds_do_not_extend_a_run(journals):
    item = {"target": TARGET, "action": "error", "reason": "same"}
    rounds = [_indexed_round(i, [item], step=720) for i in range(6)]
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
    _journal(journals, "closer-core", [_indexed_round(i, [item]) for i in range(6)])
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
            _indexed_round(
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
            _indexed_round(
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
    rounds = [_indexed_round(i, [item]) for i in range(6)] + [_indexed_round(6, [])]
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
    _journal(journals, "opener-core", [_indexed_round(0, [])])
    item = {"target": TARGET, "action": "error", "reason": "same"}
    if closer_rounds == "clean":
        _journal(journals, "closer-core", [_indexed_round(0, [])])
    elif closer_rounds == "suspect":
        _journal(journals, "closer-core", [_indexed_round(i, [item]) for i in range(6)])
    else:
        (journals / "closer-core" / "rounds.jsonl").unlink(missing_ok=True)
    report = switch_review.review(
        now=NOW,
        sources={},
        env={"ORCH_VALUE_CHAIN_MONITOR": "0"},
        feature_goals_fn=lambda stamp: {},
    )
    assert ("Nothing due." in switch_review.format_report(report)) is nothing_due


@pytest.mark.parametrize("bad_target", [123, "", ["o/r#1"]])
def test_an_invalid_target_value_makes_the_lane_unknown(journals, bad_target):
    _journal(journals, "opener-core", [_indexed_round(0, [])])
    _journal(
        journals,
        "closer-core",
        [_indexed_round(0, [{"target": bad_target, "action": "error"}])],
    )
    report = switch_review.stuck_lane_targets(now=NOW)
    assert report["status"] == "unknown"
    assert report["rounds_read"]["closer-core"] is None
    assert "Nothing due" not in "\n".join(switch_review.format_stuck_lane_targets(report))


def test_an_item_without_a_target_is_skipped(journals):
    _journal(journals, "opener-core", [_indexed_round(0, [])])
    _journal(journals, "closer-core", [_indexed_round(0, [{"action": "summary"}])])
    assert switch_review.stuck_lane_targets(now=NOW)["status"] == "ok"
