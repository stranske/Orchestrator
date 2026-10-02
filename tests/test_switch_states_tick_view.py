"""A switch is read as the TICK sees it, by every reader, and each row says where its value came from.

THE DEFECT (2026-10-02). `switch_review.switch_states()` read `os.environ`. That is right inside the
tick, where orchestrate.sh has already exported its defaults, and wrong everywhere else, because the
prologue exports some switches ON by default: ORCH_REDIRECT_APPLY_BOOTSTRAP=1. #380 mapped that
switch, the first whose tick value differs from an unset shell, and two readers outside the tick
then read it OFF while every tick had it armed:

  * an interactive `switch_review.py`, which listed it under "Held OFF ... a decision waiting to be
    made" (and `capability_advisor.HOW_TO_USE` tells agents to run exactly that);
  * `capability_propensity.read_switches()`, reached from the `offer-improvements` and `reoffer`
    CLI, so a re-offer echoed `gate_state: off` as a DECLARED FACT.

THE FIX. Each CLI entry resolves the switches the way the tick does
(`switch_review.env_as_the_tick_sees_it`, through `capability_recurrence_check.as_the_tick_sees_it`,
the one resolver that executes the prologue) and passes the result down explicitly. The tick passes
`--env process`, because its environment already IS the tick's, so the tick executes no prologue
and its sweep is unchanged. `switch_states()` itself still never executes one: a library or test
read stays cheap, and labels a switch this process does not set `tick-unconsulted`, never `unset`.

Every test that executes a prologue executes a FIXTURE prologue in real bash, except the one that
holds the review to the real orchestrate.sh, whatever its defaults are.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
from types import SimpleNamespace

import pytest

import capabilities
import capability_propensity as cp
import capability_recurrence_check as rc
import feedback
import paths
import redirect_apply
import switch_review

FLAG = redirect_apply.BOOTSTRAP_FLAG
# The real clock, not a fixed one: the CLI under test dates its review with it, and has no --now.
NOW = int(time.time())
DAY = 86400
# The shapes that matter, in the prologue's own idiom: a switch exported ON by default, one forced
# back to 0 by a CONDITIONAL (a regex of the defaults would read 1), one exported 0, and none for the
# rest. `_gh_gate()` ends the prologue, so the export after it must never be read.
FIXTURE_PROLOGUE = (
    "set -euo pipefail\n"
    f'export {FLAG}="${{{FLAG}:-1}}"\n'
    'export ORCH_RANGE_LANE_ROLLOUT="${ORCH_RANGE_LANE_ROLLOUT:-1}"\n'
    'if [[ "${ORCH_RANGE_LANE_ROLLOUT}" == "1" ]]; then\n'
    "  export ORCH_RANGE_LANE_ROLLOUT=0\n"
    "fi\n"
    'export ORCH_FRONTEND_VERIFY_START_BROWSER="${ORCH_FRONTEND_VERIFY_START_BROWSER:-0}"\n'
    "_gh_gate() { :; }\n"
    "export ORCH_STRATEGY_EXPERIMENT=1\n"
)


def _row(rep: dict, flag: str) -> tuple[str, dict]:
    found = [
        (key, row) for key in ("held_off", "on_but_idle") for row in rep[key] if row["flag"] == flag
    ]
    assert len(found) == 1, (flag, rep["held_off"], rep["on_but_idle"])
    return found[0]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A private ledger and drain, the sweep's machine reads held still, and tripwires that RECORD
    every prologue evaluation, gh call, heartbeat and Brain connection."""
    ledger = tmp_path / "capabilities.json"
    ids = set(switch_review.SWITCH_CAPABILITY.values())
    rows = {c: capabilities._blank_capability(c) for c in sorted(ids)}
    # Idle, as the live row is: an old invocation and nothing since.
    rows[redirect_apply.CAPABILITY_ID]["last_invocation"] = NOW - 30 * DAY
    capabilities.save(rows, ledger)
    monkeypatch.setattr(capabilities, "REG", ledger)
    for flag in switch_review.SWITCH_CAPABILITY:
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    plan = tmp_path / "stage2-plan.json"
    plan.write_text(json.dumps({"generated_at": NOW - 60, "plans": []}), encoding="utf-8")
    monkeypatch.setattr(
        switch_review,
        "_redirect_bootstrap_inputs",
        lambda: {
            "corpus_path": tmp_path / "corpus.jsonl",
            "plan_path": plan,
            "report_dir": tmp_path / "reports",
        },
    )
    reached: dict[str, list] = {"prologue": [], "gh": [], "heartbeat": [], "brain": []}
    real_attempt = rc._tick_env_attempt

    def attempt():
        reached["prologue"].append(str(rc.ORCHESTRATE))
        return real_attempt()

    def gh(args, *, timeout_s=30):
        reached["gh"].append(args)
        return False, "", "unmeasured: this test has no network"

    def brain(*_args, **_kwargs):
        reached["brain"].append("feedback._conn")
        raise AssertionError("a switch read opened the Brain")

    monkeypatch.setattr(rc, "_tick_env_attempt", attempt)
    monkeypatch.setattr(rc, "_TICK_ENV", None)
    monkeypatch.setattr(rc, "_TICK_ENV_DIAG", None)
    monkeypatch.setattr(
        rc, "TICK_ENV_BACKOFF", 0.0
    )  # the sleep is a constant, not logic under test
    monkeypatch.setattr(switch_review, "_GH_CALL_RUNNER", gh)
    monkeypatch.setattr(
        switch_review, "_capability_heartbeat", lambda *a, **k: reached["heartbeat"].append(a)
    )
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_k: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_k: {"status": "ok"})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {"suspect": False})
    monkeypatch.setattr(feedback, "_conn", brain)
    return SimpleNamespace(root=tmp_path, ledger=ledger, reached=reached)


@pytest.fixture
def fixture_prologue(sandbox, monkeypatch):
    script = sandbox.root / "orchestrate.sh"
    script.write_text(FIXTURE_PROLOGUE, encoding="utf-8")
    monkeypatch.setattr(rc, "ORCHESTRATE", script)
    return script


def _cli(argv: list[str]) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert switch_review.main(argv) == 0
    return out.getvalue()


def test_an_interactive_review_reads_a_tick_exported_switch_as_the_tick_does(fixture_prologue):
    """Reader (a): the shell has no ORCH_ switch at all, and the review still reads them as the tick
    does, prologue conditionals included, naming each value's source."""
    rep = json.loads(_cli(["--json"]))
    where, row = _row(rep, FLAG)
    assert where == "on_but_idle", f"{FLAG} read OFF outside the tick: {row}"
    assert (row["value"], row["value_source"]) == ("1", "tick"), row
    assert row["drain"]["drainable"] == 0, row["drain"]
    where, row = _row(rep, "ORCH_RANGE_LANE_ROLLOUT")
    assert (where, row["value"], row["value_source"]) == ("held_off", "0", "tick"), row
    where, row = _row(rep, "ORCH_FRONTEND_VERIFY_START_BROWSER")
    assert (where, row["value"], row["value_source"]) == ("held_off", "0", "tick"), row
    for flag in ("ORCH_RUNTIME_AC_ALLOW_COMMANDS", "ORCH_STRATEGY_EXPERIMENT"):
        where, row = _row(rep, flag)
        assert (where, row["value"], row["value_source"]) == ("held_off", None, "unset"), row
    # ...and the text says so, under the heading that is true of the tick.
    text = _cli([])
    held, _, on = text.partition("## ON but not triggering")
    assert FLAG not in held and FLAG in on, text
    assert "'1' — set by orchestrate.sh's prologue" in on, on


def test_the_review_agrees_with_the_real_tick_prologue_whatever_its_defaults(sandbox):
    """The same reader against the REAL orchestrate.sh, held to whatever it exports today, so the
    test pins agreement with the tick rather than any one default."""
    rep = json.loads(_cli(["--json"]))
    tick = rc.tick_env()
    status = rc.tick_env_status(log=False)
    assert status["outcome"] == "ok", rc.tick_env_failure_message(status)
    # A machine-side blip is retried, so count which script ran, never how many times.
    assert set(sandbox.reached["prologue"]) == {str(paths.orchestrate_sh())}, sandbox.reached
    for flag in sorted(switch_review.SWITCH_CAPABILITY):
        value = tick.get(flag)
        on = bool(value) and value != "0"
        found = [r for k in ("held_off", "on_but_idle") for r in rep[k] if r["flag"] == flag]
        if on:
            # ON as the tick sees it: idle here only if its capability recorded nothing in a week,
            # which only the bootstrap's row in this ledger arranges.
            assert all(r["state"] == "on" for r in found), (flag, value, found)
        else:
            assert [r["state"] for r in found] == ["off"], (flag, value, found)
        for r in found:
            expected = "tick" if value is not None else "unset"
            assert (r["value"], r["value_source"]) == (value, expected), (flag, r)


def test_the_tick_reads_its_own_environment_and_executes_no_prologue(sandbox, monkeypatch):
    """Inside the tick the review runs with `--env process`: the same rows as reading its env, and
    no prologue at all. Before this change no tick ever executed one."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("ORCH_RANGE_LANE_ROLLOUT", "0")
    monkeypatch.setenv("ORCH_FRONTEND_VERIFY_START_BROWSER", "0")
    rep = json.loads(_cli(["--json", "--env", "process"]))
    assert sandbox.reached["prologue"] == [], "the tick's own review executed the prologue"
    where, row = _row(rep, FLAG)
    assert (where, row["value"], row["value_source"]) == ("on_but_idle", "1", "ambient"), row
    where, row = _row(rep, "ORCH_RANGE_LANE_ROLLOUT")
    assert (where, row["value"], row["value_source"]) == ("held_off", "0", "ambient"), row
    # In the tick, a switch it does not set IS unset: its environment is the tick's.
    where, row = _row(rep, "ORCH_RUNTIME_AC_ALLOW_COMMANDS")
    assert (where, row["value"], row["value_source"]) == ("held_off", None, "unset"), row
    # The graded projection is (flag, state) alone, so the new fields mint no tick verdict.
    findings = cp.project_findings("switch-review", rep)
    assert findings is not None and f"flag={FLAG!r}|state='on'" in findings["on_but_idle"]
    assert sandbox.reached["gh"], "positive control: the sweep's gh seam is the injected one"


def test_orchestrate_sh_runs_the_switch_review_in_process_mode():
    """The wiring that keeps the tick unchanged. ONE site, so a second cannot hide its removal."""
    text = paths.orchestrate_sh().read_text(encoding="utf-8")
    needle = "--env " + "process"
    assert text.count(needle) == 1, (
        f"orchestrate.sh names {needle!r} {text.count(needle)} time(s), not once: its switch "
        "review must pass it, or every weekly sweep executes the prologue again from inside the tick"
    )
    line = next(ln for ln in text.splitlines() if needle in ln)
    assert line.strip().startswith("switch_args=("), line


def test_a_library_read_executes_no_prologue_and_never_claims_unset(sandbox, monkeypatch):
    """`switch_states()` with no env reads this process alone. A switch this process does not set is
    `tick-unconsulted`, and its drain is never read, so no live redirect file is either."""
    drains: list = []
    monkeypatch.setitem(
        switch_review.SWITCH_DRAIN, FLAG, lambda env, *, now: drains.append(env) or {}
    )
    rows = switch_review.switch_states(now=NOW)
    assert sandbox.reached["prologue"] == [], "switch_states() executed the prologue"
    where, row = _row(rows, FLAG)
    assert (where, row["value"], row["value_source"]) == ("held_off", None, rc.TICK_UNCONSULTED)
    assert "UNKNOWN" in row["reason"] and "decision waiting" not in row["reason"], row["reason"]
    assert drains == [], "the drain was read for a switch this process merely does not set"
    facts = cp.declared_facts(redirect_apply.CAPABILITY_ID)
    assert facts["gate_value_source"] == rc.TICK_UNCONSULTED, facts
    assert "gate_value" not in facts, facts
    assert sandbox.reached == {"prologue": [], "gh": [], "heartbeat": [], "brain": []}


def test_every_value_source_prints_its_own_words():
    """What each state PRINTS: "unset" and "0" both read OFF and must never print alike, and an
    UNKNOWN tick value must never print as either."""
    cases = {
        ("1", "ambient"),
        ("0", "ambient"),
        ("1", "tick"),
        ("0", "tick"),
        ("0", "explicit"),
        (None, "explicit"),
        (None, "unset"),
        (None, rc.TICK_UNRESOLVED_PREFIX + "timeout"),
        (None, rc.TICK_UNCONSULTED),
    }
    said = {
        case: switch_review.value_phrase({"value": case[0], "value_source": case[1]})
        for case in cases
    }
    assert len(set(said.values())) == len(cases), said
    assert "'0'" in said[("0", "tick")] and "'0'" not in said[(None, "unset")], said
    for (value, source), words in said.items():
        unknown = rc.tick_value_unknown(source)
        assert ("UNKNOWN" in words) is unknown, (source, words)
        if unknown:
            assert "timeout" in words or "nothing asked" in words, words


def test_an_unresolved_tick_is_unknown_and_raises_no_question(sandbox, monkeypatch):
    """A prologue that does not evaluate leaves the switches this process does not set UNKNOWN. Their
    rows say so, and no owner question is asked on them; one this process does set still is."""
    script = sandbox.root / "aborts.sh"
    script.write_text("set -euo pipefail\ncat /no/such/credential\n_gh_gate() { :; }\n")
    monkeypatch.setattr(rc, "ORCHESTRATE", script)
    monkeypatch.setenv("ORCH_RANGE_LANE_ROLLOUT", "0")
    env, sources = switch_review.env_as_the_tick_sees_it()
    assert sources[FLAG] == rc.TICK_UNRESOLVED_PREFIX + "nonzero_exit", sources
    assert sources["ORCH_RANGE_LANE_ROLLOUT"] == "ambient", sources
    rows = switch_review.switch_states(now=NOW, env=env, sources=sources)
    where, row = _row(rows, FLAG)
    assert where == "held_off" and "UNKNOWN" in row["reason"], row
    assert "UNKNOWN" in switch_review.value_phrase(row), row
    out = switch_review.raise_questions(rows, dry_run=True)
    assert FLAG in out["unknown_tick_value"] and FLAG not in out["raised"], out
    assert "ORCH_RANGE_LANE_ROLLOUT" in out["raised"], out
    # DRAINED reads as a list with nothing in it, never as a missing answer.
    resolved = switch_review.switch_states(now=NOW, env={}, sources=None)
    assert switch_review.raise_questions(resolved, dry_run=True)["unknown_tick_value"] == []


def test_the_propensity_cli_echoes_the_gate_as_the_tick_sees_it(fixture_prologue, sandbox):
    """Reader (b): a re-offer from the CLI echoes the bootstrap's real state and its source."""
    exp = "advice:tickview000001"
    cp.record_decline(
        redirect_apply.CAPABILITY_ID,
        exp,
        reason="it looked env-gated off to me",
        surface="fixture-surface",
        kind="gated_off",
        path=sandbox.ledger,
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cp.main(
            [
                "reoffer",
                "--capability",
                redirect_apply.CAPABILITY_ID,
                "--experiment",
                exp,
                "--kind",
                "gated_off",
                "--ledger",
                str(sandbox.ledger),
            ]
        )
    res = json.loads(out.getvalue())
    assert code == 0 and res["reoffered"] is True, res
    gate = {k: v for k, v in res["facts"].items() if k in ("gate_state", "gate_value_source")}
    assert gate == {"gate_state": "on", "gate_value_source": "tick"}, res["facts"]
    assert res["facts"]["gate_value"] == "1", res["facts"]
    assert set(sandbox.reached["prologue"]) == {str(fixture_prologue)}, sandbox.reached


def test_offer_improvements_resolves_from_the_cli_only(sandbox, monkeypatch):
    """The CLI pass asks for the tick's view; the same function called as a library does not."""
    asked: list = []
    real = switch_review.env_as_the_tick_sees_it
    monkeypatch.setattr(switch_review, "env_as_the_tick_sees_it", lambda: asked.append(1) or real())
    monkeypatch.setattr(rc, "_tick_env_attempt", lambda: ({}, {"reason": "timeout"}))
    rep = cp.propose_offer_improvements(path=sandbox.ledger)
    assert rep["measured"] is True and asked == [], asked
    with contextlib.redirect_stdout(io.StringIO()):
        assert cp.main(["offer-improvements"]) == 0
    assert asked == [1], "the offer-improvements CLI did not read the switches as the tick does"
