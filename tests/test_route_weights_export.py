import sqlite3
from pathlib import Path
from unittest.mock import patch

import capacity
import route_weights_export as export


def _capacity_fixture(*, codex_state: str = capacity.OK) -> dict:
    observed_at = 1_700_000_000
    agents = {
        agent: {
            "state": codex_state if agent == "codex" else capacity.OK,
            "window": cfg["window"],
        }
        for agent, cfg in capacity.AGENTS.items()
    }
    return {"generated_at": observed_at, "agents": agents}


def test_export_filters_threshold_reserve_and_unknown_task_types(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)

    document = export.build_document(database, minimum=20)

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

    snapshot = _capacity_fixture()
    assert (
        export.write_document(target, export.build_document(database, capacity_snapshot=snapshot))
        is True
    )
    assert (
        export.write_document(target, export.build_document(database, capacity_snapshot=snapshot))
        is False
    )


def test_policy_version_changes_when_the_weight_version_changes(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, now=100)
    with sqlite3.connect(database) as c:
        c.execute("UPDATE route_weights SET version=3 WHERE version=2")
    changed = export.build_document(database, now=100)
    assert changed["source_version"] == 3
    assert changed["exploration"]["policy_version"] != document["exploration"]["policy_version"]


def test_missing_weights_sample_remote_prior_without_creating_a_database(tmp_path: Path) -> None:
    database = tmp_path / "absent.db"
    document = export.build_document(database, now=100)
    assert document["source_version"] == 0
    assert not database.exists()
    for task_type, challenger in document["exploration"]["challengers"].items():
        assert document["task_types"][task_type]["evidence_ok"] is False
        assert challenger in export.router.KEEPALIVE_AGENTS - export.router.RESERVE_AGENTS


def test_export_does_not_label_an_exploitation_pick_as_a_challenger(tmp_path: Path) -> None:
    with patch.object(
        export.router, "select_agent", return_value={"agent": "codex", "exploration": False}
    ):
        document = export.build_document(tmp_path / "absent.db", now=100)
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
    document = export.build_document(database, now=2 * export.WEEK_SECONDS)
    assert document["task_types"]["implement"]["ranking"][0]["agent"] == "codex"
    assert document["exploration"]["challengers"]["implement"] == "gemini"


def test_capacity_block_has_one_entry_per_configured_agent(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture()
    document = export.build_document(database, now=100, capacity_snapshot=snapshot)

    assert document["schema"] == export.SCHEMA
    assert set(document["capacity"]) == set(capacity.AGENTS)
    for agent, entry in document["capacity"].items():
        assert set(entry) == {"state", "window", "observed_at"}
        assert entry["window"] == capacity.AGENTS[agent]["window"]
    assert all(entry["observed_at"] == 100 for entry in document["capacity"].values())


def test_capacity_states_are_copied_verbatim_from_the_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture(codex_state=capacity.SHED)
    document = export.build_document(database, capacity_snapshot=snapshot)
    assert document["capacity"]["codex"]["state"] == capacity.SHED


def test_v1_keys_unchanged_except_schema_and_capacity(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture()
    document = export.build_document(database, now=100, capacity_snapshot=snapshot)
    v1_keys = ("task_types", "reserve", "min_observations", "source_version")
    for key in v1_keys:
        assert key in document
    exploration = document["exploration"]
    assert set(exploration) >= {"mode", "rate", "week_start", "challengers", "policy_version"}


def test_capacity_state_change_triggers_write_document(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    ok_snapshot = _capacity_fixture(codex_state=capacity.OK)
    shed_snapshot = _capacity_fixture(codex_state=capacity.SHED)

    assert export.write_document(
        target, export.build_document(database, capacity_snapshot=ok_snapshot)
    ) is True
    assert export.write_document(
        target, export.build_document(database, capacity_snapshot=shed_snapshot)
    ) is True


def test_export_includes_capacity_block(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    assert "capacity" in document


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

    def heartbeat(capability, event, **kwargs):
        if event == "success":
            raise OSError("fixture ledger unavailable")

    monkeypatch.setattr(export.capabilities, "daily_heartbeat", heartbeat)
    assert export.main() == 0
    assert len(published) == 1
    assert (tmp_path / "route-weights-export.json").exists()
    assert "capability heartbeat failed: fixture ledger unavailable" in capsys.readouterr().err
