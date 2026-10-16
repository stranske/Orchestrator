import sqlite3
from pathlib import Path
from unittest.mock import patch

import capacity
import route_weights_export as export


def _snap(**kwargs):
    return export._fixture_capacity_snapshot(**kwargs)


def test_export_filters_threshold_reserve_and_unknown_task_types(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)

    document = export.build_document(database, minimum=20, capacity_snapshot=_snap())

    ranking = document["task_types"]["implement"]["ranking"]
    assert [row["agent"] for row in ranking] == ["codex"]
    assert all(row["n_obs"] >= 20 for row in ranking)
    assert document["task_types"]["implement"]["evidence_ok"] is True
    assert document["task_types"]["review"]["evidence_ok"] is False
    assert document["reserve"]["implement"][0]["agent"] == "claude"
    assert "unknown" not in document["task_types"]


def test_write_document_detects_an_unchanged_semantic_export(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    snap = _snap()

    assert export.write_document(target, export.build_document(database, capacity_snapshot=snap))
    assert (
        export.write_document(target, export.build_document(database, capacity_snapshot=snap))
        is False
    )


def test_capacity_state_change_triggers_publication(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    assert export.write_document(target, export.build_document(database, capacity_snapshot=_snap()))
    shed = _snap(states={"codex": capacity.SHED})
    assert export.write_document(target, export.build_document(database, capacity_snapshot=shed))


def test_capacity_window_or_observed_at_change_triggers_publication(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    assert export.write_document(
        target, export.build_document(database, capacity_snapshot=_snap(generated_at=100))
    )
    assert export.write_document(
        target, export.build_document(database, capacity_snapshot=_snap(generated_at=200))
    )
    window_flip = _snap(generated_at=200)
    window_flip["agents"]["codex"]["window"] = "monthly"
    assert export.write_document(
        target, export.build_document(database, capacity_snapshot=window_flip)
    )


def test_capacity_block_present_with_one_entry_per_configured_agent(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, capacity_snapshot=_snap())
    assert document["schema"] == export.SCHEMA
    assert "capacity" in document, "capacity block must be present"
    assert set(document["capacity"]) == set(capacity.AGENTS)


def test_capacity_states_copied_verbatim(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    states = {
        "codex": capacity.OK,
        "claude": capacity.WARN,
        "cursor": capacity.SHED,
        "gemini": capacity.UNKNOWN,
    }
    snap = _snap(states=states, generated_at=42)
    document = export.build_document(database, capacity_snapshot=snap)
    for agent, state in states.items():
        entry = document["capacity"][agent]
        assert entry["state"] == state
        assert entry["window"] == capacity.AGENTS[agent]["window"]
        assert entry["observed_at"] == 42


def test_v1_keys_unchanged_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, minimum=20, capacity_snapshot=_snap())
    assert export.V1_DOCUMENT_KEYS.issubset(document)
    # Additive capacity must not reshape the v1 ranking contract.
    ranking = document["task_types"]["implement"]["ranking"]
    assert ranking == [
        {
            "agent": "codex",
            "posterior": 0.9,
            "n_obs": 25,
            "success_rate": 0.88,
        }
    ]
    assert document["reserve"]["implement"][0]["agent"] == "claude"
    assert "exploration" in document


def test_shed_pool_flips_state_within_one_export(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    ok_doc = export.build_document(database, capacity_snapshot=_snap())
    assert ok_doc["capacity"]["codex"]["state"] == capacity.OK
    shed_doc = export.build_document(
        database, capacity_snapshot=_snap(states={"codex": capacity.SHED})
    )
    assert shed_doc["capacity"]["codex"]["state"] == capacity.SHED
    # Rankings and other v1 fields stay identical across the flip.
    assert ok_doc["task_types"] == shed_doc["task_types"]
    assert ok_doc["reserve"] == shed_doc["reserve"]


def test_capacity_block_does_not_rewrite_policy_version(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    a = export.build_document(database, now=100, capacity_snapshot=_snap())
    b = export.build_document(
        database, now=100, capacity_snapshot=_snap(states={"codex": capacity.SHED})
    )
    assert a["exploration"]["policy_version"] == b["exploration"]["policy_version"]


def test_load_capacity_snapshot_reads_out_without_shelling(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "capacity.json"
    payload = _snap(generated_at=99, states={"codex": capacity.SHED})
    artifact.write_text(
        __import__("json").dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(export.capacity, "OUT", artifact)
    built = []
    monkeypatch.setattr(export.capacity, "build", lambda: built.append("build") or payload)
    loaded = export._load_capacity_snapshot()
    assert loaded["agents"]["codex"]["state"] == capacity.SHED
    assert built == []


def test_policy_version_changes_when_the_weight_version_changes(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snap = _snap()
    document = export.build_document(database, now=100, capacity_snapshot=snap)
    with sqlite3.connect(database) as c:
        c.execute("UPDATE route_weights SET version=3 WHERE version=2")
    changed = export.build_document(database, now=100, capacity_snapshot=snap)
    assert changed["source_version"] == 3
    assert changed["exploration"]["policy_version"] != document["exploration"]["policy_version"]


def test_missing_weights_sample_remote_prior_without_creating_a_database(tmp_path: Path) -> None:
    database = tmp_path / "absent.db"
    document = export.build_document(database, now=100, capacity_snapshot=_snap())
    assert document["source_version"] == 0
    assert not database.exists()
    for task_type, challenger in document["exploration"]["challengers"].items():
        assert document["task_types"][task_type]["evidence_ok"] is False
        assert challenger in export.router.KEEPALIVE_AGENTS - export.router.RESERVE_AGENTS


def test_export_does_not_label_an_exploitation_pick_as_a_challenger(tmp_path: Path) -> None:
    with patch.object(
        export.router, "select_agent", return_value={"agent": "codex", "exploration": False}
    ):
        document = export.build_document(tmp_path / "absent.db", now=100, capacity_snapshot=_snap())
    assert all(challenger is None for challenger in document["exploration"]["challengers"].values())


def test_challenger_excludes_the_thresholded_consumer_winner(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    with sqlite3.connect(database) as c:
        # This low-observation seat leads the unfiltered posterior ranking, but
        # the consumer still exploits codex, the highest evidenced routable seat.
        c.execute("UPDATE route_weights SET posterior=0.99 WHERE agent='gemini'")
        c.execute(
            "INSERT INTO route_weights (version, task_type, agent, posterior, n_obs, success_rate) "
            "VALUES (2, 'implement', 'cursor', 0.5, 100, 0.5)"
        )
    document = export.build_document(
        database, now=2 * export.WEEK_SECONDS, capacity_snapshot=_snap()
    )
    assert document["task_types"]["implement"]["ranking"][0]["agent"] == "codex"
    assert document["exploration"]["challengers"]["implement"] == "gemini"


def test_completed_publication_survives_success_heartbeat_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "route_weights_export.py",
            "--feedback-db",
            str(tmp_path / "absent.db"),
            "--state-dir",
            str(tmp_path),
            "--publish",
        ],
    )
    published = []
    monkeypatch.setattr(
        export, "publish_document", lambda document: published.append(document) or True
    )
    monkeypatch.setattr(export, "_load_capacity_snapshot", lambda: _snap())

    def heartbeat(capability, event, **kwargs):
        if event == "success":
            raise OSError("fixture ledger unavailable")

    monkeypatch.setattr(export.capabilities, "daily_heartbeat", heartbeat)
    assert export.main() == 0
    assert len(published) == 1
    assert "capacity" in published[0]
    assert (tmp_path / "route-weights-export.json").exists()
    assert "capability heartbeat failed: fixture ledger unavailable" in capsys.readouterr().err
