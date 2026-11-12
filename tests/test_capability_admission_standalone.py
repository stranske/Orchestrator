"""Standalone gate fallback supplies only its explicitly supported disposable fixtures."""

import functools
import sys
from pathlib import Path

import pytest
import test_capability_admission as gate

import capabilities
import capability_admission as admission


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """These unit tests must never discover or write the operator's runtime state."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_audits_dir_honors_an_explicit_override(monkeypatch, tmp_path):
    override = tmp_path / "audits"
    monkeypatch.setenv("ORCH_AUDITS_DIR", str(override))
    assert admission._audits_dir() == override


def test_symbol_resolution_accepts_module_assignments_and_rejects_nested_nonclasses():
    assert admission._python_symbol_defined("FLAG = object()\n", "FLAG")
    assert admission._python_symbol_defined("FLAG: object = object()\n", "FLAG")
    assert not admission._python_symbol_defined(
        "def outer():\n    def inner(): pass\n", "outer.inner"
    )


def test_known_controls_skips_unreadable_candidate_files(monkeypatch, tmp_path):
    monkeypatch.setattr(admission.paths, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(admission.paths, "MODULE_DIR", tmp_path)

    # Keep other candidates readable so the mutation reaches the injected OSError.
    for name in ("orchestrate.sh", "repo_knowledge.py"):
        (tmp_path / name).write_text("", encoding="utf-8")

    # Create an unreadable candidate file that raises OSError
    unreadable_candidate = tmp_path / "dispatcher.py"
    unreadable_candidate.write_text("ORCH_UNREADABLE_FLAG=1", encoding="utf-8")

    # Create a readable candidate file with a control
    readable_candidate = tmp_path / "router.py"
    readable_candidate.write_text("ORCH_ROUTE_ENABLED=1", encoding="utf-8")

    # Track read attempts
    read_attempts = []
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        read_attempts.append(str(self))
        if self == unreadable_candidate:
            raise OSError("candidate file unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = admission.known_controls()

    # Assert the unreadable file was attempted
    assert str(unreadable_candidate) in read_attempts, "Unreadable candidate was not attempted"

    # Assert the readable candidate still contributes its control
    assert "ORCH_ROUTE_ENABLED" in result
    # The unreadable candidate's control should NOT be in the result
    assert "ORCH_UNREADABLE_FLAG" not in result


def test_findability_context_fails_closed_when_advisor_raises(monkeypatch):
    import capability_advisor

    monkeypatch.setattr(
        capability_advisor,
        "consulting_surfaces",
        lambda **_: (_ for _ in ()).throw(RuntimeError("no")),
    )
    context = admission._findability_context(["capability:x"])
    assert context["bound_surfaces"] == {}
    assert context["reached_surfaces"] == set()
    assert context["consult_reach"]["unreadable"].startswith("RuntimeError: no")


def test_external_caller_admission_uses_audit_proof_then_fresh_probe(monkeypatch):
    """External callers must be admitted only from a verified proof, never a declaration."""
    import capability_activation_audit as audit

    cap = {"capability_id": "capability:external", "external_caller": {"kind": "controller"}}

    # A current audit row is the authoritative result and avoids probing an external controller.
    monkeypatch.setattr(audit, "external_caller", lambda _: pytest.fail("unexpected fresh probe"))
    cached = {"exists": True, "detail": "seen"}
    assert admission.req_caller_exists(
        cap, {"audit_rows": {cap["capability_id"]: {"external_caller": cached}}}
    ) == (True, "seen")

    # With no cached proof, the fresh audit result decides the verdict.
    monkeypatch.setattr(
        audit,
        "external_caller",
        lambda _: {"exists": False, "detail": "controller heartbeat stale"},
    )
    assert admission.req_caller_exists(cap, {"audit_rows": {}}) == (
        False,
        "controller heartbeat stale",
    )

    # A malformed/missing probe result must fail closed with the operator-facing diagnosis.
    monkeypatch.setattr(audit, "external_caller", lambda _: None)
    assert admission.req_caller_exists(cap, {"audit_rows": {}}) == (
        False,
        "external caller not verified",
    )


def test_admit_and_preflight_report_requirement_exceptions_without_passing(monkeypatch):
    def broken_requirement(capability, context):
        raise RuntimeError("deliberate predicate failure")

    monkeypatch.setattr(admission, "REQUIREMENTS", (("broken", broken_requirement),))
    monkeypatch.setattr(admission, "_findability_context", lambda *args, **kwargs: {})
    row = admission.capabilities._blank_capability("capability:broken")
    admitted = admission.admit(
        "capability:broken", ledger={"capability:broken": row}, ctx={"isolated": True}
    )
    proposed = admission.preflight({"capability_id": "capability:proposed"})
    assert admitted["missing"] == ["broken"]
    assert proposed["declarable_missing"] == ["broken"]
    assert "check errored: deliberate predicate failure" in admitted["checks"]["broken"]["detail"]
    assert "check errored: deliberate predicate failure" in proposed["checks"]["broken"]["detail"]


def test_commitments_continues_past_an_unreadable_source_file(monkeypatch, tmp_path):
    audits = tmp_path / "audits"
    audits.mkdir()
    # Create files in a controlled order: a_file comes before b_file alphabetically
    unreadable = tmp_path / "a_unreadable.py"
    unreadable.write_text("ORCH_DEMO_UNTIL=2020-01-01", encoding="utf-8")
    # Create a readable file with an overdue deadline that will be detected
    readable_overdue = tmp_path / "b_overdue.py"
    readable_overdue.write_text("ORCH_TEST_UNTIL=2020-01-01", encoding="utf-8")
    monkeypatch.setattr(admission, "AUDITS", audits)

    # Mock _today to ensure 2020-01-01 is in the past
    monkeypatch.setattr(admission, "_today", lambda: "2026-11-08")

    read_attempts = []
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        read_attempts.append(str(self))
        if self == unreadable:
            raise OSError("fixture unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = admission.commitments(root=tmp_path)

    # Assert the unreadable file was attempted
    assert str(unreadable) in read_attempts, "Unreadable file was not attempted"

    # Assert the readable overdue file was processed and reported
    assert len(result["overdue_without_record"]) == 1
    overdue = result["overdue_without_record"][0]
    assert overdue["file"] == "b_overdue.py"
    assert overdue["var"] == "ORCH_TEST_UNTIL"
    assert overdue["date"] == "2020-01-01"
    assert not result["clean"]

    # Assert scan order: a_unreadable.py is processed first, then b_overdue.py
    assert read_attempts.index(str(unreadable)) < read_attempts.index(str(readable_overdue))


def test_main_handles_file_preflight_and_clean_commitments(monkeypatch, tmp_path, capsys):
    proposal = tmp_path / "proposal.json"
    proposal.write_text('{"capability_id": "capability:from-file"}', encoding="utf-8")
    monkeypatch.setattr(admission, "preflight", lambda spec: {"seen": spec["capability_id"]})
    monkeypatch.setattr(sys, "argv", ["capability_admission.py", "--preflight", f"@{proposal}"])
    assert admission.main() == 0
    assert '"seen": "capability:from-file"' in capsys.readouterr().out
    monkeypatch.setattr(admission, "commitments", lambda: {"clean": True})
    monkeypatch.setattr(sys, "argv", ["capability_admission.py", "--commitments", "--json"])
    assert admission.main() == 0
    assert '"clean": true' in capsys.readouterr().out


FIXTURE_CHECKS = (
    "test_an_act_outside_the_trailing_window_is_not_acting_now",
    "test_consumer_acting_is_the_trailing_window_not_a_first_invocation_deadline",
    "test_consumer_acts_binds_from_its_date_and_reports_older_rows_as_debt",
    "test_consumer_acts_fails_a_declared_consumer_that_never_acted",
    "test_consumer_acts_needs_a_symbol_this_tree_can_find",
    "test_consumer_report_excludes_waived_rows_and_names_the_drained_states",
)


def test_consumer_symbol_resolution_rejects_wrong_nesting_and_accepts_shell():
    """A declaration is useful only when its exact callable exists in the named file."""
    source = "class Right:\n    def run(self): pass\n\ndef run(): pass\n"
    assert admission.consumer_tokens("x.py:Right.run and x.sh:launch") == [
        ("x.py", "Right.run"),
        ("x.sh", "launch"),
    ]
    assert admission._python_symbol_defined(source, "Right.run")
    assert not admission._python_symbol_defined(source, "Wrong.run")
    assert not admission._python_symbol_defined("not python(", "run")
    assert admission._symbol_defined("function launch() { :; }", "launch", shell=True)
    assert not admission._symbol_defined("launch_other() { :; }", "launch", shell=True)


def test_original_offload_prose_is_reconciled_without_lifecycle_or_credit_changes():
    """The issue #599 row belongs in the declaration audit without seeding a new row."""
    declarations = capabilities.KNOWN_GATES | capabilities.KNOWN_DECLARATIONS
    resolution = admission.consumer_resolution(declarations.get("offload", {}))
    assert resolution["tokens"] and not resolution["unresolved"], resolution
    assert "offload" not in capabilities.declared_row_ids()

    before = capabilities._blank_capability("offload")
    before.update(status="generated", downstream_consumer="the run consuming offload output")
    before["event_history"] = [{"type": "invocation", "timestamp": 1}]
    ledger = {"offload": before}
    assert capabilities._reconcile_known_declarations(ledger, 2)
    row = ledger["offload"]
    assert row["downstream_consumer"] == declarations["offload"]["downstream_consumer"]
    assert row["status"] == "generated"
    assert row["outcome_links"] == []
    assert row["event_history"][0] == {"type": "invocation", "timestamp": 1}
    assert not capabilities._reconcile_known_declarations(ledger, 3)


def test_actual_no_verdict_main_executes_all_six_and_cleans_fixtures(monkeypatch, tmp_path, capsys):
    import feedback
    import value_chain_monitor as monitor

    # Exercise the real standalone inventory/runner with only the six synthetic
    # checks. A real admission report belongs to the separate live-ledger gate.
    monkeypatch.setattr(gate.capabilities, "REG", tmp_path / "unused-ledger.json")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "unused-feedback.db")
    monkeypatch.setattr(gate.admission, "_ACTED_ON_CACHE", {})
    monkeypatch.setattr(gate.admission, "report", lambda: {"enforced_total": 0, "legacy_debt": []})
    monkeypatch.setattr(gate.sys, "argv", [str(Path(gate.__file__))])
    before = (
        feedback.DB_PATH,
        monitor._brain,
        monitor.outcome_counts,
        dict(gate.admission.WAIVERS),
    )
    seen, paths = [], []

    def observe(fn):
        @functools.wraps(fn)
        def wrapped(**kwargs):
            seen.append(fn.__name__)
            paths.append(kwargs["tmp_path"])
            return fn(**kwargs)

        return wrapped

    for name, fn in list(vars(gate).items()):
        if name.startswith("test_") and callable(fn):
            if name in FIXTURE_CHECKS:
                monkeypatch.setattr(gate, name, observe(fn))
            else:
                monkeypatch.delattr(gate, name)
    assert gate.main() == 0
    assert seen == sorted(FIXTURE_CHECKS)
    assert len(set(paths)) == 6
    assert all(not path.exists() and not path.parent.exists() for path in paths)
    assert before == (
        feedback.DB_PATH,
        monitor._brain,
        monitor.outcome_counts,
        dict(gate.admission.WAIVERS),
    )
    assert not (tmp_path / "unused-ledger.json").exists()
    assert not (tmp_path / "unused-feedback.db").exists()
    assert "all 6 admission checks passed" in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped"])
def test_recorded_verdict_replay_does_not_allocate_or_invoke(outcome, monkeypatch):
    def check(tmp_path, monkeypatch):
        pytest.fail("recorded check must not run")

    monkeypatch.setattr(
        gate.tempfile, "TemporaryDirectory", lambda **_: pytest.fail("no fixtures for replay")
    )
    verdict = {check.__name__: {"outcome": outcome, "message": "original recorded result"}}
    if outcome == "passed":
        gate._run_standalone_check(check, verdict)
    else:
        expected = AssertionError if outcome == "failed" else gate.env_prereq.MissingPrerequisite
        with pytest.raises(expected, match="original recorded result"):
            gate._run_standalone_check(check, verdict)


def test_fixture_cleanup_and_patch_restoration_on_failed_check():
    marker, paths = {"value": "original"}, []

    def check(tmp_path, monkeypatch):
        paths.append(tmp_path)
        monkeypatch.setitem(marker, "value", "changed")
        (tmp_path.parent / "outside.py").write_text("synthetic sibling")
        raise AssertionError("original failed assertion")

    with pytest.raises(AssertionError, match="original failed assertion"):
        gate._run_standalone_check(check, {})
    assert marker == {"value": "original"}
    assert len(paths) == 1 and not paths[0].parent.exists()


def test_unsupported_fixture_fails_without_invoking_or_allocating(monkeypatch):
    def check(unknown_fixture):
        pytest.fail("unsupported fixture must not run")

    monkeypatch.setattr(
        gate.tempfile, "TemporaryDirectory", lambda **_: pytest.fail("no allocation")
    )
    with pytest.raises(TypeError, match="unsupported standalone fixtures: unknown_fixture"):
        gate._run_standalone_check(check, {})


def test_zero_argument_fallback_still_executes_original_check():
    calls = []

    def check():
        calls.append(True)

    gate._run_standalone_check(check, {})
    assert calls == [True]
