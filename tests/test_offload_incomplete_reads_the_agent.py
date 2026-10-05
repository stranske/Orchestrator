"""A finished codex offload is not failed by a marker it READ (2026-10-05).

THE DEFECT. `dispatcher._offload_incomplete_reason` searched an offload's WHOLE stdout for the
`OFFLOAD_INCOMPLETE` marker and recorded exit 70 on a hit. For codex that stdout is its `exec --json`
stream in every mode but `assess`, which is also every command the agent ran and every file it read.
The repo-audit skill's reference docs quote the marker, so a codex audit that read them was failed
after finishing its work.

MEASURED (read-only, 2026-10-05, a copy of the capacity ledger and each run's own log): 559 codex
offloads carry an exit, 51 of them 70. 30 of the 51 hold the marker only in command output, never in
an agent message, and all 30 end with `turn.completed`. 12 said it in an agent message, in each case
their last one. 9 are `assess` text runs that said it. The finished-but-failed share of codex stream
offloads rose from 1 to 2 a month before September to 23 of 98 in September and 4 of 9 in October.
Downstream, the research-program driver scored 16 of the 30 as learnable FAILs and 6 as `offload`
not-useful verdicts, and retried their units: all three codex attempts at the 2026-10-02
Travel-Plan-Permission audit finished and were failed this way.

THE RULE. For a codex stream, the marker counts only in what the agent SAID LAST, its final
`agent_message`, which is where the offload rules tell it to print the marker and stop. The run's
argv says whether stdout is a stream, never its text. A stream in which the agent said nothing is
"agent returned no message", not "no stdout". Text output (every other agent, and codex `assess`) is
read exactly as before, and gemini's progress-only check is unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_offload_failure_evidence import (
    HEAD,
    MEASURED,
    MEASURED_STDERR,
    _command,
    _event,
    _offload,
    isolated_offload,
)

import dispatcher

# What offload:codex:1782687717830210000 said, as its one message, on 2026-06-28.
STOPPED = (
    "OFFLOAD_INCOMPLETE: cannot write requested audit artifact outside the configured writable "
    "roots in this sandbox"
)
DONE = _event("turn.completed", usage={"input_tokens": 1, "output_tokens": 1})


def _say(text: str) -> str:
    return _event("item.completed", "agent_message", text=text)


def _before(output: str, *, progress_only: bool = True) -> str | None:
    """`_offload_incomplete_reason` as it was at 4f952e3, line for line: the text path's reference."""
    text = (output or "").strip()
    if not text:
        return "agent returned no stdout"
    normalized = re.sub(r"\s+", " ", text.lower())
    if "offload_incomplete:" in normalized:
        return "agent reported OFFLOAD_INCOMPLETE"
    if not progress_only or len(normalized) > 800:
        return None
    for pattern in dispatcher._OFFLOAD_PROGRESS_ONLY_PATTERNS:
        if re.search(pattern, normalized):
            return "agent returned progress-only stdout instead of a deliverable"
    return None


@pytest.fixture
def stores(tmp_path, monkeypatch):
    return isolated_offload(tmp_path, monkeypatch)


@pytest.fixture
def ledger(stores, monkeypatch):
    """The capacity-ledger rows the offload writes, in order."""
    rows: list[dict] = []
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda agent, **row: rows.append(row))
    return rows


def _completion(rows: list[dict]) -> tuple:
    complete = [row for row in rows if row.get("event") == "complete"]
    assert len(complete) == 1, rows
    return complete[0]["exit"], complete[0]["error"]


def test_a_finished_run_whose_reads_quote_the_marker_exits_0(monkeypatch, stores, ledger):
    stdout = "\n".join(MEASURED)
    assert (
        "OFFLOAD_INCOMPLETE" in stdout
    ), "the fixture must quote the marker in what the agent read"
    result = _offload(monkeypatch, stores, "codex", 0, stdout, MEASURED_STDERR)
    assert result["exit"] == 0 and "error" not in result and "raw_exit" not in result, result
    assert _completion(ledger) == (0, None)
    assert "offload marked failed" not in Path(result["log"]).read_text()


def test_a_marker_the_agent_said_last_still_fails(monkeypatch, stores, ledger):
    stdout = "\n".join([*HEAD, *_command("touch ../outside.md", "denied"), _say(STOPPED), DONE])
    result = _offload(monkeypatch, stores, "codex", 0, stdout)
    assert (result["exit"], result.get("raw_exit"), result.get("error")) == (
        70,
        0,
        "agent reported OFFLOAD_INCOMPLETE",
    ), result
    assert _completion(ledger) == (70, "agent reported OFFLOAD_INCOMPLETE")


@pytest.mark.parametrize(
    "earlier",
    [
        "The playbook says to print `OFFLOAD_INCOMPLETE: <reason>` if a write is refused; "
        "writing the report inside the workspace instead.",
        # Said and then not acted on: the rule is "print it and stop", and this agent went on to
        # deliver. Its last message is its result. 0 of the 12 recorded agent-message markers sat
        # anywhere but the last message, so this choice moves no recorded run.
        "OFFLOAD_INCOMPLETE: gh is unauthenticated",
    ],
    ids=["an-earlier-message-quotes-the-rule", "declared-then-finished-anyway"],
)
def test_only_the_last_message_is_the_agents_verdict(monkeypatch, stores, ledger, earlier):
    stdout = "\n".join(
        [*HEAD, _say(earlier), *_command("pytest -q", "12 passed"), _say("Report staged."), DONE]
    )
    result = _offload(monkeypatch, stores, "codex", 0, stdout)
    assert result["exit"] == 0 and "error" not in result, result
    assert _completion(ledger) == (0, None)


def test_a_stream_with_no_message_says_so(monkeypatch, stores):
    """No deliverable, as with empty stdout, but named for what is missing: there WAS stdout."""
    stdout = "\n".join([*HEAD, *_command("pytest -q", "12 passed"), DONE])
    result = _offload(monkeypatch, stores, "codex", 0, stdout)
    assert (result["exit"], result.get("error")) == (70, "agent returned no message"), result
    empty = _offload(monkeypatch, stores, "codex", 0, "")
    assert (empty["exit"], empty.get("error")) == (70, "agent returned no stdout"), empty


def test_the_argv_decides_never_the_text(monkeypatch, stores):
    """The same bytes, two transports: only a run that asked for the stream is read as one."""
    stdout = "\n".join(MEASURED)
    assert _offload(monkeypatch, stores, "codex", 0, stdout)["exit"] == 0
    assess = _offload(monkeypatch, stores, "codex", 0, stdout, mode="assess")
    assert (assess["exit"], assess.get("error")) == (
        70,
        "agent reported OFFLOAD_INCOMPLETE",
    ), assess


@pytest.mark.parametrize("agent", ["claude", "cursor", "gemini", "vibe"])
def test_other_agents_stdout_is_still_read_whole(monkeypatch, stores, agent):
    result = _offload(monkeypatch, stores, agent, 0, "\n".join(MEASURED))
    assert (result["exit"], result.get("error")) == (
        70,
        "agent reported OFFLOAD_INCOMPLETE",
    ), result


TEXTS = [
    "",
    "   \n  ",
    STOPPED,
    "Reviewed it.\noffload_incomplete: lower case, mid-text",
    "I am waiting for the pytest suite execution to finish. I will inspect the results as soon as "
    "it completes.",
    "No active tools are needed at the moment. Waiting for the full product verification check "
    "running as `task-85` to finish.",
    "still running " + "x" * 900,
    "Reviewed three files and found no actionable issues.",
    '{"verdict": "PASS", "findings": []}',
    "\n".join(MEASURED),
]


@pytest.mark.parametrize("progress_only", [True, False], ids=["progress-check", "no-progress"])
@pytest.mark.parametrize("text", TEXTS, ids=[f"text-{i}" for i in range(len(TEXTS))])
def test_text_is_read_exactly_as_before(text, progress_only):
    reason = dispatcher._offload_incomplete_reason(text, progress_only=progress_only)
    assert reason == _before(text, progress_only=progress_only)


@pytest.mark.parametrize(
    "stdout,error",
    [
        (
            "I am waiting for the pytest suite execution to finish. I will inspect the results as "
            "soon as it completes.",
            "agent returned progress-only stdout instead of a deliverable",
        ),
        ("", "agent returned no stdout"),
        ("Reviewed three files and found no actionable issues.", None),
    ],
    ids=["progress-only", "empty", "deliverable"],
)
def test_gemini_progress_only_is_unchanged(monkeypatch, stores, stdout, error):
    result = _offload(monkeypatch, stores, "gemini", 0, stdout)
    assert (result["exit"], result.get("error")) == ((70, error) if error else (0, None)), result
