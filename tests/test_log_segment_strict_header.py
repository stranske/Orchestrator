"""A run's log segment ends where the NEXT RUN begins, not at the next `===` line (2026-10-11).

THE DEFECT. `ledger_reconcile._log_segment` found a run's `=== <stamp> ... run_id=<id> ===` header,
then ended the segment at the next line that merely STARTED with `===`. pytest prints
`===== test session starts =====` and `===== N passed =====`, so a run that ran its tests was cut off
at its first banner, and every reader of the segment saw the truncated run: usage, cost, resume
token, owner questions, the provider-refusal evidence and the rate-incident split. And a header glued
onto the previous run's unterminated last line was never found, because it does not start a line.

MEASURED (read-only 2026-10-11, scratch copies of the dispatch and experiment logs, the capacity
ledger and the Brain). 7,782 ledger run ids, 7,573 with a log on disk. Of 5,975 lines starting
`===`, 5,784 are run headers and 191 are not (170 pytest banners) in 71 files; 12 headers are glued
mid-line. 387 runs read a different segment under this rule, every one LONGER (385 cut at a
non-header `===` line, 2 whose own header repeats), and for all 387 the values a learner reads are
unchanged: usage, cost, resume token, owner questions, provider-refusal evidence and the incident
verdict. No recorded Brain row (costs, resume_tokens, owner_questions, transient_infra) would change
on a re-read, so the reader is not gated on a start-time cutoff.

THE RULE. A header is `credential_redaction.RUN_HEADER`, the one definition, found anywhere in a
line. The run's own header is the FIRST naming it. The segment ends at the first later header that
names another run, is not stamped before this run's own, and, when the capacity ledger says who
writes the file, names one of those writers. Every other header-shaped text is the run's OUTPUT: an
agent that prints a run log prints whole, valid headers, of an older run (stamped earlier) or of
another file (not a writer of this one).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import adapters
import feedback
import ledger_reconcile
import rate_incidents

BANNER_START = "=" * 29 + " test session starts " + "=" * 29
BANNER_END = "=" * 33 + " 3 passed in 0.12s " + "=" * 33


def _header(stamp: str, run_id: str, what: str = "dispatch codex/full -> o/r#7 [testgen]") -> str:
    return f"=== 2026-10-05T{stamp}Z {what} cwd=/tmp run_id={run_id} ==="


def _log(tmp_path: Path, text: str, name: str = "shared.codex.log") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# ---- the reader ------------------------------------------------------------------------------


def test_a_pytest_banner_inside_a_run_is_its_output(tmp_path):
    log = _log(
        tmp_path,
        "\n".join(
            [
                _header("00:00:00", "r1"),
                "running the suite",
                BANNER_START,
                "tests/test_x.py ...",
                BANNER_END,
                "done",
                _header("00:05:00", "r2"),
                "next run",
            ]
        )
        + "\n",
    )
    assert ledger_reconcile._log_segment(log, "r1") == [
        "running the suite",
        BANNER_START,
        "tests/test_x.py ...",
        BANNER_END,
        "done",
    ]
    assert ledger_reconcile._log_segment(log, "r2") == ["next run"]


def test_a_header_glued_onto_the_last_line_ends_the_run_and_starts_the_next(tmp_path):
    log = _log(
        tmp_path,
        _header("00:00:00", "r1") + "\nfirst run, no trailing newline"
        # The next writer appends its header to the unterminated line.
        + _header("00:05:00", "r2") + "\nsecond run\n",
    )
    assert ledger_reconcile._log_segment(log, "r1") == ["first run, no trailing newline"]
    assert ledger_reconcile._log_segment(log, "r2") == ["second run"]


def test_an_older_runs_header_the_agent_printed_is_output(tmp_path):
    echoed = _header("00:00:00", "r0")
    log = _log(
        tmp_path,
        "\n".join(
            [
                echoed,
                "old output",
                _header("00:05:00", "r1"),
                "reading my target's log:",
                echoed,
                "old output",
                "done",
                _header("00:10:00", "r2"),
                "next run",
            ]
        )
        + "\n",
    )
    assert ledger_reconcile._log_segment(log, "r1") == [
        "reading my target's log:",
        echoed,
        "old output",
        "done",
    ]


def test_a_later_header_from_another_file_ends_the_run_only_when_the_writers_are_unknown(
    tmp_path,
):
    foreign = _header("00:07:00", "elsewhere", what="offload codex/review")
    log = _log(
        tmp_path,
        "\n".join(
            [
                _header("00:05:00", "r1"),
                "tailing a concurrent offload's log:",
                foreign,
                "its output",
                "done",
                _header("00:10:00", "r2"),
                "next run",
            ]
        )
        + "\n",
    )
    # The ledger names the file's writers: a run that is not one of them did not write here.
    assert ledger_reconcile._log_segment(log, "r1", {"r1", "r2"}) == [
        "tailing a concurrent offload's log:",
        foreign,
        "its output",
        "done",
    ]
    # Without the ledger a later-stamped header is taken at its word: the run ends there.
    assert ledger_reconcile._log_segment(log, "r1") == ["tailing a concurrent offload's log:"]
    assert ledger_reconcile._log_segment(log, "r2", {"r1", "r2"}) == ["next run"]


def test_the_first_header_naming_the_run_is_its_own(tmp_path):
    own = _header("00:05:00", "r1")
    log = _log(tmp_path, "\n".join([own, "before", own, "after"]) + "\n")
    assert ledger_reconcile._log_segment(log, "r1") == ["before", own, "after"]


def test_header_shaped_text_without_a_stamp_is_output(tmp_path):
    loose = "=== something run_id=r9 ==="
    log = _log(tmp_path, "\n".join([_header("00:05:00", "r1"), loose, "after"]) + "\n")
    assert ledger_reconcile._log_segment(log, "r1") == [loose, "after"]
    assert ledger_reconcile._log_segment(log, "r9") == []


def test_a_log_with_no_run_header_is_read_whole_and_a_mixed_one_is_not(tmp_path):
    dedicated = _log(tmp_path, f"{BANNER_START}\nusage event\n{BANNER_END}\n", "dedicated.log")
    assert ledger_reconcile._log_segment(dedicated, "run") == [
        BANNER_START,
        "usage event",
        BANNER_END,
    ]
    mixed = _log(tmp_path, _header("00:00:00", "other") + "\nits output\n", "mixed.log")
    assert ledger_reconcile._log_segment(mixed, "run") == []
    assert ledger_reconcile._log_segment(tmp_path / "absent.log", "run") == []


def test_the_writers_are_trusted_only_when_they_include_the_run(tmp_path):
    log = tmp_path / "shared.codex.log"
    rows = [
        {"run_id": "r1", "event": "start", "log_file": str(log)},
        {"run_id": "r2", "event": "start", "log_file": str(log)},
        {"run_id": "r2", "event": "complete", "log_file": str(log)},
        {"run_id": "r3", "event": "start", "log_file": str(tmp_path / "other.log")},
        {"run_id": "r4", "event": "start"},
    ]
    index = ledger_reconcile._writers_by_log(rows)
    assert ledger_reconcile._log_writers(index, log, "r1") == {"r1", "r2"}
    # A ledger that does not record this run writing here cannot say who else did: unknown, never
    # an empty set, which would make every later header this run's output.
    assert ledger_reconcile._log_writers(index, log, "r3") is None
    assert ledger_reconcile._log_writers(index, tmp_path / "unknown.log", "r1") is None


# ---- the consumers ---------------------------------------------------------------------------


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A private Brain, capacity ledger and incident authority: nothing reaches ~/.codex."""
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")
    return tmp_path


def _start(run_id: str, log: Path) -> None:
    feedback.record_run(run_id, "o/r#7", "testgen", "codex", mode="local")
    adapters.record_ledger("codex", count=1, event="start", run_id=run_id, log_file=str(log))


def test_reconcile_reads_usage_cost_and_questions_after_a_pytest_banner(world):
    question = {"question": "Which fixture layout?", "default": "keep the current one"}
    log = world / "o_r_7.codex.log"
    _start("r1", log)
    _start("r2", log)
    log.write_text(
        "\n".join(
            [
                _header("00:00:00", "r1"),
                BANNER_START,
                BANNER_END,
                json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}
                ),
                json.dumps({"total_cost_usd": 0.42}),
                "OWNER_QUESTION: " + json.dumps(question),
                _header("00:05:00", "r2"),
                json.dumps(
                    {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}
                ),
            ]
        )
        + "\n"
    )
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert summary["owner_questions_recorded"] == 1, summary
    with feedback._conn() as c:
        costs = {
            row[0]: row[1:]
            for row in c.execute(
                "SELECT run_id, tokens_in, tokens_out, cost_usd, source FROM costs"
            ).fetchall()
        }
        asked = c.execute("SELECT run_id, question FROM owner_questions").fetchall()
    assert costs["r1"] == (100, 50, 0.42, "ledger"), costs
    assert costs["r2"] == (7, 3, 0.0, "ledger"), costs
    assert asked == [("r1", question["question"])], asked


def test_completion_reads_past_a_header_the_ledger_says_is_not_a_writer(world, monkeypatch):
    monkeypatch.setattr(
        ledger_reconcile.feedback, "record_completion_event", lambda *args, **kwargs: None
    )
    log = world / "o_r_7.codex.log"
    _start("current", log)
    log.write_text(
        "\n".join(
            [
                _header("00:05:00", "current"),
                "tailing a concurrent offload's log:",
                _header("00:07:00", "elsewhere", what="offload codex/review"),
                "HTTP 429 Too Many Requests",
            ]
        )
        + "\n"
    )
    ledger_reconcile.record_completion("current", "codex", log_file=str(log), exit_code=1)
    rows = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    # Cut at the foreign header, the segment would hold no 429 and no incident would be recorded.
    assert [(row["run_id"], row["category"]) for row in rows] == [("current", "rate_limit")], rows
