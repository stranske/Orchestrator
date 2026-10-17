import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capacity
import route_weights_export as export


class PublishGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "weights.db"
        export._fixture_db(self.database)
        self.document = export.build_document(
            self.database, now=100, capacity_snapshot={"agents": {}}
        )

    def test_publish_guard_refuses_empty_db(self):
        empty = self.root / "empty.db"
        sqlite3.connect(empty).close()
        document = export.build_document(empty, now=100, capacity_snapshot={"agents": {}})
        ok, reason = export.publish_guard(document, self.document)
        self.assertFalse(ok, "publish_guard must refuse an empty DB")
        self.assertIn("source_version", reason)
        with patch.object(export.provision, "ensure_canonical") as canonical:
            with self.assertRaisesRegex(ValueError, "publish_guard refused.*source_version"):
                export.publish_document(document)
            canonical.assert_not_called()

    def test_publish_guard_refuses_all_false_evidence(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE route_weights SET n_obs=0")
        document = export.build_document(
            self.database, now=100, capacity_snapshot={"agents": {}}
        )
        self.assertGreater(document["source_version"], 0)
        ok, reason = export.publish_guard(document, None)
        self.assertFalse(ok)
        self.assertIn("all task_types", reason)
        with self.assertRaisesRegex(ValueError, "publish_guard refused.*evidence_ok=false"):
            export.publish_document(document)

    def test_publish_guard_refuses_loss_of_previously_published_evidence(self):
        previous = json.loads(json.dumps(self.document))
        previous["task_types"]["review"] = {"evidence_ok": True, "ranking": [{"agent": "codex"}]}
        ok, reason = export.publish_guard(self.document, previous)
        self.assertFalse(ok)
        self.assertIn("review", reason)
        self.assertIn("became false", reason)
        del self.document["task_types"]["review"]
        self.assertFalse(export.publish_guard(self.document, previous)[0])

    def test_publish_guard_accepts_bootstrap_unchanged_and_recovered_evidence(self):
        self.assertEqual(export.publish_guard(self.document, None), (True, ""))
        self.assertEqual(export.publish_guard(self.document, self.document), (True, ""))
        self.assertEqual(export.publish_guard(self.document, {"source_version": 0}), (True, ""))

    def _run_main(self, database, *flags):
        argv = [
            "route_weights_export.py",
            "--state-dir",
            str(self.root),
            "--feedback-db",
            str(database),
            "--publish",
            *flags,
        ]
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {"ORCH_ROUTE_WEIGHTS_PUBLISH": "1"}),
            patch.object(export.capabilities, "daily_heartbeat") as heartbeat,
            patch.object(export.capacity, "build", return_value={"agents": {}}),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = export.main()
        return result, output.getvalue(), errors.getvalue(), heartbeat

    def test_empty_db_cli_exits_nonzero_and_logs_guard_refusal(self):
        with patch.object(export.provision, "ensure_canonical") as canonical:
            result, output, errors, heartbeat = self._run_main(self.root / "missing.db")
        self.assertEqual(result, 1, "publish_guard refusal must fail the CLI")
        self.assertIn(f"db={self.root / 'missing.db'} source_version=0", output)
        self.assertIn("publish_guard refused", errors)
        canonical.assert_not_called()
        self.assertEqual(heartbeat.call_count, 1)  # Invocation only; no publish success.

    def test_allow_evidence_regression_flag_reaches_publisher(self):
        with patch.object(export, "publish_document", return_value=True) as publisher:
            result, _, _, _ = self._run_main(
                self.root / "missing.db", "--allow-evidence-regression"
            )
        self.assertEqual(result, 0)
        self.assertTrue(publisher.call_args.kwargs["allow_evidence_regression"])

    def test_shared_resolver_honours_state_dir_and_existing_overrides(self):
        with patch.dict(os.environ):
            for name in ("ORCH_FEEDBACK_DB", "ORCH_LOCAL_RUNTIME", "ORCH_STATE_DIR"):
                os.environ.pop(name, None)
            with patch.object(Path, "home", return_value=self.root):
                self.assertEqual(
                    export.feedback.resolve_db_path(),
                    self.root / ".codex" / "orchestrator" / "feedback" / "orchestrator.db",
                )
            os.environ["ORCH_STATE_DIR"] = str(self.root / "instance")
            expected = self.root / "instance" / "feedback" / "orchestrator.db"
            self.assertEqual(export.feedback.resolve_db_path(), expected)
            expected.parent.mkdir(parents=True)
            export._fixture_db(expected)
            argv = ["route_weights_export.py", "--state-dir", str(self.root / "output")]
            with (
                patch.object(sys, "argv", argv),
                patch.object(export.capabilities, "daily_heartbeat"),
                patch.object(export.capacity, "build", return_value={"agents": {}}),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(export.main(), 0)
            self.assertIn(f"db={expected} source_version=2", output.getvalue())
            os.environ["ORCH_LOCAL_RUNTIME"] = str(self.root / "runtime")
            self.assertEqual(
                export.feedback.resolve_db_path(),
                self.root / "runtime" / "feedback" / "orchestrator.db",
            )
            os.environ["ORCH_FEEDBACK_DB"] = str(self.database)
            self.assertEqual(export.feedback.resolve_db_path(), self.database)
            self.assertEqual(export.feedback.resolve_db_path(expected), expected)


V1_KEYS = (
    "schema",
    "generated_at",
    "source_version",
    "min_observations",
    "task_types",
    "reserve",
    "exploration",
)


def _capacity_fixture(*, codex_state: str = capacity.OK, generated_at: int = 1_700_000_000) -> dict:
    agents = {
        agent: {
            "state": codex_state if agent == "codex" else capacity.OK,
            "window": cfg["window"],
        }
        for agent, cfg in capacity.AGENTS.items()
    }
    return {"generated_at": generated_at, "agents": agents}


def _v1_bytes(document: dict) -> bytes:
    return json.dumps({key: document[key] for key in V1_KEYS}, sort_keys=True).encode("utf-8")


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


def test_policy_version_unchanged_when_only_capacity_is_added(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    without = export.build_document(database, now=100, include_capacity=False)
    with_capacity = export.build_document(database, now=100, capacity_snapshot=_capacity_fixture())
    assert (
        without["exploration"]["policy_version"] == with_capacity["exploration"]["policy_version"]
    )


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


def test_route_weights_schema_stays_v1_for_workflows_consumer() -> None:
    """Hosted keepalive rejects any schema other than orchestrator.route-weights/v1."""
    assert export.SCHEMA == "orchestrator.route-weights/v1"
    assert export.CAPACITY_SCHEMA == "orchestrator.route-weights-capacity/v1"


def test_build_document_loads_capacity_via_capacity_build(tmp_path: Path, monkeypatch) -> None:
    """Reuse capacity.build in-process (no CLI); emit one capacity entry per agent."""
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    calls: list[int] = []
    fixture = _capacity_fixture(codex_state=capacity.SHED, generated_at=42)

    def fake_build():
        calls.append(1)
        return fixture

    monkeypatch.setattr(export.capacity, "build", fake_build)

    document = export.build_document(database, now=100)
    assert calls == [1]
    assert document["schema"] == export.SCHEMA
    assert document["capacity_schema"] == export.CAPACITY_SCHEMA
    assert document["capacity"]["codex"]["state"] == capacity.SHED
    assert document["capacity"]["codex"]["observed_at"] == 42
    assert set(document["capacity"]) == set(capacity.AGENTS)


def test_export_ignores_stale_capacity_out_artifact(tmp_path: Path, monkeypatch) -> None:
    """A readable but stale capacity.OUT must not suppress a live state change."""
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    out_dir = tmp_path / "handoff"
    out_dir.mkdir()
    stale_out = out_dir / "capacity.json"
    stale_out.write_text(
        json.dumps(_capacity_fixture(codex_state=capacity.OK, generated_at=1)),
        encoding="utf-8",
    )
    monkeypatch.setattr(export.capacity, "OUT", stale_out)
    live = _capacity_fixture(codex_state=capacity.SHED, generated_at=99)
    monkeypatch.setattr(export.capacity, "build", lambda: live)

    document = export.build_document(database, now=100)
    assert document["capacity"]["codex"]["state"] == capacity.SHED
    assert document["capacity"]["codex"]["observed_at"] == 99
    # Pin: one call site loads via the helper; never read the handoff artifact.
    source = Path(export.__file__).read_text(encoding="utf-8")
    assert source.count("= _current_capacity_snapshot()") == 1
    assert "OUT.read_text" not in source


def test_capacity_block_has_one_entry_per_configured_agent(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture()
    document = export.build_document(database, now=100, capacity_snapshot=snapshot)

    assert document["schema"] == export.SCHEMA
    assert document["capacity_schema"] == export.CAPACITY_SCHEMA
    assert set(document["capacity"]) == set(capacity.AGENTS)
    for agent, entry in document["capacity"].items():
        assert set(entry) == {"state", "window", "observed_at"}
        assert entry["window"] == capacity.AGENTS[agent]["window"]
        assert entry["observed_at"] == snapshot["generated_at"]


def test_capacity_states_are_copied_verbatim_from_the_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture(codex_state=capacity.SHED)
    document = export.build_document(database, capacity_snapshot=snapshot)
    assert document["capacity"]["codex"]["state"] == capacity.SHED


def test_capacity_block_coerces_malformed_agent_fields(tmp_path: Path) -> None:
    """Null/non-string state, bad observed_at, and non-dict rows must not leak."""
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture()
    snapshot["generated_at"] = "not-an-int"
    snapshot["agents"]["codex"] = {"state": None, "window": ""}
    snapshot["agents"]["claude"] = "not-a-row"
    document = export.build_document(database, capacity_snapshot=snapshot)
    assert document["capacity"]["codex"]["state"] == capacity.UNKNOWN
    assert document["capacity"]["codex"]["window"] == capacity.AGENTS["codex"]["window"]
    assert document["capacity"]["codex"]["observed_at"] == 0
    assert document["capacity"]["claude"]["state"] == capacity.UNKNOWN
    assert document["capacity"]["claude"]["window"] == capacity.AGENTS["claude"]["window"]


def test_capacity_states_use_capacity_agent_state_vocabulary(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    observed = {entry["state"] for entry in document["capacity"].values()}
    assert observed <= capacity.AGENT_STATES
    assert capacity.AGENT_STATES == frozenset({"ok", "warn", "shed", "unknown"})


def test_capacity_block_rejects_nonempty_unknown_state() -> None:
    snapshot = _capacity_fixture(codex_state="paused")
    assert export._capacity_block(snapshot)["codex"]["state"] == capacity.UNKNOWN


def test_capacity_block_handles_infinite_timestamp() -> None:
    for timestamp in (float("inf"), float("-inf"), "Infinity"):
        snapshot = _capacity_fixture()
        snapshot["generated_at"] = timestamp
        assert export._capacity_block(snapshot)["codex"]["observed_at"] == 0


def test_export_selftest_handles_all_initial_capacity_states(monkeypatch) -> None:
    for state in sorted(capacity.AGENT_STATES):
        monkeypatch.setattr(export.capacity, "build", lambda: _capacity_fixture(codex_state=state))
        export._selftest()


def test_v1_key_values_byte_identical_with_and_without_capacity_block(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    snapshot = _capacity_fixture()
    without = export.build_document(database, now=100, include_capacity=False)
    with_block = export.build_document(database, now=100, capacity_snapshot=snapshot)
    assert _v1_bytes(without) == _v1_bytes(with_block)


def test_capacity_state_change_triggers_write_document(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    ok_snapshot = _capacity_fixture(codex_state=capacity.OK)
    shed_snapshot = _capacity_fixture(codex_state=capacity.SHED)

    assert (
        export.write_document(
            target, export.build_document(database, capacity_snapshot=ok_snapshot)
        )
        is True
    )
    assert (
        export.write_document(
            target, export.build_document(database, capacity_snapshot=shed_snapshot)
        )
        is True
    )


def test_shed_pool_marker_flips_state_within_one_export(tmp_path: Path, monkeypatch) -> None:
    """A live capacity.build() after touching SHED_DIR/<agent> must shed that seat in-export."""
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    shed_dir = tmp_path / "capacity-shed"
    shed_dir.mkdir()
    monkeypatch.setattr(capacity, "SHED_DIR", shed_dir)
    monkeypatch.setattr(capacity, "_ccusage_active_block", lambda timeout_s=30: None)
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    monkeypatch.setattr(capacity, "_model_health", lambda agent, tier="full": None)
    monkeypatch.setattr(capacity, "_tier_models", lambda agent: {})
    monkeypatch.setattr(capacity, "_tier_ceiling", lambda agent: None)
    monkeypatch.setattr(capacity, "_capability_heartbeat", lambda *a, **k: None)

    before = export.build_document(database, now=100)
    assert before["capacity"]["codex"]["state"] != capacity.SHED
    assert set(before["capacity"]) == set(capacity.AGENTS)

    (shed_dir / "codex").touch()
    after = export.build_document(database, now=100)
    assert after["capacity"]["codex"]["state"] == capacity.SHED
    assert after["capacity"]["codex"]["window"] == capacity.AGENTS["codex"]["window"]
    # Other seats stay non-shed; only the marked pool flips within this export.
    for agent, entry in after["capacity"].items():
        if agent != "codex":
            assert entry["state"] != capacity.SHED, (agent, entry)


def test_legacy_export_without_capacity_is_republished_when_capacity_is_added(
    tmp_path: Path,
) -> None:
    """A pre-capacity exports/route-weights artifact must publish once capacity lands."""
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    legacy = export.build_document(database, now=100, include_capacity=False)
    with_capacity = export.build_document(database, now=100, capacity_snapshot=_capacity_fixture())

    assert export.write_document(target, legacy) is True
    assert "capacity" not in json.loads(target.read_text(encoding="utf-8"))
    assert export.write_document(target, with_capacity) is True
    published = json.loads(target.read_text(encoding="utf-8"))
    assert set(published["capacity"]) == set(capacity.AGENTS)


def test_semantic_compare_keeps_state_and_window_drops_observed_at() -> None:
    """Publish when state/window move; tick-only observed_at refresh stays silent."""
    base = {
        "schema": export.SCHEMA,
        "generated_at": "2026-10-16T00:00:00Z",
        "capacity": {
            "codex": {"state": capacity.OK, "window": "5h", "observed_at": 100},
        },
    }
    later_observed = {
        **base,
        "generated_at": "2026-10-16T01:00:00Z",
        "capacity": {
            "codex": {"state": capacity.OK, "window": "5h", "observed_at": 200},
        },
    }
    shed = {
        **base,
        "capacity": {
            "codex": {"state": capacity.SHED, "window": "5h", "observed_at": 100},
        },
    }
    window = {
        **base,
        "capacity": {
            "codex": {"state": capacity.OK, "window": "1h", "observed_at": 100},
        },
    }
    assert export._semantic_for_compare(base) == export._semantic_for_compare(later_observed)
    assert export._semantic_for_compare(base) != export._semantic_for_compare(shed)
    assert export._semantic_for_compare(base) != export._semantic_for_compare(window)


def test_capacity_observed_at_only_change_does_not_trigger_write_document(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    first = _capacity_fixture(codex_state=capacity.OK, generated_at=1_700_000_000)
    second = _capacity_fixture(codex_state=capacity.OK, generated_at=1_800_000_000)

    assert (
        export.write_document(
            target, export.build_document(database, now=100, capacity_snapshot=first)
        )
        is True
    )
    assert (
        export.write_document(
            target, export.build_document(database, now=100, capacity_snapshot=second)
        )
        is False
    )


def test_capacity_window_change_triggers_write_document(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    first = _capacity_fixture()
    second = _capacity_fixture()
    second["agents"] = {
        agent: {**entry, "window": "1h" if agent == "codex" else entry["window"]}
        for agent, entry in second["agents"].items()
    }

    assert (
        export.write_document(
            target, export.build_document(database, now=100, capacity_snapshot=first)
        )
        is True
    )
    assert (
        export.write_document(
            target, export.build_document(database, now=100, capacity_snapshot=second)
        )
        is True
    )


def test_malformed_capacity_entry_triggers_rewrite(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    target = tmp_path / "route-weights.json"
    snapshot = _capacity_fixture()
    document = export.build_document(database, now=100, capacity_snapshot=snapshot)
    assert export.write_document(target, document) is True

    malformed = json.loads(target.read_text(encoding="utf-8"))
    malformed["capacity"]["rogue"] = None
    target.write_text(json.dumps(malformed, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert export.write_document(target, document) is True
    repaired = json.loads(target.read_text(encoding="utf-8"))
    assert "rogue" not in repaired["capacity"]
    assert set(repaired["capacity"]) == set(capacity.AGENTS)


def test_export_includes_capacity_block(tmp_path: Path) -> None:
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    assert "capacity" in document
    assert document.get("capacity_schema") == export.CAPACITY_SCHEMA


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
        export, "publish_document", lambda document, **kwargs: published.append(document) or True
    )

    def heartbeat(capability, event, **kwargs):
        if event == "success":
            raise OSError("fixture ledger unavailable")

    monkeypatch.setattr(export.capabilities, "daily_heartbeat", heartbeat)
    assert export.main() == 0
    assert len(published) == 1
    assert (tmp_path / "route-weights-export.json").exists()
    assert "capability heartbeat failed: fixture ledger unavailable" in capsys.readouterr().err
