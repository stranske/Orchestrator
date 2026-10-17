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
    assert block["status"] == "converged"
    assert block["lambda"]["codex"] > 0
    assert block["window_days"] == 14
    assert block["inputs"]["pacing_observed_at"] == NOW
    assert block["inputs"]["cost_source_by_agent"]["codex"]["imputed"] == ["implement", "review"]
    assert block["inputs"]["cost"]["codex"] == {"implement": 0.5, "review": 0.5}
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
    assert doc["comparative"]["status"] == "inputs_unmeasured"
    assert doc["comparative"]["reasons"]
    assert all(value is None for value in doc["comparative"]["lambda"].values())
    assert all(
        row["comparative_rank"] is None
        for task in doc["task_types"].values()
        for row in task["ranking"]
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
    assert result["inputs"]["cost"]["codex"] == {"implement": 0.75, "review": 0.25}
    assert result["inputs"]["cost"]["cursor"] == {"implement": 0.5, "review": 0.5}
    assert result["inputs"]["demand"] == {"implement": 0.5, "review": 0.5}


def test_subscription_price_is_zero_and_metered_price_needs_complete_row(tmp_path):
    db, snapshot = fixture(tmp_path)
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["inputs"]["price"] == {"codex": 0.0, "cursor": 0.0}
    snapshot["pools"]["codex-metered"] = snapshot["pools"].pop("codex-subscription")
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "inputs_unmeasured"
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
    assert result["status"] == "converged"
    assert result["lambda"] == {"codex": 0.0}
    assert result["inputs"]["demand"] == {"implement": 0.5, "review": 0.5}
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
    path = tmp_path / "history.ndjson"
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
    assert "unmeasured — needs #668 pacing state" in switch_review.comparative_routing_line(
        {"comparative": report}
    )
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


def test_infeasible_inputs_remain_max_iter_not_unmeasured(tmp_path):
    db, snapshot = fixture(tmp_path)
    for pool in snapshot["pools"].values():
        pool["pacing"]["allowed_fraction"] = pool["pacing"]["used_fraction"]
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "max_iter"
    assert result["iterations"] == 50
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
    assert result["status"] == "inputs_unmeasured"
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
    doc = export.build_document(db, now=NOW, capacity_snapshot=snapshot)
    assert "analysis" not in doc["task_types"]
    assert doc["comparative"]["assignment"]["analysis"] == "codex"
    assert [row["comparative_rank"] for row in doc["task_types"]["implement"]["ranking"]] == [2, 1]
