"""The dispatch wrapper records the AGENT's exit status, not its claim release's (2026-10-04).

THE DEFECT. `dispatcher._spawn` ran `<agent subshell>; <claims.py release>; orch_dispatch_rc=$?;
<done marker>; <complete --exit-code "$orch_dispatch_rc">; exit $orch_dispatch_rc`, so `$?` was the
release's status: 0 when it found the claim, 1 when the claim was gone, 137 when it was SIGKILLed.
Every one of the 106 live dispatch done markers held the release's status. 31 said 137, and in all
31 the run's own log shows bash reporting `Killed: 9` for the release and then for the completion
step, AFTER the agent had finished its work. `ledger_reconcile` read rc>128 as the agent dying by
signal and classed six non-merged rows `transient_infra`, which were every row that rule had ever
classified. The completion step was told the same 0, so a failed agent's log was never read as
error evidence.

THE RULE. `_spawn` reads the agent's status first, straight after the agent subshell, then releases
the claim (always, failed agent or not) and records the release's status beside the agent's as
`release_rc`. The marker says whose status its rc is (`rc_of`), and reconcile classes a signal death
only from a marker that says it is the agent's. An older marker is counted, never classified. A
failed run's log is read as evidence less the agent's own codex work transcript, which a run that
reads docs about quotas fills with the very phrases a provider refusal uses.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

import adapters
import claims
import dispatcher
import feedback
import ledger_reconcile
import rate_incidents

TARGET = "o/r#9"
RUN = "o__r_9-codex-1"
REFUSAL = (
    "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
    "credits or try again at Sep 19th, 2026 3:11 AM."
)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A private Brain, capacity ledger, claims dir, incident authority and dispatch log dir."""
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch-logs")
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    return tmp_path


def _child_env(world: Path) -> dict:
    """The wrapper's environment: every store it can write points into the test's directory."""
    return {
        **os.environ,
        "HANDOFF_DIR": str(world),
        "ORCH_STATE_DIR": str(world / "state"),
        "ORCH_LOCAL_RUNTIME": str(world / "runtime"),
        "ORCH_FEEDBACK_DB": str(world / "t.db"),
        "ORCH_CAPABILITIES_PATH": str(world / "runtime" / "capabilities.json"),
        "ORCH_PUSH_RECORD_DISABLED": "1",
    }


def _stubs(monkeypatch, world: Path, *, release: str = "exit 0") -> Path:
    """Stand-ins for the claims release and the completion step that record how they were called.
    `release` is `exit N` or `kill`: the release SIGKILLed, as it was in all 31 live rc=137 runs."""
    stubs = world / "stubs"
    stubs.mkdir()
    calls = stubs / "calls.log"
    end = "os.kill(os.getpid(), 9)" if release == "kill" else f"sys.exit({release.split()[1]})"
    (stubs / "claims.py").write_text(
        f"import os, sys\nopen({str(calls)!r}, 'a').write('release ' + ' '.join(sys.argv[2:]) + "
        f"'\\n')\n{end}\n"
    )
    (stubs / "ledger_reconcile.py").write_text(
        f"import sys\nopen({str(calls)!r}, 'a').write('complete ' + ' '.join(sys.argv[2:]) + "
        f"'\\n')\n"
    )
    monkeypatch.setattr(dispatcher, "CLAIMS_PY", stubs / "claims.py")
    monkeypatch.setattr(dispatcher, "ORCH_DIR", stubs)
    return calls


def _spawn(monkeypatch, world: Path, agent_cmd: str, *, agent: str = "codex") -> tuple[dict, str]:
    """`_spawn` exactly as the dispatcher runs it, with the agent replaced by `agent_cmd` and the
    process start captured instead of launched."""
    seen: dict = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(argv, **_kw):
        seen["argv"] = argv
        return FakeProcess()

    cwd = world / "wt"
    cwd.mkdir(exist_ok=True)
    d = {
        "run_id": RUN,
        "agent": agent,
        "mode": "full",
        "target": TARGET,
        "lane": "opener",
        "task_type": "implement",
        "model": "gpt-5.6-codex",
        "cwd": str(cwd),
        "wrapped": agent_cmd,
    }
    # `dispatcher.subprocess` IS the subprocess module: scope the fake to this one call.
    with monkeypatch.context() as scoped:
        scoped.setattr(dispatcher.subprocess, "Popen", fake_popen)
        dispatcher._spawn(d)
    assert seen["argv"][:2] == ["bash", "-lc"], seen["argv"]
    return d, seen["argv"][-1]


def _run(world: Path, d: dict, wrapped: str) -> int:
    """Run the wrapper as the detached process would, its output appended to the run's own log."""
    safe = d["target"].replace("/", "__").replace("#", "_")
    with (dispatcher.DISPATCH_LOG_DIR / f"{safe}.{d['agent']}.log").open("a") as fh:
        done = subprocess.run(
            ["bash", "-c", wrapped],
            cwd=d["cwd"],
            env=_child_env(world),
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    return done.returncode


def _marker(run_id: str = RUN) -> dict:
    return json.loads((dispatcher.DISPATCH_LOG_DIR / "done" / f"{run_id}.json").read_text())


def _steps(calls: Path) -> list[list[str]]:
    return [line.split() for line in calls.read_text().splitlines()]


def _exit_code_passed(steps: list[list[str]]) -> int:
    complete = [s for s in steps if s[0] == "complete"]
    assert len(complete) == 1, steps
    return int(complete[0][complete[0].index("--exit-code") + 1])


# ---- the wrapper -------------------------------------------------------------------------------


@pytest.mark.parametrize("agent_rc", [0, 1, 7])
def test_the_marker_the_completion_step_and_the_exit_carry_the_agents_status(
    monkeypatch, world, agent_rc
):
    calls = _stubs(monkeypatch, world)
    d, wrapped = _spawn(monkeypatch, world, f"(exit {agent_rc})")
    assert _run(world, d, wrapped) == agent_rc, "the wrapper must exit with the agent's status"
    marker = _marker()
    assert marker["rc"] == agent_rc and marker["rc_of"] == adapters.MARKER_RC_OF_AGENT, marker
    assert marker["release_rc"] == 0, marker
    assert _exit_code_passed(_steps(calls)) == agent_rc


@pytest.mark.parametrize("agent_rc", [0, 1])
def test_the_claim_is_released_after_the_agent_whatever_its_status(monkeypatch, world, agent_rc):
    calls = _stubs(monkeypatch, world, release="exit 1")
    d, wrapped = _spawn(monkeypatch, world, f"(exit {agent_rc})")
    _run(world, d, wrapped)
    steps = _steps(calls)
    assert [s[0] for s in steps] == ["release", "complete"], steps
    assert steps[0][1:] == [TARGET, "codex"], steps
    marker = _marker()
    assert (marker["rc"], marker["release_rc"]) == (agent_rc, 1), marker


def test_a_killed_release_is_recorded_beside_the_agents_status_never_as_it(monkeypatch, world):
    """The live shape of all 31 rc=137 markers: the agent finished, then the release was killed."""
    calls = _stubs(monkeypatch, world, release="kill")
    d, wrapped = _spawn(monkeypatch, world, "(exit 0)")
    assert _run(world, d, wrapped) == 0
    marker = _marker()
    assert (marker["rc"], marker["release_rc"]) == (0, 137), marker
    assert _exit_code_passed(_steps(calls)) == 0
    feedback.record_outcome(RUN, adjudicated_verdict="FAIL", merged=False, durability="abandoned")
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert (summary["infra_classified"], summary["infra_unattributed_marker_rc"]) == (0, 0), summary
    assert _failure_class(RUN) is None, "a killed release must never read as the agent's death"


def test_an_agent_killed_by_signal_now_reads_as_a_signal_death(monkeypatch, world):
    _stubs(monkeypatch, world)
    d, wrapped = _spawn(monkeypatch, world, "(sh -c 'kill -9 $$')")
    assert _run(world, d, wrapped) == 137
    assert _marker()["rc"] == 137 and _marker()["release_rc"] == 0, _marker()
    feedback.record_outcome(RUN, adjudicated_verdict="FAIL", merged=False, durability="abandoned")
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert summary["infra_classified"] == 1, summary
    assert _failure_class(RUN) == "transient_infra"


def test_the_completion_step_reads_a_failed_agents_own_words_as_evidence(monkeypatch, world):
    """Producer to consumer, nothing stubbed between them: the real release frees the real claim,
    and the real completion step, told the agent's exit 1, reads its log as provider evidence. The
    release's status (0) used to stand in for the agent's, and this incident was never recorded."""
    assert claims.claim(TARGET, "claude")
    d, wrapped = _spawn(
        monkeypatch, world, f"(echo {shlex.quote(REFUSAL)}; exit 1)", agent="claude"
    )
    assert _run(world, d, wrapped) == 1
    assert claims.holder(TARGET) is None, "the claim must be released after a failed agent"
    incidents = _incidents()
    assert [(i["run_id"], i["agent"], i["category"]) for i in incidents] == [
        (d["run_id"], "claude", "quota")
    ], incidents


def test_the_same_words_from_a_successful_agent_are_ordinary_output(monkeypatch, world):
    assert claims.claim(TARGET, "claude")
    d, wrapped = _spawn(
        monkeypatch, world, f"(echo {shlex.quote(REFUSAL)}; exit 0)", agent="claude"
    )
    assert _run(world, d, wrapped) == 0
    assert claims.holder(TARGET) is None
    assert _incidents() == [], "a successful run's prose must never become an incident"


# ---- the signal-death rule ---------------------------------------------------------------------


def _marked_run(world: Path, run_id: str, marker: dict, *, outcome: bool = True) -> None:
    log = world / "dispatch-logs" / "o__r_9.codex.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(f"=== 2026-10-05T00:00:00Z dispatch codex/full -> {TARGET} run_id={run_id} ===\n")
    feedback.record_run(run_id, TARGET, "implement", "codex", mode="local")
    if outcome:
        feedback.record_outcome(
            run_id, adjudicated_verdict="FAIL", merged=False, durability="abandoned"
        )
    adapters.record_ledger(
        "codex", count=1, event="start", run_id=run_id, log_file=str(log), ts=100
    )
    done = log.parent / "done"
    done.mkdir(exist_ok=True)
    (done / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "ts": 160, **marker}))


def _failure_class(run_id: str):
    with feedback._conn() as c:
        row = c.execute("SELECT failure_class FROM outcomes WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row else None


def test_a_marker_that_does_not_say_whose_rc_it_holds_is_counted_never_classified(world):
    _marked_run(world, "old", {"rc": 137})
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert (summary["infra_classified"], summary["infra_unattributed_marker_rc"]) == (0, 1), summary
    assert _failure_class("old") is None


def test_the_unset_variable_sentinel_is_not_a_signal_death(world):
    _marked_run(world, "unset", {"rc": 999, "rc_of": adapters.MARKER_RC_OF_AGENT})
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert (summary["infra_classified"], summary["infra_unattributed_marker_rc"]) == (0, 0), summary
    assert _failure_class("unset") is None


@pytest.mark.parametrize("rc", [0, 1, 128])
def test_an_agents_own_exit_is_not_a_signal_death(world, rc):
    _marked_run(world, "exited", {"rc": rc, "rc_of": adapters.MARKER_RC_OF_AGENT})
    assert ledger_reconcile.reconcile(adapters.LEDGER)["infra_classified"] == 0
    assert _failure_class("exited") is None


def test_a_drained_pass_prints_both_numbers(world, capsys):
    """Nothing to classify and nothing unattributable must print as two measured zeros."""
    _marked_run(world, "clean", {"rc": 0, "rc_of": adapters.MARKER_RC_OF_AGENT})
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    ledger_reconcile._print_summary(summary, as_json=False)
    printed = capsys.readouterr().out
    assert "agent signal deaths: classified 0, marker rc not the agent's 0" in printed, printed


# ---- a failed run's evidence -------------------------------------------------------------------


def _event(kind: str, item_type: str | None = None, **fields) -> str:
    if item_type is None:
        return json.dumps({"type": kind, **fields})
    return json.dumps({"type": kind, "item": {"id": "item_1", "type": item_type, **fields}})


# Verbatim shape of the 11 live codex transcripts that read as quota exhaustion with no refusal: the
# agent `sed`-ed a doc that discusses usage limits, and the doc's text sits in its own work events.
TRANSCRIPT = [
    _event("thread.started", thread_id="t"),
    _event("item.completed", "error", message="Under-development features enabled: chronicle."),
    _event(
        "item.completed",
        "command_execution",
        command="sed -n '1,220p' ORCHESTRATOR.md",
        aggregated_output=f"... a refused turn prints: {REFUSAL} ...",
        exit_code=0,
        status="completed",
    ),
    _event("item.completed", "agent_message", text=f"The doc quotes {REFUSAL!r}."),
]


def _classify(lines: list[str], agent: str = "codex"):
    return ledger_reconcile._classify_run_log_segment(
        lines, agent, "failed", TARGET, None, successful=False
    )


def _incidents() -> list[dict]:
    if not rate_incidents.INCIDENT_FILE.exists():
        return []
    return [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]


def test_a_failed_codex_run_whose_transcript_reads_about_quotas_is_no_incident(world):
    assert (
        rate_incidents.classify_provider_failure("\n".join(TRANSCRIPT))[2] == "high"
    ), "the fixture must hold the phrase the old path would have read as a refusal"
    assert _classify(TRANSCRIPT) is None
    assert _incidents() == [] and not (rate_incidents.SHED_DIR / "codex").exists()


def test_a_failed_codex_run_keeps_its_harness_error_as_evidence(world):
    """A limit hit AFTER work: not the before-any-work rule's, but still the provider talking."""
    lines = [
        *TRANSCRIPT,
        _event("error", message=REFUSAL),
        _event("turn.failed", error={"message": REFUSAL}),
    ]
    assert ledger_reconcile.provider_limit_before_work(lines) is None
    evidence = _classify(lines)
    assert evidence is not None and evidence["category"] == "quota", evidence
    assert [i["run_id"] for i in _incidents()] == ["failed"]


@pytest.mark.parametrize("agent", ["claude", "cursor", "vibe", "gemini"])
def test_a_failed_text_only_log_is_still_read_whole(world, agent):
    evidence = _classify(["working...", REFUSAL], agent=agent)
    assert evidence is not None and evidence["category"] == "quota", evidence


@pytest.mark.parametrize(
    "line,is_work",
    [
        (_event("item.completed", "command_execution", command="ls"), True),
        (_event("item.started", "command_execution", command="ls"), True),
        (_event("item.completed", "agent_message", text="done"), True),
        (_event("turn.completed", usage={}), True),
        (_event("item.completed", "error", message="warning"), False),
        (_event("turn.failed", error={"message": "x"}), False),
        (_event("error", message="x"), False),
        ("plain stderr line", False),
    ],
)
def test_one_work_predicate_serves_both_readers(line, is_work):
    """The refusal detector and the failure filter must agree on what counts as the agent's work."""
    event = ledger_reconcile._json_event(line)
    assert ledger_reconcile._is_codex_work_event(event or {}) is is_work
    assert (ledger_reconcile._failure_evidence([line]) == []) is is_work
    if is_work:
        refused = [line, _event("turn.failed", error={"message": REFUSAL})]
        assert ledger_reconcile.provider_limit_before_work(refused) is None


def test_the_marker_writer_and_reader_share_one_attribution_value(world, tmp_path):
    cmd = adapters.done_marker_cmd("r", tmp_path / "x.log", "agent_rc", release_rc_var="rel_rc")
    subprocess.run(["bash", "-c", f"agent_rc=3; rel_rc=0; {cmd}"], check=True, timeout=60)
    marker = json.loads((tmp_path / "done" / "r.json").read_text())
    assert marker == {
        "run_id": "r",
        "rc": 3,
        "rc_of": adapters.MARKER_RC_OF_AGENT,
        "ts": marker["ts"],
        "release_rc": 0,
    }, marker
    assert abs(marker["ts"] - time.time()) < 60, marker
