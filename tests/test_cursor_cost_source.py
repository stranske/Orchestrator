import json
import shlex
from contextlib import closing, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import adapters
import comparative
import dispatcher
import feedback
import ledger_reconcile
import roles
import storage_lifecycle


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


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": 1000, "output_tokens": 200}, id="snake-case"),
        pytest.param({"inputTokens": 1000, "outputTokens": 200}, id="camel-case"),
        pytest.param({"prompt_tokens": 1000, "completion_tokens": 200}, id="prompt-completion"),
    ],
)
def test_cursor_json_result_with_usage_records_a_priced_cost(tmp_path, brain, usage):
    lane_argv = adapters.build_command("cursor", "Implement it.", transport="local")
    assert "-p" in lane_argv  # Cursor's alias for --print.
    assert lane_argv[lane_argv.index("--output-format") + 1] == "json"
    offload_argv = adapters.build_command("cursor", "Review it.", transport="offload")
    assert offload_argv[offload_argv.index("--output-format") + 1] == "stream-json"

    tokens_in, tokens_out, cost_usd, source = _reconcile_fixture(
        tmp_path, model="Composer 2.5", usage=usage
    )
    assert source == "cursor-cli-json"
    assert (tokens_in, tokens_out) == (1000, 200)
    assert cost_usd == pytest.approx(0.001)


def test_cursor_zero_usage_records_a_measured_zero_cost(tmp_path, brain):
    assert _reconcile_fixture(
        tmp_path, model="Composer 2.5", usage={"input_tokens": 0, "output_tokens": 0}
    ) == (0, 0, 0.0, "cursor-cli-json")


def test_cursor_json_result_without_usage_records_measured_absence(tmp_path, brain):
    assert _reconcile_fixture(tmp_path) == (
        None,
        None,
        None,
        "unmeasured: cursor CLI result has no usage",
    )


@pytest.mark.parametrize(
    ("model", "tokens_in", "expected"),
    [
        ("unknown-model", 7, (7, 3, None, adapters.CURSOR_CLI_UNKNOWN_PRICE)),
        ("Composer 2.5", True, (None, None, None, adapters.CURSOR_CLI_NO_USAGE)),
        ("Composer 2.5", -1, (None, None, None, adapters.CURSOR_CLI_NO_USAGE)),
        ("Composer 2.5", 2**63, (None, None, None, adapters.CURSOR_CLI_NO_USAGE)),
    ],
)
def test_cursor_usage_without_a_price(model, tokens_in, expected):
    result = {"model": model, "usage": {"input_tokens": tokens_in, "output_tokens": 3}}
    assert adapters.cursor_result_cost(result) == expected


@pytest.mark.parametrize("completion", ["ledger", "marker"])
def test_protected_cursor_cost_recovers_late_latency(tmp_path, brain, completion):
    run_id = "lane-run"
    log = tmp_path / "example__repo_1.cursor.log"
    log.write_text(
        json.dumps(
            {
                "type": "result",
                "result": "Completed",
                "model": "Composer 2.5",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            }
        )
        + "\n"
    )
    feedback.record_run(run_id, "example/repo#1", "implement", "cursor")
    adapters.record_ledger(
        "cursor",
        run_id=run_id,
        log_file=str(log),
        event="start",
        cursor_json_output=True,
        ts=100,
    )

    def cost_row():
        with closing(feedback._conn()) as conn:
            return conn.execute(
                "SELECT tokens_in,tokens_out,cost_usd,latency_s,source FROM costs WHERE run_id=?",
                (run_id,),
            ).fetchone()

    assert ledger_reconcile.reconcile(adapters.LEDGER)["written_cost_rows"] == 1
    assert cost_row() == (1000, 200, 0.001, 0.0, adapters.CURSOR_CLI_COST_SOURCE)
    if completion == "ledger":
        adapters.record_ledger("cursor", run_id=run_id, event="complete", exit=0, ts=160)
    else:
        done = tmp_path / "done"
        done.mkdir()
        (done / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "rc": 0, "rc_of": "agent", "ts": 160})
        )
    assert ledger_reconcile.reconcile(adapters.LEDGER, dry_run=True)["written_cost_rows"] == 0
    assert cost_row() == (1000, 200, 0.001, 0.0, adapters.CURSOR_CLI_COST_SOURCE)
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert summary["marker_backfills"] == int(completion == "marker")
    assert cost_row() == (1000, 200, 0.001, 60.0, adapters.CURSOR_CLI_COST_SOURCE)
    feedback.record_cost(
        run_id,
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.001,
        source=adapters.CURSOR_CLI_COST_SOURCE,
    )
    assert cost_row() == (1000, 200, 0.001, 60.0, adapters.CURSOR_CLI_COST_SOURCE)
    assert ledger_reconcile.reconcile(adapters.LEDGER)["written_cost_rows"] == 0


@pytest.mark.parametrize("output_format", ["json", "stream-json"])
def test_cursor_dispatch_completion_flag_matches_transport(
    tmp_path, brain, monkeypatch, output_format
):
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(dispatcher.subprocess, "Popen", popen)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher.claims, "update_metadata", Mock(return_value=True))
    monkeypatch.setattr(dispatcher.provision, "worktree_path", Mock(return_value=tmp_path))
    monkeypatch.setattr(storage_lifecycle, "owned_agent_tmpdir", Mock(return_value=None))
    monkeypatch.setattr(
        dispatcher.repo_knowledge, "append_context", lambda prompt, *a, **kw: prompt
    )
    monkeypatch.setattr(
        roles, "activate_dispatch_roles", lambda _a, prompt, **kw: {"prompt": prompt}
    )
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", Mock(return_value=""))
    monkeypatch.setattr(dispatcher, "_auth_prelude", Mock(return_value=""))
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")

    assignment = {
        "agent": "cursor",
        "target": "example/repo#1",
        "task_type": "implement",
        "prompt": "Task",
    }
    plan = dispatcher.plan_dispatch(assignment, dry_run=True)
    if output_format == "stream-json":
        offload_argv = adapters.build_command("cursor", "Task", cwd=tmp_path, transport="offload")
        plan["wrapped"] = plan["wrapped"].replace(
            shlex.join(plan["argv"]), shlex.join(offload_argv)
        )
        plan["argv"] = offload_argv
    argv = plan["argv"]
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == output_format
    assert dispatcher._spawn(plan) == 123

    start = json.loads(adapters.LEDGER.read_text())
    assert start["event"] == "start"
    assert start.get("cursor_json_output") is (True if output_format == "json" else None)
    shell = popen.call_args.args[0][-1]
    assert shlex.join(argv) in shell
    assert ("--cursor-json-output" in shell) == (output_format == "json")


def test_cost_only_cursor_shares_demand_with_a_capacity_constrained_agent():
    result = comparative.shadow_prices(
        {"codex": {"implement": 0.9}, "cursor": {"implement": 0.7}},
        {"implement": 5},
        {"codex": {"implement": 0.2}, "cursor": {"implement": 0.1}},
        {"codex": 0.4},
        {"codex": 0.0, "cursor": 1.0},
        cost_only_agents=frozenset({"cursor"}),
    )
    assert result["status"] == "optimal"
    assert result["assignment"]["implement"] == pytest.approx({"codex": 2, "cursor": 3})
    assert result["lambda"] == pytest.approx({"codex": 1.5, "cursor": 0})
    assert result["mu"] == pytest.approx({"implement": 0.6})
    assert result["objective"] == pytest.approx(3.6)


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
    snapshot = {
        "generated_at": now,
        "pools": {
            "cursor-subscription": {
                "agent": "cursor",
                "pacing": {
                    "state": "ok",
                    "used_fraction": 0.5,
                    "effective_reserve": 0.15,
                    "period_end": now + 86400,
                    "burn_per_day": 1.0,
                },
            },
        },
    }
    below = comparative.measure(brain, snapshot, now=now, q=q)
    assert below["excluded"] == ["cursor"]
    assert "excluded: no pacing source (capacity model metered)" in below["reasons"][0]
    assert below["inputs"]["cost"]["cursor"]["implement"] is None

    add_run("threshold", "cursor-cli-json", 0.15, ts=window_start)
    included = comparative.measure(brain, snapshot, now=now, q=q)
    assert included["status"] == "optimal"
    assert included["excluded"] == []
    assert included["lambda"]["cursor"] == 0.0
    assert included["inputs"]["budget"]["cursor"] is None
    expected = 0.05 + 0.10 / comparative.MIN_OBS
    assert included["inputs"]["cost"]["cursor"]["implement"] == pytest.approx(expected)
    assert included["assignment"]["implement"]["cursor"] == comparative.MIN_OBS + 2
    assert included["objective"] == pytest.approx((0.9 - expected) * (comparative.MIN_OBS + 2))
