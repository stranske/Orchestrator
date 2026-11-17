import json
from contextlib import closing, redirect_stdout

import pytest

import adapters
import comparative
import feedback
import ledger_reconcile


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    return db


def _reconcile_fixture(tmp_path, **fields):
    log = tmp_path / "example__repo_1.cursor.log"
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Updated the worker.\nChecks passed.",
        "session_id": "cursor-session-1",
        **fields,
    }
    log.write_text("CLI diagnostic\n" + json.dumps(result) + "\n")
    feedback.record_run("lane-run", "example/repo#1", "implement", "cursor")
    feedback.record_cost("lane-run", cost_usd=0.01, source="langsmith")
    adapters.record_ledger(
        "cursor", run_id="lane-run", log_file=str(log), event="start", cursor_json_output=True
    )
    # The detached dispatcher appends the completion command's stdout to the same log.
    with log.open("a") as stream, redirect_stdout(stream):
        args = [
            "complete",
            "--run-id",
            "lane-run",
            "--agent",
            "cursor",
            "--log-file",
            str(log),
            "--exit-code",
            "0",
            "--cursor-json-output",
        ]
        assert ledger_reconcile.main(args) == 0
    assert log.read_text().endswith("Updated the worker.\nChecks passed.\n")
    assert ledger_reconcile.reconcile(adapters.LEDGER)["written_cost_rows"] == 1
    feedback.record_cost("lane-run", cost_usd=0.02, source="langsmith")
    assert ledger_reconcile.reconcile(adapters.LEDGER)["written_cost_rows"] == 0
    with closing(feedback._conn()) as conn:
        return conn.execute(
            "SELECT tokens_in,tokens_out,cost_usd,source FROM costs WHERE run_id='lane-run'"
        ).fetchone()


def test_cursor_json_result_with_usage_records_a_priced_cost(tmp_path, brain):
    lane_argv = adapters.build_command("cursor", "Implement it.", transport="local")
    assert "-p" in lane_argv  # Cursor's alias for --print.
    assert lane_argv[lane_argv.index("--output-format") + 1] == "json"
    offload_argv = adapters.build_command("cursor", "Review it.", transport="offload")
    assert offload_argv[offload_argv.index("--output-format") + 1] == "stream-json"

    tokens_in, tokens_out, cost_usd, source = _reconcile_fixture(
        tmp_path, model="Composer 2.5", usage={"input_tokens": 1000, "output_tokens": 200}
    )
    assert source == "cursor-cli-json"
    assert (tokens_in, tokens_out) == (1000, 200)
    assert cost_usd == pytest.approx(0.001)


def test_cursor_json_result_without_usage_records_measured_absence(tmp_path, brain):
    assert _reconcile_fixture(tmp_path) == (
        None,
        None,
        None,
        "unmeasured: cursor CLI result has no usage",
    )


def test_cursor_enters_the_solve_only_with_enough_priced_runs(brain):
    now = 1_800_000_000
    q = {"cursor": {"implement": 0.9}}
    window_start = now - comparative.WINDOW_DAYS * 86400

    def add_run(run_id, source, cost_usd, ts=now):
        feedback.record_run(run_id, "example/repo#1", "implement", "cursor", ts=ts)
        feedback.record_cost(run_id, source=source, cost_usd=cost_usd)

    add_run("missing", "unmeasured: cursor CLI result has no usage", None)
    missing = comparative.measure(brain, {}, now=now, q=q)
    assert missing["excluded"] == ["cursor"]
    assert "unmeasured: cursor CLI result has no usage" in missing["reasons"][0]

    add_run("legacy", "ledger", 0.01)
    add_run("outside-window", "cursor-cli-json", 0.20, ts=window_start - 1)
    for index in range(comparative.MIN_OBS - 1):
        add_run(f"priced-{index}", "cursor-cli-json", 0.05)
    below = comparative.measure(brain, {}, now=now, q=q)
    assert below["excluded"] == ["cursor"]
    assert "excluded: no pacing source (capacity model metered)" in below["reasons"][0]
    assert below["inputs"]["cost"]["cursor"]["implement"] is None

    add_run("threshold", "cursor-cli-json", 0.15, ts=window_start)
    included = comparative.measure(brain, {}, now=now, q=q)
    assert included["status"] == "optimal"
    assert included["excluded"] == []
    assert included["lambda"]["cursor"] == 0.0
    assert included["inputs"]["budget"]["cursor"] is None
    expected = 0.05 + 0.10 / comparative.MIN_OBS
    assert included["inputs"]["cost"]["cursor"]["implement"] == pytest.approx(expected)
    assert included["assignment"]["implement"]["cursor"] == comparative.MIN_OBS + 2
    assert included["objective"] == pytest.approx((0.9 - expected) * (comparative.MIN_OBS + 2))
