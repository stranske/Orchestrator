import json

import feedback
import judge_reliability


def _epochs(tmp_path, since="2026-10-16T00:00:00Z"):
    path = tmp_path / "model_epochs.json"
    path.write_text(json.dumps({"epochs": {"gemini": {"since": since}}}))
    return path


def test_floor_raises_the_window_start_for_a_listed_agent_only(tmp_path):
    path = _epochs(tmp_path)
    epoch = 1792108800  # 2026-10-16T00:00:00Z
    assert feedback.model_epoch_floor("gemini", 1000, path) == epoch
    assert feedback.model_epoch_floor("codex", 1000, path) == 1000
    assert feedback.model_epoch_floor("gemini", epoch + 5, path) == epoch + 5  # later window wins


def test_unreadable_or_malformed_file_leaves_learning_unchanged(tmp_path):
    assert feedback.model_epoch_floor("gemini", 1000, tmp_path / "missing.json") == 1000
    assert feedback.model_epoch_floor("gemini", 1000, _epochs(tmp_path, since="garbage")) == 1000


def test_shipped_config_names_argon_and_parses():
    assert feedback.model_epoch_floor("gemini", 0) > 0


def test_relearn_ignores_runs_before_the_model_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback, "MODEL_EPOCHS_PATH", _epochs(tmp_path))
    epoch = feedback.model_epoch_floor("gemini", 0)
    for run_id, ts in (("old-1", epoch - 86400), ("old-2", epoch - 3600), ("new-1", epoch + 3600)):
        feedback.record_run(run_id, "o/r#1", "implement", "gemini")
        with feedback._conn() as c:
            c.execute("UPDATE runs SET ts=? WHERE run_id=?", (ts, run_id))
        feedback.record_outcome(run_id, durability="durable", adjudicated_verdict="PASS")
    version = feedback.relearn({"implement": {"gemini": 0.5}}, window_days=3650)
    with feedback._conn() as c:
        n_obs = c.execute(
            "SELECT n_obs FROM route_weights WHERE version=? AND agent='gemini'", (version,)
        ).fetchone()[0]
    assert n_obs == 1  # only the run at or after the epoch counts; priors carry the rest
    monkeypatch.setattr(feedback, "MODEL_EPOCHS_PATH", tmp_path / "none.json")
    version = feedback.relearn({"implement": {"gemini": 0.5}}, window_days=3650)
    with feedback._conn() as c:
        assert c.execute(
            "SELECT n_obs FROM route_weights WHERE version=? AND agent='gemini'", (version,)
        ).fetchone()[0] == 3


def test_judge_reliability_drops_evaluations_before_the_epoch(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback, "MODEL_EPOCHS_PATH", _epochs(tmp_path))
    epoch = feedback.model_epoch_floor("gemini", 0)
    now = int(__import__("time").time())
    with feedback._conn() as c:
        for exp, ts in (("e-old", epoch - 3600), ("e-new", max(now - 60, epoch + 60))):
            c.execute(
                "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
                (exp, "claude", "gemini", 7.0, None, None, ts),
            )
    report = judge_reliability.summarize(window_days=3650)
    seen = report["judges"].get("gemini", {})
    assert seen.get("raw_score_count") == 1, seen
