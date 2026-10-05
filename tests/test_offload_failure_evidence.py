"""A failed codex run's own work is not provider evidence on the synchronous paths (2026-10-04).

THE DEFECT. `dispatcher.offload` classified a failed run's WHOLE stdout as provider evidence. For
codex that stdout is its `exec --json` stream, which is also every command the agent ran and every
file it read. Two codex offloads (offload:codex:1789849098467771000 on 2026-09-19 and
offload:codex:1789871765003498000 on 2026-09-20), both repo audits, printed ORCHESTRATOR.md, whose
paragraph on this very incident authority says "quota exhausted". Each was recorded as a codex quota
incident and shed the seat. Neither stream holds a refusal. Both ended with `turn.completed`, and
neither agent said `OFFLOAD_INCOMPLETE`: their exit 70 came from the repo-audit skill's reference
docs, which quote that marker.

MEASURED (read-only, 2026-10-05, the capacity ledger and each run's own log): 76 of the 577 recorded
codex offloads exited non-zero. Three read as a provider limit only through their own work events:
those two, and one from 2026-07-03, before incidents were recorded. The 10 real refusals among the 76
still read as one without the work events.

THE RULE. A failed run's stdout is read less codex's own work events, through the one predicate the
completion reconciler already applies to a failed run's log segment (stranske/Orchestrator#453), now
`rate_incidents.failure_evidence`. Harness events (`error`, `turn.failed`) and stderr stay. Every
other agent's stdout is text and is read whole. A successful run's stdout counts only through the
strict envelope, as before.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import adapters
import dispatcher
import ledger_reconcile
import rate_incidents

REFUSAL = (
    "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
    "credits or try again at Sep 28th, 2026 8:26 PM."
)
STREAM_429 = "stream error: exceeded retry limit, last status: 429 Too Many Requests"
WARNING = "Under-development features enabled: chronicle."
# What the two measured runs printed: a repo-audit skill reference, and ORCHESTRATOR.md:51-53.
SKILL_NOTE = (
    '- **codex offload sandbox refuses disk-first writes OUTSIDE `--cwd`** ("OFFLOAD_INCOMPLETE: '
    'cannot write ... outside the configured writable roots").'
)
MANUAL = (
    "creates `~/.codex/handoff/capacity-shed/<agent>` markers for authoritative errors (quota "
    "exhausted,\n  resource_exhausted, and ActionRequiredError with explicit quota evidence)"
)


def _event(kind: str, item_type: str | None = None, **fields) -> str:
    # Raw UTF-8, as codex prints it: the refusal's "You’ve" reaches the text classifier unescaped.
    if item_type is None:
        return json.dumps({"type": kind, **fields}, ensure_ascii=False)
    event = {"type": kind, "item": {"id": "item_1", "type": item_type, **fields}}
    return json.dumps(event, ensure_ascii=False)


def _command(command: str, output: str) -> list[str]:
    return [
        _event("item.started", "command_execution", command=command, status="in_progress"),
        _event(
            "item.completed",
            "command_execution",
            command=command,
            aggregated_output=output,
            exit_code=0,
            status="completed",
        ),
    ]


HEAD = [
    _event("thread.started", thread_id="t"),
    _event("item.completed", "error", message=WARNING),
    _event("turn.started"),
]
# The measured shape: the agent reads the two docs, writes its report, and finishes its turn.
MEASURED = [
    *HEAD,
    *_command("/bin/zsh -lc \"sed -n '1,$p' reference/playbook.md\"", f"...\n{SKILL_NOTE}\n..."),
    *_command("/bin/zsh -lc \"sed -n '1,$p' ORCHESTRATOR.md\"", f"...\n{MANUAL}\n..."),
    _event("item.completed", "file_change", changes=[{"path": "report.md", "kind": "add"}]),
    _event("item.completed", "agent_message", text="Audit complete; report staged."),
    _event("turn.completed", usage={"input_tokens": 1, "output_tokens": 1}),
]
MEASURED_STDERR = (
    "Reading additional input from stdin...\n"
    "ERROR codex_core::tools::router: error=patch rejected: writing outside of the project\n"
)
# The real refusal shape (offload:codex:1790322148167312000, 2026-09-25): refused before any work.
REFUSED = [
    *HEAD,
    _event("error", message=REFUSAL),
    _event("turn.failed", error={"message": REFUSAL}),
]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Private incident authority, shed markers and offload logs; no agent CLI, Brain or ledger."""
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    # Each agent's runtime home (vibe's carries a config copy) is built per offload; keep it here.
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 1)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(dispatcher, "_agent_log_tail_from_argv", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *args: (False, "test")
    )
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args, **kwargs: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *args, **kwargs: None)
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    return tmp_path


def _offload(monkeypatch, stores, agent: str, returncode: int, stdout: str, stderr: str = ""):
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["agent"], returncode, stdout, stderr),
    )
    return dispatcher.offload(agent, "test", cwd=str(stores))


def _incidents() -> list[dict]:
    if not rate_incidents.INCIDENT_FILE.exists():
        return []
    return [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]


def _shed(agent: str) -> bool:
    return (rate_incidents.SHED_DIR / agent).exists()


@pytest.mark.parametrize(
    "returncode,exit_code",
    [(0, 70), (1, 1)],
    ids=["measured-exit-70-from-the-quoted-marker", "codex-exited-1-after-reading-the-doc"],
)
def test_a_failed_codex_offload_whose_work_quotes_a_limit_is_no_incident(
    monkeypatch, stores, returncode, exit_code
):
    stdout = "\n".join(MEASURED)
    assert (
        rate_incidents.classify_provider_failure(stdout)[2] == "high"
    ), "the fixture must hold the phrase the whole-stdout read took for a refusal"
    result = _offload(monkeypatch, stores, "codex", returncode, stdout, MEASURED_STDERR)
    assert result["exit"] == exit_code, result
    assert result["rate_incident_evidence"]["confidence"] == "none", result
    assert _incidents() == [] and not _shed("codex")


def test_a_codex_refusal_still_records_and_sheds(monkeypatch, stores):
    result = _offload(monkeypatch, stores, "codex", 1, "\n".join(REFUSED), "Reading input...\n")
    assert result["rate_incident_evidence"]["category"] == "quota", result
    rows = _incidents()
    assert [(r["run_id"], r["surface"], r["category"]) for r in rows] == [
        (result["run_id"], "dispatcher.offload", "quota")
    ], rows
    assert "usage limit" in rows[0]["evidence_excerpt"] and _shed("codex")


@pytest.mark.parametrize(
    "harness,category",
    [
        (
            [_event("error", message=REFUSAL), _event("turn.failed", error={"message": REFUSAL})],
            "quota",
        ),
        ([_event("error", message=STREAM_429)], "rate_limit"),
    ],
    ids=["usage-limit-after-work", "stream-429-error-event-alone"],
)
def test_a_mid_run_harness_error_still_sheds(monkeypatch, stores, harness, category):
    """A limit hit after work: the work events are dropped, the harness's own events stay."""
    work = [*HEAD, *_command("pytest -q", "12 passed in 0.4s")]
    result = _offload(monkeypatch, stores, "codex", 1, "\n".join([*work, *harness]))
    assert result["rate_incident_evidence"]["category"] == category, result
    assert [r["category"] for r in _incidents()] == [category] and _shed("codex")


@pytest.mark.parametrize("agent", ["claude", "cursor", "gemini", "vibe"])
def test_a_text_only_failure_is_still_read_whole(monkeypatch, stores, agent):
    stdout = (
        "Starting the review.\nYou've hit your usage limit for this period.\nNotes so far: none."
    )
    result = _offload(monkeypatch, stores, agent, 1, stdout)
    assert result["rate_incident_evidence"]["category"] == "quota", result
    assert [r["agent"] for r in _incidents()] == [agent] and _shed(agent)


@pytest.mark.parametrize("agent", ["claude", "cursor", "gemini", "vibe", "aider"])
def test_only_codex_stdout_is_read_less_its_work(agent):
    """By design the filter reads codex's schema on codex's stdout only. 0 of 4,706 non-codex
    dispatch logs on 2026-10-05 held a codex work-event line, so the reconcile pass, which filters
    every agent's segment, and this cannot disagree on any recorded run."""
    stdout = "\n".join(MEASURED)
    assert rate_incidents.failed_stdout_evidence(agent, stdout) == stdout
    kept = rate_incidents.failed_stdout_evidence("codex", stdout).splitlines()
    assert kept == [HEAD[0], HEAD[1], HEAD[2]], kept


def test_a_successful_offload_keeps_the_envelope_path(monkeypatch, stores):
    clean = [line for line in MEASURED if "OFFLOAD_INCOMPLETE" not in line]
    result = _offload(monkeypatch, stores, "codex", 0, "\n".join(clean))
    assert result["exit"] == 0 and result["rate_incident_evidence"]["confidence"] == "none"
    assert _incidents() == []
    result = _offload(monkeypatch, stores, "codex", 0, "partial answer\n[resource_exhausted]")
    assert result["exit"] == 0 and result["rate_incident_evidence"]["category"] == "capacity"
    assert [r["run_id"] for r in _incidents()] == [result["run_id"]] and _shed("codex")


@pytest.mark.parametrize(
    "lines,expected", [(MEASURED, None), (REFUSED, "quota")], ids=["measured", "refusal"]
)
def test_the_offload_and_the_reconcile_pass_read_one_run_alike(
    monkeypatch, stores, lines, expected
):
    """Both observers of a run share one incident (one idempotency key), so the second must not
    record what the first refused: each reads the run through the same predicate."""
    result = _offload(monkeypatch, stores, "codex", 1, "\n".join(lines), MEASURED_STDERR)
    segment = ledger_reconcile._log_segment(Path(result["log"]), result["run_id"])
    assert segment, "the offload must have written its own log segment"
    evidence = ledger_reconcile._classify_run_log_segment(
        segment, "codex", result["run_id"], shed=False, successful=False
    )
    assert (evidence["category"] if evidence else None) == expected
    assert result["rate_incident_evidence"]["category"] == (expected or "unknown")
    assert [r["category"] for r in _incidents()] == ([expected] if expected else [])


@pytest.mark.parametrize(
    "lines,expected", [(MEASURED, []), (REFUSED, ["quota"])], ids=["measured", "refusal"]
)
def test_the_synchronous_adapter_applies_the_same_rule(monkeypatch, stores, lines, expected):
    """adapters.dispatch has no production caller today; it is the one other reader of a failed
    codex stdout, so it reads it the same way."""
    calls = iter(
        (
            subprocess.CompletedProcess(["agent"], 1, "\n".join(lines), ""),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        )
    )
    monkeypatch.setattr(adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(adapters.subprocess, "run", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(adapters, "record_ledger", lambda *args, **kwargs: None)
    adapters.dispatch("codex", "test", cwd=str(stores))
    assert [r["category"] for r in _incidents()] == expected
    assert _shed("codex") is bool(expected)
