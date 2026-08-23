#!/usr/bin/env python3
"""capability_recurrence_check.py — if the conditions recurred TODAY, would the capability fire?

THE QUESTION THIS ANSWERS. `capability_activation_audit` asks whether a capability *could* fire in
principle. That is necessary but not sufficient: a trigger can be structurally reachable and still
miss the specific work that actually occurs. So this module replays REAL historical instances —
named issues, PRs, run patterns, and fleet states that already happened — through the CURRENT
machinery, and reports whether each would now route to the capability that exists to handle it.

WHY IT IS A MODULE AND NOT AN ANALYSIS. Two months were lost waiting for evidence from triggers that
could not fire, on the strength of an assurance that the design would work. An assurance is not
checkable. A replay is: every fixture below is a real instance with a real expected capability, so
re-running this after each fix shows movement, and a regression shows up as a fixture that stops
firing.

FIXTURE DISCIPLINE. Each fixture names its source (issue/PR/run evidence) so the claim "this
condition occurred" is auditable. `live=True` fixtures re-read the real issue from GitHub so the
check cannot drift from reality; the rest are frozen snapshots of conditions already verified.
A fixture whose expected capability is None asserts the machinery must NOT fire (no false routing).

    python3 capability_recurrence_check.py            # per-capability verdicts
    python3 capability_recurrence_check.py --json
    python3 capability_recurrence_check.py --offline  # frozen fixtures only, no GitHub
    python3 capability_recurrence_check.py --selftest
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import backlog
import capabilities
import env_prereq

# --------------------------------------------------------------------------- fixtures
# Each entry: the capability under test, the real instance, and how to decide if it would fire.
#
#   kind="classify"   -> does backlog.classify() emit the capability's task_type for this issue?
#   kind="high_stakes"-> does adversarial.high_stakes_reason() fire for this item?
#   kind="predicate"  -> a named callable answers it (used for non-routing machinery)
#
FIXTURES = (
    # ---- task-routed lanes, replayed against the real issues that occurred -------------------
    {
        "capability": "codemod-campaign",
        "kind": "classify",
        "expect_task_type": "codemod",
        "repo": "Trend_Model_Project",
        "issue": 5856,
        "live": True,
        "source": "Legacy removal Phase 6b -> PR #5911 (192 files). Campaign of 6+ issues.",
        "apply_label_repair": True,
    },
    {
        "capability": "testgen-lane",
        "kind": "classify",
        "expect_task_type": "testgen",
        "repo": "trip-planner",
        "issue": 1694,
        "live": True,
        "source": "dispatched as testgen 2026-08; agent:gemini worked and closed it.",
    },
    {
        "capability": "cross-repo-coordination",
        "kind": "classify",
        "expect_task_type": "cross_repo",
        "repo": "Workflows",
        "issue": 2879,
        "live": True,
        "source": "one of the #2879-2883 consumer-sync series.",
    },
    {
        "capability": "epic-decomposition",
        "kind": "classify",
        "expect_task_type": "epic",
        "repo": "Inv-Man-Intake",
        "issue": 845,
        "live": True,
        "apply_label_repair": True,
        "source": "[Epic] Export layer — decomposed BY THE OWNER into 8 child issues by hand.",
    },
    # runtime-ac is a CLOSER GATE, not an opener task_type — my first fixture tested the wrong
    # mechanism. Fixed 2026-08-20 to replay the real shape: a closer PR whose SOURCE issue carries a
    # runtime-AC label must make the gate REQUIRED. (Applying that label automatically is NOT safe
    # and is deliberately not attempted — see the sequencing note in IMPROVEMENT_BACKLOG: the label
    # diverts the opener to spec-authoring instead of implementation, and blocks the closer merge
    # until a spec exists. That is an owner workflow decision, not a wiring fix.)
    {
        "capability": "runtime-ac-checks",
        "kind": "gate_required",
        "lane": "closer",
        "labels": ["agent:codex"],
        "source_labels": ["bug", "runtime-ac", "priority:high"],
        "title": "Codex bootstrap for #5889",
        "source": "Trend_Model_Project#5889 — merged PR #5888 returned CONCERNS; ACs unverified.",
    },
    # docs-drift-fix-agent lives in the Workflows repo and consumes a DRIFT DETECTOR's output, not a
    # labelled issue — `task_type=docs` was always the wrong matcher shape, and its fleet `docs`
    # label is broader than drift (in-app help, tutorials, a GPU backend removal), so routing
    # doc-labelled issues here would be wrong. Activation needed a caller in Workflows CI: a
    # CROSS-REPO PR, not an Orchestrator wiring change. RESOLVED 2026-08-20 — Workflows PR #3138
    # merged maint-87-docs-drift-fix-agent.yml (weekly, report-only by default) and run
    # 32435031188 succeeded. This fixture no longer asserts a frozen miss; it CHECKS the caller
    # (see _external_caller_state), so it tracks reality in both directions.
    {
        "capability": "docs-drift-fix-agent",
        "kind": "predicate_note",
        "source": "Workflows#3134/#3133 + continuous drift; caller landed via Workflows PR #3138.",
    },
    # ---- vocabulary-gated ---------------------------------------------------------------------
    # These replay the REAL closer shape: the PR carries only `agent:*`, and the risk label lives on
    # the SOURCE issue. My first version of these fixtures put the risk label directly on the item
    # and tested the opener lane, which tested a mechanism that does not exist — at opener time
    # there is no PR to review. Verified 2026-08-20: 0 PRs in the fleet carry a `risk:*` label.
    {
        "capability": "adversarial-review",
        "kind": "high_stakes",
        "lane": "closer",
        "labels": ["agent:gemini"],
        "source_labels": ["bug", "type:policy", "risk:major", "validation"],
        "title": "Gemini bootstrap for #1429",
        "source": "Travel-Plan-Permission#1429 (Policy fails open) reaching the closer as a PR.",
    },
    {
        "capability": "adversarial-review",
        "kind": "high_stakes",
        "lane": "closer",
        "labels": ["agent:codex"],
        "source_labels": ["bug", "risk:major", "architecture"],
        "title": "Codex bootstrap for #1436",
        "source": "Travel-Plan-Permission#1436 (non-atomic audit record) reaching the closer.",
    },
    # ---- must NOT fire (guards against false routing) ------------------------------------------
    {
        "capability": None,
        "kind": "classify",
        "expect_task_type": "implement",
        "labels": [],
        "title": "[Epic #845][P1] Export panel UI + Gate 1 + Gate 2",
        "source": "an already-decomposed CHILD subtask must stay implement, not go to the epic lane.",
    },
    {
        "capability": None,
        "kind": "high_stakes",
        "lane": "closer",
        "labels": ["bug", "priority:normal", "risk:low"],
        "title": "ordinary fix",
        "source": "routine work must not spend multiple reviewer seats.",
    },
)


# --------------------------------------------------------------------------- machinery predicates


def _predicate_heartbeat(capability_id: str) -> dict:
    """Would this capability be CREDITED when its code path runs? (audit's reachability check)"""
    try:
        import capabilities as caps_mod
        import capability_activation_audit as audit

        cap = caps_mod.load(caps_mod.REG).get(capability_id) or {}
        hb = audit.heartbeat_reachable(cap)
        return {"fires": hb.get("status") == "reachable", "detail": hb}
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}


ORCHESTRATE = pathlib.Path(__file__).resolve().parent / "orchestrate.sh"
_TICK_ENV: dict[str, str] | None = None
_TICK_ENV_DIAG: dict | None = None

# Bounded retry — never a spin. The prologue evaluates in ~20ms, so the extra tries are paid ONLY
# on a failure, and the ceiling is a constant so a persistent fault still fails fast and loud.
TICK_ENV_ATTEMPTS = 3
TICK_ENV_TIMEOUT = 60
TICK_ENV_BACKOFF = 0.25
# Reasons whose cause is the MACHINE, not the tree: a retry can clear them. Reporting one of these
# as "the prologue does not evaluate" is exactly the confusion this taxonomy exists to prevent.
TICK_ENV_RETRYABLE = frozenset(
    {"spawn_failed", "timeout", "nonzero_exit", "empty_output", "script_unreadable"}
)
# Which bucket each reason lands in, i.e. WHERE TO LOOK when it happens.
#   defect       — the tree is wrong: no script, or it ran clean and exported nothing.
#   script_error — bash ran and the prologue itself aborted; rc + stderr are attached, because a
#                  raced credential read and a real syntax error both land here and only the
#                  evidence separates them.
#   environment  — the subprocess never produced the env at all, which says NOTHING about
#                  orchestrate.sh. This is the class that used to masquerade as `defect`.
TICK_ENV_OUTCOME = {
    "ok": "ok",
    "script_missing": "defect",
    "no_orch_keys": "defect",
    "nonzero_exit": "script_error",
    "bash_missing": "environment",
    "timeout": "environment",
    "spawn_failed": "environment",
    "empty_output": "environment",
    "script_unreadable": "environment",
}


def _classify_tick_env(returncode: int, stdout: str, stderr: str) -> tuple[dict[str, str], dict]:
    """Turn ONE bash run into (resolved flags, a record naming what happened). Pure, so testable.

    The distinctions matter because they mean opposite things. A non-zero exit or an empty stdout is
    the SUBPROCESS failing; a clean run that printed something and exported no ORCH_ flag is the
    PROLOGUE failing. The old code collapsed both into `{}` and left the caller asserting on a key's
    presence, so on 2026-08-21 a subprocess that lost a race under tick load was reported as
    "orchestrate.sh prologue did not evaluate" — a red pointing at the tree for a fault in the
    machine, which passed on the very next run.
    """
    resolved: dict[str, str] = {}
    for ln in (stdout or "").splitlines():
        if ln.startswith("ORCH_") and "=" in ln:
            k, v = ln.split("=", 1)
            resolved[k] = v
    rec: dict = {
        "returncode": returncode,
        "stdout_lines": len((stdout or "").splitlines()),
        "keys": len(resolved),
    }
    tail = [ln for ln in (stderr or "").splitlines() if ln.strip()][-2:]
    if tail:
        rec["stderr_tail"] = tail
    if resolved:
        return resolved, {**rec, "reason": "ok"}
    if returncode != 0:
        # `set -euo pipefail` ends the prologue at the first failing command, before `env` runs.
        return {}, {**rec, "reason": "nonzero_exit"}
    if not (stdout or "").strip():
        # rc 0 and NOTHING printed. A refused fork for `env`/`grep` lands exactly here: the
        # `|| true` swallows the failure, so the status stays 0 and the output is empty.
        return {}, {**rec, "reason": "empty_output"}
    # bash ran, exited clean, printed — and exported no ORCH_ flag. THE REAL DEFECT.
    return {}, {**rec, "reason": "no_orch_keys"}


def _tick_env_attempt() -> tuple[dict[str, str], dict]:
    """One bash evaluation of the prologue, with every failure NAMED instead of swallowed."""
    if not ORCHESTRATE.is_file():
        return {}, {"reason": "script_missing", "detail": str(ORCHESTRATE)}
    try:
        lines = ORCHESTRATE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:  # a cloud-sync mount can fail a read that then succeeds
        return {}, {"reason": "script_unreadable", "detail": f"{type(exc).__name__}: {exc}"[:120]}
    end = next((i for i, ln in enumerate(lines) if ln.startswith("_gh_gate()")), len(lines))
    script = "\n".join(lines[:end]) + "\nenv | grep '^ORCH_' || true\n"
    try:
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=TICK_ENV_TIMEOUT
        )
    except FileNotFoundError:
        return {}, {"reason": "bash_missing", "detail": "no `bash` on PATH"}
    except subprocess.TimeoutExpired:
        return {}, {"reason": "timeout", "detail": f"bash exceeded {TICK_ENV_TIMEOUT}s"}
    except OSError as exc:  # fork/spawn refused: EAGAIN or ENOMEM under tick load
        return {}, {"reason": "spawn_failed", "detail": f"{type(exc).__name__}: {exc}"[:120]}
    return _classify_tick_env(proc.returncode, proc.stdout or "", proc.stderr or "")


def tick_env(refresh: bool = False, *, log: bool = True) -> dict[str, str]:
    """The ORCH_* flag values a REAL TICK would see, resolved by bash from orchestrate.sh itself.

    WHY THIS EXISTS. `_predicate_flag` read os.environ directly, so the headline number depended on
    which shell you happened to invoke from: 30 of 42 from a bare terminal, 35 of 42 inside a tick,
    with nothing in the output saying which one you got. A score that moves with the caller is not a
    scoreboard -- and it is the same invisible-omission failure as reporting "18 of 21" for a subset.

    orchestrate.sh is the authority on tick defaults (`export ORCH_X="${ORCH_X:-1}"`), including
    CONDITIONAL overrides a Python reimplementation would silently get wrong -- notably the
    range-lane trial window, which forces ORCH_RANGE_LANE_ROLLOUT back to 0 once the review date
    passes (so its naive default of 1 is a lie today). So bash evaluates the real prologue and we
    read the result back, rather than re-deriving the logic here and letting the two drift.

    The prologue is verified side-effect-free: `set -euo pipefail`, `unset GITHUB_TOKEN`, exports
    and echoes only. Ambient ORCH_* values still win, exactly as `${X:-default}` means they do in a
    real tick, so an operator override is honoured rather than hidden.

    WHY IT RETRIES AND REPORTS A REASON. Resolving the env means running a subprocess against a
    script on a cloud-sync volume, on a machine that also runs the real tick hourly at :40. That
    subprocess can fail for reasons that have nothing to do with the tree: a refused fork, a
    timeout, a raced credential read. Returning a bare `{}` for all of them made a transient blip
    indistinguishable from a prologue that stopped exporting — and a flaky red in the instrument
    that certifies this tree is worse than a flaky test elsewhere, because it teaches the reader to
    re-run until green. So: machine-side failures get a bounded retry, every outcome carries a
    structured reason (`tick_env_status`), and the reason is logged when resolution degrades. It
    still resolves to `{}` on failure — callers must fall back to ambient — but the failure now says
    WHERE TO LOOK instead of accusing the script.
    """
    global _TICK_ENV, _TICK_ENV_DIAG
    if _TICK_ENV is not None and not refresh:
        return _TICK_ENV
    ceiling = max(1, TICK_ENV_ATTEMPTS)
    attempts: list[dict] = []
    resolved: dict[str, str] = {}
    for i in range(ceiling):
        env, rec = _tick_env_attempt()
        attempts.append({"attempt": i + 1, **rec})
        if rec["reason"] == "ok":
            resolved = env
            break
        if rec["reason"] not in TICK_ENV_RETRYABLE:
            break  # retrying a deterministic fault would only hide it
        if i + 1 < ceiling:
            time.sleep(TICK_ENV_BACKOFF * (i + 1))
    reason = attempts[-1]["reason"]
    _TICK_ENV = resolved
    _TICK_ENV_DIAG = {
        "reason": reason,
        "outcome": TICK_ENV_OUTCOME.get(reason, "environment"),
        "keys": len(resolved),
        "retried": len(attempts) - 1,
        "attempts": attempts,
    }
    if _TICK_ENV_DIAG["outcome"] != "ok" and log:
        # LOG IT. Silence is how one degraded resolution read as a tree defect once and as nothing
        # at all on the re-run. `log=False` exists only for the selftest's own fixtures: an
        # instrument that prints DEFECT lines during its own PASSING run is the same cry-wolf
        # problem in a smaller costume.
        print(f"[tick_env] {tick_env_failure_message(_TICK_ENV_DIAG)}", file=sys.stderr)
    return _TICK_ENV


def tick_env_status(refresh: bool = False, *, log: bool = True) -> dict:
    """WHY the last resolution came out as it did — assert on THIS, not on a key's presence."""
    if _TICK_ENV_DIAG is None or refresh:
        tick_env(refresh=True, log=log)
    return dict(_TICK_ENV_DIAG or {})


def tick_env_failure_message(diag: dict) -> str:
    """One line saying whether to look at the TREE or at the MACHINE, with the evidence."""
    reason = diag.get("reason", "unknown")
    outcome = diag.get("outcome") or TICK_ENV_OUTCOME.get(reason, "environment")
    attempts = diag.get("attempts") or [{}]
    last = attempts[-1]
    tried = f"{len(attempts)} attempt(s)"
    ev = (
        "; ".join(
            part
            for part in (
                f"rc={last['returncode']}" if last.get("returncode") is not None else "",
                (
                    f"{last['stdout_lines']} stdout line(s)"
                    if last.get("stdout_lines") is not None
                    else ""
                ),
                (
                    f"stderr: {' | '.join(last.get('stderr_tail') or [])}"
                    if last.get("stderr_tail")
                    else ""
                ),
                str(last.get("detail") or ""),
            )
            if part
        )
        or "no evidence captured"
    )
    if outcome == "defect":
        if reason == "script_missing":
            return (
                f"DEFECT (tree): orchestrate.sh is not there — {ev}. There is no prologue to "
                "evaluate, so every flag verdict falls back to ambient."
            )
        return (
            "DEFECT (tree): the prologue evaluated CLEANLY and exported no ORCH_ flag "
            f"({ev}) — the exports were renamed or removed, so flag verdicts fall back to "
            "ambient and range-lane would score as FIRING on its naive default of 1."
        )
    if outcome == "script_error":
        return (
            f"PROLOGUE ABORTED after {tried} ({ev}). `set -euo pipefail` ends the prologue at "
            "the first failing command — the stderr above says whether that was a raced "
            "credential read (machine) or a script fault (tree)."
        )
    return (
        f"ENVIRONMENT, not the tree: {reason} after {tried} ({ev}). bash never produced the "
        "prologue's env, so this says NOTHING about orchestrate.sh — the flag rows are "
        "UNRESOLVED, not disproved."
    )


def _external_caller_state(capability_id: str) -> dict:
    """Does the cross-repo caller this capability needs actually exist YET?

    This branch used to be a frozen `fires: False` / "needs a caller outside this repo". That is the
    same latched-state bug as the range-lane flag: a blocked verdict whose CLEAR PATH was never
    checked, so landing the caller would not have moved the number and the fixture would have gone
    on reporting a fixed miss forever. Workflows PR #3138 merged
    maint-87-docs-drift-fix-agent.yml on 2026-08-20; the fixture has to notice that by itself.
    """
    try:
        import capabilities as caps_mod
        import capability_activation_audit as audit

        cap = caps_mod.load(caps_mod.REG).get(capability_id) or {}
        caller = audit.external_caller(cap)
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "external_blocker": True, "detail": {"error": str(exc)[:90]}}
    if not caller:
        return {
            "fires": False,
            "external_blocker": True,
            "detail": {
                "error": f"{capability_id} has no ci_workflow matcher — "
                "predicate_note is the wrong fixture kind for it"
            },
        }
    return {
        "fires": bool(caller.get("exists")),
        "external_blocker": not caller.get("exists"),
        "detail": caller,
    }


def _predicate_flag(flag: str, want: str = "1") -> dict:
    """Evaluate a switch as THE TICK would see it, naming where the value came from."""
    ambient = os.environ.get(flag)
    resolved = tick_env()
    actual = ambient if ambient is not None else resolved.get(flag)
    if ambient is not None:
        source = "ambient"
    elif flag in resolved:
        source = "tick"
    else:
        # An UNRESOLVED tick and a genuinely-unset flag are not the same claim. Name the reason, so
        # a degraded run is visible in the row instead of reading as a real "unset".
        _diag = _TICK_ENV_DIAG or {}
        source = (
            "unset"
            if _diag.get("outcome") in (None, "ok")
            else f"tick-unresolved:{_diag.get('reason')}"
        )
    out = {
        "fires": actual == want,
        "detail": {"flag": flag, "value": actual, "needs": want, "value_source": source},
    }
    criterion = SWITCH_ON_CRITERIA.get(flag)
    if criterion and not out["fires"]:
        out["detail"]["switch_on_when"] = criterion
    return out


# A flag that is OFF is not automatically a defect — but "when should it go on?" must be a
# measurable condition, not a standing judgement call, or it never gets revisited. Each entry names
# the machine-checkable precondition and why the switch is held.
SWITCH_ON_CRITERIA = {
    "ORCH_REDIRECT_APPLY_BOOTSTRAP": "all three hold at once: (a) `redirect_apply.py --status` reports bootstrap_needed=true "
    "(the gate still has a synced_role_outcomes or linked_disagreements deficit); (b) the "
    "keepalive supervisor's stage-2 status is `historical_replay_ready_wait_for_live_links`, "
    "i.e. the non-mutating historical route is EXHAUSTED (124 replays, "
    "ready_for_historical_replay_analysis=true) so applied advice is the only remaining "
    "source; and (c) `redirect_apply.py --apply --dry-run` authorises >=1 candidate, which "
    "requires a dead prior process, no foreign claim, and a lineage-stamped plan. When all "
    "three hold, arming it adds no destructive power the rails do not already exercise on a "
    "dead lane: no kill runs, and the apply reduces to release-claim + delegate. It disarms "
    "itself when bootstrap_needed goes false.",
    "ORCH_RANGE_LANE_ROLLOUT": "the daily range-rollout PREVIEW reports >=1 eligible candidate with a provisionable "
    "worktree on >=3 consecutive days. The 2026-07-08 trial failed on THIN EVIDENCE, not bad "
    "outcomes: 2 dispatches, both transient_infra rc=137, 5 days skipped by a stale worktree. "
    "Routing for codemod/epic/cross_repo only started working 2026-08-20, so eligible work is "
    "new. Read the criterion from range-rollout.json, not from a calendar.",
    "ORCH_FRONTEND_VERIFY_START_BROWSER": "operator policy, not evidence: this lets cron/launchd start a GUI Chrome for the CDP "
    "endpoint. Turn it on only if you want a browser process kept alive on this machine. The "
    "ordering defect that made flipping it pointless is FIXED (2026-08-22): the doctor used to "
    "run above the ORCH_CAPABILITY_HEARTBEATS export, so the switch would have recorded "
    "nothing and read as 'the switch did not help'. See `ORCH-ANCHOR: heartbeat-export` and "
    "`ORCH-ANCHOR: frontend-verify-doctor` in orchestrate.sh — the doctor now runs below the "
    "export, so flipping this WILL produce a frontend-verifier invocation. Enforced by "
    "capability_activation_audit.heartbeat_env_gate, whose `heartbeat_env_suppressed` defect "
    "re-raises the ordering if it regresses. This criterion used to cite two orchestrate.sh "
    "line numbers, 133 and 152, and both had rotted to 171 and 190 by the time anyone read "
    "them — cite anchors, never line numbers.",
    "ORCH_STRATEGY_EXPERIMENT": "no demand yet: all 365 recorded experiments were single-agent-per-arm, so there is no "
    "strategy comparison waiting to run. Switch on when a multi-agent strategy question is "
    "actually being asked (e.g. 'is claude+cursor with synthesis better than claude alone' "
    "appears as real work), not before.",
    "ORCH_EXPLORATION_MODE": "REVIEWED 2026-08-22 — this is a DECISION, not a wait. The previous text said the blocker "
    "was the review rather than the data, which was right; the review was then run. "
    "`exploration_review` returns status=`epsilon_still_preferred`, "
    "recommendation=`keep_epsilon_greedy`: the evidence gates ARE met (ready_tasks 5/9, "
    "zero-cell rate 16.2%, 727 instrumented runs, 65 direct exploration outcomes) and "
    "Thompson-hybrid still does not improve BOTH simulated challenger quality and direct "
    "exploration outcomes. So epsilon-greedy is kept on merit, not for lack of data. The "
    "criterion is no longer prose: `_check_thompson` runs the review and reports its own "
    "recommendation, so the switch re-raises by itself if that flips to "
    "`switch_to_thompson_hybrid`. The review already runs weekly inside periodic_report.",
    "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "CORRECTED 2026-08-22 after running the code. The previous text said this gated BOTH the "
    "template-built deliberate-break command AND agent-authored `command`/`non_regression` "
    "checks, and that the actionable step was to SPLIT the flag. The split already existed: see "
    "`ORCH-ANCHOR: runtime-ac-command-exec-gate` in runtime_ac.py — COMMAND_EXEC_GATED_TYPES is "
    "{command, non_regression} and has never included deliberate_break. Measured by executing a "
    "real deliberate_break spec both ways: identical results with allow_command_checks False "
    "and True. So this flag holds exactly one thing — an agent-authored argv reaching "
    "shlex.split — and there is nothing to un-split. It should stay OFF: a spec's "
    "`command`/`non_regression` string is written by an agent, and the machine gate that "
    "matters (deliberate_break, the sole producer of FAIL_HOLLOW) already runs without it. "
    "Flip it only for a bounded supervised run on a specific spec you have read.",
}


def _check_deliberate_break_executable() -> dict:
    """Would a runtime-AC spec carrying a deliberate_break check actually RUN one?

    Two conditions, both read from the real machinery rather than restated here:
      * `deliberate_break` is exempt from runtime_ac's command-exec gate (ORCH-ANCHOR:
        runtime-ac-command-exec-gate), so no flag has to be flipped for it to execute; and
      * the closer's runtime-AC gate itself is enabled (ORCH_RUN_RUNTIME_AC), which orchestrate.sh
        exports =1 by default.

    When this fires, non-use means NO MATCHING WORK — no closer item carries a spec with a
    deliberate_break check. That is a different problem, with a different fix, from a held switch.
    """
    try:
        import runtime_ac

        exempt = not runtime_ac.command_execution_gated("deliberate_break")
        gated_types = sorted(runtime_ac.COMMAND_EXEC_GATED_TYPES)
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}
    gate = _predicate_flag("ORCH_RUN_RUNTIME_AC")
    return {
        "fires": bool(exempt and gate["fires"]),
        "detail": {
            "deliberate_break_exempt_from_command_gate": exempt,
            "command_exec_gated_types": gated_types,
            "runtime_ac_gate_enabled": gate["fires"],
            "runtime_ac_gate_source": gate["detail"].get("value_source"),
            "non_use_means": (
                "no closer item carries a runtime-AC spec with a "
                "deliberate_break check (no matching work), NOT a held "
                "switch"
            ),
        },
    }


def _check_gemini_isolation() -> dict:
    """A gemini offload must carry --add-dir pointing at the exact worktree."""
    try:
        import adapters

        argv = adapters.build_command("gemini", "probe", "mid", cwd="/tmp/wt-probe")
        ok = "--add-dir" in argv
        target = argv[argv.index("--add-dir") + 1] if ok else None
        return {
            "fires": bool(ok and target),
            "detail": {"add_dir": target, "note": "writes confined to the target worktree"},
        }
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}


def _check_keepalive_escalation() -> dict:
    """A PR carrying an escalation label must be selectable by the supervisor."""
    try:
        import keepalive_supervisor as ks

        labels = set(getattr(ks, "ESCALATION_LABELS", set()))
        ok = bool({"agent:needs-attention", "needs-human"} & labels)
        return {"fires": ok, "detail": {"escalation_labels": sorted(labels)}}
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}


def _check_adjudicator() -> dict:
    """Adjudication needs the shadow switch AND a real disagreement to arbitrate."""
    flag = _predicate_flag("ORCH_ROLE_SHADOW")
    try:
        import feedback

        c = feedback._conn()
        n = c.execute(
            "SELECT COUNT(*) FROM outcomes WHERE verifier_verdict IS NOT NULL "
            "AND adjudicated_verdict IS NOT NULL "
            "AND verifier_verdict<>adjudicated_verdict"
        ).fetchone()[0]
        c.close()
    except Exception:
        n = 0
    return {
        "fires": bool(flag["fires"]) and n > 0,
        "detail": {**flag["detail"], "recorded_disagreements": n},
    }


def _check_synthesis_gate() -> dict:
    """Its gate must be ENCODED and free of unobtainable requires, so evidence can lift it."""
    try:
        import capabilities as caps_mod

        cap = caps_mod.load(caps_mod.REG)["synthesis-promotion"]
        spec = caps_mod.gate_policy(cap)
        return {
            "fires": bool(spec["encoded"]) and not spec["requires"],
            "detail": {"encoded": spec["encoded"], "requires": spec["requires"]},
        }
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}


def _check_model_trial_gate() -> dict:
    """DELIBERATELY unliftable: firing means the quarantine is correctly DECLARED."""
    try:
        import capabilities as caps_mod

        cap = caps_mod.load(caps_mod.REG)["local-model-profile-trial"]
        spec = caps_mod.gate_policy(cap)
        held = "atomic_brain_ingestion" in (spec["requires"] or [])
        return {
            "fires": held,
            "detail": {
                "requires": spec["requires"],
                "note": "quarantine correctly declared; NOT a request to lift it",
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}


def _check_thompson() -> dict:
    """Thompson routing needs its mode selected; epsilon-greedy is the reviewed default.

    The criterion used to be the PROSE string "exploration_review recommends it", which nothing
    evaluated — so the row said "waiting for a review" indefinitely while the review itself ran
    weekly inside `periodic_report` and its answer was read by no one. That is a verdict cached as
    prose outliving its evidence. This now RUNS the review (0.4s, read-only) and reports its actual
    recommendation, so "held" and "reviewed and decided to keep epsilon" stop looking identical.

    A missing Brain/route_weights is a PREREQUISITE, not a defect: the fallback names what is
    missing rather than raising, because a fixture that errors reads exactly like a real miss.
    """
    try:
        import router

        mode = router._exploration_mode()
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}
    detail: dict = {"mode": mode, "needs": "thompson-hybrid"}
    try:
        import exploration_review

        review = exploration_review.build_report()
        detail["review_recommendation"] = review.get("recommendation")
        detail["review_status"] = review.get("status")
        detail["review_reason"] = str(review.get("reason") or "")[:200]
        detail["switch_on_when"] = (
            "exploration_review's own recommendation turns to `switch_to_thompson_hybrid`; it "
            f"currently says {review.get('recommendation')!r} "
            f"({review.get('status')!r}), which is a DECISION, not a wait for data"
        )
    except Exception as exc:  # noqa: BLE001
        # Named prerequisite, never an `error` key: on a machine with no Brain the review cannot
        # run, and reporting that as a blocked capability would be a false miss.
        detail["review_unavailable"] = (
            f"exploration_review could not run here ({type(exc).__name__}); it needs the local "
            "route_weights/outcomes store"
        )
        detail["switch_on_when"] = "exploration_review recommends it (review not runnable here)"
    return {"fires": mode == "thompson-hybrid", "detail": detail}


def _check_reference_sync_gate() -> dict:
    """Can this compiled-workflow rail's PROMOTION GATE ever be satisfied?

    This check used to assert a routing gap -- "its matcher names task_types classify() cannot
    emit" -- and reported that as the blocker. That diagnosis was already FIXED: the matcher was
    corrected on 2026-08-20 to {"kind": "compiled_workflow", "name": "reference_sync_hygiene"} and
    names no task_type at all. The fixture had frozen a stale diagnosis and kept reporting it, the
    same latched-state bug as the docs-drift predicate_note. There is no routing decision here, and
    routing it WOULD be wrong: CLAUDE.md forbids a compiler from dispatching or activating, and the
    ledger's own gate_reason is "compiled workflow remains candidate-only in shadow".

    The REAL blocker is upstream of any switch. `_causal_readiness` needs
    min_independent_durable_reuse (3) distinct durable subjects, drawn from
    `feedback.capability_causal_evidence()`, which joins `influence_edges` on
    (capability_id, capability_version_id). Brain-wide there are 555 edges, and only `role-triage`
    carries capability attribution (326) because
    `capability_outcome_bridge.backfill_role_capability_edges` filters
    `WHERE r.influence_type = 'role'`. Compiled-workflow rails are outside that filter, so this
    capability has ZERO attributed edges and `independent_durable_reuse` / `evidence_age` can never
    flip -- no matter how long anyone waits. Its 367 ledger events live in `event_history`, which
    the readiness join never reads.

    So `fires` here asks the only question that matters for a shadow rail: is there a populated
    evidence path by which its gate could EVER become ready? Waiting is not a plan when the
    evidence source is empty by construction.
    """
    CID = "capability:reference-sync-hygiene-test-gate"
    try:
        import capabilities as caps_mod
        import capability_outcome_bridge as bridge
        import feedback as fb

        cap = caps_mod.load(caps_mod.REG).get(CID) or {}
        vid = str(cap.get("capability_version_id") or "")
        rows = fb.capability_causal_evidence(CID, vid) if vid else []
        readiness = (cap.get("causal_evidence") or {}).get("readiness") or {}
        attributed = len(rows)
        # FIXED 2026-08-21. `fires` asks the question that actually matters for a shadow rail:
        # does an evidence path EXIST by which its gate could ever become ready? Before, the answer
        # was no for structural reasons -- compiled-workflow rails were outside every attribution
        # producer class, so waiting was futile no matter how long it went on. Now the rail is in
        # the bridge's coverage and subject identity flows, so waiting is productive. It is
        # deliberately NOT "the gate is ready": that requires durable deliveries the rail has not
        # produced yet, and claiming otherwise would be the wiring-only victory this file exists to
        # prevent. Read `subjects_seen` / `attributed_edges` for the actual progress.
        covered = CID in (bridge.attribute_compiled_workflow_edges(dry_run=True)["rails"])
        return {
            "fires": covered,
            "detail": {
                "status": cap.get("status"),
                "attributed_edges": attributed,
                "durable_subjects": readiness.get("durable_subjects") or [],
                "needs_distinct_subjects": caps_mod.DEFAULT_PROMOTION_POLICY[
                    "min_independent_durable_reuse"
                ],
                "gate_reason": cap.get("gate_reason"),
                "in_bridge_coverage": covered,
                "blocker": "none structural: attribution now covers compiled-workflow "
                "rails and subject identity (consumer repo) flows through "
                "effect schema v2. Remaining is EVIDENCE, not wiring - the "
                "gate needs 3 distinct consumer repos with durable "
                "deliveries that explicitly name the capability",
                "switch_on_when": "nothing to switch. The gate is evidence-driven and now fed: "
                "watch capability_outcome_bridge's compiled_workflow_edges report "
                "(subjects_seen, then attributed). subjects_seen stays 0 until the "
                "rail records a new shadow result, because the 367 historical "
                "events predate the subject field and are NOT backfilled - "
                "attributing the July pilot's 3 same-day PRs would have faked the "
                "3 independent subjects the gate requires.",
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"fires": False, "detail": {"error": str(exc)[:90]}}


# Non-routing machinery: these capabilities are entered directly or gated, so "would it fire"
# means "is it on the executed path / is its switch on".
PREDICATE_FIXTURES = (
    {
        "capability": "offload",
        "check": lambda: _predicate_heartbeat("offload"),
        "source": "196 runs/week; NameError fixed 2026-08-19.",
    },
    {
        "capability": "repo-playbook",
        "check": lambda: _predicate_heartbeat("repo-playbook"),
        "source": "runs on every dispatch via repo_knowledge.append_context.",
    },
    {
        "capability": "stall-watcher",
        "check": lambda: _predicate_heartbeat("stall-watcher"),
        "source": "78-day opener_cap_pressure latch; 24 of 30 blockers expired-but-blocking.",
    },
    {
        "capability": "redirect-policy",
        "check": lambda: _predicate_heartbeat("redirect-policy"),
        "source": "PAEM#2043: 4 attempts, 1 agent, never switched.",
    },
    {
        "capability": "redirect-plan",
        "check": lambda: _predicate_heartbeat("redirect-plan"),
        "source": "143 role:redirect shadow runs accumulating corpus.",
    },
    {
        "capability": "redirect-apply-bootstrap",
        "check": lambda: _predicate_flag("ORCH_REDIRECT_APPLY_BOOTSTRAP"),
        "source": "apply_plan had 0 callers; the Stage-2 gate needs 5 more synced_role_outcomes and "
        "3 linked_disagreements, and only APPLIED advice can supply either (measured "
        "2026-08-21: 143 proposals, 124 historical replays exhausted, 5 hand-made links).",
    },
    {
        "capability": "research-scheduler",
        "check": lambda: _predicate_heartbeat("research-scheduler"),
        "source": "runs every tick via research_scheduler.build_research_plan.",
    },
    # REPLACED 2026-08-22 — this fixture asserted a blocker that does not exist. It tested
    # `_predicate_flag("ORCH_RUNTIME_AC_ALLOW_COMMANDS")`, on the recorded belief that shell-check
    # execution gated the deliberate-break command too. It does not: `COMMAND_EXEC_GATED_TYPES`
    # (ORCH-ANCHOR: runtime-ac-command-exec-gate) is {command, non_regression}, and a real
    # deliberate_break spec was executed both ways with identical results. So this reported the
    # capability as switch-held for as long as the belief survived — a frozen verdict outliving its
    # evidence, and the one shape a recurrence fixture must never have.
    #
    # The honest predicate is the EXECUTED condition: `deliberate_break` is exempt from the
    # command-exec gate AND the closer's runtime-AC gate is enabled, so a spec carrying one WOULD
    # run. It consumes runtime_ac's own name rather than restating the type set, so a regression that
    # starts gating deliberate_break flips this fixture instead of being invisible. Non-use is then
    # correctly classified as NO MATCHING WORK — no closer item carries a runtime-AC spec with a
    # deliberate_break check — which has the opposite fix from a held switch.
    {
        "capability": "deliberate-break-verifier",
        "check": lambda: _check_deliberate_break_executable(),
        "source": "9 FAIL_HOLLOW verdicts; the sole producer of that verdict, and NOT switch-held.",
    },
    {
        "capability": "feature-reflection-cli",
        "check": lambda: _predicate_heartbeat("feature-reflection-cli"),
        "source": "4 new reusable structures created 2026-08-19, none logged.",
    },
    {
        "capability": "range-lane-rollout",
        "check": lambda: _predicate_flag("ORCH_RANGE_LANE_ROLLOUT"),
        "source": "the mechanism behind every specialized-lane dispatch ever made.",
    },
    # The condition this replays actually happened, on 2026-08-22, and is why the lane exists in
    # shadow: `unblock()` marked range-lane-rollout and synthesis-promotion feedable while BOTH were
    # held by a documented default-off switch. Feeding either would have manufactured work they
    # cannot execute, so the durable reuse their gate needs could never be produced and the same two
    # would be fed every cycle forever. Testing the flag tests the real condition -- the lane is held
    # by a safety switch, not by a defect, and `feedable 0` behind it is the honest state.
    {
        "capability": "evidence-acquisition",
        "check": lambda: _predicate_flag("ORCH_EVIDENCE_ACQUISITION"),
        "source": "2026-08-22: unblock() called 2 default-off capabilities feedable; the feed guard "
        "closed that and the feedable set went to 0 of 42. Lane is shadow until a switch flips.",
    },
    {
        "capability": "adversarial-review-flag",
        "check": lambda: _predicate_flag("ORCH_RUN_ADVERSARIAL_REVIEW"),
        "source": "orchestrate.sh exports this; checked here as the tick would see it.",
    },
    {
        "capability": "runtime-ac-flag",
        "check": lambda: _predicate_flag("ORCH_RUN_RUNTIME_AC"),
        "source": "orchestrate.sh exports this; checked here as the tick would see it.",
    },
    # ---- the 19 previously-uncovered capabilities ------------------------------------------------
    # Added 2026-08-20 under the test_capability_set_coverage gate, which FAILS while any ledger
    # capability lacks a fixture. Each names the real historical condition it replays.
    {
        "capability": "abcd-experiment",
        "check": lambda: _predicate_heartbeat("abcd-experiment"),
        "source": "365 distinct experiments ran; implement routing is spread unevenly across 7 agents "
        "(codex 1248 / gemini 129) — the selection bias it exists to correct.",
    },
    {
        "capability": "agy-runtime-isolation",
        "check": _check_gemini_isolation,
        "source": "every gemini offload (196/wk). Without --add-dir, agy trusts the wrong cwd and "
        "writes land outside the target worktree.",
    },
    {
        "capability": "capability-activation-audit",
        "check": lambda: _predicate_heartbeat("capability-activation-audit"),
        "source": "daily cadence step; it flagged ITSELF no_heartbeat on its first run.",
    },
    {
        "capability": "partitioned-review",
        "check": lambda: _predicate_heartbeat("partitioned-review"),
        "source": "the recurring shape behind it: a corpus too large for one prompt, where a "
        "timeout or a truncated answer is indistinguishable from a successful review. "
        "Registered 2026-08-22 — it merged in PR #2 with no ledger record, no heartbeat and "
        "a CLI-only caller, so the firing monitor could only ever have reported it as "
        "never-fired no matter how often it ran.",
    },
    {
        "capability": "capability-propensity",
        "check": lambda: _predicate_heartbeat("capability-propensity"),
        "source": "every capability_advisor.advise call ranks its candidates by propensity, plus the "
        "CLI. Registered 2026-08-22 to close the loop the advisor left open: it recorded a "
        "`match` for each candidate and nothing recorded whether the candidate was then "
        "TRIGGERED or whether triggering HELPED, so 'recommend the useful ones more often' "
        "had no signal. First live read: 13 natural experiments already in the ledger, 0 "
        "resolved, 0 of 41 capabilities carrying usefulness evidence — the propensities are "
        "all prior, and the report says so rather than looking informative.",
    },
    {
        "capability": "capability-firing-monitor",
        "check": lambda: _predicate_heartbeat("capability-firing-monitor"),
        "source": "weekly cadence step. Its first live run flagged range-lane-rollout silent 29.9d "
        "against its own 'daily preview' cadence, and separated 17 never-fired-in-a-tick "
        "capabilities from those whose caller is the suite, where production_heartbeat is a "
        "no-op and the firing record says nothing either way.",
    },
    {
        "capability": "capability-admission-gate",
        "check": lambda: _predicate_heartbeat("capability-admission-gate"),
        "source": "every suite run. Its first live report found 26 of 37 capabilities short of the "
        "required parts, a dangling citation in orchestrate.sh:95 to a decision record "
        "nobody wrote, and two expired trial windows with no record — one of which "
        "(consumer-sync ingest) turned out to be failing every run with 0 accepted "
        "artifacts.",
    },
    {
        "capability": "capability:reference-sync-hygiene-test-gate",
        "check": _check_reference_sync_gate,
        "source": "20 consumer-sync-drift issues (Workflows#2753/#2750/#2878/#2210) — real demand. "
        "Matcher fixed 2026-08-20; the live blocker is zero capability-attributed "
        "influence_edges, so its promotion gate is unsatisfiable by construction.",
    },
    {
        "capability": "completion-event-lineage",
        "check": lambda: _predicate_heartbeat("completion-event-lineage"),
        "source": "every record_run; 2.25 inv/wk, continuous.",
    },
    {
        "capability": "feedback-store",
        "check": lambda: _predicate_heartbeat("feedback-store"),
        "source": "every recorded run — the Brain itself.",
    },
    {
        "capability": "frontend-verifier",
        "check": lambda: _predicate_flag("ORCH_FRONTEND_VERIFY_START_BROWSER"),
        "source": "2 real UX-evaluation sessions in the transcripts; the browser keepalive is off by "
        "operator policy, so this is a held switch, not a defect.",
    },
    {
        "capability": "issue-readiness",
        "check": lambda: _predicate_heartbeat("issue-readiness"),
        "source": "94 open issues against a backlog of 1 — the condition it was built for.",
    },
    {
        "capability": "live-keepalive-supervisor",
        "check": _check_keepalive_escalation,
        "source": "5 real agent:needs-attention items (Workflows#3123/#1023/#869/#848, MD#239) — that "
        "label is precisely its input.",
    },
    {
        "capability": "local-model-profile-trial",
        "check": _check_model_trial_gate,
        "source": "gpt-5.6-sol (15 runs) and terra (14) are in live use, so the identity question is "
        "real; promotion stays quarantined until Brain ingestion is atomic.",
    },
    {
        "capability": "role-adjudicator",
        "check": _check_adjudicator,
        "source": "13 outcomes where verifier_verdict != adjudicated_verdict, incl. 9 FAIL_HOLLOW.",
    },
    {
        "capability": "role-decomposer",
        "check": lambda: _predicate_flag("ORCH_ROLE_SHADOW"),
        "source": "0 instances in any corpus; redundant with epic-decomposition until epic routing "
        "works. Shadow-gated, so the flag is the honest condition.",
    },
    {
        "capability": "role-prompt",
        "check": lambda: _predicate_heartbeat("role-prompt"),
        "source": "1 run ever; its circumstance occurs on every dispatch but is served "
        "deterministically by PROMPT_TEMPLATES + repo_knowledge.",
    },
    {
        "capability": "role-redirect",
        "check": lambda: _predicate_flag("ORCH_REDIRECT_SWEEP_RECORD_CORPUS"),
        "source": "143 role:redirect shadow runs; PAEM#2043 had 4 attempts with 1 agent, never "
        "switched.",
    },
    {
        "capability": "role-triage",
        "check": lambda: _predicate_heartbeat("role-triage"),
        "source": "688 role:triage runs, ~98/wk — the most-used role.",
    },
    {
        "capability": "strategy-experiments",
        "check": lambda: _predicate_flag("ORCH_STRATEGY_EXPERIMENT"),
        "source": "0 instances: all 365 experiments were single-agent-per-arm. The question AFTER "
        "abcd, not a current gap.",
    },
    {
        "capability": "synthesis-promotion",
        "check": _check_synthesis_gate,
        "source": "6 synthesize runs and 2 FAIL_SYNTHESIS_PROMOTION verdicts — two real promotion "
        "attempts that failed validation.",
    },
    {
        "capability": "thompson-hybrid-routing",
        "check": _check_thompson,
        "source": "5,371 implement runs across 7 agents — enough evidence for sampling to differ "
        "materially from epsilon-greedy.",
    },
    {
        "capability": "windowed-capacity-policy",
        "check": lambda: _predicate_heartbeat("windowed-capacity-policy"),
        "source": "every tick — it gates whether dispatch happens at all.",
    },
    {
        "capability": "switch-review",
        "check": lambda: _predicate_heartbeat("switch-review"),
        "source": "ORCH_RANGE_LANE_ROLLOUT was enabled 2026-07-08, reviewed 07-15, extended to 07-22, "
        "then silently ended up off with no recorded decision — the exact deferral this "
        "re-raises.",
    },
    {
        "capability": "feature-scan",
        "check": lambda: _predicate_heartbeat("feature-scan"),
        "source": "60 of 74 reusable modules were unlogged, including four created the day before.",
    },
)


# --------------------------------------------------------------------------- replay


def _fetch_issue(repo: str, number: int) -> dict | None:
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            f"stranske/{repo}",
            "--json",
            "number,title,labels,body",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _labels_of(fixture: dict, *, offline: bool) -> tuple[list[str], str, str]:
    """(labels, title, provenance) for a fixture, live-read when it names a real issue."""
    if fixture.get("live") and not offline:
        got = _fetch_issue(fixture["repo"], fixture["issue"])
        if got:
            return (
                [l["name"] for l in got.get("labels", [])],
                got.get("title") or "",
                f"live {fixture['repo']}#{fixture['issue']}",
            )
    return (
        list(fixture.get("labels") or []),
        fixture.get("title") or "",
        (
            "frozen fixture"
            if not fixture.get("live")
            else f"frozen (live read failed) {fixture.get('repo')}#{fixture.get('issue')}"
        ),
    )


def replay(*, offline: bool = False) -> dict:
    rows = []
    for fixture in FIXTURES:
        labels, title, prov = _labels_of(fixture, offline=offline)
        row = {
            "capability": fixture["capability"],
            "kind": fixture["kind"],
            "source": fixture["source"],
            "provenance": prov,
            "labels": labels,
            "title": title[:80],
        }

        if fixture["kind"] == "predicate_note":
            row.update(_external_caller_state(fixture["capability"]))

        elif fixture["kind"] == "classify":
            effective = list(labels)
            repaired = None
            if fixture.get("apply_label_repair"):
                try:
                    import issue_readiness

                    repaired = issue_readiness.task_label_for(
                        {"labels": [{"name": l} for l in labels], "title": title}
                    )
                except Exception:
                    repaired = None
                if repaired:
                    effective = labels + [repaired]
            got = backlog.classify(effective)
            row.update(
                {
                    "expected": fixture["expect_task_type"],
                    "actual": got,
                    "label_repair_applied": repaired,
                    "fires": got == fixture["expect_task_type"],
                }
            )

        elif fixture["kind"] == "gate_required":
            try:
                from pathlib import Path as _P

                import runtime_ac_gate

                item = {
                    "target": "o/r#1",
                    "lane": fixture.get("lane", "closer"),
                    "labels": labels,
                    "source_labels": list(fixture.get("source_labels") or []),
                }
                # No spec file on disk, so this tests LABEL-driven eligibility only.
                elig = runtime_ac_gate.eligibility(item, _P("/nonexistent/spec.json"))
                row.update({"eligibility": elig, "fires": bool(elig.get("required"))})
            except Exception as exc:  # noqa: BLE001
                row.update({"fires": False, "eligibility": {"error": str(exc)[:80]}})

        elif fixture["kind"] == "high_stakes":
            try:
                import adversarial

                item = {
                    "target": "o/r#1",
                    "labels": labels,
                    "title": title,
                    "source_labels": list(fixture.get("source_labels") or []),
                    "lane": fixture.get("lane", "closer"),
                }
                reason = adversarial.high_stakes_reason(item)
            except Exception as exc:  # noqa: BLE001
                reason = f"ERROR {exc}"
            row.update(
                {
                    "lane": fixture.get("lane"),
                    "reason": reason,
                    "fires": bool(reason) and not str(reason).startswith("ERROR"),
                }
            )
            if fixture["capability"] is None:
                row["fires"] = not row["fires"]  # this fixture asserts it must NOT fire
                row["expected"] = "must not fire"
        rows.append(row)

    for fixture in PREDICATE_FIXTURES:
        try:
            got = fixture["check"]()
        except Exception as exc:  # noqa: BLE001
            got = {"fires": False, "detail": {"error": str(exc)[:80]}}
        rows.append(
            {
                "capability": fixture["capability"],
                "kind": "predicate",
                "source": fixture["source"],
                "fires": bool(got.get("fires")),
                "detail": got.get("detail"),
            }
        )

    firing = [r for r in rows if r["fires"]]
    return {
        "total": len(rows),
        "would_fire": len(firing),
        "would_miss": len(rows) - len(firing),
        "rows": rows,
    }


def format_report(rep: dict) -> str:
    lines = [
        "# Recurrence check — if the conditions happened again today, would it fire?",
        "",
        f"  WOULD FIRE:  {rep['would_fire']:>2} of {rep['total']}",
        f"  WOULD MISS:  {rep['would_miss']:>2}",
        "",
    ]
    miss = [r for r in rep["rows"] if not r["fires"]]
    if miss:
        lines += ["## Would still MISS — the condition recurs and nothing routes it", ""]
        for r in miss:
            name = r["capability"] or "(must-not-fire guard)"
            lines.append(f"  {name}  [{r['kind']}]")
            lines.append(f"     instance: {r['source']}")
            if r["kind"] == "classify":
                lines.append(
                    f"     expected task_type={r.get('expected')} "
                    f"but classify() gives '{r.get('actual')}'  labels={r['labels']}"
                )
            elif r["kind"] == "high_stakes":
                lines.append(
                    f"     lane={r.get('lane')} labels={r['labels']} -> "
                    f"reason={r.get('reason')}"
                )
            else:
                lines.append(f"     detail: {json.dumps(r.get('detail'))[:150]}")
            lines.append("")
    lines += ["## Would fire", ""]
    for r in rep["rows"]:
        if not r["fires"]:
            continue
        name = r["capability"] or "(must-not-fire guard)"
        extra = ""
        if r["kind"] == "classify":
            extra = f"-> {r.get('actual')}"
            if r.get("label_repair_applied"):
                extra += f" (via auto-label '{r['label_repair_applied']}')"
        elif r["kind"] == "high_stakes":
            extra = f"-> {r.get('reason')}"
        lines.append(f"  {name:<30} {extra}")
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    # `docs-drift-fix-agent`'s fixture reads its matcher out of the LIVE ledger, so on a machine
    # where that row was never registered the fixture errors — and an errored fixture is
    # indistinguishable from a broken one, which is what the loop below exists to catch. Gate it
    # by NAME and keep the guard live for every other fixture: excusing one row is bounded, and
    # the reason says which. Two places need it, so it is computed once.
    gaps: list[str] = []
    _docs_drift_ok = env_prereq.runnable(
        gaps, env_prereq.ledger_rows_absent("docs-drift-fix-agent")
    )

    # The must-not-fire guards are the ones that keep this honest: a check that only ever says
    # "yes" would pass trivially and tell us nothing.
    rep = replay(offline=True)
    assert rep["total"] == len(FIXTURES) + len(PREDICATE_FIXTURES), rep["total"]
    guards = [r for r in rep["rows"] if r["capability"] is None]
    assert len(guards) == 2, guards
    # An epic CHILD must classify as implement, not epic.
    child = [r for r in guards if r["kind"] == "classify"][0]
    assert child["actual"] == "implement", child
    assert child["fires"], "the child-subtask guard should pass (it correctly stays implement)"
    # Routine low-risk work must not trip the adversarial panel.
    routine = [r for r in guards if r["kind"] == "high_stakes"][0]
    assert routine["fires"], "routine work incorrectly triggers adversarial review"

    # A held flag must carry a MACHINE-CHECKABLE switch-on criterion, so "when do we turn it on?"
    # is never left as a standing judgement call that quietly never gets revisited.
    for row in rep["rows"]:
        det = row.get("detail") or {}
        if isinstance(det, dict) and det.get("flag") in SWITCH_ON_CRITERIA and not row["fires"]:
            assert det.get("switch_on_when"), f"{det['flag']} is held with no stated criterion"

    # ...AND THAT CRITERION MAY NOT POINT AT A LINE NUMBER. Line citations rot silently: this table
    # told two readers to look at `orchestrate.sh:133` and `:152` for months after both had moved to
    # 171/190, and `runtime_ac.py:955` was already off by one. A stored pointer that no longer
    # resolves is the same defect class as a cached blocker description that outlives the blocker.
    # Cite a grep-able `ORCH-ANCHOR: <name>` instead, and prove the anchor exists.
    _line_citation = re.compile(r"\b[A-Za-z0-9_]+\.(?:py|sh|json|md):\d+")
    _anchor = re.compile(r"ORCH-ANCHOR: ([a-z0-9-]+)")
    _tree_text = "\n".join(
        p.read_text(errors="ignore")
        for p in sorted(pathlib.Path(__file__).resolve().parent.glob("*.py"))
    ) + "\n".join(
        p.read_text(errors="ignore")
        for p in sorted(pathlib.Path(__file__).resolve().parent.glob("*.sh"))
    )
    _anchors_cited = 0
    for flag, criterion in SWITCH_ON_CRITERIA.items():
        bad = _line_citation.findall(criterion)
        # A criterion may RECORD that it used to carry a rotted citation; what it may not do is
        # tell the reader to go there. The distinction is the word "until"/"cited" in the same
        # clause, so keep the rule blunt instead: no live citation, and the historical note spells
        # the numbers without a file prefix.
        assert not bad, (
            f"{flag}'s criterion cites {bad} by line number; line citations rot — "
            f"cite an ORCH-ANCHOR instead"
        )
        for name in _anchor.findall(criterion):
            _anchors_cited += 1
            # THE ANCHOR MUST BE A DEFINING COMMENT LINE, not merely the substring appearing
            # somewhere. A plain `in _tree_text` test passed a citation to a nonexistent anchor,
            # because this table's own source is part of the tree — the check satisfied itself,
            # which is the circular-measurement mode (FM7) in three lines of code. Requiring
            # `^# ORCH-ANCHOR: <name>` also excludes a "See ORCH-ANCHOR: x" back-reference from
            # standing in for the definition.
            assert re.search(
                rf"^\s*#\s*ORCH-ANCHOR: {re.escape(name)}\b", _tree_text, re.MULTILINE
            ), (
                f"{flag}'s criterion cites `ORCH-ANCHOR: {name}`, but no file defines that anchor "
                f"on its own comment line"
            )
    assert _anchors_cited >= 3, (
        f"only {_anchors_cited} anchor citations found; the anchor rule is not being exercised, so "
        "this assertion would pass on a table full of prose"
    )

    # NO FIXTURE MAY ERROR. A check that raises reports `fires=False` with an {"error": ...}
    # detail, which reads like a blocked capability but is actually a broken test — exactly how a
    # guessed function name (router.resolve_exploration_mode, which does not exist) masqueraded as
    # a real miss. Every fixture must exercise real machinery.
    for row in rep["rows"]:
        det = row.get("detail")
        if not _docs_drift_ok and row["capability"] == "docs-drift-fix-agent":
            continue  # the ONE excused row, named in `gaps` above
        if isinstance(det, dict):
            assert "error" not in det, f"fixture for {row['capability']} ERRORED: {det['error']}"

    # An EXTERNAL blocker must TRACK the caller, not LATCH. This replaced a frozen
    # `assert not docs["fires"]`, which would have gone on passing after the caller landed — the
    # very latched-state bug the fixture exists to catch (a blocked verdict whose clear path is
    # never re-checked). So test the mechanism in BOTH directions; a regression to a hardcoded
    # verdict fails here rather than quietly under-reporting forever.
    if _docs_drift_ok:
        import capability_activation_audit as _audit

        _stub = {"repo": "R", "workflow": "w", "path": "/p"}
        _real = _audit.external_caller
        try:
            _audit.external_caller = lambda cap: {**_stub, "exists": False}
            absent = _external_caller_state("docs-drift-fix-agent")
            _audit.external_caller = lambda cap: {**_stub, "exists": True}
            present = _external_caller_state("docs-drift-fix-agent")
        finally:
            _audit.external_caller = _real
        assert not absent["fires"] and absent["external_blocker"] is True, absent
        assert present["fires"] and present["external_blocker"] is False, present
        # ...and the LIVE row must agree with the caller actually on disk, in whichever direction.
        docs = [r for r in rep["rows"] if r["capability"] == "docs-drift-fix-agent"][0]
        _cap = capabilities.load_declared(capabilities.REG)["docs-drift-fix-agent"]
        _live = _audit.external_caller(_cap)
        assert docs["fires"] is bool(_live and _live.get("exists")), (docs, _live)

    # tick_env must EXECUTE orchestrate.sh's conditionals, not regex-scrape its defaults. The
    # range-lane flag is the proof case: its naive default is 1, but past the trial-review date the
    # script forces it to 0 — a regex parser would report 1 and wrongly score range-lane as firing.
    # ASSERT ON THE STRUCTURED REASON, NOT ON A BARE KEY'S PRESENCE. On 2026-08-21 the old form of
    # this assertion went red ONCE inside a verify.py run from the exec mirror and passed on every
    # re-run: the bash subprocess failed for a machine-side reason, `tick_env` returned `{}`, and
    # `{}` was indistinguishable from a prologue that had stopped exporting. A flaky red HERE is
    # worse than a flaky test elsewhere — verify.py is this tree's only honest verdict, and a
    # spurious red teaches the reader to re-run until green, which is how a real red gets ignored.
    # The check is NOT softened into a skip (a regex-scraped default would score range-lane as
    # FIRING); it still fails in every direction. What changed is that the message now names WHERE
    # TO LOOK, and machine-side failures get a bounded retry first.
    tenv = tick_env(refresh=True)
    tdiag = tick_env_status()
    assert tdiag["outcome"] == "ok", tick_env_failure_message(tdiag)
    assert "ORCH_RUN_ADVERSARIAL_REVIEW" in tenv, (
        f"the prologue DID evaluate ({tdiag['keys']} ORCH_ flags resolved in "
        f"{tdiag['retried'] + 1} attempt(s)) but ORCH_RUN_ADVERSARIAL_REVIEW was not among them — "
        "that export was renamed or removed, so its flag verdict falls back to ambient"
    )
    _m = re.search(
        r"ORCH_RANGE_LANE_TRIAL_UNTIL:-([0-9-]+)", ORCHESTRATE.read_text(encoding="utf-8")
    )
    if (
        _m
        and datetime.date.today().isoformat() > _m.group(1)
        and not os.environ.get("ORCH_RANGE_LANE_ROLLOUT")
    ):
        assert (
            tenv.get("ORCH_RANGE_LANE_ROLLOUT") == "0"
        ), "trial window elapsed but tick_env reports the naive default — conditional not evaluated"

    # THE FAILURE-REASON TAXONOMY ITSELF. Each shape below really happens on this machine, and the
    # point is that they must NOT collapse into one verdict. `_classify_tick_env` is pure, so these
    # are exact rather than load-dependent.
    for _rc, _out, _err, _reason, _outcome in (
        (0, "ORCH_A=1\nORCH_B=0\n", "", "ok", "ok"),
        # rc 0 with nothing printed: a refused fork for `env`/`grep`, swallowed by `|| true`.
        (0, "", "", "empty_output", "environment"),
        (0, "   \n", "", "empty_output", "environment"),
        # printed, exited clean, exported no ORCH_ flag: the prologue itself. THIS one is the tree.
        (0, "hello\n", "", "no_orch_keys", "defect"),
        # `set -euo pipefail` aborted before `env` ran; the attached evidence says which kind.
        (1, "", "bash: line 30: /x/creds: No such file\n", "nonzero_exit", "script_error"),
    ):
        _env, _rec = _classify_tick_env(_rc, _out, _err)
        assert _rec["reason"] == _reason, (_rc, _out, _rec)
        assert TICK_ENV_OUTCOME[_rec["reason"]] == _outcome, _rec
        assert bool(_env) is (_reason == "ok"), _rec
    # A machine-side reason must never be REPORTABLE as the tree's fault, and vice versa.
    _env_msg = tick_env_failure_message(
        {
            "reason": "timeout",
            "outcome": "environment",
            "attempts": [{"detail": "bash exceeded 60s"}] * 3,
        }
    )
    _tree_msg = tick_env_failure_message(
        {
            "reason": "no_orch_keys",
            "outcome": "defect",
            "attempts": [{"returncode": 0, "stdout_lines": 4}],
        }
    )
    assert _env_msg.startswith("ENVIRONMENT") and "3 attempt" in _env_msg, _env_msg
    assert "orchestrate.sh" in _env_msg and "NOTHING" in _env_msg, _env_msg
    assert _tree_msg.startswith("DEFECT") and "range-lane" in _tree_msg, _tree_msg

    # BOUNDED RETRY, AND ONLY FOR THE RIGHT REASONS. A machine-side blip must be retried (so it
    # cannot masquerade as a defect), a tree-side one must NOT be (retrying only hides a
    # deterministic fault), and neither may spin: the ceiling is TICK_ENV_ATTEMPTS.
    _real_attempt = globals()["_tick_env_attempt"]
    _saved = (_TICK_ENV, _TICK_ENV_DIAG, TICK_ENV_BACKOFF)
    try:
        globals()["TICK_ENV_BACKOFF"] = 0.0  # the sleep is a constant, not logic under test
        _calls: list[int] = []

        def _flaky() -> tuple[dict, dict]:
            _calls.append(1)
            if len(_calls) < 3:
                return {}, {"reason": "spawn_failed", "detail": "BlockingIOError: [Errno 35]"}
            return {"ORCH_RUN_ADVERSARIAL_REVIEW": "1"}, {"reason": "ok", "keys": 1}

        globals()["_tick_env_attempt"] = _flaky
        assert tick_env(refresh=True, log=False) == {"ORCH_RUN_ADVERSARIAL_REVIEW": "1"}, _calls
        _d = tick_env_status()
        assert _d["outcome"] == "ok" and _d["retried"] == 2, _d
        # If it never clears, the verdict is ENVIRONMENT — not "the prologue does not evaluate".
        _calls.clear()

        def _always_timeout() -> tuple[dict, dict]:
            _calls.append(1)
            return {}, {"reason": "timeout", "detail": "bash exceeded 60s"}

        globals()["_tick_env_attempt"] = _always_timeout
        # THE REASON MUST BE LOGGED, not just returned — a degraded resolution that says nothing is
        # how the flag rows quietly fell back to ambient with no trace of why.
        _log = io.StringIO()
        with contextlib.redirect_stderr(_log):
            assert tick_env(refresh=True) == {}
        assert "[tick_env]" in _log.getvalue() and "timeout" in _log.getvalue(), _log.getvalue()
        _d = tick_env_status(log=False)
        assert _d["outcome"] == "environment" and len(_calls) == TICK_ENV_ATTEMPTS, (_d, _calls)
        assert "ENVIRONMENT" in tick_env_failure_message(_d), _d
        # ...and with the tick unresolved, a flag row must SAY so rather than claim "unset".
        _row = _predicate_flag("ORCH_DEFINITELY_NOT_SET_ANYWHERE")
        assert _row["detail"]["value_source"] == "tick-unresolved:timeout", _row
        # A DEFECT must not be retried at all: one attempt, and it reads as the tree's fault.
        _calls.clear()

        def _no_exports() -> tuple[dict, dict]:
            _calls.append(1)
            return {}, {"reason": "no_orch_keys", "returncode": 0, "stdout_lines": 3, "keys": 0}

        globals()["_tick_env_attempt"] = _no_exports
        assert tick_env(refresh=True, log=False) == {}
        _d = tick_env_status(log=False)
        assert _d["outcome"] == "defect" and len(_calls) == 1, (_d, _calls)
        assert "DEFECT" in tick_env_failure_message(_d), _d
    finally:
        globals()["_tick_env_attempt"] = _real_attempt
        globals()["_TICK_ENV"], globals()["_TICK_ENV_DIAG"] = _saved[0], _saved[1]
        globals()["TICK_ENV_BACKOFF"] = _saved[2]

    # ...and the whole chain must hold against a REAL bash subprocess, not just the pure classifier:
    # truncation at `_gh_gate()`, the parse, and each verdict class end to end. ORCH_* is scrubbed
    # from the environment so `env | grep` cannot inherit ambient flags and blur the no-exports case.
    import tempfile

    _saved_path, _saved_cache = ORCHESTRATE, (_TICK_ENV, _TICK_ENV_DIAG)
    _scrubbed = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("ORCH_")}
    try:
        with tempfile.TemporaryDirectory(prefix="tickenv-") as _td:
            _dir = pathlib.Path(_td)
            _good = _dir / "orchestrate.sh"
            _good.write_text(
                "set -euo pipefail\n"
                'export ORCH_FIXTURE_FLAG="${ORCH_FIXTURE_FLAG:-7}"\n'
                "_gh_gate() { :; }\nexport ORCH_AFTER_THE_GATE=9\n"
            )
            globals()["ORCHESTRATE"] = _good
            _env = tick_env(refresh=True, log=False)
            assert _env.get("ORCH_FIXTURE_FLAG") == "7", _env
            assert "ORCH_AFTER_THE_GATE" not in _env, "evaluation must stop at _gh_gate()"
            assert tick_env_status(log=False)["outcome"] == "ok", tick_env_status(log=False)
            _aborts = _dir / "aborts.sh"
            _aborts.write_text("set -euo pipefail\ncat /no/such/credential\n_gh_gate() { :; }\n")
            globals()["ORCHESTRATE"] = _aborts
            assert tick_env(refresh=True, log=False) == {}
            assert tick_env_status(log=False)["reason"] == "nonzero_exit", tick_env_status(
                log=False
            )
            _silent = _dir / "no-exports.sh"
            _silent.write_text(
                'set -euo pipefail\necho "prologue with no ORCH_ exports"\n' "_gh_gate() { :; }\n"
            )
            globals()["ORCHESTRATE"] = _silent
            assert tick_env(refresh=True, log=False) == {}
            assert tick_env_status(log=False)["outcome"] == "defect", tick_env_status(log=False)
            globals()["ORCHESTRATE"] = _dir / "deleted.sh"
            assert tick_env(refresh=True, log=False) == {}
            assert tick_env_status(log=False)["reason"] == "script_missing", tick_env_status(
                log=False
            )
    finally:
        globals()["ORCHESTRATE"] = _saved_path
        os.environ.update(_scrubbed)
        globals()["_TICK_ENV"], globals()["_TICK_ENV_DIAG"] = _saved_cache
    # classify() still cannot emit `docs`; if that ever changes, the routing decision was taken.
    assert (
        backlog.classify(["documentation"]) == "mechanical"
    ), "docs became emittable — revisit the docs-drift matcher"

    # Offline mode must not reach the network.
    for row in rep["rows"]:
        assert not str(row.get("provenance", "")).startswith("live "), row
    text = format_report(rep)
    assert "WOULD FIRE" in text and "WOULD MISS" in text
    env_prereq.report_gaps("capability_recurrence_check.py", gaps)
    print(
        "capability_recurrence_check.py selftest: OK (must-not-fire guards hold, unemittable "
        "task_type reads as a miss, offline stays offline)"
        + (f" — {len(set(gaps))} section(s) skipped, see above" if gaps else "")
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="frozen fixtures only")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = replay(offline=args.offline)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
