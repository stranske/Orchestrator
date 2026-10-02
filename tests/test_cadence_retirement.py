"""A retired cadence step is ONE declared fact, and every surface that reports the step reads it.

The issue-readiness step was retired by default on 2026-09-15, and three surfaces disagreed about it
for seventeen days. orchestrate.sh skipped it with a silent `:` behind its own copy of the re-enable
condition, against the tick's own rule that a disable announces itself. `inspect_cadence` read a
registry row that still described a live daily step and called it `stale` (340 h on 2026-10-02): a
retired step's stamp only ages, so `stale` was the one verdict it could never leave and a stale count
of zero was unreachable. And the capability ledger's `gate_reason` still said the gate was ARMED and
"the assessment and the label write both run".

The fix declares the retirement once, as `retired` on the step's `cadence_registry` row. These tests
hold each reader to that row, and hold that no second copy of the condition exists:

1. the tick's REAL `_cadence_due`, replayed from orchestrate.sh over the generated shell, skips the
   step with a `[retired]` line naming the flags that lift it, touches no stamp, and runs the step
   when either flag is set;
2. the shell's decision follows the ROW: move the retirement to another step and the shell follows;
3. no call site restates the condition in its if/elif chain (the shape that went silent);
4. `inspect_cadence` reports `retired`, with the condition, instead of `stale`, and its stale count
   reaches zero and says so;
5. the ledger's `gate_reason` is derived from the row, and reconciliation replaces the frozen prose;
6. a retired row declares the capabilities it runs, which is the only link through which
   `capability_firing_monitor` reports them held off rather than overdue.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from test_cadence_period import ORCHESTRATE, _aged, _definition

import cadence_registry
import capabilities
import capability_advisor

STEP = "issue-readiness"
ROW = cadence_registry.STEP_BY_KEY[STEP]
# The live ledger's prose before this change, verbatim, so the drain is proven against the real text.
FROZEN_GATE_REASON = (
    "label writes were gated on ORCH_ISSUE_AUTOREADY; ARMED 2026-08-21, so the gate no longer "
    "blocks the delivering path — the assessment and the label write both run"
)
MONTH_S = 30 * 86400


def _retired_rows() -> tuple[dict, ...]:
    return tuple(row for row in cadence_registry.CADENCE_STEPS if row.get("retired"))


def _due_replay(
    state: Path,
    keys: tuple[str, ...],
    env: dict[str, str],
    registry: tuple[dict, ...] | None = None,
) -> tuple[set[str], str]:
    """Ask the tick's OWN helpers whether each step is due; return (due keys, everything printed).

    `_due`, `_step_disabled`, `_step_retired` and `_cadence_due` are read out of orchestrate.sh, each
    asserted unique, and run over the registry's generated shell: the resolution the tick makes.
    """
    script = "\n".join(
        [
            "set -euo pipefail",
            cadence_registry.shell_functions(registry),
            _definition("_due"),
            _definition("_step_disabled"),
            _definition("_step_retired"),
            _definition("_cadence_due"),
            *(f"if _cadence_due {shlex.quote(key)}; then echo DUE:{key}; fi" for key in keys),
        ]
    )
    full_env = {"PATH": os.environ.get("PATH") or os.defpath, "STAMP_DIR": str(state), **env}
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=full_env, timeout=60
    )
    # `if` swallows a failing condition, so a missing function would read as "not due" everywhere.
    assert (
        proc.returncode == 0 and not proc.stderr.strip()
    ), f"the shell replay did not run cleanly (rc={proc.returncode}): {proc.stderr.strip()[:500]}"
    due = {line.split(":", 1)[1] for line in proc.stdout.splitlines() if line.startswith("DUE:")}
    return due, proc.stdout


def test_the_tick_skips_a_retired_step_out_loud_and_touches_no_stamp(tmp_path: Path) -> None:
    state = _aged(tmp_path / "state", {ROW["success_stamp"]}, MONTH_S)
    stamp = state / ROW["success_stamp"]
    before = stamp.stat().st_mtime
    flags = ROW["retired"]["re_enable_env"]

    # Neither flag set, and the tick's own default for the dispatch lane: skipped, and said so.
    for env in ({}, {"ORCH_DISPATCH_LANE": "0"}):
        due, out = _due_replay(state, (STEP,), env)
        assert STEP not in due, f"{STEP} ran with {env or 'no flag'}; it is retired:\n{out}"
        said = [line for line in out.splitlines() if f"[retired] {STEP} skipped" in line]
        assert len(said) == 1, f"the skip must print exactly one [retired] line, got:\n{out}"
        for name, value in flags.items():
            assert f"{name}={value}" in said[0], f"the line must name {name}={value}: {said[0]}"
        assert cadence_registry.retirement_line(ROW) in said[0], said[0]

    # Either flag lifts it, and a lifted step is simply due: no [retired] line at all.
    for name, value in flags.items():
        due, out = _due_replay(state, (STEP,), {name: value})
        assert STEP in due, f"{name}={value} must bring {STEP} back:\n{out}"
        assert "[retired]" not in out, out

    assert stamp.stat().st_mtime == before, "a retirement must defer the step, never stamp it"


def test_the_shell_decision_is_read_from_the_row(tmp_path: Path) -> None:
    """Move the retirement to another step and the shell follows; there is no second copy."""
    other = "durability-sweep"
    moved = {
        "since": "2026-10-02",
        "reason": "fixture",
        "re_enable_env": {"ORCH_FIXTURE_ON": "yes"},
    }
    registry = tuple(
        {**row, "retired": moved if row["key"] == other else None}
        for row in cadence_registry.CADENCE_STEPS
    )
    stamps = {cadence_registry.STEP_BY_KEY[key]["success_stamp"] for key in (STEP, other)}
    state = _aged(tmp_path / "state", stamps, MONTH_S)

    due, out = _due_replay(state, (STEP, other), {}, registry)
    assert STEP in due, f"{STEP} is not retired in this registry, so nothing may skip it:\n{out}"
    assert other not in due and f"[retired] {other} skipped" in out, out
    assert "ORCH_FIXTURE_ON=yes" in out, out

    due, out = _due_replay(state, (STEP, other), {"ORCH_FIXTURE_ON": "yes"}, registry)
    assert due == {STEP, other}, out


def test_no_call_site_restates_a_retirement() -> None:
    """The condition lives on the row. An if/elif chain that tests a re-enable flag before the step's
    `_cadence_due` is the shape that went silent, and it would drift from the row the day either
    one changed."""
    assert _retired_rows(), "no step declares a retirement -- nothing was checked"
    lines = ORCHESTRATE.read_text(encoding="utf-8").splitlines()
    for row in _retired_rows():
        key = row["key"]
        sites = [
            i
            for i, line in enumerate(lines)
            if re.search(rf"_cadence_due {re.escape(key)}\b", line.split("#", 1)[0])
        ]
        assert len(sites) == 1, f"expected one `_cadence_due {key}` site, found {len(sites)}"
        site = sites[0]
        indent = len(lines[site]) - len(lines[site].lstrip())
        chain = [lines[site]]
        cursor = site
        while lines[cursor].lstrip().startswith("elif"):
            cursor -= 1
            while cursor >= 0 and not (
                len(lines[cursor]) - len(lines[cursor].lstrip()) == indent
                and re.match(r"(?:if|elif)\b", lines[cursor].lstrip())
            ):
                cursor -= 1
            assert cursor >= 0, f"the chain holding `_cadence_due {key}` has no opening `if`"
            chain.append(lines[cursor])
        for condition in chain:
            code = condition.split("#", 1)[0]
            restated = [name for name in row["retired"]["re_enable_env"] if name in code]
            assert not restated, (
                f"{key}: the branch `{condition.strip()}` tests {restated} itself. The retirement "
                "is the row's `retired` field and `_cadence_due` reads it; delete the copy."
            )


def test_every_declared_retirement_is_well_formed_and_reaches_the_tick() -> None:
    text = ORCHESTRATE.read_text(encoding="utf-8")
    for row in _retired_rows():
        held = cadence_registry.retirement(row)
        assert held is not None and held["re_enable_env"], row["key"]
        # `_cadence_due` is the one reader, so a retirement on a row that never reaches it is a
        # declaration the tick cannot honour: a stampless step never asks whether it is due.
        assert row.get("success_stamp"), f"{row['key']} is stampless; `_cadence_due` never sees it"
        assert text.count(f"_cadence_due {row['key']} ") == 1, row["key"]

    base = {"key": "fixture", "success_stamp": ".last-fixture"}
    good = {"since": "2026-10-02", "reason": "why", "re_enable_env": {"ORCH_FIXTURE": "1"}}
    assert cadence_registry.retirement({**base, "retired": good}) == good
    assert cadence_registry.retirement(base) is None
    for broken in (
        {k: v for k, v in good.items() if k != "reason"},
        {**good, "since": "yesterday"},
        {**good, "reason": "  "},
        {**good, "re_enable_env": {}},
        {**good, "re_enable_env": {"PATH": "1"}},
        {**good, "re_enable_env": {"ORCH_FIXTURE": "1; rm -rf ~"}},
        {**good, "surprise": True},
    ):
        with pytest.raises(ValueError):
            cadence_registry.retirement({**base, "retired": broken})


def _fresh_state_except(tmp_path: Path, age_s: int | None) -> Path:
    """Every stamped step fresh (1h old); the retired step's stamp `age_s` old, or absent."""
    state = tmp_path / "state"
    stamps = {
        row["success_stamp"]
        for row in cadence_registry.CADENCE_STEPS
        if row.get("success_stamp") and row["key"] != STEP
    }
    _aged(state, stamps, 3600)
    if age_s is not None:
        (state / ROW["success_stamp"]).touch()
        then = time.time() - age_s
        os.utime(state / ROW["success_stamp"], (then, then))
    return state


@pytest.mark.parametrize("age_s", [340 * 3600, None], ids=["aged", "missing"])
def test_the_inspector_reports_retired_with_its_condition_not_stale(
    tmp_path: Path, age_s: int | None
) -> None:
    report = cadence_registry.inspect_cadence(
        _fresh_state_except(tmp_path, age_s), now=int(time.time()), environ={}
    )
    row = next(step for step in report["steps"] if step["key"] == STEP)
    assert row["success_status"] == "retired", row
    assert row["retired"]["re_enable_when"] == "ORCH_DISPATCH_LANE=1 or ORCH_ISSUE_AUTOREADY=1"
    assert row["retired"]["lifted_by"] is None
    assert row["retired"]["line"] == cadence_registry.retirement_line(ROW)
    # THE DRAINED STATE, BY CONSTRUCTION: every live step fresh prints a stale count of ZERO. Asserted
    # as `== 0`, never as falsiness, which is the test shape that forbids reaching zero at all.
    assert report["stale_step_count"] == 0, report["stale_step_count"]
    assert report["retired_step_count"] == 1
    assert report["retired_steps"] == [STEP]
    json.dumps(report)  # the CLI prints it, so it must serialise


def test_a_lifted_retirement_is_judged_like_any_step_and_a_fresh_stamp_outranks_it(
    tmp_path: Path,
) -> None:
    aged = _fresh_state_except(tmp_path / "aged", 340 * 3600)
    lifted = cadence_registry.inspect_cadence(aged, environ={"ORCH_ISSUE_AUTOREADY": "1"})
    row = next(step for step in lifted["steps"] if step["key"] == STEP)
    assert row["success_status"] == "stale", "re-enabled and overdue is late, not retired"
    assert row["retired"]["lifted_by"] == "ORCH_ISSUE_AUTOREADY=1"
    assert (lifted["stale_step_count"], lifted["retired_step_count"]) == (1, 0)

    # The tick's environment may lift a retirement this process cannot see, and a recent run is
    # evidence that it did. So a fresh stamp reads fresh, whatever this environment says.
    fresh = _fresh_state_except(tmp_path / "fresh", 3600)
    report = cadence_registry.inspect_cadence(fresh, environ={})
    row = next(step for step in report["steps"] if step["key"] == STEP)
    assert row["success_status"] == "fresh", row
    assert report["retired_step_count"] == 0


def test_the_ledger_gate_reason_is_derived_from_the_row(tmp_path: Path, monkeypatch) -> None:
    declared = capabilities.KNOWN_DECLARATIONS[STEP]["gate_reason"]
    assert declared == cadence_registry.declared_gate_reason(STEP)
    assert cadence_registry.re_enable_when(ROW) in declared and ROW["gate"] in declared, declared

    # The frozen prose is DRAINED by reconciliation: a row carrying the live ledger's text, with the
    # gated fields validation demands, comes back with the derived text, and a writing load (of this
    # temporary ledger only) persists it with the event that names the field.
    row = {
        **capabilities._blank_capability(STEP),
        "status": "wired",
        "gate_reason": FROZEN_GATE_REASON,
        "gate_evidence": "fixture",
        "evidence_threshold": "fixture",
        "expiry": int(time.time()) + 86400,
        "next_transition": "fixture",
    }
    ledger = tmp_path / "capabilities.json"
    capabilities.save({STEP: row}, ledger)
    assert capabilities.load_declared(ledger)[STEP]["gate_reason"] == declared
    written = capabilities.load(ledger, create=True)[STEP]
    assert written["gate_reason"] == declared and "ARMED" not in written["gate_reason"]
    assert any(
        event.get("type") == "declaration_reconciled"
        and "gate_reason" in (event.get("changed_fields") or [])
        for event in written["event_history"]
    ), written["event_history"]

    # And the derivation follows the row: lift the retirement there and the prose stops saying it.
    monkeypatch.setitem(cadence_registry.STEP_BY_KEY, STEP, {**ROW, "retired": None})
    assert cadence_registry.declared_gate_reason(STEP) == ROW["gate"]


def test_the_advisor_quotes_the_row_too() -> None:
    how = capability_advisor.HOW_TO_USE[STEP]
    assert cadence_registry.retirement_line(ROW) in how, how


def test_every_retired_step_declares_the_capabilities_it_runs() -> None:
    """The firing monitor holds a capability off only through this declared link, so a retirement
    without it would leave the capability alarming as overdue. Explicit on every retired row, an
    empty tuple when the step runs no capability; well-formed on every row that has it."""
    assert _retired_rows(), "no step declares a retirement -- nothing was checked"
    for row in _retired_rows():
        assert cadence_registry.CAPABILITIES_FIELD in row, (
            f"{row['key']} is retired and does not declare `{cadence_registry.CAPABILITIES_FIELD}`"
            ", so the monitor cannot tell which capability it holds off"
        )
    for row in cadence_registry.CADENCE_STEPS:
        cadence_registry.carried_capabilities(row)  # raises on a malformed declaration
    assert cadence_registry.carried_capabilities(ROW) == (STEP,)


def test_a_malformed_capabilities_declaration_raises() -> None:
    base = {"key": "fixture"}
    assert cadence_registry.carried_capabilities(base) == ()
    assert cadence_registry.carried_capabilities({**base, "capabilities": ()}) == ()
    assert cadence_registry.carried_capabilities({**base, "capabilities": ["a", "b"]}) == ("a", "b")
    for broken in ("issue-readiness", ("a", "a"), ("",), (" a",), (1,), {"a": 1}):
        with pytest.raises(ValueError):
            cadence_registry.carried_capabilities({**base, "capabilities": broken})


def test_the_live_retirement_holds_its_capability_until_a_flag_lifts_it() -> None:
    held = cadence_registry.retirement_holds(environ={})
    assert [hold["step"] for hold in held[STEP]] == [STEP], held
    assert held[STEP][0]["line"] == cadence_registry.retirement_line(ROW)
    assert held[STEP][0]["re_enable_when"] == cadence_registry.re_enable_when(ROW)
    for name, value in ROW["retired"]["re_enable_env"].items():
        assert STEP not in cadence_registry.retirement_holds(environ={name: value}), name
    # An empty registry is empty: it must never fall back to the live one.
    assert cadence_registry.retirement_holds(environ={}, registry=()) == {}
