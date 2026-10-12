"""Every offload run records which work it was for, so both capability readers can key its subject.

THE DEFECT (measured 2026-10-12 on a read-only copy of the live Brain). Both capability readers
key a run's subject by an explicit `subject_id` in its routing metadata, else by `runs.target`.
`dispatcher.offload` recorded no subject and set the target to `offload:<cwd>`, so of 2,126 offload
runs since 2026-09-01 none had a subject: 591 were `offload:/` (every role backend: the tick runs
in `/`), 300 the research-program directory. Every research-program run was ONE subject, and
agy-runtime-isolation's eight failed research units on five different subjects counted as one.

THE RULE. The writer records `subject_id` and `subject_source`: a registered research round's
subject first (the completion-identity path keys a round-bound run that way already), else the
caller's `subject_id`, else nothing. A role passes its own target when that names one work item
(`owner/repo#N`); triage ranks a batch and passes none. Earlier rows get the same two keys once,
from the same two structured links (the round, and the role run whose `backend_run_id` is the
offload), and `restore-offload-subjects` puts them back. Prose never supplies a subject.

The runs here are written by the production writers: `dispatcher.offload` with only the agent
process and the capacity ledger stubbed, `feedback.record_run` / `record_role_run`, and
`research_subjects.record_research_round`."""

from __future__ import annotations

import json
import subprocess

import pytest

import dispatcher
import feedback
import rate_incidents
import research_subjects

MARKER = "SELECT detail FROM data_migrations WHERE name=?"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    return tmp_path


@pytest.fixture
def offloads(monkeypatch, brain):
    """`dispatcher.offload` writing the real Brain; the agent CLI and the capacity ledger are fake."""
    monkeypatch.setattr(rate_incidents, "HANDOFF", brain)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", brain / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", brain / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", brain / "capacity-shed")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", brain / "logs")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", brain / "agent-runtime")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 30)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(dispatcher, "_agent_log_tail_from_argv", lambda *args, **kwargs: "")
    monkeypatch.setattr(dispatcher.adapters, "can_report_cli_identity", lambda *a: (False, "test"))
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args, **kwargs: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "done\n", ""),
    )
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    workspace = brain / "work"
    workspace.mkdir()

    def run(**kwargs) -> dict:
        out = dispatcher.offload("cursor", "review this", cwd=str(workspace), **kwargs)
        assert out["exit"] == 0, out
        return out

    return run


def _metadata(run_id: str) -> dict:
    with feedback._conn() as c:
        row = c.execute("SELECT routing_metadata FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row is not None, f"{run_id} was not recorded"
    return json.loads(row[0]) if row[0] else {}


def _round(kind: str = "d-audit") -> tuple[str, str]:
    round_id, identity = research_subjects.record_research_round(
        "stranske/counter_risk", kind, "2026-09-10T17-45-12Z", "audit the repo", ["gemini"]
    )
    return round_id, identity["subject_id"]


def test_a_caller_subject_is_recorded_on_the_run(offloads):
    out = offloads(subject_id=" stranske/Counter_Risk#77 ")
    assert _metadata(out["run_id"]) == {
        "subject_id": "stranske/Counter_Risk#77",
        "subject_source": "caller",
    }


def test_an_unnamed_run_stays_unkeyed(offloads):
    """No subject and no profile: the metadata stays NULL, exactly as before this change."""
    out = offloads()
    assert _metadata(out["run_id"]) == {}


def test_a_registered_round_supplies_the_subject_and_wins(offloads, capsys):
    round_id, subject = _round()
    bound = offloads(research_round=round_id)
    assert _metadata(bound["run_id"]) == {"subject_id": subject, "subject_source": "research_round"}

    both = offloads(research_round=round_id, subject_id="stranske/Counter_Risk#77")
    assert _metadata(both["run_id"])["subject_id"] == subject
    assert "ignored; research round" in capsys.readouterr().err


def test_an_unregistered_round_falls_back_to_the_caller(offloads):
    out = offloads(research_round="nowhere:d-audit:2026-01-01", subject_id="o/r#3")
    assert _metadata(out["run_id"]) == {"subject_id": "o/r#3", "subject_source": "caller"}


@pytest.mark.parametrize(
    ("target", "subject"),
    [
        ("stranske/trip-planner#1366", "stranske/trip-planner#1366"),
        ("  o/r#7 ", "o/r#7"),
        ("triage:9-items", None),
        ("redirect-role", None),
        ("stranske/Workflows#new", None),
        ("stranske/Orchestrator", None),
        ("", None),
        (None, None),
    ],
)
def test_only_one_work_item_is_a_role_subject(target, subject):
    assert feedback.work_item_subject(target) == subject


def _open_as_a_brain_from_before_the_migration() -> None:
    """The live Brain has no marker until the first open after the sync: drop the one this test's
    first open wrote, so the next open runs the migration over the rows seeded since."""
    with feedback._conn() as c:
        c.execute("DELETE FROM data_migrations WHERE name=?", (feedback.OFFLOAD_SUBJECTS_MIGRATION,))
        c.commit()
    feedback._conn().close()


def _seed_before_the_writer() -> dict[str, str]:
    """Offload runs as the writer recorded them before 2026-10-12: no subject, target the cwd."""
    round_id, round_subject = _round()
    profiled = {"selected_profile_id": "p1", "transport": "offload"}
    runs = {
        "round": ("offload:gemini:1", "offload:/rp", round_id, profiled),
        "redirect": ("offload:codex:2", "offload:/", None, None),
        "triage": ("offload:gemini:3", "offload:/", None, None),
        "ambiguous": ("offload:gemini:4", "offload:/", None, None),
        "cwd": ("offload:cursor:5", "offload:/private/tmp", None, None),
        "keyed": ("offload:cursor:6", "offload:/x", None, {"subject_id": "o/r#9"}),
    }
    for run_id, target, round_, metadata in runs.values():
        feedback.record_run(
            run_id,
            target,
            "offload",
            run_id.split(":")[1],
            mode="offload",
            experiment_id=round_,
            routing_metadata=metadata,
        )
    feedback.record_role_run("role:redirect:a", "redirect", "o/r#5", "codex", backend_run_id="offload:codex:2")
    feedback.record_role_run("role:triage:a", "triage", "triage:9-items", "gemini", backend_run_id="offload:gemini:3")
    feedback.record_role_run("role:adjudicator:a", "adjudicator", "o/r#6", "gemini", backend_run_id="offload:gemini:4")
    feedback.record_role_run("role:adjudicator:b", "adjudicator", "o/r#7", "gemini", backend_run_id="offload:gemini:4")
    return {"round_subject": round_subject, **{k: v[0] for k, v in runs.items()}}


def test_earlier_runs_are_keyed_once_from_structured_links_only(brain):
    seeded = _seed_before_the_writer()
    with feedback._conn() as c:
        before = dict(c.execute("SELECT run_id,routing_metadata FROM runs").fetchall())
    _open_as_a_brain_from_before_the_migration()

    assert _metadata(seeded["round"]) == {
        "selected_profile_id": "p1",
        "transport": "offload",
        "subject_id": seeded["round_subject"],
        "subject_source": "research_round",
    }
    assert _metadata(seeded["redirect"]) == {"subject_id": "o/r#5", "subject_source": "role_target"}
    for unkeyed in ("triage", "ambiguous", "cwd"):
        assert _metadata(seeded[unkeyed]) == {}, unkeyed
    assert _metadata(seeded["keyed"]) == {"subject_id": "o/r#9"}  # a subject already there is kept

    with feedback._conn() as c:
        detail = json.loads(c.execute(MARKER, (feedback.OFFLOAD_SUBJECTS_MIGRATION,)).fetchone()[0])
    assert detail["keyed_by_source"] == {"research_round": 1, "role_target": 1}
    assert (detail["examined"], detail["keyed"], detail["unkeyed"]) == (6, 2, 4)
    assert detail["ambiguous_role_links"] == 1

    # Once: another open changes nothing.
    feedback._conn().close()
    assert _metadata(seeded["redirect"])["subject_source"] == "role_target"

    # Restore puts back every keyed row's original metadata, and only those rows were touched.
    assert feedback.restore_offload_subjects() == {"restored": 2, "of": 2}
    with feedback._conn() as c:
        after = dict(c.execute("SELECT run_id,routing_metadata FROM runs").fetchall())
    assert after == before
    assert feedback.restore_offload_subjects()["reason"] == "already restored"
    feedback._conn().close()
    assert _metadata(seeded["redirect"]) == {}, "a restored Brain is not keyed again on next open"


def test_a_brain_with_no_offload_runs_records_an_empty_marker(brain):
    feedback._conn().close()
    with feedback._conn() as c:
        detail = json.loads(c.execute(MARKER, (feedback.OFFLOAD_SUBJECTS_MIGRATION,)).fetchone()[0])
    assert (detail["examined"], detail["keyed"], detail["rows"]) == (0, 0, [])


@pytest.fixture
def role_offloads(monkeypatch, brain):
    """What each role hands `dispatcher.offload`; the backend answers with nothing usable."""
    import roles

    monkeypatch.setattr(roles, "_role_capability_event", lambda *a, **kw: None)
    roles.reset_role_invocation_counts()
    calls: list[dict] = []

    def offload(backend, prompt, **kwargs):
        calls.append(kwargs)
        return {"run_id": f"offload:{backend}:{len(calls)}", "output": "{}", "exit": 0}

    monkeypatch.setattr(roles.dispatcher, "offload", offload)
    return roles, calls


def test_a_role_passes_its_own_work_item_and_triage_passes_none(role_offloads):
    roles, calls = role_offloads
    roles.run_prompt_agent(target="owner/repo#1", goal="g", backend="cursor", dispatch=True)
    roles.run_prompt_agent(target="owner/repo#new", goal="g", backend="cursor", dispatch=True)
    roles.run_decomposer_agent(goal="g", repo="owner/repo", backend="codex", dispatch=True)
    roles.run_triage_agent(
        backlog_items=[{"target": "owner/repo#2", "title": "t"}],
        backend="codex",
        cap={},
        learned={},
        dispatch=True,
    )
    assert [call.get("subject_id") for call in calls] == ["owner/repo#1", None, None, None]

    # The writer and the backfill agree: the subject is the role run's own recorded target.
    with feedback._conn() as c:
        role_target = c.execute(
            "SELECT target FROM runs WHERE role_name='prompt' "
            "AND json_extract(decomposition,'$.backend_run_id')='offload:cursor:1'"
        ).fetchone()
    assert role_target == ("owner/repo#1",)
