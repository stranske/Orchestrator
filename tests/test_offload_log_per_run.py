"""Two offloads started in the same microsecond get a log and a run_id each (2026-10-05).

THE DEFECT. `dispatcher.offload` named its log `offload.<agent>.<time.time_ns()>.log` and, a few
lines later, its run_id `offload:<agent>:<time.time_ns()>`. On macOS that clock is microsecond-
granular: every value ends in 000, and consecutive reads inside one microsecond are equal. So two
offloads started in the same microsecond in one process (the research driver's `--parallel`
threads, a fanned-out audit) could share either name.

MEASURED (read-only, 2026-10-05, a copy of the capacity ledger, 4,521 offload runs): 4 of the 4,517
offload logs were written by two runs each (8 runs), and 3 offload run_ids were carried by 7 runs.
`offload.codex.1788528486818850000.log` holds two repo audits started 2026-09-04T13:28:06Z with
their two headers on lines 1 and 2, so `ledger_reconcile._log_segment` returns nothing for the run
whose header is first and both runs' output for the other: usage and cost harvest, resume tokens,
owner questions and incident classification read the wrong run. `offload:codex:1788314670987748000`
was two executors with different targets, and the Brain's `runs` table, INSERT OR REPLACE on
run_id, keeps one row for them.

THE RULE. `dispatcher._claim_offload_log` creates the log with O_EXCL, so a name belongs to whoever
creates it first, in this process or another; a taken name moves the claim one nanosecond on. The
run_id is built from the same claimed number, so each names the other and both are unique.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time as real_time
from pathlib import Path

import pytest

import adapters
import dispatcher
import ledger_reconcile
import rate_incidents

# The clock value of the measured shared log, offload.codex.1788528486818850000.log.
FROZEN_NS = 1788528486818850000
NAME = re.compile(r"^offload\.(?P<agent>[a-z]+)\.(?P<ns>\d+)\.log$")


class _FrozenClock:
    """The `time` module as `dispatcher` sees it, with `time_ns` stopped at one microsecond."""

    def __getattr__(self, name: str):
        return getattr(real_time, name)

    @staticmethod
    def time_ns() -> int:
        return FROZEN_NS


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Private logs and incident authority; the ledger rows and Brain run ids are captured here.

    `dispatcher.time` is replaced, not the `time` module, so only the dispatcher's clock stops."""
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 30)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(dispatcher, "_agent_log_tail_from_argv", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *args: (False, "test")
    )
    # gemini's argv carries agy's default `--log-file`, which the offload must rewrite per run.
    monkeypatch.setattr(
        dispatcher.adapters,
        "build_command",
        lambda agent, *args, **kwargs: (
            ["agy", "--log-file", str(tmp_path / "agy-default.log")]
            if agent == "gemini"
            else ["agent"]
        ),
    )
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args, **kwargs: "test-model")
    ledger: list[dict] = []
    brain_runs: list[str] = []
    lock = threading.Lock()

    def record_ledger(agent, **row):
        with lock:
            ledger.append({"agent": agent, **row})

    def record_run(run_id, *args, **kwargs):
        with lock:
            brain_runs.append(run_id)

    monkeypatch.setattr(dispatcher.adapters, "record_ledger", record_ledger)
    monkeypatch.setattr(dispatcher.feedback, "record_run", record_run)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "time", _FrozenClock())
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    return {"root": tmp_path, "ledger": ledger, "brain_runs": brain_runs}


def _workspace(stores, name: str) -> Path:
    path = stores["root"] / name
    path.mkdir()
    return path


def _claimed_ns(result: dict) -> int:
    """The number the run_id carries, checked against the number its log name carries."""
    match = NAME.match(Path(result["log"]).name)
    assert match, f"the log keeps the offload.<agent>.<ns>.log shape readers glob: {result['log']}"
    prefix, agent, ns = result["run_id"].split(":")
    assert (prefix, agent) == ("offload", match["agent"]), result
    assert ns == match["ns"], f"run_id and log name must carry one number: {result}"
    return int(ns)


@pytest.mark.parametrize("agent", ["codex", "cursor", "gemini"])
def test_two_offloads_in_one_microsecond_get_a_log_and_a_run_id_each(monkeypatch, stores, agent):
    """The measured interleaving: both runs' headers are on disk before either prints anything."""
    workspaces = [_workspace(stores, "audit-a"), _workspace(stores, "audit-b")]
    both_started = threading.Barrier(2, timeout=20)
    commands: dict[str, str] = {}

    def run(argv, *, cwd, **kwargs):
        both_started.wait()
        name = Path(cwd).name
        commands[name] = argv[-1]  # the shell command each run actually executed
        return subprocess.CompletedProcess(argv, 0, f"report from {name}\n", "")

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    results: dict[str, dict] = {}

    def offload(workspace: Path) -> None:
        results[workspace.name] = dispatcher.offload(agent, "audit", cwd=str(workspace))

    threads = [threading.Thread(target=offload, args=(w,)) for w in workspaces]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results) == ["audit-a", "audit-b"], results
    a, b = results["audit-a"], results["audit-b"]
    assert a["exit"] == 0 and b["exit"] == 0, (a, b)

    # What the reconcile pass reads for each run: its own output, and nothing else.
    segments = {
        name: ledger_reconcile._log_segment(Path(result["log"]), result["run_id"])
        for name, result in results.items()
    }
    assert segments == {
        "audit-a": ["report from audit-a"],
        "audit-b": ["report from audit-b"],
    }, segments
    assert a["log"] != b["log"] and a["run_id"] != b["run_id"], (a, b)
    # The anchor: both offloads read ONE clock value, so this is the same-microsecond case.
    assert sorted([_claimed_ns(a), _claimed_ns(b)]) == [FROZEN_NS, FROZEN_NS + 1], (a, b)
    for result in results.values():
        rows = [row for row in stores["ledger"] if row["run_id"] == result["run_id"]]
        assert [row["event"] for row in rows] == ["start", "complete"], rows
        assert {row["log_file"] for row in rows} == {result["log"]}, rows
    assert sorted(stores["brain_runs"]) == sorted([a["run_id"], b["run_id"]])
    if agent == "gemini":
        # agy's per-run log is derived from the dispatch log, so it is as private as that log. Read
        # from the command each run executed, which is where an argv rewrite has to land.
        own = {name: adapters.agy_log_for(result["log"]) for name, result in results.items()}
        assert own["audit-a"] != own["audit-b"], own
        for name, command in commands.items():
            assert f"--log-file {shlex.quote(str(own[name]))}" in command, (name, command)
            assert "agy-default.log" not in command, (name, command)


def test_a_name_another_process_holds_is_never_written(monkeypatch, stores):
    """O_EXCL is what makes the claim hold across processes: the other process's log is on disk."""
    logs = dispatcher.DISPATCH_LOG_DIR
    logs.mkdir(parents=True)
    theirs = logs / f"offload.codex.{FROZEN_NS}.log"
    theirs.write_text(f"=== other process run_id=offload:codex:{FROZEN_NS} ===\ntheir report\n")
    before = theirs.read_bytes()
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "our report\n", ""),
    )
    ours = dispatcher.offload("codex", "audit", cwd=str(_workspace(stores, "audit")))
    assert theirs.read_bytes() == before, theirs.read_text()
    assert ours["log"] == str(logs / f"offload.codex.{FROZEN_NS + 1}.log"), ours
    assert ours["run_id"] == f"offload:codex:{FROZEN_NS + 1}", ours
    assert ledger_reconcile._log_segment(theirs, f"offload:codex:{FROZEN_NS}") == ["their report"]
    assert ledger_reconcile._log_segment(Path(ours["log"]), ours["run_id"]) == ["our report"]


def test_a_free_name_is_the_clock_reading_itself(stores):
    """No collision, no change: the claim takes the clock's value, and creates the log empty."""
    run_id, logf = dispatcher._claim_offload_log("cursor")
    assert (run_id, logf) == (
        f"offload:cursor:{FROZEN_NS}",
        dispatcher.DISPATCH_LOG_DIR / f"offload.cursor.{FROZEN_NS}.log",
    )
    assert logf.read_bytes() == b""
    # Another agent's names are its own: the same reading is free for it.
    assert dispatcher._claim_offload_log("gemini")[0] == f"offload:gemini:{FROZEN_NS}"


def test_the_claim_is_bounded_and_names_the_directory(monkeypatch, stores):
    monkeypatch.setattr(dispatcher, "OFFLOAD_LOG_CLAIM_ATTEMPTS", 3)
    logs = dispatcher.DISPATCH_LOG_DIR
    logs.mkdir(parents=True)
    taken = [logs / f"offload.codex.{FROZEN_NS + step}.log" for step in range(3)]
    for path in taken:
        path.write_text("taken\n")
    with pytest.raises(RuntimeError) as raised:
        dispatcher._claim_offload_log("codex")
    message = str(raised.value)
    assert str(logs) in message and f"ending at {FROZEN_NS + 2}" in message, message
    assert sorted(logs.iterdir()) == sorted(taken), "a refused claim creates nothing"
