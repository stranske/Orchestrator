"""The env rate reaches the artifact and a rate-only change is publishable immediately."""

import json

import pytest

import route_weights_export as export


def test_published_rate_follows_env_override(tmp_path, monkeypatch):
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    for raw, expected, source in [("0.20", 0.20, "env"), (None, 0.05, "default")]:
        if raw is None:
            monkeypatch.delenv("ORCH_EXPLORATION_RATE", raising=False)
        else:
            monkeypatch.setenv("ORCH_EXPLORATION_RATE", raw)
        document = export.build_document(database, now=1_792_195_200, include_capacity=False)
        assert export.write_document(target, document)
        published = json.loads(target.read_text())["exploration"]
        assert published["rate"] == expected
        assert published["rate_source"] == source
        assert not export.write_document(target, document)


def test_rate_only_change_is_semantic_and_env_is_clamped(tmp_path, monkeypatch):
    target = tmp_path / "export.json"
    assert export.write_document(target, {"exploration": {"rate": 0.05}})
    assert export.write_document(target, {"exploration": {"rate": 0.20}})
    for raw, expected, source in [
        ("-1", 0.0, "env"),
        ("2", 1.0, "env"),
        ("garbage", 0.05, "default"),
    ]:
        monkeypatch.setenv("ORCH_EXPLORATION_RATE", raw)
        document = export.build_document(tmp_path / "absent.db", now=100, include_capacity=False)
        assert document["exploration"]["rate"] == expected
        assert document["exploration"]["rate_source"] == source


@pytest.mark.parametrize("week", [2, 3], ids=["epsilon-greedy", "thompson-hybrid"])
def test_challengers_exclude_hosted_unrunnable_agents(monkeypatch, week):
    by_task = {
        task_type: [
            {
                "agent": agent,
                "posterior": posterior,
                "score": posterior,
                "n_obs": n_obs,
                "success_rate": posterior,
            }
            for agent, posterior, n_obs in [
                ("gemini", 0.99, 1),
                ("codex", 0.8, 50),
                ("cursor", 0.7, 50),
            ]
        ]
        for task_type in export.CONSUMER_TASK_TYPES
    }
    # Gemini has the best posterior but insufficient evidence for exploitation.
    task_types = {
        task_type: {"ranking": export._ranking(rows, export.DEFAULT_MIN_OBSERVATIONS)}
        for task_type, rows in by_task.items()
    }
    block = export._exploration_block(2, by_task, task_types, week * export.WEEK_SECONDS)
    assert set(block["challengers"]) == set(export.CONSUMER_TASK_TYPES)
    assert "gemini" not in block["challengers"].values()
    assert all(challenger in {"cursor", "codex"} for challenger in block["challengers"].values())

    # With only one runnable seat, there is no non-winner to explore.
    monkeypatch.setattr(export.router, "KEEPALIVE_AGENTS", {"codex", "gemini"})
    block = export._exploration_block(2, by_task, task_types, week * export.WEEK_SECONDS)
    assert all(challenger is None for challenger in block["challengers"].values())
