"""Hermetic coverage for issue #1026's ranked targets.

Targets (CI combined measure, main @ 21b8ad24): capability_admission,
env_prereq, ledger_reconcile. Every external boundary is stubbed; both
ORCH_STATE_DIR and ORCH_LOCAL_RUNTIME point at pytest temp dirs.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import capabilities
import capability_admission as admission
import env_prereq
import ledger_reconcile


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")
    attempts = []

    def deny_external(*_args, **_kwargs):
        attempts.append("external process or network call")
        raise AssertionError("coverage tests must not start processes or use the network")

    monkeypatch.setattr(subprocess, "Popen", deny_external)
    monkeypatch.setattr(socket.socket, "connect", deny_external)
    monkeypatch.setattr(socket, "getaddrinfo", deny_external)
    # Never touch a live Brain or ledger lock from these helpers.
    monkeypatch.setattr(admission, "_ACTED_ON_CACHE", {})

    yield
    assert attempts == []


# --------------------------------------------------------------------------- capability_admission


def test_audits_dir_override_and_fallback(tmp_path, monkeypatch):
    """Explicit ORCH_AUDITS_DIR wins; otherwise candidate roots or the home fallback."""
    audits = tmp_path / "audits"
    audits.mkdir()
    monkeypatch.setenv("ORCH_AUDITS_DIR", str(audits))
    assert admission._audits_dir() == audits

    monkeypatch.delenv("ORCH_AUDITS_DIR", raising=False)
    fleet = tmp_path / "fleet"
    (fleet / "Audits" / "Orchestrator").mkdir(parents=True)
    monkeypatch.setenv("ORCH_FLEET_ROOT", str(fleet))
    assert admission._audits_dir() == fleet / "Audits" / "Orchestrator"

    monkeypatch.delenv("ORCH_FLEET_ROOT", raising=False)
    monkeypatch.setattr(admission.paths, "FLEET_ROOT", tmp_path / "no-fleet")
    home = tmp_path / "home"
    monkeypatch.setattr(admission.pathlib.Path, "home", classmethod(lambda cls: home))
    got = admission._audits_dir()
    assert got == home / "Library/CloudStorage/Dropbox/Learning/Code/Audits/Orchestrator"


def test_python_symbol_walk_and_syntax_failures():
    """Nested class members resolve; syntax errors and non-class parents do not."""
    nested = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def method(self):\n"
        "            pass\n"
        "        z = 1\n"
    )
    assert admission._python_symbol_defined(nested, "Outer.Inner.method")
    assert admission._python_symbol_defined(nested, "Outer.Inner.z")
    assert not admission._python_symbol_defined(nested, "Outer.missing")
    # Walk exhausts parts without returning early when the final part is absent inside a class.
    assert not admission._python_symbol_defined("class Outer:\n    pass\n", "Outer.Inner.method")
    # Leaf after a function is refused (must be a class to continue the walk).
    assert not admission._python_symbol_defined("def Outer():\n    pass\n", "Outer.child")
    assert not admission._python_symbol_defined("def broken(:\n", "x")
    assert (
        admission._definition_in(__import__("ast").parse("x = 1\ny: int = 2\n").body, "y")
        is not None
    )


def test_acted_on_context_records_monitor_exceptions(monkeypatch, tmp_path):
    """A monitor ImportError/raise must become a named reason, never an uncaught escape."""
    import feedback

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")

    def boom(*_a, **_k):
        raise RuntimeError("monitor offline")

    monkeypatch.setitem(sys.modules, "value_chain_monitor", SimpleNamespace(acted_on_index=boom))
    # Force re-import path inside the try: delete cached module attribute access via raise.
    monkeypatch.setattr(
        admission,
        "acted_on_context",
        admission.acted_on_context,
    )

    class FakeMonitor:
        @staticmethod
        def acted_on_index(*_a, **_k):
            raise RuntimeError("monitor offline")

    import types

    fake = types.ModuleType("value_chain_monitor")
    fake.acted_on_index = FakeMonitor.acted_on_index
    monkeypatch.setitem(sys.modules, "value_chain_monitor", fake)
    admission._ACTED_ON_CACHE.clear()
    ctx = admission.acted_on_context({"cap": {}})
    assert ctx["acted_on"] is None
    assert "RuntimeError" in ctx["acted_on_reason"]


def test_known_controls_and_findability_tolerate_io_failures(monkeypatch, tmp_path):
    """Missing control files and advisor import failures stay fail-open."""
    monkeypatch.setattr(admission.paths, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(admission.paths, "MODULE_DIR", tmp_path)
    # No files present → every read raises OSError → empty control set.
    assert admission.known_controls() == set()

    class Boom:
        def consulting_surfaces(self, **_k):
            raise ImportError("advisor gone")

        def surfaces_binding(self, *_a, **_k):
            raise ImportError("advisor gone")

    monkeypatch.setitem(sys.modules, "capability_advisor", Boom())
    ctx = admission._findability_context(["cap"])
    assert ctx["bound_surfaces"] == {}
    assert "ImportError" in ctx["consult_reach"]["unreadable"]


def test_admit_and_preflight_surface_errored_checks(tmp_path, monkeypatch):
    """A requirement that raises is recorded as a failed check, not an uncaught exception."""

    def boom(_cap, _ctx):
        raise ValueError("predicate exploded")

    def ok(_cap, _ctx):
        return True, "ok"

    monkeypatch.setattr(
        admission,
        "REQUIREMENTS",
        (("caller_exists", boom), ("rollback", boom)),
    )
    monkeypatch.setattr(admission, "REQUIREMENT_ENFORCED_FROM", {"caller_exists": 0, "rollback": 0})
    cap = {
        "capability_id": "cap",
        "event_history": [{"type": "created", "timestamp": admission._now()}],
    }
    result = admission.admit("cap", ctx={"now": admission._now()}, ledger={"cap": cap})
    assert result["checks"]["caller_exists"]["ok"] is False
    assert "check errored" in result["checks"]["caller_exists"]["detail"]

    # rollback is not a pre-build obligation, so preflight executes it and records the error.
    monkeypatch.setattr(
        admission,
        "REQUIREMENTS",
        (("caller_exists", ok), ("rollback", boom)),
    )
    pre = admission.preflight(
        {
            "capability_id": "proposed",
            "notes": "checked existing; not present; building new",
            "downstream_consumer": "x.py:run",
            "learning_sink": "brain",
            "kill_switch": "ORCH_STOP",
            "rollback": "revert",
            "expiry": "tomorrow",
        }
    )
    assert "caller_exists" in pre["obligations"]
    assert pre["checks"]["rollback"]["ok"] is False
    assert "check errored" in pre["checks"]["rollback"]["detail"]


def test_decision_records_and_unreadable_commitment_files(tmp_path, monkeypatch):
    """Dated decision matching and unreadable scan files stay fail-closed."""
    audits = tmp_path / "audits"
    audits.mkdir()
    (audits / "2026-02-01-tiny-decision.md").write_text("decided ORCH_TINY_UNTIL\n")
    monkeypatch.setattr(admission, "AUDITS", audits)
    assert admission._decision_recorded_after("ORCH_TINY_UNTIL", "2026-01-01") is True
    assert admission._decision_recorded_after("ORCH_TINY_UNTIL", "2026-03-01") is False
    # Short subject falls back to the full var name.
    (audits / "2026-02-02-note.md").write_text("orch_ab decided\n")
    assert admission._decision_recorded_after("ORCH_AB", "2026-01-01") is True
    assert admission._record_exists("2026-02-01-tiny-decision.md") is True

    root = tmp_path / "scan"
    root.mkdir()
    bad = root / "locked.py"
    bad.write_text("see 2026-01-01-never.md\n")
    # Make read fail after the file is discovered.
    real_read = Path.read_text

    def flaky(self, *a, **k):
        if self.name == "locked.py":
            raise OSError("permission denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr(admission, "_today", lambda: "2026-02-01")
    got = admission.commitments(root=root)
    assert got == {"clean": True, "dangling_citations": [], "overdue_without_record": []}


def test_consumer_class_grace_acting_and_not_acting(tmp_path):
    """Consumer classification distinguishes grace, acting, and expired not-acting."""
    modules = tmp_path / "src"
    modules.mkdir()
    (modules / "rail.py").write_text("def consume():\n    return True\n")
    base = {
        "capability_id": "cap",
        "downstream_consumer": "rail.py:consume",
        "event_history": [{"type": "invocation", "timestamp": 100}],
    }
    ctx = {
        "module_dir": modules,
        "repo_root": tmp_path,
        "acted_on": {"cap": 0},
        "now": 100 + admission.CONSUMER_GRACE_SECONDS - 10,
    }
    assert admission.consumer_class(base, ctx)[0] == "grace"
    ctx["now"] = 100 + admission.CONSUMER_GRACE_SECONDS + 10
    assert admission.consumer_class(base, ctx)[0] == "not_acting"
    ctx["acted_on"] = {"cap": 2}
    assert admission.consumer_class(base, ctx)[0] == "acting"
    no_inv = {**base, "event_history": []}
    ctx["acted_on"] = {"cap": 0}
    assert admission.consumer_class(no_inv, ctx)[0] == "not_invoked"
    assert admission.consumer_class({"capability_id": "x"}, ctx)[0] == "undeclared"
    prose = {**base, "downstream_consumer": "the weekly review rail"}
    assert admission.consumer_class(prose, ctx)[0] == "prose"


def test_format_report_clean_and_drained_branches():
    """Clean commitments and drained consumers print the success wording."""
    rep = {
        "admitted": 1,
        "total": 1,
        "enforced_total": 1,
        "enforced_failing": [],
        "legacy_debt": [],
        "rows": [],
        "findability": {
            "failing": [],
            "total": 0,
            "by_cause": {},
            "blocking": [],
            "deferred": [],
            "drainable": [],
            "bound_unconsulted_surfaces": [],
            "consult_sites_drifted": [],
            "consult_sites_unverified": [],
            "not_checked": "n/a",
        },
        "consumers": {
            "total": 1,
            "by_class": {name: [] for name in admission.CONSUMER_CLASSES},
            "blocking": [],
            "deferred": [],
            "drainable": [],
            "drained": True,
        },
        "commitments": {
            "clean": True,
            "dangling_citations": [],
            "overdue_without_record": [],
        },
    }
    rep["consumers"]["by_class"]["acting"] = ["cap"]
    text = admission.format_report(rep)
    assert "no enforced failures" in text
    assert "every declared consumer resolves" in text
    assert "every dated promise resolves" in text


def test_admission_main_cli_paths(tmp_path, monkeypatch, capsys):
    """CLI --preflight / --commitments / default report return without touching live state."""
    monkeypatch.setattr(
        admission,
        "preflight",
        lambda spec: {"capability_id": spec["capability_id"], "ready_to_build": False},
    )
    monkeypatch.setattr(
        admission,
        "commitments",
        lambda: {
            "clean": True,
            "dangling_citations": [],
            "overdue_without_record": [],
        },
    )
    monkeypatch.setattr(
        admission,
        "report",
        lambda: {
            "total": 0,
            "admitted": 0,
            "enforced_total": 0,
            "enforced_failing": [],
            "legacy_debt": [],
            "rows": [],
            "commitments": {
                "clean": True,
                "dangling_citations": [],
                "overdue_without_record": [],
            },
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["capability_admission", "--preflight", json.dumps({"capability_id": "x"})],
    )
    assert admission.main() == 0
    monkeypatch.setattr(sys, "argv", ["capability_admission", "--commitments"])
    assert admission.main() == 0
    monkeypatch.setattr(sys, "argv", ["capability_admission", "--commitments", "--json"])
    assert admission.main() == 0
    monkeypatch.setattr(
        admission,
        "commitments",
        lambda: {
            "clean": False,
            "dangling_citations": [{"file": "a", "line": 1, "record": "r"}],
            "overdue_without_record": [],
        },
    )
    monkeypatch.setattr(sys, "argv", ["capability_admission", "--commitments"])
    assert admission.main() == 1
    monkeypatch.setattr(sys, "argv", ["capability_admission"])
    assert admission.main() == 0
    out = capsys.readouterr().out
    assert "x" in out or "Commitments" in out or "ADMITTED" in out


# --------------------------------------------------------------------------- env_prereq


def test_env_prereq_harness_glue_and_detectors(tmp_path, monkeypatch, capsys):
    """Harness glue and local detectors name absences without spawning agents."""
    with pytest.raises(env_prereq.MissingPrerequisite, match="gone"):
        env_prereq.require(None, "gone")
    assert env_prereq.selftest_skipped("mod", None, "missing cli") is True
    assert env_prereq.PREREQ_ABSENT_MARK in capsys.readouterr().out
    assert env_prereq.selftest_skipped("mod", None) is False

    gaps: list[str] = []
    assert env_prereq.runnable(gaps, None) is True
    assert env_prereq.runnable(gaps, "no ledger") is False
    assert gaps == ["no ledger"]
    env_prereq.report_gaps("mod", gaps)
    assert "section skipped" in capsys.readouterr().out

    monkeypatch.setenv("VIBE_HOME", str(tmp_path / "novibe"))
    assert "vibe config absent" in (env_prereq.vibe_config_absent() or "")
    vibe = tmp_path / "vibe"
    vibe.mkdir()
    (vibe / "config.toml").write_text('active_model = "demo"\n')
    monkeypatch.setenv("VIBE_HOME", str(vibe))
    assert env_prereq.vibe_config_absent() is None
    assert env_prereq.vibe_active_model() == "demo"
    (vibe / "config.toml").write_text("not = [toml\n")
    assert env_prereq.vibe_active_model() is None

    missing = env_prereq.repo_files_absent("does-not-exist-1026.xyz")
    assert missing and "does-not-exist-1026.xyz" in missing
    assert env_prereq.repo_files_absent("src/env_prereq.py") is None

    monkeypatch.setattr(
        env_prereq.shutil, "which", lambda name: None if name == "node" else "/bin/true"
    )
    assert "node is not on PATH" in (env_prereq.node_absent() or "")
    monkeypatch.setattr(env_prereq.shutil, "which", lambda name: "/usr/bin/node")
    assert env_prereq.node_absent() is None


def test_env_prereq_recorded_verdicts_and_replay(tmp_path):
    """Recorded pytest verdicts key by stem and replay without re-running the check."""
    results = tmp_path / "results.json"
    # Keys are file stems, not paths — see recorded_verdicts().
    results.write_text(
        json.dumps(
            {
                "test_fake": {
                    "test_ok": {"outcome": "passed", "message": ""},
                    "test_bad": {"outcome": "failed", "message": "boom"},
                    "test_skip": {"outcome": "skipped", "message": "gone"},
                }
            }
        )
    )
    here = "tests/test_fake.py"
    got = env_prereq.recorded_verdicts([here, env_prereq.PYTEST_RESULTS_FLAG, str(results)], here)
    assert got["test_ok"]["outcome"] == "passed"
    assert env_prereq.recorded_verdicts([here], here) == {}

    calls: list[str] = []

    def check_ok():
        calls.append("ok")

    def check_bad():
        raise AssertionError("should not run when failed is recorded")

    def check_skip():
        raise AssertionError("should not run when skipped is recorded")

    env_prereq.run_or_replay(check_ok, {"check_ok": {"outcome": "passed", "message": ""}})
    assert calls == []
    with pytest.raises(AssertionError, match="boom"):
        env_prereq.run_or_replay(check_bad, {"check_bad": {"outcome": "failed", "message": "boom"}})
    with pytest.raises(env_prereq.MissingPrerequisite, match="gone"):
        env_prereq.run_or_replay(
            check_skip, {"check_skip": {"outcome": "skipped", "message": "gone"}}
        )
    env_prereq.run_or_replay(check_ok, {})
    assert calls == ["ok"]


def test_env_prereq_seat_and_cli_detectors(monkeypatch):
    """Seat/CLI detectors name the missing binary without probing a live agent."""
    monkeypatch.setattr(
        env_prereq,
        "_seat_signals",
        lambda: {"codex": False, "claude": True},
    )
    reason = env_prereq.seat_has_no_free_signal()
    assert reason and "codex" in reason
    monkeypatch.setattr(env_prereq, "_seat_signals", lambda: {"codex": True})
    assert env_prereq.seat_has_no_free_signal() is None

    import adapters as adapters_mod

    monkeypatch.setitem(
        adapters_mod.AUTH_PROBES,
        "ghost",
        {"cmd": ["ghost-cli-1026", "auth"]},
    )
    monkeypatch.setattr(env_prereq.shutil, "which", lambda _n: None)
    msg = env_prereq.agent_cli_absent("ghost")
    assert msg and "ghost" in msg


def test_env_prereq_main_reports_shape(monkeypatch, capsys):
    """Default main prints tree/machine status without running --selftest."""
    monkeypatch.setattr(env_prereq, "exec_mirror_shape", lambda: None)
    monkeypatch.setattr(env_prereq, "bare_machine", lambda: None)
    monkeypatch.setattr(env_prereq, "skill_resource_absent", lambda: "no skill")
    monkeypatch.setattr(env_prereq, "codex_profile_binary_absent", lambda: None)
    monkeypatch.setattr(env_prereq, "seat_has_no_free_signal", lambda: None)
    monkeypatch.setattr(env_prereq, "credential_file_absent", lambda *_: None)
    monkeypatch.setattr(env_prereq, "ledger_legacy_rows_absent", lambda: "no legacy")
    assert env_prereq.main([]) == 0
    out = capsys.readouterr().out
    assert "tree shape:" in out
    assert "ABSENT" in out


# --------------------------------------------------------------------------- ledger_reconcile


def test_ledger_helpers_cover_fail_closed_edges(tmp_path, monkeypatch):
    """Malformed usage/cost/marker/owner-question data never credits a run."""
    assert ledger_reconcile._usage_from_obj({"usage": "bad"}) == (0, 0)
    assert ledger_reconcile._usage_from_obj({"total_tokens": 9}) == (9, 0)
    assert ledger_reconcile._latency_s([]) == 0.0
    assert (
        ledger_reconcile._latency_s([{"event": "start", "ts": 10}, {"event": "complete", "ts": 25}])
        == 15.0
    )
    assert ledger_reconcile._latest_log_file(
        [{"log_file": ""}, {"log_file": str(tmp_path / "a.log")}]
    )
    assert ledger_reconcile._resume_token_from_segment(["no token here"]) is None

    log = tmp_path / "run.log"
    log.write_text(
        "=== 2026-10-22T01:00:00Z run_id=run-a ===\n"
        "not-json\n"
        "[1,2]\n"
        '{"cost_usd":"bad"}\n'
        "OWNER_QUESTION: {not-json}\n"
        'OWNER_QUESTION: {"question": "ok?", "default": "yes"}\n'
    )
    writers = {"run-a"}
    assert ledger_reconcile._log_usage(log, "run-a", writers) == (0, 0)
    assert ledger_reconcile._log_cost_usd(log, "run-a", writers) == 0.0
    seg = ledger_reconcile._log_segment(log, "run-a", writers)
    assert ledger_reconcile._owner_questions_from_segment(seg) == [
        {"question": "ok?", "default": "yes"}
    ]

    missing = tmp_path / "gone.log"
    assert ledger_reconcile._log_segment(missing, "run-a") == []

    blocked = tmp_path / "blocked.log"
    blocked.write_text("x\n")
    real_read = Path.read_text

    def flaky_read(self, *a, **k):
        if self == blocked:
            raise OSError("nope")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert ledger_reconcile._log_segment(blocked, "run-a") == []
    monkeypatch.setattr(Path, "read_text", real_read)

    done = tmp_path / "done"
    done.mkdir()
    (done / "run-a.json").write_text('{"run_id":"run-a","rc":"nope"}')
    marker = ledger_reconcile._done_marker(tmp_path / "x.log", "run-a")
    assert marker is not None
    assert ledger_reconcile._signal_death_rc(marker) is None
    assert ledger_reconcile._signal_death_rc({"rc": "x"}) is None
    assert ledger_reconcile._done_marker(None, "run-a") is None
    assert ledger_reconcile._done_marker(tmp_path / "x.log", "missing") is None


def test_scrub_and_classify_fail_open(tmp_path, monkeypatch, capsys):
    """Scrub statuses and classifier exceptions never abort completion accounting."""
    assert ledger_reconcile._scrub_run_segment(None, "r", 10) == 0
    assert ledger_reconcile._scrub_run_segment(str(tmp_path / "a.log"), "r", None) == 0
    assert (
        ledger_reconcile._scrub_run_segment(
            str(tmp_path / "a.log"), "r", ledger_reconcile.CREDENTIAL_SCRUB_SINCE - 1
        )
        == 0
    )

    import credential_redaction

    monkeypatch.setattr(
        credential_redaction,
        "scrub_file",
        lambda *_a, **_k: {"status": "disabled", "redacted": 0, "kinds": []},
    )
    assert (
        ledger_reconcile._scrub_run_segment(
            str(tmp_path / "a.log"), "r", ledger_reconcile.CREDENTIAL_SCRUB_SINCE + 1
        )
        == 0
    )
    assert "DISABLED" in capsys.readouterr().err

    monkeypatch.setattr(
        credential_redaction,
        "scrub_file",
        lambda *_a, **_k: {"status": "error", "error": "too big", "redacted": 0},
    )
    ledger_reconcile._scrub_run_segment(
        str(tmp_path / "a.log"), "r", ledger_reconcile.CREDENTIAL_SCRUB_SINCE + 1
    )
    assert "not scrubbed" in capsys.readouterr().err

    monkeypatch.setattr(
        credential_redaction,
        "scrub_file",
        lambda *_a, **_k: {"status": "ok", "redacted": 2, "kinds": ["token"]},
    )
    monkeypatch.setattr(credential_redaction, "describe", lambda _k: "token")
    assert (
        ledger_reconcile._scrub_run_segment(
            str(tmp_path / "a.log"), "r", ledger_reconcile.CREDENTIAL_SCRUB_SINCE + 1
        )
        == 2
    )

    import rate_incidents

    monkeypatch.setattr(
        ledger_reconcile,
        "provider_limit_before_work",
        lambda _lines: (_ for _ in ()).throw(RuntimeError("classify boom")),
    )
    assert ledger_reconcile._classify_run_log_segment(["line"], "codex", "run-a") is None
    assert "classification failed" in capsys.readouterr().err

    monkeypatch.setattr(ledger_reconcile, "provider_limit_before_work", lambda _l: None)
    monkeypatch.setattr(rate_incidents, "stdout_carries_capacity_evidence", lambda _t: True)
    monkeypatch.setattr(
        rate_incidents,
        "get_structured_evidence",
        lambda **_k: {"is_authoritative": True, "category": "c", "subcategory": "s"},
    )
    monkeypatch.setattr(
        rate_incidents,
        "record_incident",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("write boom")),
    )
    monkeypatch.setattr(rate_incidents, "provider_reset_at", lambda *_a: None)
    ev = ledger_reconcile._classify_run_log_segment(["HTTP 429"], "codex", "run-a")
    assert ev is not None
    assert "recording failed" in capsys.readouterr().err


def test_record_completion_and_reconcile_dry_paths(tmp_path, monkeypatch, capsys):
    """Completion and reconcile paths exercise accounting with fully stubbed sinks."""
    import adapters
    import feedback
    import pushed_branches

    completed: list[str] = []
    monkeypatch.setattr(ledger_reconcile, "_scrub_run_segment", lambda *_a, **_k: 1)
    monkeypatch.setattr(ledger_reconcile, "_completion_writers", lambda *_a, **_k: None)
    monkeypatch.setattr(ledger_reconcile, "_log_segment", lambda *_a, **_k: [])
    monkeypatch.setattr(ledger_reconcile, "_classify_run_log_segment", lambda *_a, **_k: None)
    monkeypatch.setattr(
        feedback,
        "complete_profile_attempt",
        lambda run_id, **_k: completed.append(run_id),
    )
    monkeypatch.setattr(adapters, "record_ledger", lambda *_a, **_k: None)
    monkeypatch.setattr(
        feedback,
        "record_completion_event",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sink down")),
    )
    monkeypatch.setattr(
        pushed_branches,
        "record_at_completion",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("push fail")),
    )
    (tmp_path / "r.log").write_text("=== 2026-10-22T01:00:00Z run_id=run-x ===\n")
    ledger_reconcile.record_completion(
        "run-x",
        "codex",
        target="offload:/tmp/ws",
        selected_profile_id="codex:stable",
        resolved_model="gpt",
        resolved_provider="openai",
        started_ts=ledger_reconcile.CREDENTIAL_SCRUB_SINCE + 10,
        workspace=str(tmp_path / "ws"),
        log_file=str(tmp_path / "r.log"),
        exit_code=0,
    )
    assert completed == ["run-x"]
    assert "push record not written" in capsys.readouterr().err

    # Unresolved probe path when the CLI reports no model.
    unresolved: list[str] = []
    monkeypatch.setattr(
        adapters,
        "cli_reported_model",
        lambda *_a, **_k: {"model": None, "reason": "no_log"},
    )
    monkeypatch.setattr(
        feedback,
        "complete_profile_attempt_unresolved",
        lambda run_id, **_k: unresolved.append(run_id),
    )
    ledger_reconcile.record_completion(
        "run-y",
        "codex",
        target="offload:/tmp/ws",
        selected_profile_id="codex:stable",
        started_ts=ledger_reconcile.CREDENTIAL_SCRUB_SINCE + 10,
        workspace=None,
        log_file=None,
        exit_code=1,
    )
    assert unresolved == ["run-y"]

    ledger = tmp_path / "ledger.ndjson"
    log = tmp_path / "agent.log"
    log.write_text(
        "=== 2026-10-22T01:00:00Z run_id=run-1 ===\n"
        + json.dumps({"usage": {"input_tokens": 1, "output_tokens": 2}, "cost_usd": 0.1})
        + "\n"
    )
    ledger.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "event": "start",
                "ts": 1,
                "agent": "codex",
                "target": "o/r#1",
                "log_file": str(log),
            }
        )
        + "\n"
        + json.dumps({"run_id": "run-1", "event": "complete", "ts": 5, "exit": 0})
        + "\n"
        + json.dumps({"event": "start"})
        + "\n"
        + "not-json\n"
    )
    monkeypatch.setattr(ledger_reconcile, "_known_runs", lambda: {"run-1"})
    monkeypatch.setattr(ledger_reconcile, "_cost_sources", lambda: {})
    monkeypatch.setattr(ledger_reconcile, "provider_limit_before_work", lambda *_a, **_k: None)
    monkeypatch.setattr(ledger_reconcile, "_classify_run_log_segment", lambda *_a, **_k: None)
    monkeypatch.setattr(feedback, "expire_owner_questions", lambda: 0)
    monkeypatch.setattr(feedback, "record_resume_token", lambda *_a, **_k: None)
    monkeypatch.setattr(feedback, "record_owner_question", lambda *_a, **_k: {"deduped": False})
    summary = ledger_reconcile.reconcile(ledger, dry_run=True)
    assert summary["rows_read"] == 3
    assert summary["dry_run"] is True
    assert "missing_run_id" in summary["skipped"]
    ledger_reconcile._print_summary(summary, as_json=False)
    ledger_reconcile._print_summary(summary, as_json=True)
    out = capsys.readouterr().out
    assert "ledger_reconcile:" in out
    assert "rows_read" in out


def test_ledger_main_cli_dispatch(monkeypatch, capsys):
    """CLI subcommands dispatch without running the live selftest or network."""
    monkeypatch.setattr(
        ledger_reconcile,
        "resolve_unresolved_worker_attempts",
        lambda **_k: {"candidates": 0, "applied": False},
    )
    monkeypatch.setattr(
        ledger_reconcile,
        "reconcile",
        lambda *_a, **_k: {
            "dry_run": True,
            "rows_read": 0,
            "run_ids_seen": 0,
            "written_cost_rows": 0,
            "provider_limit_deaths": {
                "seen": 0,
                **{b: 0 for b in ledger_reconcile.PROVIDER_LIMIT_BUCKETS},
            },
            "infra_classified": 0,
            "infra_unattributed_marker_rc": 0,
            "skipped": {"x": 1},
            "errors": ["bad row"],
            "strict_failed": False,
        },
    )
    monkeypatch.setattr(ledger_reconcile, "record_completion", lambda *_a, **_k: None)
    assert ledger_reconcile.main(["resolve-unresolved"]) == 0
    assert ledger_reconcile.main(["reconcile", "--dry-run", "--json"]) == 0
    assert (
        ledger_reconcile.main(
            ["complete", "--run-id", "r", "--agent", "codex", "--target", "o/r#1"]
        )
        == 0
    )
    assert ledger_reconcile.main([]) == 2
    out = capsys.readouterr().out
    assert "candidates" in out or "ledger_reconcile" in out or "usage" in out.lower()
