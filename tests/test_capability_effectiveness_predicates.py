"""`capability_effectiveness` counts an outcome the way `capability_causal_evidence` does, and scores
nothing a role run holds.

THE DEFECTS (2026-10-11, after #529). Two readers of the same edges and the same `outcomes` rows
wrote their own predicates and disagreed:

- An edge onto a ROLE run counted the role run's outcome. That outcome is a copy of the acting run's
  (`feedback._propagate_outcome_lineage_in_conn`), and the acting run carries the same capability's
  own edge, so one delivery counted twice, and `_target_of` pooled every `role:<name>:*` run into a
  single subject `<name>`. On a copy of the live Brain all 27 outcome-bearing role-run edges were
  such copies, and role-redirect's 762 edges, all on its own role runs, read "awaiting outcomes"
  although nothing will ever give them one.
- PASS with `pending` durability counted as a terminal non-durable result. A merge stays pending
  until `durability_sweep` judges it, so every merge in its grace window lowered every rate; and an
  unmerged run is selected by no sweep, so its pending is permanent (38 agy-runtime-isolation
  offload runs).
- A merge the fleet verifier marked NON_PASS was a durable success here and a failure in
  `capability_causal_evidence`. 42 of deliberate-break-verifier's 47 pending merges carry one.

THE RULE THIS PINS. Terminal and durable success are `feedback.outcome_is_terminal` and
`feedback.outcome_is_durable_success`, which `capability_causal_evidence` also calls. An edge onto a
role run is an association (`feedback.role_target_kind`), counted and never scored. A merge the
sweep has not judged is `awaiting_sweep`; a run that never merges is `no_merge`, scored by its
verdict. An arm none of whose edges can ever be scored reads `unscoreable`, never
`not_yet_measurable`.

Every row is written by the production writers: `record_role_run` and `record_run`'s role
inheritance for role advice, `record_capability_consumption` for a consumed capability (as
`capability_lifecycle` does), and `record_outcome` for what became of a run."""

from __future__ import annotations

import time

import pytest

import capabilities
import capability_effectiveness as effectiveness
import durability_sweep
import feedback
import value_chain_monitor as monitor

T0 = 1_791_756_000  # 2026-10-11T23:20:00Z
VERSIONS = {
    "role-triage": "cv:triage",
    "role-redirect": "cv:redirect",
    "offload": "cv:offload",
    "deliberate-break-verifier": "cv:dbv",
}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        feedback,
        "_resolve_capability_versions",
        lambda ids: [VERSIONS[i] for i in ids] if all(i in VERSIONS for i in ids) else [],
    )
    monkeypatch.setattr(time, "time", lambda: float(T0))
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for cap_id in VERSIONS:
        rows[cap_id] = capabilities._blank_capability(cap_id)
        rows[cap_id]["status"] = "shadow"
    capabilities.save(rows, ledger)
    return ledger


def _advised(n: int, role: str = "triage") -> tuple[str, str]:
    """A role run an offload produced, and the run that acted on its advice."""
    role_run, run = f"role:{role}:gemini:{n}", f"remote:o/r#{n}:gemini"
    feedback.record_role_run(role_run, role, f"o/r#{n}", "gemini", backend_run_id=f"offload:{n}")
    feedback.record_run(
        run,
        f"o/r#{n}",
        "implement",
        "gemini",
        mode="remote",
        pr_number=n,
        influenced_by_role_run_ids=[role_run],
    )
    return role_run, run


def _consumed(run: str, cap_id: str = "deliberate-break-verifier", **outcome) -> str:
    """What `capability_lifecycle` records when a capability's output is accepted into a run."""
    n = run.split("#")[-1].split(":")[0] if "#" in run else run
    feedback.record_run(
        run,
        f"o/r#{n}",
        "implement",
        "codex",
        mode="offload" if run.startswith("offload:") else "remote",
        pr_number=None if run.startswith("offload:") else int(n),
    )
    feedback.record_capability_consumption(
        capability_id=cap_id,
        capability_version_id=VERSIONS[cap_id],
        source_run_id=f"capability-producer:{cap_id}:{run}",
        target_run_id=run,
        accepted=True,
    )
    if outcome:
        feedback.record_outcome(run, **outcome)
    return run


def _report(ledger) -> dict:
    return effectiveness.measure(path=ledger)


def _row(rep: dict, cap_id: str) -> dict:
    return next(row for row in rep["rows"] if row["capability_id"] == cap_id)


def _outcome_of(run: str) -> tuple | None:
    with feedback._conn() as c:
        return c.execute(
            "SELECT adjudicated_verdict, durability FROM outcomes WHERE run_id=?", (run,)
        ).fetchone()


MERGED_DURABLE = {"adjudicated_verdict": "PASS", "merged": 1, "durability": "durable"}


@pytest.mark.parametrize("merged", [None, False])
@pytest.mark.parametrize("verdict", ["FAIL_BROKEN", "PASS", "NON_PASS"])
def test_pending_pr_outcome_keeps_its_delivery_semantics(brain, merged, verdict):
    # The six-subject production-writer reproduction from #556. A local verifier writes no
    # merge update, so NULL/pending is normal even when the run already has a PR.
    for n in range(1, 6):
        _consumed(f"remote:o/r#{n}:codex", **MERGED_DURABLE)
    run = "ordinary-pr-attempt"
    feedback.record_run(run, "o/r#6", "implement", "codex", mode="local", pr_number=6)
    _consumed_run(run, verifier_verdict=verdict, merged=merged)
    with feedback._conn() as c:
        assert c.execute(
            "SELECT merged, durability FROM outcomes WHERE run_id=?", (run,)
        ).fetchone() == (merged, "pending")
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    if verdict == "PASS":
        assert (arm["terminal"], arm["durable_rate"]) == (5, 1.0), arm
        with feedback._conn() as c:
            edge = next(e for e in effectiveness._edge_rows(c) if e["run_id"] == run)
        assert effectiveness._classify(edge) == "open", edge
    else:
        assert (arm["terminal_subjects"], arm["durable_subjects"], arm["durable_rate"]) == (
            6,
            5,
            0.833,
        ), arm
    assert arm["no_merge"] == 0, arm
    assert arm["verdict"]["judged"] == 0, arm


@pytest.mark.parametrize("mode", [None, "local", "offload"])
@pytest.mark.parametrize("merged", [None, False])
def test_pending_pr_lifecycle_stays_consistent_across_readers(brain, mode, merged):
    """A recorded PR can deliver even when its mode is unknown or says offload."""
    for n in range(1, 6):
        _consumed(f"remote:o/r#{n}:codex", **MERGED_DURABLE)
    run = "pending-pr-lifecycle"
    feedback.record_run(run, "o/r#6", "implement", "codex", mode=mode, pr_number=6)
    _consumed_run(run, verifier_verdict="PASS", merged=merged)

    def assert_stage(expected, terminal, durable, waiting):
        with feedback._conn() as c:
            edge = next(e for e in effectiveness._edge_rows(c) if e["run_id"] == run)
        assert edge["delivery_eligibility"] == "merge_path", edge
        assert effectiveness._classify(edge) == expected, edge
        arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
        assert (arm["terminal_subjects"], arm["durable_subjects"]) == (terminal, durable), arm
        assert arm["durable_rate"] == round(durable / terminal, 3), arm
        assert (arm["no_merge"], arm["verdict"]["judged"]) == (0, 0), arm
        assert arm["awaiting_sweep"] == waiting, arm

        inputs = monitor._brain(feedback.DB_PATH, T0 - 1, T0 + 1)
        assert inputs["errors"] == [], inputs
        edges = [e for e in inputs["edges"] if e["capability_id"] == "deliberate-break-verifier"]
        counts = monitor.outcome_counts("deliberate-break-verifier", edges)
        assert (counts["acted_on"], counts["outcome"], counts["outcome_success"]) == (
            6,
            terminal,
            durable,
        ), counts
        assert counts["outcome_no_merge"] == 0, counts
        assert counts["outcome_awaiting_sweep"] == waiting, counts
        assert counts["outcome_open"] == (1 if expected == "open" else 0), counts
        assert (run in {r["run_id"] for r in durability_sweep._pending_merged_runs()}) == bool(
            waiting
        )

    assert_stage("open", 5, 5, 0)
    feedback.record_outcome(run, merged=True)
    assert_stage("awaiting_sweep", 5, 5, 1)
    feedback.record_outcome(run, durability="durable")
    assert_stage("terminal", 6, 6, 0)
    # A later verifier rejection still vetoes success after the merge has held.
    feedback.record_outcome(run, verifier_verdict="NON_PASS")
    assert_stage("terminal", 6, 5, 0)


# ---- edges onto role runs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,pr_number,merged,eligibility",
    [
        (None, None, None, "unknown"),
        ("research", None, False, "unknown"),
        ("local", None, None, "merge_path"),
        ("remote", None, False, "merge_path"),
        ("offload", None, None, "no_delivery"),
        ("offload", 9, None, "merge_path"),
        ("offload", None, True, "merge_path"),
    ],
)
@pytest.mark.parametrize("verdict", ["PASS", "FAIL_BROKEN"])
def test_delivery_eligibility_requires_recorded_provenance(
    brain, mode, pr_number, merged, eligibility, verdict
):
    # Even an offload-shaped id proves nothing without its recorded mode. A PR/merge takes
    # precedence over no-delivery provenance, including conflicting historical rows.
    run = "offload:looks-like-research"
    feedback.record_run(run, "research:subject", "review", "gemini", mode=mode, pr_number=pr_number)
    _consumed_run(run, verifier_verdict=verdict, merged=merged)
    with feedback._conn() as c:
        edge = effectiveness._edge_rows(c)[0]
    assert edge["delivery_eligibility"] == eligibility
    expected = (
        "no_merge"
        if eligibility == "no_delivery"
        else "terminal" if verdict.startswith("FAIL") else "awaiting_sweep" if merged else "open"
    )
    assert effectiveness._classify(edge) == expected, edge


def test_a_role_runs_copied_outcome_is_an_association_not_a_second_delivery(brain):
    role_run, run = _advised(1)
    feedback.record_outcome(run, **MERGED_DURABLE)
    # The precondition the defect needs: propagation gave the role run a copy of the outcome.
    assert _outcome_of(role_run) == ("PASS", "durable")
    rep = _report(brain)
    for cap_id, kind in (("role-triage", "advisory_self"), ("offload", "role_association")):
        arm = _row(rep, cap_id)["accepted"]
        assert (arm["attributed"], arm["terminal"], arm["durable"]) == (2, 1, 1), (cap_id, arm)
        assert (arm["associations"], arm["terminal_subjects"]) == (1, 1), (cap_id, arm)
        assert arm["advisory_self"] == (1 if kind == "advisory_self" else 0), (cap_id, arm)


def test_role_runs_never_pool_into_one_subject(brain):
    # Before: `role:triage:gemini:<n>` folded into the subject `triage` for every n, so five
    # deliveries counted five subjects plus one pooled `triage` holding all five copies.
    for n in range(1, 6):
        _, run = _advised(n)
        feedback.record_outcome(run, **MERGED_DURABLE)
    arm = _row(_report(brain), "role-triage")["accepted"]
    assert (arm["terminal"], arm["terminal_subjects"], arm["associations"]) == (5, 5, 5), arm
    assert arm["durable_rate"] == 1.0 and arm["status"] == "measured", arm


def test_a_capability_whose_edges_are_all_on_its_own_role_runs_is_unscoreable(brain):
    # role-redirect: its role runs are recorded, and no run is ever joined to their advice.
    for n in range(1, 4):
        feedback.record_role_run(f"role:redirect:cursor:{n}", "redirect", f"o/r#{n}", "cursor")
    rep = _report(brain)
    row = _row(rep, "role-redirect")
    assert row["accepted"]["associations"] == 3, row
    assert row["verdict"] == (
        "unscoreable (3 edges, none can be scored: 3 associations onto role runs)"
    ), row["verdict"]
    assert "role-redirect" not in rep["awaiting_outcomes"]
    assert "role-redirect" in rep["unscoreable"]
    text = effectiveness.format_report(rep)
    assert "3 assoc" in text and "can ever be scored" in text, text


def test_associations_do_not_turn_excluded_only_into_unscoreable(brain):
    _, run = _advised(1)
    feedback.record_outcome(
        run, adjudicated_verdict=None, durability="abandoned", failure_class="transient_infra"
    )
    arm = _row(_report(brain), "role-triage")["accepted"]
    assert (arm["excluded"], arm["associations"]) == (1, 1), arm
    assert arm["status"].startswith("excluded_only (1 attempts on 1 subjects"), arm["status"]


# ---- PASS with pending durability ---------------------------------------------------------------


def test_a_merge_the_sweep_has_not_judged_waits_and_then_scores(brain):
    run = _consumed(
        "remote:o/r#7:codex", adjudicated_verdict="PASS", merged=1, durability="pending"
    )
    # The sweep's own selector picks it up: this is the population that resolves.
    assert run in {r["run_id"] for r in durability_sweep._pending_merged_runs()}
    rep = _report(brain)
    arm = _row(rep, "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["awaiting_sweep"], arm["no_merge"]) == (0, 1, 0), arm
    assert arm["status"] == "not_yet_measurable", arm
    assert "deliberate-break-verifier" in rep["awaiting_outcomes"]
    feedback.record_outcome(run, durability="durable")  # the sweep's verdict
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["durable"], arm["awaiting_sweep"]) == (1, 1, 0), arm


def test_a_run_that_never_merges_is_scored_by_verdict_never_awaiting(brain):
    # An offload run: a verdict, no PR, durability left at its default. No sweep selects it, so it
    # has no durability to wait for; its verdict is what it has (owner decision 2026-10-12).
    run = _consumed(
        "offload:gemini:9",
        verifier_verdict="PASS",
        adjudicated_verdict="PASS",
        durability="pending",
    )
    assert run not in {r["run_id"] for r in durability_sweep._pending_merged_runs()}
    rep = _report(brain)
    arm = _row(rep, "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["awaiting_sweep"], arm["no_merge"]) == (0, 0, 1), arm
    assert arm["status"] == (
        "no_merge_only (1 attempts on 1 subjects never merge: see the verdict rate)"
    ), arm["status"]
    assert (arm["verdict"]["judged"], arm["verdict"]["passed"]) == (1, 1), arm["verdict"]
    assert "deliberate-break-verifier" not in rep["awaiting_outcomes"]
    assert "deliberate-break-verifier" in rep["no_merge_only"]
    assert "deliberate-break-verifier" not in rep["unscoreable"]
    text = effectiveness.format_report(rep)
    assert "1 nomerge" in text and "1/1 subj (1 att)" in text, text


def test_pending_merges_no_longer_lower_the_rate(brain):
    for n in range(1, 6):
        _consumed(f"remote:o/r#{n}:codex", **MERGED_DURABLE)
    for n in range(6, 9):
        _consumed(f"remote:o/r#{n}:codex", adjudicated_verdict="PASS", merged=1)
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["awaiting_sweep"], arm["durable_rate"]) == (5, 3, 1.0), arm


def test_a_small_sample_names_what_is_still_waiting_and_what_never_will(brain):
    _consumed("remote:o/r#1:codex", **MERGED_DURABLE)
    _consumed("remote:o/r#2:codex", adjudicated_verdict="PASS", merged=1, durability="pending")
    _consumed("offload:gemini:3", adjudicated_verdict="PASS", durability="pending")
    status = _row(_report(brain), "deliberate-break-verifier")["accepted"]["status"]
    assert status == (
        "insufficient_evidence (1/5 distinct subjects from 1 attempts; "
        "1 awaiting the durability sweep; 1 never merge, scored by verdict)"
    ), status


def test_a_failure_that_never_merges_scores_by_verdict_not_durability(brain):
    # Before 2026-10-12 this FAIL was a non-durable terminal result while a PASS in the same
    # position never scored: only failures could count for a run that never merges.
    _consumed("offload:gemini:4", verifier_verdict="FAIL", adjudicated_verdict="FAIL")
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["durable"], arm["no_merge"]) == (0, 0, 1), arm
    assert (arm["verdict"]["judged"], arm["verdict"]["passed"]) == (1, 0), arm["verdict"]


def test_a_failure_closed_unmerged_stays_in_the_durable_rate(brain):
    # Judged: the PR was closed, so its durability is `abandoned`, not unjudged.
    _consumed("remote:o/r#4:codex", adjudicated_verdict="FAIL", merged=0, durability="abandoned")
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["no_merge"]) == (1, 0), arm


def test_the_verdict_rate_counts_subjects_and_refuses_small_samples(brain):
    # Five distinct no-merge subjects (offload runs keyed by their own targets), one failing twice.
    for n in range(1, 6):
        run = f"offload:gemini:{n}"
        feedback.record_run(run, f"offload:task-{n}", "review", "gemini", mode="offload")
        verdict = "FAIL" if n == 5 else "PASS"
        _consumed_run(run, verifier_verdict=verdict, adjudicated_verdict=verdict)
    feedback.record_run("offload:gemini:6", "offload:task-5", "review", "gemini", mode="offload")
    _consumed_run("offload:gemini:6", verifier_verdict="FAIL", adjudicated_verdict="FAIL")
    rep = _report(brain)
    stats = _row(rep, "deliberate-break-verifier")["accepted"]["verdict"]
    assert (stats["judged"], stats["judged_subjects"], stats["pass_subjects"]) == (6, 5, 4), stats
    assert (stats["rate"], stats["status"]) == (0.8, "measured"), stats
    # Four subjects is a refusal, never a figure.
    four = effectiveness._verdict_stats(
        [{"run_id": f"r{i}", "subject": f"s{i}", "verdict": "PASS"} for i in range(4)]
    )
    assert four["rate"] is None and four["status"].startswith("insufficient_evidence"), four


def test_a_verifier_failure_under_an_adjudicated_pass_is_a_failing_verdict(brain):
    _consumed("offload:gemini:7", verifier_verdict="NON_PASS", adjudicated_verdict="PASS")
    stats = _row(_report(brain), "deliberate-break-verifier")["accepted"]["verdict"]
    assert (stats["judged"], stats["passed"]) == (1, 0), stats


# ---- the verifier's NON_PASS --------------------------------------------------------------------


@pytest.mark.parametrize("durability", ["durable", "pending"])
def test_a_verifier_non_pass_is_terminal_and_never_a_durable_success(brain, durability):
    _consumed(
        "remote:o/r#5:codex",
        verifier_verdict="NON_PASS",
        adjudicated_verdict="PASS",
        merged=1,
        durability=durability,
    )
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["durable"], arm["awaiting_sweep"]) == (1, 0, 0), arm


# ---- one definition, two readers ----------------------------------------------------------------


def test_this_reader_and_capability_causal_evidence_agree(brain):
    """Every outcome shape seen on the live Brain, written through the production writers. The
    lifecycle tally and this reader must agree on what is terminal and what is a durable success."""
    shapes = [
        MERGED_DURABLE,
        {"adjudicated_verdict": "PASS", "merged": 1, "durability": "pending"},
        {"adjudicated_verdict": "PASS", "merged": 1},
        {"verifier_verdict": "PASS", "adjudicated_verdict": "PASS", "durability": "pending"},
        {"verifier_verdict": "FAIL", "adjudicated_verdict": "FAIL", "durability": "pending"},
        {"verifier_verdict": "NON_PASS", "adjudicated_verdict": "PASS", "merged": 1},
        {
            "verifier_verdict": "NON_PASS",
            "adjudicated_verdict": "PASS",
            "merged": 1,
            "durability": "durable",
        },
        {"adjudicated_verdict": "PASS", "merged": 1, "durability": "broke_later"},
        {"adjudicated_verdict": "FAIL", "merged": 0, "durability": "abandoned"},
    ]
    for n, outcome in enumerate(shapes, 1):
        _consumed(f"remote:o/r#{n}:codex", **outcome)
    _consumed(f"remote:o/r#{len(shapes) + 1}:codex")  # no outcome yet
    lifecycle = feedback.capability_causal_evidence(
        "deliberate-break-verifier", VERSIONS["deliberate-break-verifier"]
    )
    assert len(lifecycle) == len(shapes) + 1
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    # These are ordinary delivery runs, including NULL/pending verifier outcomes. The readers
    # agree: only positively identified offloads belong in the separate no-delivery population.
    assert arm["terminal"] == sum(r["terminal_outcome"] for r in lifecycle) == 6, arm
    assert arm["durable"] == sum(r["tally_class"] == "success" for r in lifecycle) == 1, arm
    assert (arm["awaiting_sweep"], arm["no_merge"]) == (2, 0), arm
    with feedback._conn() as c:
        assert sum(effectiveness._classify(e) == "open" for e in effectiveness._edge_rows(c)) == 2


def test_role_target_kind_is_the_lifecycles_advisory_self(brain):
    _advised(1)
    for row in feedback.capability_causal_evidence("role-triage", VERSIONS["role-triage"]):
        kind = feedback.role_target_kind("role-triage", row["target_role_name"])
        assert (kind == "advisory_self") == (row["tally_class"] == "advisory_self"), row
    assert feedback.role_target_kind("offload", "triage") == "role_association"
    assert feedback.role_target_kind("offload", None) == ""


# ---- the subject ---------------------------------------------------------------------------------


def _local_attempt(n: int, issue: int, **outcome) -> str:
    """A local run: its id is not `<kind>:<target>:<agent>`, so parsing it finds no issue."""
    run = f"stranske__Repo_{issue}-gemini-{n}"
    feedback.record_run(run, f"stranske/Repo#{issue}", "implement", "gemini", mode="local")
    return _consumed_run(run, **outcome)


def _consumed_run(run: str, cap_id: str = "deliberate-break-verifier", **outcome) -> str:
    feedback.record_capability_consumption(
        capability_id=cap_id,
        capability_version_id=VERSIONS[cap_id],
        source_run_id=f"capability-producer:{cap_id}:{run}",
        target_run_id=run,
        accepted=True,
    )
    if outcome:
        feedback.record_outcome(run, **outcome)
    return run


def test_two_local_attempts_at_one_issue_are_one_subject(brain):
    # The live case: agy-runtime-isolation's 0.8 rested on two attempts at Deliverable-Render#20
    # counting as two of its five subjects.
    _local_attempt(1, 20, **MERGED_DURABLE)
    _local_attempt(2, 20, **MERGED_DURABLE)
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal"], arm["terminal_subjects"], arm["durable_subjects"]) == (2, 1, 1), arm
    assert arm["attempts_per_subject"] == 2.0, arm


def test_this_reader_and_the_lifecycle_count_the_same_subjects(brain):
    for n, issue in ((1, 20), (2, 20), (3, 21)):
        _local_attempt(n, issue, **MERGED_DURABLE)
    _consumed("remote:o/r#7:codex", **MERGED_DURABLE)
    lifecycle = feedback.capability_causal_evidence(
        "deliberate-break-verifier", VERSIONS["deliberate-break-verifier"]
    )
    durable = {r["subject_id"] for r in lifecycle if r["tally_class"] == "success"}
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert durable == {"stranske/Repo#20", "stranske/Repo#21", "o/r#7"}, durable
    assert arm["durable_subjects"] == len(durable) == 3, arm


@pytest.mark.parametrize(
    "metadata",
    [
        {"causal_context": "not-a-dict"},
        {"profile_decision": "not-a-dict"},
        {"profile_decision": {"causal_context": ["not", "a", "dict"]}},
    ],
)
def test_malformed_nested_metadata_is_absent_never_a_crash(brain, metadata):
    run = "remote:o/r#8:codex"
    feedback.record_run(
        run, "o/r#8", "implement", "codex", mode="remote", routing_metadata=metadata
    )
    _consumed_run(run, **MERGED_DURABLE)
    assert feedback.run_subject_id(metadata, "o/r#8", run) == "o/r#8"
    arm = _row(_report(brain), "deliberate-break-verifier")["accepted"]
    assert (arm["terminal_subjects"], arm["durable_subjects"]) == (1, 1), arm
    rows = feedback.capability_causal_evidence(
        "deliberate-break-verifier", VERSIONS["deliberate-break-verifier"]
    )
    assert [r["subject_id"] for r in rows] == ["o/r#8"], rows
