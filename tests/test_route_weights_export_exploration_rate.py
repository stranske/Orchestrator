"""The env rate reaches the artifact and a rate-only change is publishable immediately."""

import json

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
