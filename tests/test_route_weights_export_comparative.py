import copy
import json
import sqlite3
import subprocess
import sys

import pytest

import comparative
import pacing
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
                    "effective_reserve": 0.25 if agent == "codex" else 0.0,
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
        "effective_reserve",
        "period_end",
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
        snapshot["generated_at"] -= comparative.STALE_AFTER_SECONDS + 1
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
            assert row["comparative_rank"] is None
            assert row["comparative_reason"]
            assert row["comparative_status"] == (
                doc["comparative"]["status"] + ": " + "; ".join(doc["comparative"]["reasons"])
            )


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
        pool["pacing"]["burn_per_day"] = 0.01
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
        pool["pacing"]["effective_reserve"] = 1 - pool["pacing"]["used_fraction"]
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
    snapshot["pools"]["codex-subscription"]["pacing"]["effective_reserve"] = 0.38
    snapshot["pools"]["cursor-subscription"]["pacing"]["burn_per_day"] = 0.01
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


def test_realistic_units_bind_codex_and_clamp_over_reserve(agents_fixture):
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
            allowed_fraction=0.4,
            effective_reserve=0.35 if a == "codex" else 0.0,
        )
    r = comparative.measure(db, snapshot, now=NOW)
    assert r["status"] == "optimal"
    assert r["inputs"]["cost"]["codex"]["implement"] == pytest.approx(0.002)
    assert r["inputs"]["demand"]["implement"] == 240
    assert r["inputs"]["budget"]["codex"] == pytest.approx(0.15)
    assert r["usage"]["codex"] == pytest.approx(0.15)
    assert r["lambda"]["codex"] > 0
    snapshot["agents"]["codex"]["pacing"]["used_fraction"] = 0.9
    snapshot["agents"]["cursor"]["pacing"]["period_end"] = NOW + 86400
    r = comparative.measure(db, snapshot, now=NOW)
    assert r["inputs"]["budget"]["codex"] == 0
    assert r["inputs"]["cost_source_by_agent"]["codex"]["over_reserve"] is True
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
    assert all(r["comparative_rank"] is None for r in rows)
    assert all(
        "partial:" in r["comparative_status"] and "gemini" in r["comparative_status"] for r in rows
    )
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
            a["pacing"].update(ts=row["ts"], burn_rate_per_day=0.01, stale=(i == 4))
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


@pytest.fixture
def main_680_pacing_fixture(tmp_path):
    """Use evaluate() itself so unknown reset inventory matches the live contract."""
    db, snapshot = fixture(tmp_path)
    snapshot["agents"] = {}
    for agent in ("codex", "cursor"):
        row = pacing.evaluate(
            agent,
            now=NOW,
            used_fraction=0.394,
            period_start=NOW - 3 * 86400,
            period_end=NOW + 4 * 86400,
            resets=None,
            samples=[
                dict(ts=NOW - 6 * 3600, used_fraction=0.374, source="fixture-percent"),
                dict(ts=NOW, used_fraction=0.394, source="fixture-percent"),
            ],
            source="fixture-percent",
            source_ts=NOW,
        )
        assert row["resets_available"] is None
        assert row["effective_reserve"] == 0.15
        assert row["allowed_fraction"] < row["used_fraction"]
        snapshot["agents"][agent] = {"pacing": row}
    del snapshot["pools"]
    return db, snapshot


def test_main_680_unknown_resets_remain_optimal(main_680_pacing_fixture):
    db, snapshot = main_680_pacing_fixture
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "optimal"
    assert result["inputs"]["budget"] == pytest.approx(dict(codex=0.456, cursor=0.456))
    assert result["inputs"]["cost_source_by_agent"]["codex"]["over_reserve"] is False


def test_effective_reserve_missing_is_unmeasured(main_680_pacing_fixture):
    db, snapshot = main_680_pacing_fixture
    del snapshot["agents"]["codex"]["pacing"]["effective_reserve"]
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "partial"
    assert result["inputs"]["budget"]["codex"] is None
    assert result["lambda"]["codex"] is None
    assert "codex: agents[codex].pacing: effective_reserve missing" in result["reasons"]


@pytest.mark.parametrize("allowance", [None, 0, 0.99, "absent"])
def test_allowance_to_date_is_only_fyi(main_680_pacing_fixture, allowance):
    db, snapshot = main_680_pacing_fixture
    row = snapshot["agents"]["codex"]["pacing"]
    row["allowed_fraction"] = allowance
    if allowance == "absent":
        del row["allowed_fraction"]
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "optimal"
    assert result["inputs"]["budget"]["codex"] == pytest.approx(0.456)
    assert result["inputs"]["cost_source_by_agent"]["codex"]["allowed_fraction"] == row.get(
        "allowed_fraction"
    )


def test_explicit_freshness_uses_shared_pacing_verdict(main_680_pacing_fixture):
    db, snapshot = main_680_pacing_fixture
    row = snapshot["agents"]["codex"]["pacing"]
    row["ts"] = NOW - pacing.STALE_AFTER_SECONDS - 1
    # An explicit verdict is authoritative; legacy pools without it use the shared age limit.
    assert comparative.measure(db, snapshot, now=NOW)["status"] == "optimal"
    del row["stale"]
    assert comparative.measure(db, snapshot, now=NOW)["status"] == "partial"
    row["ts"] = NOW - pacing.STALE_AFTER_SECONDS
    assert comparative.measure(db, snapshot, now=NOW)["status"] == "optimal"


@pytest.mark.parametrize("reserve", [None, -0.1, 1.1, True, float("nan")])
def test_invalid_effective_reserve_is_unmeasured(main_680_pacing_fixture, reserve):
    db, snapshot = main_680_pacing_fixture
    snapshot["agents"]["codex"]["pacing"]["effective_reserve"] = reserve
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "partial"
    assert result["inputs"]["budget"]["codex"] is None
    assert any("effective_reserve unmeasured or invalid" in r for r in result["reasons"])


def test_default_history_path_is_the_writers_ledger(monkeypatch, tmp_path):
    """The reader must default to where #680's pacing_usage WRITES pacing_snapshot rows."""
    import capacity

    monkeypatch.setattr(capacity, "HANDOFF", tmp_path / "handoff")
    assert comparative.history_path() == tmp_path / "handoff" / "capacity-ledger.ndjson"
    assert (
        comparative.history_path(tmp_path / "state")
        == tmp_path / "state" / "capacity-ledger.ndjson"
    )


def test_below_threshold_rows_get_an_explicit_reason_and_global_ranks_survive():
    result = {
        "status": "optimal",
        "ranks": {
            "implement": [
                {"agent": "claude", "comparative_score": 0.5},
                {"agent": "codex", "comparative_score": 0.4},
            ]
        },
        "inputs": {"cost_source_by_agent": {}},
        "reasons": ["global reason"],
    }
    task_rows = [{"agent": "codex", "n_obs": 30}, {"agent": "cursor", "n_obs": 5}]
    reserve_rows = [{"agent": "claude", "n_obs": 40}]
    export._attach_comparative_ranks(task_rows, result, "implement")
    export._attach_comparative_ranks(reserve_rows, result, "implement")
    assert reserve_rows[0]["comparative_rank"] == 1  # one enumeration across reserve and ranking
    assert task_rows[0]["comparative_rank"] == 2
    assert task_rows[1]["comparative_rank"] is None
    assert task_rows[1]["comparative_reason"] == (
        f"n_obs below comparative threshold ({comparative.MIN_OBS})"
    )


@pytest.mark.parametrize("minimum", [5, 25])
def test_comparative_uses_export_threshold(tmp_path, minimum):
    db, snapshot = fixture(tmp_path)
    doc = export.build_document(db, minimum, now=NOW, capacity_snapshot=snapshot)
    assert doc["comparative"]["status"] == ("optimal" if minimum == 5 else "inputs_unmeasured")
    if minimum == 25:
        assert any("n_obs >= 25" in r for r in doc["comparative"]["reasons"])
    rows = [{"agent": "codex", "n_obs": minimum - 1}]
    export._attach_comparative_ranks(
        rows, {**doc["comparative"], "ranks": {}}, "implement", minimum
    )
    assert rows[0]["comparative_reason"] == f"n_obs below comparative threshold ({minimum})"


def test_stale_claude_suppresses_every_export_rank(agents_fixture):
    db, snapshot = agents_fixture
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE route_weights SET agent='claude' WHERE agent='cursor'")
        conn.execute("UPDATE runs SET agent='claude' WHERE agent='cursor'")
    snapshot["agents"]["claude"] = snapshot["agents"].pop("cursor")
    snapshot["agents"]["claude"]["pacing"]["ts"] = NOW - 20.1 * 3600
    doc = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    assert doc["comparative"]["status"] == "partial"
    assert doc["comparative"]["lambda"]["codex"] is not None
    rows = [r for section in doc["task_types"].values() for r in section["ranking"]]
    rows += [r for section in doc["reserve"].values() for r in section]
    assert {r["agent"] for r in rows} == {"codex", "claude"}
    for row in rows:
        assert row["comparative_rank"] is None
        assert "partial:" in row["comparative_status"]
        assert "claude" in row["comparative_status"]
        assert (
            f"age 20.1h > {comparative.STALE_AFTER_SECONDS / 3600:g}h" in row["comparative_status"]
        )


@pytest.mark.parametrize(
    "stamp,expected",
    [
        (None, "absent (age unknown"),
        (NOW + 3600, "in the future (age -1.0h"),
        (NOW - 3 * 3600, f"stale (age 3.0h > {comparative.STALE_AFTER_SECONDS / 3600:g}h)"),
    ],
)
def test_pacing_timestamp_reason_is_specific(agents_fixture, stamp, expected):
    db, snapshot = agents_fixture
    snapshot["agents"]["codex"]["pacing"]["ts"] = stamp
    result = comparative.measure(db, snapshot, now=NOW)
    reason = result["inputs"]["cost_source_by_agent"]["codex"]["reason"]
    assert expected in reason
    assert f"{comparative.STALE_AFTER_SECONDS / 3600:g}h" in reason


def test_switch_review_partial_includes_comparative_reasons():
    reason = "claude: pacing stale (age 20.1h > 2h)"
    line = switch_review.comparative_routing_line(
        {
            "comparative": {
                "status": "partial",
                "classes_differing": None,
                "window_days": 14,
                "lambda": {"codex": 0.0, "claude": None},
                "reasons": [reason],
            }
        }
    )
    assert "status partial" in line
    assert reason in line


@pytest.mark.parametrize(
    "status", ["partial", "inputs_unmeasured", "infeasible_inputs", "infeasible", "iteration_limit"]
)
def test_every_nonoptimal_status_suppresses_even_computed_ranks(status):
    rows = [{"agent": "codex", "n_obs": 30}]
    result = {
        "status": status,
        "ranks": {"implement": [{"agent": "codex", "comparative_score": 0.4}]},
        "inputs": {"cost_source_by_agent": {}},
        "reasons": ["claude: pacing stale (age 20.1h > 2h)"],
    }
    export._attach_comparative_ranks(rows, result, "implement")
    assert rows[0]["comparative_rank"] is None
    assert rows[0]["comparative_status"] == status + ": " + result["reasons"][0]
