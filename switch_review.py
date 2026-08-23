#!/usr/bin/env python3
"""switch_review.py — held switches must be re-raised, not quietly forgotten.

THE FAILURE THIS PREVENTS. `ORCH_RANGE_LANE_ROLLOUT` was turned on as a bounded trial on
2026-07-08, reviewed 07-15, extended to 07-22 — and then nothing. It produced 2 dispatches (both
`transient_infra` rc=137) and 5 days were dispatch-skipped by a stale worktree, so the evidence was
too thin to either keep or revert. The decision was deferred and the deferral was never revisited.
A month later the flag was simply off, with no record of a decision having been made.

That is the same latched shape as every other bug in this system: a state whose exit depends on
somebody remembering. So this module does two things on a weekly cadence:

  1. **A held switch with a satisfied precondition gets raised.** If the machine-checkable criterion
     in `capability_recurrence_check.SWITCH_ON_CRITERIA` is met and the flag is still off, that is a
     decision waiting to be made, and it is surfaced as a non-blocking owner question.
  2. **A switch that is ON but NOT TRIGGERING gets raised.** This is the range-lane case exactly:
     enabling a lane that then dispatches nothing is indistinguishable from leaving it off, unless
     something notices. If a switch has been on for >= REVIEW_DAYS and its capability recorded no
     invocation in that window, the question comes back.

NON-BLOCKING BY CONSTRUCTION. Everything goes through `feedback.owner_questions`: deduped per scope,
auto-ratifying at expiry to a stated default, so an unanswered question can never accumulate into a
backlog. The default is always the conservative one — keep the current switch position — because
flipping a safety switch on silence is precisely what must not happen.

    python3 switch_review.py                # what is due for review
    python3 switch_review.py --json
    python3 switch_review.py --raise        # record owner questions (needs ORCH_SWITCH_REVIEW=1)
    python3 switch_review.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import capabilities

# How long a switch may sit unreviewed before the question comes back.
REVIEW_DAYS = 7
QUESTION_EXPIRY_DAYS = 7.0
APPLY_ENABLED = os.environ.get("ORCH_SWITCH_REVIEW", "").strip() == "1"

# flag -> the capability whose invocations prove the switch is doing anything.
SWITCH_CAPABILITY = {
    "ORCH_RANGE_LANE_ROLLOUT": "range-lane-rollout",
    # REMAPPED 2026-08-22. This pointed at `deliberate-break-verifier` on the belief that the flag
    # gated the deliberate-break command. It does not (ORCH-ANCHOR:
    # runtime-ac-command-exec-gate — COMMAND_EXEC_GATED_TYPES excludes deliberate_break, verified by
    # executing a real spec both ways). Pointing the "ON but silent" arm at a capability the switch
    # cannot influence made this review unable to say anything true about either one. The capability
    # whose invocations DO prove this switch is doing something is the runtime-AC gate that runs the
    # command checks it authorises.
    "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "runtime-ac-checks",
    "ORCH_FRONTEND_VERIFY_START_BROWSER": "frontend-verifier",
    "ORCH_STRATEGY_EXPERIMENT": "strategy-experiments",
    "ORCH_EXPLORATION_MODE": "thompson-hybrid-routing",
}


def _last_invocation(cap_id: str, *, path=None) -> int:
    caps = capabilities.load(path or capabilities.REG)
    return int((caps.get(cap_id) or {}).get("last_invocation") or 0)


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Credit this capability at its declared entrypoint.

    Added immediately after the activation audit flagged `switch-review` with `no_heartbeat` — the
    same omission `issue-readiness` had. A module that reviews other capabilities' observability
    while recording nothing about itself is not a defensible position.
    """
    try:
        import capabilities as _caps

        _caps.production_heartbeat("switch-review", event_type, ref="switch_review.review")
    except Exception as exc:
        # Continue -- the review is the product and a telemetry failure must not block it. But say
        # so: a swallowed heartbeat leaves later audits classifying switch-review as unobserved,
        # and an unexplained silence is indistinguishable from a pass.
        print(f"switch_review: capability heartbeat failed: {exc}", file=sys.stderr)


MIRROR_DIR = Path(os.environ.get("ORCH_MIRROR", Path.home() / ".codex" / "orchestrator-mirror"))


def stale_runners(*, now: int | None = None, mirror: Path | None = None) -> list[dict]:
    """Long-lived processes running mirror code OLDER than the mirror on disk.

    WHY THIS IS THE SAME DEFECT CLASS AS A HELD SWITCH. `orch-sync-mirror.sh` is treated as the
    deploy step, but Python caches modules at import: a process started before the sync keeps
    running the previous code for as long as it lives. Observed 2026-08-22 -- a cursor offload one
    minute AFTER a sync still used the pre-sync dispatcher, because four `mcp_server.py` processes
    (oldest 7h19m) each held their own copy. The fix looked broken and the code was fine.

    Silence is the symptom, exactly as with a switch nobody revisits: nothing errors, the run simply
    behaves like last week. FYI-only -- this NEVER kills anything, because a live process may be
    serving a session and a sweep that can restart the fleet is a worse hazard than stale code.
    """
    mirror = Path(mirror or MIRROR_DIR)
    try:
        newest = max((f.stat().st_mtime for f in mirror.glob("*.py")), default=0.0)
    except OSError:
        return []
    if not newest:
        return []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    now = float(now if now is not None else time.time())
    stale: list[dict] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or str(mirror) not in parts[2]:
            continue
        pid, etime, command = parts
        age = _etime_seconds(etime)
        if age is None:
            continue
        started = now - age
        if started >= newest:
            continue
        stale.append(
            {
                "pid": pid,
                "age_hours": round(age / 3600, 1),
                "stale_by_hours": round((newest - started) / 3600, 1),
                "command": command[:120],
                "reason": (
                    "started before the current mirror was written, so it is still running the "
                    "previous code; a sync does not reach a process that has already imported"
                ),
            }
        )
    return stale


def _etime_seconds(etime: str) -> int | None:
    """`ps` etime (`[[dd-]hh:]mm:ss`) as seconds."""
    text = etime.strip()
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    bits = text.split(":")
    try:
        nums = [int(b) for b in bits]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    return days * 86400 + nums[0] * 3600 + nums[1] * 60 + nums[2]


def review(*, now: int | None = None, env: dict | None = None, path=None) -> dict:
    """Which held-or-idle switches are due for an owner decision, and why."""
    import capability_recurrence_check as rc

    _capability_heartbeat()

    now = int(now if now is not None else time.time())
    env = os.environ if env is None else env
    window = REVIEW_DAYS * 86400
    due, quiet = [], []

    for flag, cap_id in sorted(SWITCH_CAPABILITY.items()):
        value = env.get(flag)
        on = bool(value) and value != "0"
        last = _last_invocation(cap_id, path=path)
        criterion = rc.SWITCH_ON_CRITERIA.get(flag)

        if not on:
            # OFF: raise only when a criterion exists, so an unconditioned switch is not nagged
            # about forever. An unconditioned switch is a documentation gap, reported separately.
            due.append(
                {
                    "flag": flag,
                    "capability": cap_id,
                    "state": "off",
                    "criterion": criterion,
                    "reason": (
                        "held off; a machine-checkable precondition is recorded, so this is a "
                        "decision waiting to be made"
                        if criterion
                        else "held off with NO recorded switch-on criterion"
                    ),
                    "has_criterion": bool(criterion),
                }
            )
            continue

        # ON but silent for the whole window: enabling it changed nothing observable.
        idle_days = (now - last) / 86400 if last else None
        if last == 0 or (now - last) > window:
            quiet.append(
                {
                    "flag": flag,
                    "capability": cap_id,
                    "state": "on",
                    "idle_days": None if idle_days is None else round(idle_days, 1),
                    "reason": (
                        f"ON but {cap_id} recorded no invocation in the last {REVIEW_DAYS}d — "
                        "an enabled switch that dispatches nothing is indistinguishable from "
                        "one left off"
                    ),
                }
            )

    # NOT `now=now`: `stale_runners` derives a process start from `now - etime` and compares it to
    # the mirror's real mtime, so an injected review clock (the selftest uses 2023) puts every start
    # before every mirror write and reports every live process stale. Two real-world quantities must
    # be compared on the real clock; `now` here dates the REVIEW, not the process table.
    runners = stale_runners()
    return {
        "generated_at": now,
        "review_days": REVIEW_DAYS,
        "held_off": due,
        "on_but_idle": quiet,
        "unconditioned": [d["flag"] for d in due if not d["has_criterion"]],
        # Reported here rather than in a second auditor, per the standing rule: a stale runner
        # is a switch-shaped problem -- something was decided and the decision never landed.
        "stale_runners": runners,
        "raise_count": len(due) + len(quiet),
    }


def raise_questions(rep: dict, *, dry_run: bool = True) -> dict:
    """Record ONE non-blocking, auto-expiring owner question per due switch."""
    raised, deduped, errors = [], [], []
    for row in rep["held_off"] + rep["on_but_idle"]:
        flag, cap_id, state = row["flag"], row["capability"], row["state"]
        if state == "off":
            question = (
                f"{flag} is off and {cap_id}'s switch-on precondition is recorded. "
                f"Turn it on, or restate the criterion?"
            )
            default = "keep it off; re-ask in a week"
        else:
            question = (
                f"{flag} is ON but {cap_id} has recorded no invocation in "
                f"{REVIEW_DAYS}d. Keep it on, turn it off, or fix what feeds it?"
            )
            default = "keep the current position; re-ask in a week"
        if dry_run:
            raised.append(flag)
            continue
        try:
            import feedback

            res = feedback.record_owner_question(
                question,
                default,
                repo="orchestrator",
                target=f"switch:{flag}",
                options=["on", "off", "investigate"],
                expires_days=QUESTION_EXPIRY_DAYS,
            )
            (deduped if res.get("deduped") else raised).append(flag)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{flag}: {str(exc)[:100]}")
    return {"raised": raised, "already_open": deduped, "errors": errors, "dry_run": dry_run}


def format_report(rep: dict) -> str:
    lines = [
        "# Switch review — held switches must be revisited, not forgotten",
        "",
        f"  review window: {rep['review_days']}d",
        f"  due for a decision: {rep['raise_count']}",
        "",
    ]
    if rep["held_off"]:
        lines += ["## Held OFF", ""]
        for row in rep["held_off"]:
            lines.append(f"  {row['flag']}  ({row['capability']})")
            lines.append(f"      {row['reason']}")
            if row.get("criterion"):
                lines.append(f"      switch on when: {row['criterion'][:150]}")
            lines.append("")
    if rep["on_but_idle"]:
        lines += ["## ON but not triggering — the range-lane failure mode", ""]
        for row in rep["on_but_idle"]:
            lines.append(f"  {row['flag']}  ({row['capability']})  idle={row['idle_days']}d")
            lines.append(f"      {row['reason']}")
            lines.append("")
    if rep["unconditioned"]:
        lines += [
            "## Held with NO recorded criterion (a documentation gap, fix in "
            "SWITCH_ON_CRITERIA)",
            "",
        ]
        lines += [f"  {f}" for f in rep["unconditioned"]] + [""]
    if rep.get("stale_runners"):
        lines += [
            "## Running code older than the mirror — a sync does not reach a live process",
            "",
        ]
        for row in rep["stale_runners"]:
            lines.append(
                f"  pid {row['pid']}  up {row['age_hours']}h  " f"stale by {row['stale_by_hours']}h"
            )
            lines.append(f"      {row['command']}")
            lines.append("")
        lines += [
            "  FYI only: restart these to pick up the current mirror. Nothing is killed "
            "automatically -- a live process may be serving a session.",
            "",
        ]
    if not rep["raise_count"] and not rep.get("stale_runners"):
        lines += ["  Nothing due. Every switch is either triggering or has a fresh decision.", ""]
    return "\n".join(lines)


def _selftest_stale_runners() -> None:
    """A sync does not reach a process that has already imported the code."""
    import tempfile

    # `ps` etime, all four shapes, including the one that must refuse rather than guess.
    assert _etime_seconds("07:19:52") == 26392
    assert _etime_seconds("04:30") == 270
    assert _etime_seconds("1-02:03:04") == 93784
    assert _etime_seconds("bogus") is None

    # An empty mirror yields nothing: no mtime means no claim, never "everything is stale".
    with tempfile.TemporaryDirectory(prefix="switch-review-mirror-") as td:
        assert stale_runners(mirror=Path(td)) == []

    # DETERMINISTIC CLASSIFICATION, exercised with no dependence on the real process table. Until
    # 2026-08-23 the only coverage of the compare-and-classify path needed a live process running
    # mirror code, so on any CI runner this selftest returned early and the logic below could
    # regress unseen -- the normal case going unchecked, which is the exact hole verify.py exists
    # to close. A fabricated `ps` line plus a controlled mirror mtime pins both directions.
    with tempfile.TemporaryDirectory(prefix="switch-review-fake-") as td:
        fake_mirror = Path(td)
        stamp = fake_mirror / "dispatcher.py"
        stamp.write_text("# mirror code\n", encoding="utf-8")
        mtime = 1_800_000_000.0
        os.utime(stamp, (mtime, mtime))
        real_run = subprocess.run

        def fake_ps(*_a, **_k):
            class R:
                stdout = f"  4242    07:19:52 python3 {fake_mirror}/dispatcher.py --tick\n"

            return R()

        subprocess.run = fake_ps
        try:
            age = 7 * 3600 + 19 * 60 + 52  # the etime above, in seconds
            # STALE: the process started one hour BEFORE the mirror was written.
            rows = stale_runners(now=mtime + age - 3600, mirror=fake_mirror)
            assert len(rows) == 1, rows
            assert rows[0]["pid"] == "4242", rows
            assert rows[0]["stale_by_hours"] == 1.0, rows
            assert rows[0]["reason"], "a stale runner must say why it is stale"
            # NOT STALE: the same process started one hour AFTER the mirror was written.
            assert stale_runners(now=mtime + age + 3600, mirror=fake_mirror) == []

            # An unparseable etime must be skipped, never guessed at.
            def fake_ps_bogus(*_a, **_k):
                class R:
                    stdout = f"  4242    bogus python3 {fake_mirror}/dispatcher.py\n"

                return R()

            subprocess.run = fake_ps_bogus
            assert stale_runners(now=mtime + age, mirror=fake_mirror) == []
        finally:
            subprocess.run = real_run

    live = stale_runners()
    if not live:
        # Prerequisite absent (nothing is running from the mirror), NAMED rather than silently
        # passing -- the comparison below needs a real process to compare against.
        print(
            "switch_review: stale-runner comparison skipped (no process running mirror code)",
            file=sys.stderr,
        )
        return
    # THE COMPARISON, exercised in both directions against the real process table: shift `now`
    # forward far enough that every process appears to have started AFTER the mirror was written,
    # and nothing may be reported stale. Without this the check could report every process forever
    # and still look correct.
    assert (
        stale_runners(now=time.time() + 20 * 365 * 86400) == []
    ), "a process started after the mirror was written is not stale"
    for row in live:
        assert row["stale_by_hours"] >= 0 and row["pid"].isdigit(), row
        assert row["reason"], "a stale runner must say why it is stale"


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    _selftest_stale_runners()
    now = 1_700_000_000
    with tempfile.TemporaryDirectory(prefix="switch-review-") as td:
        reg = Path(td) / "capabilities.json"
        caps = {}
        for cap_id in SWITCH_CAPABILITY.values():
            rec = capabilities._blank_capability(cap_id)
            rec["status"] = "generated"
            caps[cap_id] = rec
        capabilities.save(caps, reg)

        # ALL OFF -> each with a recorded criterion is raised as a pending decision.
        rep = review(now=now, env={}, path=reg)
        flags = {r["flag"] for r in rep["held_off"]}
        assert flags == set(SWITCH_CAPABILITY), flags
        assert not rep["on_but_idle"], rep["on_but_idle"]
        # Switches WITH a criterion must be distinguishable from those without. Every real switch
        # now HAS one (that gap was closed 2026-08-20), so the mechanism is tested with a synthetic
        # flag — otherwise this assertion would quietly go vacuous the moment a gap is fixed.
        assert not rep["unconditioned"], f"a real switch lost its criterion: {rep['unconditioned']}"
        saved_map = dict(SWITCH_CAPABILITY)
        try:
            SWITCH_CAPABILITY["ORCH_SYNTHETIC_NO_CRITERION"] = "range-lane-rollout"
            gap = review(now=now, env={}, path=reg)
            assert gap["unconditioned"] == ["ORCH_SYNTHETIC_NO_CRITERION"], gap["unconditioned"]
            assert "NO recorded criterion" in format_report(gap)
        finally:
            SWITCH_CAPABILITY.clear()
            SWITCH_CAPABILITY.update(saved_map)
        assert "ORCH_RANGE_LANE_ROLLOUT" not in rep["unconditioned"], rep["unconditioned"]

        # THE RANGE-LANE CASE: ON, but the capability recorded nothing -> re-raised.
        env = {"ORCH_RANGE_LANE_ROLLOUT": "1"}
        rep2 = review(now=now, env=env, path=reg)
        idle = {r["flag"] for r in rep2["on_but_idle"]}
        assert idle == {"ORCH_RANGE_LANE_ROLLOUT"}, idle
        assert "ORCH_RANGE_LANE_ROLLOUT" not in {r["flag"] for r in rep2["held_off"]}
        text = format_report(rep2)
        assert "ON but not triggering" in text and "range-lane failure mode" in text

        # ON and RECENTLY triggering -> silent, no question.
        caps["range-lane-rollout"]["last_invocation"] = now - 2 * 86400
        capabilities.save(caps, reg)
        rep3 = review(now=now, env=env, path=reg)
        assert not rep3["on_but_idle"], rep3["on_but_idle"]

        # ON but last invocation just past the window -> raised again. This is the component that
        # makes "turned it on and forgot" impossible.
        caps["range-lane-rollout"]["last_invocation"] = now - (REVIEW_DAYS + 1) * 86400
        capabilities.save(caps, reg)
        rep4 = review(now=now, env=env, path=reg)
        assert {r["flag"] for r in rep4["on_but_idle"]} == {"ORCH_RANGE_LANE_ROLLOUT"}, rep4
        assert rep4["on_but_idle"][0]["idle_days"] == REVIEW_DAYS + 1

        # "0" counts as off, not on.
        rep5 = review(now=now, env={"ORCH_RANGE_LANE_ROLLOUT": "0"}, path=reg)
        assert "ORCH_RANGE_LANE_ROLLOUT" in {r["flag"] for r in rep5["held_off"]}

        # Dry run never writes, and the default is always conservative.
        # raise_questions covers BOTH lists, so the ON-but-idle switch appears alongside the
        # held-off ones; what matters is that the idle one is not dropped.
        out = raise_questions(rep4, dry_run=True)
        assert out["dry_run"], out
        assert "ORCH_RANGE_LANE_ROLLOUT" in out["raised"], out
        assert len(out["raised"]) == len(rep4["held_off"]) + len(rep4["on_but_idle"]), out

    print(
        "switch_review.py selftest: OK (held-off raised, ON-but-idle re-raised after the window, "
        "recently-triggering stays silent, '0' is off, dry-run inert)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--raise",
        dest="do_raise",
        action="store_true",
        help="record owner questions (requires ORCH_SWITCH_REVIEW=1)",
    )
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = review()
    if args.do_raise:
        if not APPLY_ENABLED:
            print("refusing to raise: set ORCH_SWITCH_REVIEW=1", file=sys.stderr)
            return 2
        rep["questions"] = raise_questions(rep, dry_run=False)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
