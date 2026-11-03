"""Hermetic regression coverage for capability-advisor's defensive decision paths.

These tests deliberately exercise the conservative outcomes: unavailable facts remain unknown,
failed reads never turn into offers, and a real recurring shape outranks a keyword fallback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import capabilities
import capability_advisor as advisor

# The live runtime directories as this process found them, before the guard below replaces them.
# The checkout itself may sit under the live state dir (agents work in its `worktrees/`), so the
# checkout is exempt: reading source is not reading runtime state.
_LIVE_RUNTIME_ROOTS = tuple(
    {
        Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator")).resolve(),
        Path(
            os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator")
        ).resolve(),
        capabilities.LOCAL_RUNTIME.resolve(),
        capabilities.REG.resolve().parent,
    }
)
# pytest's own temporary root is exempt too: an agent's TMPDIR can itself sit under the live
# runtime dir, and every redirected path below lives there.
_EXEMPT_ROOTS: list[Path] = [Path(__file__).resolve().parent.parent]
# Every process-start and network boundary the exercised code could reach, by audit event rather
# than by helper, so an agent launch through any wrapper (dispatcher, fleet_shapes' `gh`, os.system,
# posix_spawn) is caught the same way.
_EXTERNAL_EVENTS = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "os.forkpty",
    "pty.spawn",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "urllib.Request",
    "http.client.connect",
)
_attempted: list[tuple[str, str]] | None = None


def _live_runtime_path(raw) -> bool:
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return False
    try:
        path = Path(os.fsdecode(raw)).resolve()
    except (OSError, ValueError):
        return False
    if any(path.is_relative_to(root) for root in _EXEMPT_ROOTS):
        return False
    return any(path.is_relative_to(root) for root in _LIVE_RUNTIME_ROOTS)


def _guard(event: str, args: tuple) -> None:
    if _attempted is None:
        return
    if event in _EXTERNAL_EVENTS:
        _attempted.append((event, repr(args)[:200]))
        raise RuntimeError(f"advisor coverage test attempted external operation {event}")
    if event == "open" and args and _live_runtime_path(args[0]):
        _attempted.append((event, os.fsdecode(args[0])))
        raise RuntimeError(f"advisor coverage test touched live runtime state {args[0]}")


sys.addaudithook(_guard)


@pytest.fixture(scope="module", autouse=True)
def isolated_runtime_and_external_calls(tmp_path_factory):
    """Redirect both runtime directories and arm the external-operation recorder for the module.

    `capabilities` resolved its ledger path when it was imported, before any fixture could run, so
    the import-time constants are repointed too; the audit hook then proves no read reached the
    live directories by any path that was resolved earlier. The guard is a MonkeyPatch of its own,
    so no test's `monkeypatch` can undo it mid-test.
    """
    global _attempted
    root = tmp_path_factory.mktemp("advisor-coverage")
    _EXEMPT_ROOTS.append(tmp_path_factory.getbasetemp().resolve())
    guard = pytest.MonkeyPatch()
    guard.setenv("ORCH_STATE_DIR", str(root / "state"))
    guard.setenv("ORCH_LOCAL_RUNTIME", str(root / "runtime"))
    guard.setenv(capabilities.LEDGER_PATH_ENV, str(root / "runtime" / "capabilities.json"))
    guard.setattr(capabilities, "LOCAL_RUNTIME", root / "runtime")
    guard.setattr(capabilities, "REG", root / "runtime" / "capabilities.json")
    _attempted = []
    try:
        yield _attempted
    finally:
        attempted, _attempted = _attempted, None
        guard.undo()
        del _EXEMPT_ROOTS[1:]
    assert attempted == []


@pytest.fixture(autouse=True)
def no_external_operation_in_this_test(isolated_runtime_and_external_calls):
    """Attribute any attempted operation to the test that made it, even if the code swallowed it."""
    before = len(isolated_runtime_and_external_calls)
    yield
    assert isolated_runtime_and_external_calls[before:] == []


def test_guard_records_and_blocks_external_operations(isolated_runtime_and_external_calls):
    """The recorder is live: a blocked launch is refused and recorded, then cleared for this check."""
    import subprocess

    live_ledger = next(
        root / "capabilities.json"
        for root in _LIVE_RUNTIME_ROOTS
        if _live_runtime_path(root / "capabilities.json")
    )
    before = len(isolated_runtime_and_external_calls)
    with pytest.raises(RuntimeError, match="subprocess.Popen"):
        subprocess.run(["true"])
    with pytest.raises(RuntimeError, match="live runtime state"):
        open(live_ledger)
    assert [event for event, _ in isolated_runtime_and_external_calls[before:]] == [
        "subprocess.Popen",
        "open",
    ]
    del isolated_runtime_and_external_calls[before:]
    assert not _live_runtime_path(os.environ["ORCH_STATE_DIR"])
    assert not _live_runtime_path(os.environ["ORCH_LOCAL_RUNTIME"])


def test_dispatcher_failure_and_legacy_matcher_degrade_without_inventing_a_route(monkeypatch):
    """A broken dispatcher may remove direct routes, but it must not manufacture one."""
    import dispatcher

    monkeypatch.setattr(dispatcher, "TASK_TYPE_CAPABILITY", object())
    assert advisor.direct_entry() == {"offload": "offload"}
    assert advisor.entry_requirement({"matcher": {"old": "shape"}}) == {
        "mode": "legacy",
        "keys": ["old"],
        "detail": "legacy matcher over ['old']",
    }


def test_fact_missing_note_has_a_safe_default():
    assert advisor._fact_missing_note({}) == "PR size unknown"
    assert (
        advisor._fact_missing_note({"unevaluated_because": ["labels unavailable"]})
        == "labels unavailable"
    )


def test_contested_verdict_requires_two_known_and_disagreeing_values():
    assert advisor._probe_contested_verdict({"verifier_verdict": "pass"})[0] is None
    assert advisor._probe_contested_verdict(
        {"verifier_verdict": " pass ", "merge_disposition": "PASS"}
    ) == (False, "verifier verdict agrees with merge disposition")
    assert advisor._probe_contested_verdict(
        {"verifier_verdict": "PASS", "merge_disposition": "CONCERNS"}
    ) == (True, "verifier PASS differs from merge disposition CONCERNS")


def test_repeated_pattern_prefers_measured_shape_then_has_honest_fallback(monkeypatch):
    seen = []

    monkeypatch.setattr(
        advisor.fleet_shapes,
        "shape_signature",
        lambda title, labels, paths: seen.append((title, labels, paths)) or {"key": "fix|python"},
    )
    monkeypatch.setattr(
        advisor.fleet_shapes,
        "recurring_shape",
        lambda key: {"prs": 3, "repos": ["a/r", "b/r"], "window_days": 14, "examples": ["a/r#1"]},
    )
    matched, evidence = advisor._probe_repeated_pattern(
        {"title": "fix", "labels": ["bug"], "paths": ["src/a.py"]}
    )
    assert matched is True and "3 PRs in 2 repos" in evidence and "a/r#1" in evidence
    assert seen == [("fix", ["bug"], ["src/a.py"])]

    monkeypatch.setattr(advisor.fleet_shapes, "recurring_shape", lambda _key: None)
    assert advisor._probe_repeated_pattern({"title": "bulk cleanup"}) == (
        True,
        "repeated-pattern signal: 'bulk'",
    )
    assert advisor._probe_repeated_pattern({})[0] is None
    assert advisor._probe_repeated_pattern({"title": "ordinary fix", "changedFiles": 1})[0] is False


def test_high_stakes_probe_keeps_shadow_and_incomplete_facts_unknown(monkeypatch):
    import adversarial

    monkeypatch.setattr(
        adversarial, "shape_shadow_readiness", lambda *, path=None: (False, "no cohort")
    )
    assert advisor._probe_high_stakes_shape({}) == (None, "shape route held in shadow: no cohort")

    monkeypatch.setattr(adversarial, "shape_shadow_readiness", lambda *, path=None: (True, "ready"))
    monkeypatch.setattr(adversarial, "high_stakes_from_shape", lambda facts: "workflow changed")
    monkeypatch.setattr(adversarial, "high_stakes_label_reason", lambda facts: None)
    monkeypatch.setattr(adversarial, "high_stakes_title_reason", lambda facts: None)
    assert advisor._probe_high_stakes_shape({}) == (True, "workflow changed")

    monkeypatch.setattr(adversarial, "high_stakes_from_shape", lambda facts: None)
    monkeypatch.setattr(adversarial, "shape_facts_complete", lambda facts: False)
    assert advisor._probe_high_stakes_shape({})[0] is None
    monkeypatch.setattr(adversarial, "shape_facts_complete", lambda facts: True)
    assert advisor._probe_high_stakes_shape({"source_labels_complete": True})[0] is False


def test_pr_reference_and_fetch_failures_do_not_guess(monkeypatch):
    assert advisor._pr_number_from("review o/r#42", "o/r", None) == 42
    assert advisor._pr_number_from("review #19", "", {"pr": "not-an-int"}) is None
    assert advisor._pr_number_from("review #19", "", None) == 19
    # The boundary the fix changed: an identifier glued to the hash is not a reference (the old `\b` pattern matched it).
    assert advisor._pr_number_from("abc#19", "", None) is None
    assert advisor._pr_number_from("PR#19", "", None) is None

    monkeypatch.setattr(
        advisor.fleet_shapes, "fetch_facts", lambda *_args: (_ for _ in ()).throw(OSError())
    )
    assert advisor._fetch_pr_facts("o/r", 42) is None
    monkeypatch.setattr(
        advisor.fleet_shapes, "fetch_facts", lambda *_args: {42: {"files_total": 3}}
    )
    assert advisor._fetch_pr_facts("o/r", 42) == {"files_total": 3, "number": 42, "changedFiles": 3}


def test_observable_surface_detects_multiple_marker_kinds_and_ignores_hidden_tree(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main />")
    (tmp_path / "widget.tsx").write_text("export default 1")
    (tmp_path / "package.json").write_text('{"dependencies":{"fastapi":"x"}}')
    # `node_modules` is excluded only by the skip list and `.hidden-tree` only by the dot-prefix
    # rule, so dropping either rule leaks a marker.
    for name in (".git", "node_modules", ".hidden-tree"):
        hidden = tmp_path / name
        hidden.mkdir()
        (hidden / "ignored.html").write_text("<main />")

    result = advisor.detect_observable_surface(tmp_path)
    assert result["observable"] is True
    assert {"html:index.html", "component:widget.tsx", "dependency:fastapi:package.json"} <= set(
        result["markers"]
    )
    # The hidden tree is IGNORED, not merely not required: nothing under `.git` may appear as a marker.
    assert not any("ignored.html" in marker for marker in result["markers"])
    assert advisor.detect_observable_surface(tmp_path / "missing")["observable"] is None


def test_observable_surface_bounds_large_walks_and_ignores_unreadable_dependency_files(
    monkeypatch, tmp_path: Path
):
    import os
    import pathlib

    with monkeypatch.context() as walk:
        walk.setattr(
            os, "walk", lambda _root: iter([(str(tmp_path), [], ["plain.txt"] * 40001)])
        )
        truncated = advisor.detect_observable_surface(tmp_path)
    assert truncated == {
        "observable": False,
        "markers": [],
        "truncated": True,
        "detail": f"stopped after {advisor._SURFACE_MAX_FILES} files",
    }

    dependency = tmp_path / "package.json"
    dependency.write_text("fastapi")
    real_read_text = pathlib.Path.read_text
    monkeypatch.setattr(
        pathlib.Path,
        "read_text",
        lambda self, *args, **kwargs: (
            (_ for _ in ()).throw(OSError())
            if self == dependency
            else real_read_text(self, *args, **kwargs)
        ),
    )
    unreadable = advisor.detect_observable_surface(tmp_path)
    assert unreadable["observable"] is False and unreadable["markers"] == []


def test_precondition_probe_failure_is_unevaluated_and_memoized(monkeypatch, tmp_path: Path):
    cap_id = "coverage-probe"
    monkeypatch.setitem(advisor.CAPABILITY_PRECONDITIONS, cap_id, {"requires": "test-probe"})

    calls = []

    def exploding_probe(path):
        calls.append(path)
        raise RuntimeError("private path must not leak")

    monkeypatch.setitem(advisor.REPO_FACT_PROBES, "test-probe", exploding_probe)
    memo = {}
    first = advisor.evaluate_precondition(cap_id, repo_path=str(tmp_path), facts=memo)
    second = advisor.evaluate_precondition(cap_id, repo_path=str(tmp_path), facts=memo)
    assert calls == [str(tmp_path)]
    assert first["requirement_met"] is None
    assert first["unevaluated_because"] == ["'test-probe': probe failed: RuntimeError"]
    assert second["requirement_evidence"] == first["requirement_evidence"]


def test_phase_consult_reports_per_phase_failures_and_budget_exhaustion(monkeypatch):
    clock = iter([0, 0, advisor.CONSULT_BUDGET_S + 1])
    monkeypatch.setattr(advisor.time, "monotonic", lambda: next(clock))

    def broken_advice(*_args, **_kwargs):
        raise RuntimeError("synthetic ledger failure")

    monkeypatch.setattr(advisor, "advise", broken_advice)
    report = advisor.consult_phases(
        day="2026-10-16", surfaces=["tick:one", "tick:two"], record=False
    )
    assert report["errors"] == ["tick:one", "tick:two"]
    assert "RuntimeError" in report["rows"][0]["error"]
    assert "budget" in report["rows"][1]["error"]
    rendered = advisor.format_phase_consult(report)
    assert "tick:one: ERROR" in rendered and "tick:two: ERROR" in rendered


def test_guarded_phase_consult_returns_a_report_when_signal_setup_or_consult_fails(monkeypatch):
    import signal

    monkeypatch.setattr(
        signal, "signal", lambda *_args: (_ for _ in ()).throw(ValueError("no signal"))
    )
    monkeypatch.setattr(
        advisor, "consult_phases", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    report = advisor.consult_phases_guarded(day="2026-10-16")
    assert report["errors"] == ["*"]
    assert report["error"] == "RuntimeError: boom"


def test_adjudicator_is_withheld_and_decline_write_failures_stay_nonfatal(monkeypatch):
    entries = [
        {
            "capability_id": "role-adjudicator",
            "pr_requirement_met": None,
            "precondition_note": "unknown",
        },
        {"capability_id": "other", "pr_requirement_met": None},
    ]
    summary = {}
    advisor._filter_contested_verdict_offers(entries, summary)
    assert [row["capability_id"] for row in entries] == ["other"]
    assert summary["withheld"] == [{"capability_id": "role-adjudicator", "reason": "unknown"}]

    import capability_propensity

    monkeypatch.setattr(
        capability_propensity,
        "record_decline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("temporary")),
    )
    assert (
        advisor._record_auto_declines(
            {
                "experiment_id": "advice:test",
                "capabilities": [{"capability_id": "other", "auto_declined": {"reason": "no"}}],
            }
        )
        == 0
    )


def test_cli_tick_and_task_modes_render_their_report_without_external_calls(monkeypatch, capsys):
    monkeypatch.setattr(
        advisor,
        "consult_phases_guarded",
        lambda: {"day": "2026-10-16", "phases": 0, "offered": 0, "recorded": 0, "rows": []},
    )
    assert advisor.main(["--consult-tick-phases", "--json"]) == 0
    assert '"phases": 0' in capsys.readouterr().out

    monkeypatch.setattr(
        advisor,
        "advise",
        lambda *_args, **_kwargs: {
            "useful": False,
            "reason": "synthetic",
            "capabilities": [],
            "task_types": [],
        },
    )
    assert advisor.main(["plain", "task"]) == 0
    assert "NO ORCHESTRATOR CAPABILITY APPLIES" in capsys.readouterr().out


def test_reask_reports_only_meaningful_context_changes():
    previous = {
        "task_types": ["review"],
        "skill": "repo-audit",
        "repository": "owner/one",
        "dispatch_ready_count": 0,
    }
    changed = advisor.should_reask(
        previous,
        {
            "task": "add unit tests for the retry helper",
            "skill": "testgen",
            "repository": "owner/two",
            "capabilities_ready": 1,
        },
    )
    assert changed == {
        "reask": True,
        "reasons": [
            "task_reclassified:testgen",
            "skill_invoked:testgen",
            "scope_changed",
            "capability_became_dispatch_ready",
        ],
    }
    assert advisor.should_reask(
        previous, {"task": "audit", "skill": "repo-audit", "repository": "owner/one"}
    ) == {
        "reask": False,
        "reasons": [],
    }


def test_surface_status_distinguishes_unspecified_declared_inherited_and_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(advisor, "SURFACE_BINDINGS", {"test-root": {"cap": "reason"}})
    monkeypatch.setattr(advisor, "CONSULT_SITES", {})
    monkeypatch.setattr(advisor, "KNOWN_UNCONSULTED", set())
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)

    assert advisor.surface_status("", path=ledger)["status"] == "unspecified"
    assert advisor.surface_status("test-root", path=ledger) == {
        "surface": "test-root",
        "status": "declared",
        "resolved_from": "test-root",
        "did_you_mean": [],
    }
    assert advisor.surface_status("test-root:phase-1", path=ledger)["status"] == "inherited"
    unknown = advisor.surface_status("unknown-surface", path=ledger)
    assert unknown["status"] == "unknown"
    assert unknown["did_you_mean"] == ["test-root"]


def test_consulting_surfaces_reports_verified_missing_and_drifted_callers(tmp_path, monkeypatch):
    verified = tmp_path / "verified.py"
    drifted = tmp_path / "drifted.py"
    verified.write_text("surface='test:verified'\n", encoding="utf-8")
    drifted.write_text("surface='different'\n", encoding="utf-8")
    monkeypatch.setattr(advisor, "SURFACE_BINDINGS", {})
    monkeypatch.setattr(
        advisor,
        "CONSULT_SITES",
        {
            "test:verified": {"caller": str(verified), "literal": "test:verified"},
            "test:missing": {"caller": str(tmp_path / "missing.py"), "literal": "test:missing"},
            "test:drifted": {"caller": str(drifted), "literal": "test:drifted"},
        },
    )

    report = advisor.consulting_surfaces(ledger={})
    assert report["verified"] == ["test:verified"]
    assert report["unverified"] == [
        {
            "surface": "test:missing",
            "caller": str(tmp_path / "missing.py"),
            "why": "caller not present on this machine",
        }
    ]
    assert report["drifted"] == [
        {
            "surface": "test:drifted",
            "caller": str(drifted),
            "why": "caller no longer names 'test:drifted'",
        }
    ]
    assert report["reached"] == ["test:missing", "test:verified"]
