import json

import pytest

import switch_review


def write_summary(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    path = tmp_path / "capability-program" / "profile-trial.json"
    path.parent.mkdir()
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "bad",
        {"profile_count": "bad"},
        {"instance_count": "bad"},
        {"profile_count": float("inf")},
        {"cost_tokens_by_profile": [1]},
        {"quality_by_profile": [1]},
        {"cost_tokens_by_profile": {"p": []}},
        {"cost_tokens_by_profile": {"p": 3}},
    ],
)
def test_malformed_profile_summary_does_not_crash_report(tmp_path, monkeypatch, payload):
    write_summary(tmp_path, monkeypatch, payload)
    assert switch_review.profile_trial_summary_line() is None


def test_profile_cost_reports_both_token_directions(tmp_path, monkeypatch):
    write_summary(
        tmp_path,
        monkeypatch,
        {
            "profile_count": 2,
            "instance_count": 6,
            "identity_verified": True,
            "cost_tokens_by_profile": {
                "b": {"tokens_in": 20, "tokens_out": 9},
                "a": {"tokens_in": 12, "tokens_out": 7},
            },
            "quality_by_profile": {"a": 0.8, "b": 0.9},
        },
    )
    assert switch_review.profile_trial_summary_line() == (
        "profile trial: profiles 2, instances 6, identity verified Y, "
        "quality 0.8/0.9, cost 12in+7out/20in+9out"
    )


def test_missing_profile_cost_fields_remain_unknown(tmp_path, monkeypatch):
    write_summary(tmp_path, monkeypatch, {"cost_tokens_by_profile": {"p": {}}})
    assert "cost n/ain+n/aout" in switch_review.profile_trial_summary_line()
