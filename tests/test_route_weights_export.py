import sqlite3
from pathlib import Path
from unittest.mock import patch

import route_weights_export as export


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

    assert export.write_document(target, export.build_document(database)) is True
    assert export.write_document(target, export.build_document(database)) is False


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
