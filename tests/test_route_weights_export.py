import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import capacity
import route_weights_export as export

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
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    monkeypatch.setattr(
        "sys.argv",
        [
            "route_weights_export.py",
            "--feedback-db",
            str(database),
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


@pytest.mark.parametrize("exists", [False, True], ids=["missing-db", "empty-db"])
@pytest.mark.parametrize("allow", [False, True], ids=["default", "owner-override"])
def test_publish_guard_refuses_empty_db(tmp_path, monkeypatch, capsys, exists, allow):
    """The real CLI must fail at the guard, before touching the publication repo."""
    database = tmp_path / "empty.db"
    if exists:
        sqlite3.connect(database).close()
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    monkeypatch.setattr(export, "_current_capacity_snapshot", _capacity_fixture)
    heartbeats = []
    monkeypatch.setattr(
        export.capabilities,
        "daily_heartbeat",
        lambda capability, event, **kwargs: heartbeats.append((capability, event)),
    )
    monkeypatch.setattr(
        export.provision,
        "ensure_canonical",
        lambda *a: pytest.fail("publish_guard must refuse an empty DB before provisioning"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "route_weights_export.py",
            "--feedback-db",
            str(database),
            "--state-dir",
            str(tmp_path),
            "--publish",
        ]
        + (["--allow-evidence-regression"] if allow else []),
    )
    assert export.main() == 1, "publish_guard must refuse the empty DB"
    output = capsys.readouterr()
    assert f"db={database} source_version=0" in output.err
    assert "publish_guard refused: source_version == 0" in output.err
    assert database.exists() is exists, "read-only export must not create a missing DB"
    assert (
        heartbeats == [("route-weights-export", "invocation")]
    ), "publish_guard refusal must not record a successful publication"


def test_publish_guard_refuses_all_false_evidence(tmp_path):
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE route_weights SET n_obs=0")
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    assert document["source_version"] == 2
    ok, reason = export.publish_guard(document, None)
    assert not ok, "publish_guard must refuse all-false evidence even with a nonzero version"
    assert "all task types have evidence_ok false" in reason


@pytest.mark.parametrize("omit_task", [False, True], ids=["false", "missing-task"])
def test_publish_guard_refuses_loss_of_published_task_evidence(omit_task):
    previous = {"task_types": {"review": {"evidence_ok": True}}}
    current = {"source_version": 2, "task_types": {"implement": {"evidence_ok": True}}}
    if not omit_task:
        current["task_types"]["review"] = {"evidence_ok": False}
    ok, reason = export.publish_guard(current, previous)
    assert not ok, "publish_guard must preserve each published task's evidence"
    assert "review" in reason


@pytest.mark.parametrize("previous", [None, {"task_types": {"implement": {"evidence_ok": True}}}])
def test_publish_guard_allows_evidenced_publication(previous):
    assert export.publish_guard(
        {"source_version": 2, "task_types": {"implement": {"evidence_ok": True}}}, previous
    ) == (True, "")


@pytest.mark.parametrize("aged", [False, True], ids=["fresh", "aged"])
@pytest.mark.parametrize("allow", [False, True], ids=["refused", "owner-override"])
@pytest.mark.parametrize(
    "regression", ["zero-version", "all-false", "lost-review", "capacity-only"]
)
def test_publish_guard_preserves_remote_until_explicit_override(
    tmp_path, monkeypatch, capsys, allow, regression, aged
):
    """Use real fetch/show/commit/push against an isolated local bare remote."""

    def git(repository, *args):
        return subprocess.run(
            ["git", "-C", str(repository), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Guard Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "guard@example.invalid")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(checkout))
    git(checkout, "config", "user.name", "Publication Tester")
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    previous = export.build_document(database, now=100, capacity_snapshot=_capacity_fixture())
    previous["task_types"]["review"] = dict(previous["task_types"]["implement"])
    target = checkout / export.EXPORT_PATH
    export.write_document(target, previous)
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "fixture publication")
    git(checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
    before_head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    before_bytes = target.read_bytes()
    document = export.build_document(database, now=101, capacity_snapshot=_capacity_fixture())
    if regression == "zero-version":
        document["source_version"] = 0
    elif regression == "all-false":
        for row in document["task_types"].values():
            row.update(evidence_ok=False, ranking=[])
    if regression == "capacity-only":
        document["task_types"] = json.loads(json.dumps(previous["task_types"]))
        document["capacity"]["muse"]["state"] = "shed"

    class FixedDateTime(export.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(200000, tz=tz)

    monkeypatch.setattr(export.dt, "datetime", FixedDateTime)
    hold_path = tmp_path / "state" / "route-weights-regression-hold.json"
    if aged:
        export.write_document(
            hold_path,
            {
                "first_seen": 200000 - export.EVIDENCE_REGRESSION_HOLD_HOURS * 3600,
                "task_types": export._regressing_tasks(document, previous),
                "reason": "fixture evidence loss",
            },
        )
    monkeypatch.setattr(export.provision, "ensure_canonical", lambda *a: checkout)
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(export, "build_document", lambda *a: document)
    argv = [
        "route_weights_export.py",
        "--publish",
        "--feedback-db",
        str(database),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    monkeypatch.setattr("sys.argv", argv + (["--allow-evidence-regression"] if allow else []))
    allowed = regression == "capacity-only" or ((allow or aged) and regression == "lost-review")
    assert export.main() == (0 if allowed else 1)
    after_head = git(remote, "rev-parse", export.EXPORT_BRANCH)
    output = capsys.readouterr()
    if allowed:
        assert after_head != before_head
        published = json.loads(git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}"))
        assert published == document
        message = git(remote, "log", "-1", "--format=%B", export.EXPORT_BRANCH)
        assert message == (
            f"chore: export route weights (db={database.resolve()} "
            "source_version=2 by=Publication Tester)"
        )
        if regression == "capacity-only":
            assert published["task_types"] == previous["task_types"]
            assert not hold_path.exists(), "evidence recovery clears the hold"
        elif allow:
            assert "override --allow-evidence-regression" in output.err
        else:
            assert (
                "evidence regression held 24h; publishing with evidence_ok=false for review"
            ) in output.err
    else:
        assert after_head == before_head, "publish_guard must leave the remote untouched"
        assert target.read_bytes() == before_bytes, "publish_guard must preserve publication bytes"
        assert git(checkout, "branch", "--show-current") == "main"
        assert "publish_guard refused:" in output.err
        assert "refused: regressing_tasks=" in output.err
        assert "drainable_in=" in output.err
        if regression == "lost-review":
            assert "regressing_tasks=1, drainable_in=24h" in output.err
            hold = json.loads(hold_path.read_text())
            assert hold == {
                "first_seen": 200000,
                "task_types": ["review"],
                "reason": "evidence_ok true -> false for: review",
            }


@pytest.mark.parametrize(
    "selection", ["default", "state", "cli-state", "runtime", "env-db", "cli-db"]
)
def test_feedback_db_path_binding(tmp_path, monkeypatch, capsys, selection):
    monkeypatch.setattr(export, "_current_capacity_snapshot", _capacity_fixture)
    for name in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME", export.feedback.DB_PATH_ENV):
        monkeypatch.delenv(name, raising=False)
    shared_db = tmp_path / "shared.db"
    monkeypatch.setattr(export.feedback, "DB_PATH", shared_db)
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    expected = shared_db
    argv = ["route_weights_export.py"]
    if selection != "default":
        monkeypatch.setenv("ORCH_STATE_DIR", str(state))
        expected = state / "feedback" / "orchestrator.db"
    if selection == "cli-state":
        argv += ["--state-dir", str(tmp_path / "cli-state")]
        expected = tmp_path / "cli-state" / "feedback" / "orchestrator.db"
    if selection in ("runtime", "env-db", "cli-db"):
        monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
        expected = runtime / "feedback" / "orchestrator.db"
    if selection in ("env-db", "cli-db"):
        expected = tmp_path / "env.db"
        monkeypatch.setenv(export.feedback.DB_PATH_ENV, str(expected))
    if selection == "cli-db":
        expected = tmp_path / "explicit.db"
        argv += ["--feedback-db", str(expected)]
    expected.parent.mkdir(parents=True, exist_ok=True)
    export._fixture_db(expected)
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(export.capabilities, "daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(export, "write_document", lambda *a: False)
    with patch.object(
        export.feedback, "resolve_db_path", wraps=export.feedback.resolve_db_path
    ) as resolver:
        assert export.main() == 0
    resolver.assert_called_once()
    assert export.feedback.resolve_db_path(*resolver.call_args.args) == expected.resolve()
    assert f"db={expected.resolve()} source_version=2" in capsys.readouterr().err

    # Each selection must remain diagnosable even if building the snapshot fails.
    def fail_capacity():
        raise OSError("capacity snapshot unavailable")

    monkeypatch.setattr(export, "_current_capacity_snapshot", fail_capacity)
    with pytest.raises(OSError, match="capacity snapshot unavailable"):
        export.main()
    assert capsys.readouterr().err == f"db={expected.resolve()} source_version=2\n"


def test_cadence_surfaces_export_refusal(tmp_path):
    script = (Path(__file__).resolve().parents[1] / "orchestrate.sh").read_text()
    start = script.index("if _cadence_due route-weights-export ")
    block = script[start : script.index("if _cadence_due periodic-report ", start)]
    stubs = """
_cadence_due() { return 0; }
_attempt_ok() { return 0; }
_mark_success() { echo SUCCESS; }
_mark_fail() { echo "FAIL:$1"; }
python3() { echo 'publish_guard refused: source_version == 0' >&2; return 1; }
"""
    result = subprocess.run(
        ["bash", "-c", stubs + block],
        env={
            **os.environ,
            "STAMP_DIR": str(tmp_path),
            "ORCH": str(tmp_path),
            "ORCH_ROUTE_WEIGHTS_PUBLISH": "1",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert "publish_guard refused: source_version == 0" in result.stdout
    assert "FAIL:route-weights-export" in result.stdout
    assert "SUCCESS" not in result.stdout


@pytest.mark.parametrize(
    "database_kind", ["missing", "empty", "no-weights-table", "empty-weights-table"]
)
def test_publish_refuses_absent_evidence_before_git_or_capacity_write(tmp_path, database_kind):
    database = tmp_path / "weights.db"
    if database_kind != "missing":
        with sqlite3.connect(database) as connection:
            if database_kind == "no-weights-table":
                connection.execute("CREATE TABLE unrelated (value INTEGER)")
    if database_kind == "empty-weights-table":
        export._fixture_db(database)
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM route_weights")
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    before = json.dumps(document, sort_keys=True)
    with patch.object(export.provision, "ensure_canonical") as provisioner:
        with pytest.raises(ValueError, match="local route-weight evidence is missing or empty"):
            export.publish_document(document)
    provisioner.assert_not_called()
    assert json.dumps(document, sort_keys=True) == before


def test_publish_refuses_threshold_empty_overwrite_and_preserves_fresh_capacity(tmp_path):
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    existing = export.build_document(database, capacity_snapshot=_capacity_fixture())
    existing["capacity"]["muse"] = {"state": "shed", "window": "5h+weekly", "observed_at": 999}
    target = tmp_path / export.EXPORT_PATH
    target.parent.mkdir()
    target.write_text(json.dumps(existing))
    before = target.read_bytes()
    incoming = export.build_document(database, minimum=1000, capacity_snapshot=_capacity_fixture())
    with patch.object(export.provision, "ensure_canonical", return_value=tmp_path):
        with patch.object(export.subprocess, "run") as git:
            git.return_value.returncode = 0
            with pytest.raises(ValueError, match="empty rankings would erase published evidence"):
                export.publish_document(incoming)
    assert target.read_bytes() == before
    assert not any(call.args[0][3] in {"add", "commit", "push"} for call in git.call_args_list)


def test_publish_accepts_real_evidence_recovery_over_version_zero(tmp_path):
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    export._validate_publication(document, {"source_version": 0, "task_types": {}, "reserve": {}})


def test_publication_identity_falls_back_to_exporter(tmp_path, monkeypatch):
    database = tmp_path / "weights.db"
    export._fixture_db(database)
    document = export.build_document(database, capacity_snapshot=_capacity_fixture())
    monkeypatch.setattr(export.provision, "ensure_canonical", lambda *a: tmp_path)

    def run(argv, **kwargs):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 1, "")
        return subprocess.CompletedProcess(argv, 0, "")

    with patch.object(export.subprocess, "run", side_effect=run) as commands:
        assert export.publish_document(document, db_path=database)
    commits = [call.args[0] for call in commands.call_args_list if "commit" in call.args[0]]
    assert commits[0][-1] == (
        f"chore: export route weights (db={database.resolve()} "
        "source_version=2 by=orchestrator-export)"
    )
