"""Standalone gate fallback supplies only its explicitly supported disposable fixtures."""

import functools
from pathlib import Path

import pytest
import test_capability_admission as gate

FIXTURE_CHECKS = (
    "test_an_act_outside_the_trailing_window_is_not_acting_now",
    "test_consumer_acting_is_the_trailing_window_not_a_first_invocation_deadline",
    "test_consumer_acts_binds_from_its_date_and_reports_older_rows_as_debt",
    "test_consumer_acts_fails_a_declared_consumer_that_never_acted",
    "test_consumer_acts_needs_a_symbol_this_tree_can_find",
    "test_consumer_report_excludes_waived_rows_and_names_the_drained_states",
)


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
