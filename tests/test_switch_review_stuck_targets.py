"""Lane repetition is an FYI measurement; unknown journals never mean a drained lane."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

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
            "since": NOW - 20 * 900,
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
    assert report["rows"][0]["since"] == NOW - 5 * 900


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
    journal.write_text("invalid old line\n" + journal.read_text(), encoding="utf-8")
    report = switch_review.stuck_lane_targets(now=NOW, roots=roots)
    assert report["status"] == "ok"
    assert report["rows"][0]["rounds"] == 200
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


def test_skip_without_deliberate_hold_is_suspect(tmp_path):
    roots = _roots(tmp_path)
    for action in ("skip", "skip-held", "wait"):
        _write(roots["opener-core"], _repeated(action=action, reason="experiment lock busy"))
        assert switch_review.stuck_lane_targets(now=NOW, roots=roots)["suspect"] == 1


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
