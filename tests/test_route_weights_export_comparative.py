import copy
import json
import sqlite3
import subprocess
import sys

import pytest

import comparative
import route_weights_export as export
import switch_review

NOW = 1_800_000_000


def fixture(tmp_path):
    db = tmp_path / "brain.db"
    export._fixture_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM route_weights")
        for task, qualities in (("implement", (0.9, 0.6)), ("review", (0.8, 0.7))):
            for agent, quality in zip(("codex", "cursor"), qualities):
                conn.execute(
                    "INSERT INTO route_weights VALUES (2,0,?,?,0,?,20,0.5,0,0,'',0,0)",
                    (task, agent, quality),
                )
        conn.execute(
            "CREATE TABLE runs (run_id TEXT, ts INTEGER, agent TEXT, task_type TEXT, source TEXT)"
        )
        conn.execute(
            "CREATE TABLE costs (run_id TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            [
                ("a1", NOW - 30, "codex", "implement", "orchestrator_local"),
                ("a2", NOW - 30, "codex", "review", "keepalive"),
                ("b1", NOW - 30, "cursor", "implement", "orchestrator_local"),
                ("b2", NOW - 30, "cursor", "review", "keepalive"),
            ],
        )
    snapshot = {
        "generated_at": NOW,
        "pools": {
            f"{agent}-subscription": {
                "agent": agent,
                "pacing": {
                    "state": "ok",
                    "used_fraction": 0.5,
                    "allowed_fraction": allowed,
                    "period_end": NOW + 86400,
                    "resets_available": 0,
                    "burn_per_day": 1.0,
                },
            }
            for agent, allowed in (("codex", 0.75), ("cursor", 1.0))
        },
    }
    return db, snapshot


def test_export_carries_comparative_rank_and_keeps_v1_order(tmp_path):
    db, snapshot = fixture(tmp_path)
    doc = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    ranking = doc["task_types"]["review"]["ranking"]
    assert [row["agent"] for row in ranking] == ["codex", "cursor"]
    assert [row["comparative_rank"] for row in ranking] == [2, 1]
    assert [row["posterior"] for row in ranking] == [0.8, 0.7]
    assert doc["schema"] == "orchestrator.route-weights/v1"
    block = doc["comparative"]
    assert block["schema"] == comparative.SCHEMA
    assert block["status"] == "optimal"
    assert block["lambda"]["codex"] > 0
    assert block["window_days"] == 14
    assert block["inputs"]["pacing_observed_at"] == NOW
    assert block["inputs"]["cost_source_by_agent"]["codex"]["imputed"] == ["implement", "review"]
    assert block["inputs"]["cost"]["codex"] == {"implement": 7.0, "review": 7.0}
    absent = export.build_document(db, now=NOW, capacity_snapshot={})
    assert absent["exploration"] == doc["exploration"]


@pytest.mark.parametrize(
    "missing",
    [
        "pacing",
        "burn_per_day",
        "used_fraction",
        "allowed_fraction",
        "period_end",
        "resets_available",
        "stale",
        "expired",
        "future",
        "pools",
    ],
)
def test_missing_or_stale_pacing_exports_null_and_reason(tmp_path, missing):
    db, snapshot = fixture(tmp_path)
    pacing = snapshot["pools"]["codex-subscription"]["pacing"]
    if missing == "pacing":
        del snapshot["pools"]["codex-subscription"]["pacing"]
    elif missing == "pools":
        del snapshot["pools"]
    elif missing == "stale":
        snapshot["generated_at"] -= comparative.MAX_AGE_SECONDS + 1
    elif missing == "future":
        snapshot["generated_at"] += 1
    elif missing == "expired":
        pacing["period_end"] = NOW
    else:
        del pacing[missing]
    doc = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    all_missing = missing in ("pools", "stale", "future")
    assert doc["comparative"]["status"] == ("inputs_unmeasured" if all_missing else "partial")
    assert doc["comparative"]["reasons"]
    assert doc["comparative"]["lambda"]["codex"] is None
    for task in doc["task_types"].values():
        for row in task["ranking"]:
            if row["agent"] == "codex" or all_missing:
                assert row["comparative_rank"] is None
                assert row["comparative_reason"]
            else:
                assert row["comparative_rank"] == 1


def test_local_tokens_scale_class_cost_and_remote_tokens_do_not(tmp_path):
    db, snapshot = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE runs SET source='orchestrator_local' WHERE run_id='a2'")
        conn.executemany(
            "INSERT INTO costs VALUES (?,?,?,?,?)",
            [
                ("a1", 30, 0, 1.0, "ccusage"),
                ("a2", 10, 0, 1.0, "ledger"),
                ("b2", 10000, 0, 1.0, "ccusage"),
            ],
        )
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["inputs"]["cost"]["codex"] == {"implement": 10.5, "review": 3.5}
    assert result["inputs"]["cost"]["cursor"] == {"implement": 7.0, "review": 7.0}
    assert result["inputs"]["demand"] == pytest.approx({"implement": 1 / 7, "review": 1 / 7})


def test_subscription_price_is_zero_and_metered_price_needs_complete_row(tmp_path):
    db, snapshot = fixture(tmp_path)
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["inputs"]["price"] == {"codex": 0.0, "cursor": 0.0}
    snapshot["pools"]["codex-metered"] = snapshot["pools"].pop("codex-subscription")
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "partial"
    assert result["inputs"]["price"]["codex"] is None
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO costs VALUES ('a1',100,10,0.2,'ccusage')")
    assert comparative.measure(db, snapshot, now=NOW)["inputs"]["price"]["codex"] == 0.2


def test_threshold_window_zero_burn_and_read_only_database(tmp_path):
    db, snapshot = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE route_weights SET n_obs=19 WHERE agent='cursor'")
        conn.execute(
            "INSERT INTO runs VALUES ('old',?,'codex','implement','local')", (NOW - 15 * 86400,)
        )
        conn.execute("INSERT INTO runs VALUES ('future',?,'codex','implement','local')", (NOW + 1,))
    before = db.read_bytes()
    snapshot["pools"]["codex-subscription"]["pacing"]["burn_per_day"] = 0.0
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "optimal"
    assert result["lambda"] == {"codex": 0.0}
    assert result["inputs"]["demand"] == pytest.approx({"implement": 1 / 7, "review": 1 / 7})
    assert db.read_bytes() == before
    missing = tmp_path / "absent.db"
    assert comparative.measure(missing, snapshot, now=NOW)["status"] == "inputs_unmeasured"
    assert not missing.exists()


def test_report_counts_distinct_hours_and_slack_verdict(tmp_path):
    db, snapshot = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE route_weights SET agent='claude' WHERE agent='cursor'")
        conn.execute("UPDATE runs SET agent='claude' WHERE agent='cursor'")
    snapshot["pools"]["claude-subscription"] = snapshot["pools"].pop("cursor-subscription")
    snapshot["pools"]["claude-subscription"]["agent"] = "claude"
    later = copy.deepcopy(snapshot)
    later["generated_at"] += 3600
    path = tmp_path / "pacing-history.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in (snapshot, snapshot, later)))
    report = comparative.report(db, path, now=NOW + 3600)
    assert report["classes_differing"] == 1
    assert report["classes"]["review"]["hours_differing"] == 2
    assert report["classes"]["review"]["posterior_top"] == "codex"
    assert report["classes"]["review"]["comparative_top"] == "claude"
    assert report["lambda"]["codex"] > 0
    assert "classes_differing 1" in comparative.format_report(report)
    assert "classes_differing 1 (window 14d)" in switch_review.comparative_routing_line(
        {"comparative": report}
    )
    for pool in snapshot["pools"].values():
        pool["pacing"]["allowed_fraction"] = 10
    path.write_text(json.dumps(snapshot) + "\n")
    slack = comparative.report(db, path, now=NOW)
    assert slack["status"] == "not_testable_slack"
    assert slack["classes_differing"] == 0


def test_missing_history_is_unmeasured_not_zero_and_cli_is_read_only(tmp_path):
    db, _ = fixture(tmp_path)
    missing = tmp_path / "missing.ndjson"
    report = comparative.report(db, missing, now=NOW)
    assert report["classes_differing"] is None
    assert report["status"] == "inputs_unmeasured"
    assert "unmeasured;" in switch_review.comparative_routing_line({"comparative": report})
    proc = subprocess.run(
        [
            sys.executable,
            str(export.__file__),
            "--comparative-report",
            "--window-days",
            "14",
            "--feedback-db",
            str(db),
            "--pacing-history",
            str(missing),
            "--state-dir",
            str(tmp_path / "output"),
        ],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "classes_differing None" in proc.stdout
    assert "inputs_unmeasured" in proc.stdout
    assert not (tmp_path / "output").exists()


def test_timestamp_only_change_does_not_republish_or_change_policy(tmp_path):
    db, snapshot = fixture(tmp_path)
    first = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    snapshot["generated_at"] += 1
    second = export.build_document(db, now=NOW + 1, capacity_snapshot=snapshot)
    assert first["exploration"] == second["exploration"]
    target = tmp_path / "export.json"
    assert export.write_document(target, first)
    assert not export.write_document(target, second)
    target.write_text(json.dumps({"comparative": {}}))
    assert export.write_document(target, first), "malformed old additive blocks must be repairable"


def test_zero_budget_optimizes_and_reports_unmet_demand(tmp_path):
    db, snapshot = fixture(tmp_path)
    for pool in snapshot["pools"].values():
        pool["pacing"]["allowed_fraction"] = pool["pacing"]["used_fraction"]
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "optimal"
    assert result["objective"] == 0
    assert any(result["unmet_demand"].values())
    assert all(value is not None for value in result["lambda"].values())


@pytest.mark.parametrize("malformed", [None, [], {"codex": None}])
def test_malformed_pool_mapping_is_unmeasured(tmp_path, malformed):
    db, snapshot = fixture(tmp_path)
    snapshot["pools"] = malformed
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "inputs_unmeasured"
    assert result["inputs"]["budget"]["codex"] is None


def test_fresh_snapshot_cannot_refresh_stale_pacing(tmp_path):
    db, snapshot = fixture(tmp_path)
    snapshot["pools"]["codex-subscription"]["pacing"]["observed_at"] = NOW - 10000
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "partial"
    assert any("stale" in reason for reason in result["reasons"])


def test_opportunity_cost_includes_classes_outside_the_hosted_export(tmp_path):
    db, snapshot = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        for agent, quality in (("codex", 0.99), ("cursor", 0.2)):
            conn.execute(
                "INSERT INTO route_weights VALUES (2,0,'analysis',?,0,?,20,0.5,0,0,'',0,0)",
                (agent, quality),
            )
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?,'analysis','keepalive')",
                (f"{agent}-analysis", NOW - 30, agent),
            )
    snapshot["pools"]["codex-subscription"]["pacing"]["allowed_fraction"] = 0.62
    snapshot["pools"]["cursor-subscription"]["pacing"]["allowed_fraction"] = 10
    doc = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    assert "analysis" not in doc["task_types"]
    assert doc["comparative"]["assignment"]["analysis"]["codex"] > 0
    assert [row["comparative_rank"] for row in doc["task_types"]["implement"]["ranking"]] == [2, 1]


@pytest.fixture
def agents_fixture(tmp_path):
    db, snapshot = fixture(tmp_path)
    snapshot["agents"] = {
        row["agent"]: {"state": "ok", "pacing": row["pacing"]}
        for row in snapshot.pop("pools").values()
    }
    for row in snapshot["agents"].values():
        row["pacing"]["burn_rate_per_day"] = row["pacing"].pop("burn_per_day")
        row["pacing"]["ts"] = NOW
    return db, snapshot


def test_agents_shape_preferred_over_pool_fallback(agents_fixture):
    db, snapshot = agents_fixture
    snapshot["pools"] = {"old": {"agent": "codex", "pacing": {"stale": True}}}
    r = comparative.measure(db, snapshot, now=NOW)
    assert r["status"] == "optimal"
    assert r["lambda"]["codex"] > 0
    assert (
        "agents[codex].pacing.burn_rate_per_day"
        in r["inputs"]["cost_source_by_agent"]["codex"]["source"]
    )


@pytest.mark.parametrize(
    "preferred,alias,expected", [(None, 0.08, 0.56), (0.08, None, 0.56), (0.08, 2, 0.56)]
)
def test_numeric_burn_alias(agents_fixture, preferred, alias, expected):
    db, snapshot = agents_fixture
    snapshot["agents"]["codex"]["pacing"].update(burn_rate_per_day=preferred, burn_per_day=alias)
    assert comparative.measure(db, snapshot, now=NOW)["inputs"]["cost"]["codex"][
        "implement"
    ] == pytest.approx(expected)


def test_explicit_stale_flag_is_unmeasured(agents_fixture):
    db, snapshot = agents_fixture
    snapshot["agents"]["codex"]["pacing"]["stale"] = True
    r = comparative.measure(db, snapshot, now=NOW)
    assert r["status"] == "partial"
    assert r["lambda"]["codex"] is None
    assert any("agents[codex].pacing: stale=true" in s for s in r["reasons"])


def test_realistic_units_bind_codex_and_clamp_over_allowance(agents_fixture):
    db, snapshot = agents_fixture
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM runs")
        conn.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            [
                (str(i), NOW - 30, a, "implement", "orchestrator_local")
                for i, a in enumerate(["codex"] * 560 + ["cursor"] * 560)
            ],
        )
    for a, row in snapshot["agents"].items():
        row["pacing"].update(
            burn_rate_per_day=0.08,
            period_end=NOW + 3 * 86400,
            used_fraction=0.5,
            allowed_fraction=0.65 if a == "codex" else 1.0,
        )
    r = comparative.measure(db, snapshot, now=NOW)
    assert r["status"] == "optimal"
    assert r["inputs"]["cost"]["codex"]["implement"] == pytest.approx(0.002)
    assert r["inputs"]["demand"]["implement"] == 240
    assert r["inputs"]["budget"]["codex"] == pytest.approx(0.15)
    assert r["usage"]["codex"] == pytest.approx(0.15)
    assert r["lambda"]["codex"] > 0
    snapshot["agents"]["codex"]["pacing"]["allowed_fraction"] = 0.4
    snapshot["agents"]["cursor"]["pacing"]["period_end"] = NOW + 86400
    r = comparative.measure(db, snapshot, now=NOW)
    assert r["inputs"]["budget"]["codex"] == 0
    assert r["inputs"]["cost_source_by_agent"]["codex"]["over_allowance"] is True
    assert r["inputs"]["demand"]["implement"] == 80
    assert r["inputs"]["period_end"] == NOW + 86400


def test_two_paced_agents_and_one_unpaced_export_partial(agents_fixture):
    db, snapshot = agents_fixture
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO route_weights VALUES (2,0,'implement','gemini',0,0.4,20,0.5,0,0,'',0,0)"
        )
    doc = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    assert doc["comparative"]["status"] == "partial"
    assert doc["comparative"]["lambda"]["gemini"] is None
    rows = doc["task_types"]["implement"]["ranking"]
    assert len([r for r in rows if r["comparative_rank"] is not None]) == 2
    assert next(r for r in rows if r["agent"] == "gemini")["comparative_reason"]


def test_ledger_filter_ts_and_one_stale_hour_keeps_window_verdict(agents_fixture, tmp_path):
    db, snapshot = agents_fixture
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE route_weights SET agent='claude' WHERE agent='cursor'")
        conn.execute("UPDATE runs SET agent='claude' WHERE agent='cursor'")
    snapshot["agents"]["claude"] = snapshot["agents"].pop("cursor")
    rows = []
    for i in range(12):
        row = copy.deepcopy(snapshot)
        row.pop("generated_at")
        row.update(ts=NOW + i * 3600, event="pacing_snapshot")
        for a in row["agents"].values():
            a["pacing"].update(ts=row["ts"], allowed_fraction=10, stale=(i == 4))
        rows.append(row)
    rows += [dict(event="dispatch", ts=NOW + 13 * 3600, agents={})]
    path = comparative.history_path(tmp_path)
    assert path.name == "capacity-ledger.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    report = comparative.report(db, path, now=NOW + 13 * 3600)
    assert report["status"] == "not_testable_slack"
    assert report["hours_measured"] == 11
    assert report["hours_observed"] == 12
    assert report["hour_reasons"]
    assert "hours_measured 11/12" in comparative.format_report(report)
    # Exactly the same named threshold controls a poorly measured window.
    for row in rows[:7]:
        for a in row["agents"].values():
            a["pacing"]["stale"] = True
    path.write_text("\n".join(json.dumps(row) for row in rows))
    assert comparative.report(db, path, now=NOW + 13 * 3600)["status"] == "inputs_unmeasured"


@pytest.mark.parametrize("field", ["demand", "budget", "burn", "lambda"])
def test_diagnostic_drift_does_not_publish(agents_fixture, tmp_path, field):
    db, snapshot = agents_fixture
    first = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    second = copy.deepcopy(first)
    if field == "burn":
        second["comparative"]["inputs"]["burn_rate_per_day"] = dict(codex=0.123)
        second["comparative"]["inputs"]["cost"]["codex"]["review"] += 0.1
    elif field == "lambda":
        second["comparative"]["lambda"]["codex"] += 0.00001
    else:
        second["comparative"]["inputs"][field][
            next(iter(second["comparative"]["inputs"][field]))
        ] += 0.01
    target = tmp_path / "export.json"
    assert export.write_document(target, first)
    assert not export.write_document(target, second)
    second["comparative"]["inputs"]["cost_source_by_agent"]["codex"]["imputed"] = []
    assert export.write_document(target, second)
