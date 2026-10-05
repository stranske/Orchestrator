"""A run the provider refused before it did any work is infrastructure, not capability (2026-10-04).

THE DEFECT. Five codex testgen delegates dispatched 2026-09-15 11:31-11:33Z each died in 4-5 s on
"You've hit your usage limit" before running a single command. A refusal is not a signal death, so
the only infra classifier (`ledger_reconcile`, keyed on a done marker's rc>128) never saw them, and
outcome ingest scored them from whatever their issues' PRs did: Counter_Risk#1072 stayed a plain FAIL
inside `relearn_quality`'s population, and three were credited as PASSes. The exit status the
completion step was handed, 0, kept the rate-incident gate shut, so none of the five recorded an
incident or shed codex, and the next three were dispatched into the same wall two minutes later.
(That 0 was the claims release's status, not codex's, which exits 1 on a refused turn: see
test_dispatch_rc_is_the_agents.py.)

THE RULE. `ledger_reconcile.provider_limit_before_work` reads codex's `exec --json` stream, the
only log in the fleet that can show nothing ran. It needs the harness's own terminal `turn.failed`
event, rated a high-confidence provider limit by `rate_incidents.classify_provider_failure` (the one
text authority), and no work event anywhere in the segment. A phrase anywhere else in the log is
never enough, and a text-only log (claude, cursor, vibe, gemini) is UNKNOWN and left as recorded.
`reconcile` marks such a run `transient_infra` when a learner still scores its row, the row is not
merged, and the run started under the rule; every other case is counted under its reason. The same
evidence opens the incident path, so the seat sheds until the time the provider names.
"""

from __future__ import annotations

import json
import time

import pytest

import adapters
import feedback
import ledger_reconcile
import rate_incidents

REFUSAL = (
    "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
    "credits or try again at Sep 19th, 2026 3:11 AM."
)
# The far-future spelling, for the one test that needs `reset_at` to be in the future.
FUTURE_REFUSAL = "You've hit your usage limit. Try again at Jan 5th, 2099 3:11 AM."
# Verbatim shape of the 2026-09-15 segments, minus the per-run ids.
WARNING = {
    "type": "item.completed",
    "item": {
        "id": "item_0",
        "type": "error",
        "message": "Under-development features enabled: chronicle.",
    },
}


def _refused(message: str = REFUSAL, *, before: tuple = (), after: tuple = ()) -> list[str]:
    events = [
        {"type": "thread.started", "thread_id": "01a0a4d5-f89e-7180-8799-97fb022e1b93"},
        WARNING,
        *before,
        {"type": "turn.started"},
        {"type": "error", "message": message},
        {"type": "turn.failed", "error": {"message": message}},
        *after,
    ]
    return ["Reading additional input from stdin...", *(json.dumps(e) for e in events)]


def _item(kind: str, item_type: str, **fields) -> dict:
    return {"type": kind, "item": {"id": "item_1", "type": item_type, **fields}}


# ---- the detector ----------------------------------------------------------------------------


def test_a_refusal_before_any_work_is_found():
    found = ledger_reconcile.provider_limit_before_work(_refused())
    assert found == {
        "category": "quota",
        "subcategory": "quota_exhausted",
        "message": REFUSAL,
    }, found


def test_the_curly_apostrophe_spelling_is_the_same_refusal():
    curly = REFUSAL.replace("You've", "You’ve")
    found = ledger_reconcile.provider_limit_before_work(_refused(curly))
    assert found is not None and found["subcategory"] == "quota_exhausted", found


@pytest.mark.parametrize(
    "work",
    [
        _item("item.started", "command_execution", command="git status"),
        _item("item.completed", "command_execution", command="pytest", exit_code=1),
        _item("item.completed", "file_change", changes=[]),
        _item("item.completed", "agent_message", text="Looking at the tests."),
        _item("item.completed", "reasoning", text="Plan."),
        _item("item.updated", "todo_list", items=[]),
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ],
    ids=lambda e: f"{e['type']}:{(e.get('item') or {}).get('type', '-')}",
)
@pytest.mark.parametrize("where", ["before", "after"])
def test_any_work_event_means_it_is_not_this_rule(work, where):
    """A run that did anything is not a run the provider refused before work, wherever the work sits."""
    lines = _refused(**{where: (work,)})
    assert ledger_reconcile.provider_limit_before_work(lines) is None


@pytest.mark.parametrize(
    "lines",
    [
        # The phrase inside a command's captured output: the run was working.
        [
            json.dumps(
                _item(
                    "item.completed",
                    "command_execution",
                    command="grep -r 'usage limit' .",
                    aggregated_output=REFUSAL,
                )
            )
        ],
        # The phrase in the model's own words, with no terminal failure at all.
        [json.dumps(_item("item.completed", "agent_message", text=REFUSAL))],
        # The phrase as a bare text line, not the harness's event.
        [REFUSAL],
        # A top-level `error` alone is a warning stream, not the terminal failure.
        [json.dumps({"type": "error", "message": REFUSAL})],
    ],
    ids=["command-output", "agent-message", "bare-text", "error-event-only"],
)
def test_the_phrase_outside_the_terminal_event_is_not_a_refusal(lines):
    assert ledger_reconcile.provider_limit_before_work(lines) is None


@pytest.mark.parametrize(
    "lines",
    [
        # claude -p: the 2026-06-27 Manager-Database#1264 segment, verbatim.
        ["You've hit your weekly limit · resets 12am (America/Chicago)"],
        # cursor: stderr only, text output.
        [
            "[stderr]",
            "ActionRequiredError: Increase limits for faster responses You're out of usage. "
            "Switch to Auto, or ask your admin to increase your limit to continue.",
            "[orchestrator] offload marked failed: agent exited 1",
        ],
        # gemini/agy: the same shape covered both a 6 s death and a run that worked 6 minutes.
        [
            "[stderr]",
            "Error: Individual quota reached. Please upgrade your subscription to increase your "
            "limits. Resets in 2h47m33s.",
            "[orchestrator] offload marked failed: agent exited 1",
        ],
    ],
    ids=["claude", "cursor", "gemini"],
)
def test_a_text_only_log_is_unknown_and_never_classified(lines):
    """Those CLIs log final text only, so the log cannot show that nothing ran before the limit."""
    assert ledger_reconcile.provider_limit_before_work(lines) is None


@pytest.mark.parametrize(
    "message",
    [
        "stream disconnected before completion: error sending request for url",
        "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header",
        "Your input exceeds the context window of this model.",
    ],
)
def test_a_failed_turn_that_is_not_a_provider_limit_is_not_this_rule(message):
    assert ledger_reconcile.provider_limit_before_work(_refused(message)) is None


# ---- reconcile -------------------------------------------------------------------------------


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


def _dispatch(world, run_id: str, lines: list[str], *, ts: int | None = None, outcome=None):
    """One run as the fleet records it: a Brain decision, a ledger start row, its log segment."""
    feedback.record_run(run_id, "o/r#7", "testgen", "codex", mode="local")
    if ts is not None:
        with feedback._conn() as c:
            c.execute("UPDATE runs SET ts=? WHERE run_id=?", (ts, run_id))
    if outcome is not None:
        feedback.record_outcome(run_id, **outcome)
    log = world / f"{run_id}.codex.log"
    log.write_text(
        f"=== 2026-10-05T00:00:00Z dispatch codex/full -> o/r#7 [testgen] cwd=/tmp "
        f"run_id={run_id} ===\n" + "\n".join(lines) + "\n"
    )
    adapters.record_ledger("codex", count=1, event="start", run_id=run_id, log_file=str(log))


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT verifier_verdict, adjudicated_verdict, merged, ci_status, durability, "
            "durability_checked_ts, notes, failure_class FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


FAIL = {"adjudicated_verdict": "FAIL", "merged": False, "durability": "abandoned"}
ZEROS = {"seen": 0, **{bucket: 0 for bucket in ledger_reconcile.PROVIDER_LIMIT_BUCKETS}}


def test_reconcile_classifies_a_refused_failure_and_no_learner_scores_it(world):
    _dispatch(world, "refused", _refused(), outcome=FAIL)
    _dispatch(world, "real-failure", [json.dumps({"type": "turn.completed"})], outcome=FAIL)
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert summary["provider_limit_deaths"] == {**ZEROS, "seen": 1, "classified": 1}
    row = _row("refused")
    assert row[-1] == "transient_infra", row
    assert "[infra: provider limit before any work: quota_exhausted]" in row[-2], row[-2]
    assert _row("real-failure")[-1] is None, "a run that worked keeps its verdict"
    version = feedback.relearn_quality({"testgen": {"codex": 0.5}})
    with feedback._conn() as c:
        prior, posterior, n_obs = c.execute(
            "SELECT prior, posterior, n_obs FROM route_weights "
            "WHERE version=? AND task_type='testgen' AND agent='codex'",
            (version,),
        ).fetchone()
    assert n_obs == 1, f"the learner scored the refused run: n_obs={n_obs}"
    assert posterior < prior, "the control FAIL must still lower the posterior"


def test_each_reason_is_counted_once_and_untouched_rows_stay_byte_identical(world):
    before_rule = ledger_reconcile.PROVIDER_LIMIT_INFRA_SINCE - 1
    _dispatch(world, "a-forward-fail", _refused(), outcome=FAIL)
    _dispatch(
        world,
        "b-unattributed",
        _refused(),
        outcome={
            "durability": "abandoned",
            "failure_class": feedback.UNATTRIBUTED_CLOSING_PR,
            "notes": "issue closed by a PR no candidate branch produced (#9)",
        },
    )
    _dispatch(
        world,
        "c-merged-pass",
        _refused(),
        outcome={"adjudicated_verdict": "PASS", "merged": True, "durability": "durable"},
    )
    _dispatch(world, "d-before-rule", _refused(), ts=before_rule, outcome=FAIL)
    _dispatch(world, "e-no-outcome", _refused())
    untouched = ("b-unattributed", "c-merged-pass", "d-before-rule")
    recorded = {run_id: _row(run_id) for run_id in untouched}
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    deaths = summary["provider_limit_deaths"]
    assert deaths == {
        "seen": 5,
        "classified": 1,
        "excluded": 1,
        "merged": 1,
        "predates_rule": 1,
        "no_outcome_row": 1,
    }, deaths
    assert deaths["seen"] == sum(deaths[b] for b in ledger_reconcile.PROVIDER_LIMIT_BUCKETS)
    assert {run_id: _row(run_id) for run_id in untouched} == recorded
    assert _row("e-no-outcome") is None, "the rule must never invent an outcome row"


def test_a_drained_pass_prints_zeros_not_nothing(world, capsys):
    """Nothing to classify must read as measured zero, both in the JSON and in the printed line."""
    _dispatch(world, "worked", [json.dumps({"type": "turn.completed"})], outcome=FAIL)
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert summary["provider_limit_deaths"] == ZEROS
    ledger_reconcile._print_summary(summary, as_json=False)
    printed = capsys.readouterr().out
    assert (
        "provider refusals before any work: seen 0, classified 0, excluded 0, merged 0, "
        "predates_rule 0, no_outcome_row 0"
    ) in printed, printed


def test_the_next_pass_reads_a_classified_run_as_excluded(world):
    _dispatch(world, "refused", _refused(), outcome=FAIL)
    first = ledger_reconcile.reconcile(adapters.LEDGER)["provider_limit_deaths"]
    second = ledger_reconcile.reconcile(adapters.LEDGER)["provider_limit_deaths"]
    assert first == {**ZEROS, "seen": 1, "classified": 1}
    assert second == {**ZEROS, "seen": 1, "excluded": 1}


def test_a_dry_run_counts_what_it_would_classify_and_writes_nothing(world):
    _dispatch(world, "refused", _refused(), outcome=FAIL)
    recorded = _row("refused")
    summary = ledger_reconcile.reconcile(adapters.LEDGER, dry_run=True)
    assert summary["provider_limit_deaths"] == {**ZEROS, "seen": 1, "classified": 1}
    assert _row("refused") == recorded
    assert not rate_incidents.INCIDENT_FILE.exists(), "a dry run must record no incident"


@pytest.mark.parametrize("source", ["ccusage", "langsmith"])
def test_a_richer_cost_row_does_not_hide_the_refusal(world, source):
    """That skip ends the run's whole pass, so the rule must run before it."""
    _dispatch(world, "refused", _refused(), outcome=FAIL)
    feedback.record_cost("refused", tokens_in=0, tokens_out=0, source=source)
    summary = ledger_reconcile.reconcile(adapters.LEDGER)
    assert summary["skipped"].get(f"{source}_cost_exists") == 1, summary["skipped"]
    assert summary["provider_limit_deaths"]["classified"] == 1
    assert _row("refused")[-1] == "transient_infra"


# ---- the incident authority ------------------------------------------------------------------


def test_the_refusal_is_an_incident_although_codex_exited_0(world):
    """The live completion path: exit 0 used to read as ordinary output, so no seat was ever shed."""
    lines = _refused(FUTURE_REFUSAL)
    evidence = ledger_reconcile._classify_run_log_segment(
        lines, "codex", "refused", "o/r#7", world / "refused.log", successful=True
    )
    assert evidence is not None and evidence["category"] == "quota", evidence
    incidents = [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]
    assert len(incidents) == 1, incidents
    assert incidents[0]["extra"]["source"] == "turn_failed_event", incidents[0]
    assert incidents[0]["evidence_excerpt"] == FUTURE_REFUSAL, incidents[0]
    reset_at = rate_incidents.parse_relay_reset_at(FUTURE_REFUSAL)
    assert reset_at and incidents[0]["reset_at"] == reset_at, incidents[0]
    marker = json.loads((rate_incidents.SHED_DIR / "codex").read_text())
    assert marker["expires_at"] >= reset_at > time.time(), marker


def test_successful_prose_about_a_usage_limit_still_records_nothing(world):
    lines = [json.dumps(_item("item.completed", "agent_message", text=REFUSAL))]
    assert (
        ledger_reconcile._classify_run_log_segment(
            lines, "codex", "worked", "o/r#7", world / "worked.log", successful=True
        )
        is None
    )
    assert not rate_incidents.INCIDENT_FILE.exists()
    assert not (rate_incidents.SHED_DIR / "codex").exists()
