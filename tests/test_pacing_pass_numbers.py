import json

import capacity
import pacing
import pacing_usage

NOW = 1_800_000_000.0


def _snapshot(ts, pool, **pacing_fields):
    return {"event": "pacing_snapshot", "ts": ts, "agents": {pool: {"pacing": pacing_fields}}}


def _write(handoff, rows):
    handoff.mkdir(parents=True, exist_ok=True)
    (handoff / "capacity-ledger.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_usage_in_band_is_the_share_of_measured_pools(tmp_path):
    reserve = 0.15
    lo, hi = pacing.pass_bounds(reserve)
    end = NOW - 60
    rows = [
        _snapshot(
            end - 30,
            "claude",
            period_end=end,
            used_fraction=(lo + hi) / 2,
            effective_reserve=reserve,
            stale=False,
        ),
        _snapshot(
            end - 30,
            "codex",
            period_end=end,
            used_fraction=0.99,
            effective_reserve=reserve,
            stale=False,
        ),
    ]
    _write(tmp_path, rows)
    usage = pacing_usage.pass_numbers(tmp_path, NOW)["usage_in_target_band"]
    assert usage["value"] == 0.5
    assert "claude" in usage["reason"] and "codex" in usage["reason"]


def test_unmeasured_pass_numbers_are_none_never_zero(tmp_path):
    _write(tmp_path, [])
    result = pacing_usage.pass_numbers(tmp_path, NOW)
    assert result["usage_in_target_band"]["value"] is None
    assert result["quota_incidents_while_other_ok"]["value"] is None
    assert result["non_codex_share_at_least_double"]["value"] is None
    assert result["quota_incidents_while_other_ok"]["reason"].startswith("2: unmeasured")


def test_pacing_report_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "HANDOFF", tmp_path)
    _write(tmp_path, [])
    before = sorted(p.name for p in tmp_path.iterdir())
    data = capacity.pacing_report(NOW)
    assert set(data) == {
        "usage_in_target_band",
        "quota_incidents_while_other_ok",
        "non_codex_share_at_least_double",
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_text_report_lines_come_from_the_same_computation(tmp_path):
    _write(tmp_path, [])
    text = pacing_usage.report(tmp_path, now=NOW, current={}, previous={})
    for line in pacing_usage.period_results(tmp_path, [], NOW)["lines"]:
        assert line in text


def test_one_pool_in_band_and_one_unmeasured_reads_half(tmp_path):
    reserve = 0.15
    lo, hi = pacing.pass_bounds(reserve)
    end = NOW - 60
    _write(
        tmp_path,
        [
            _snapshot(
                end - 30,
                "claude",
                period_end=end,
                used_fraction=(lo + hi) / 2,
                effective_reserve=reserve,
                stale=False,
            )
        ],
    )
    usage = pacing_usage.pass_numbers(tmp_path, NOW)["usage_in_target_band"]
    assert usage["value"] == 0.5
    assert "codex unmeasured" in usage["reason"]
