"""An expiry ends by decision, not by timeout: `capabilities.renew` and switch-review's notice.

Every ledger row carrying an `expiry` is retired by `capabilities._expire_in_place` once it passes,
and until `renew` existed nothing could move an expiry after registration set it. A row retired that
way could not come back either: `transition(id, "observed")` fails outright for a gate row, because
the timeout clears the `next_transition` every live gate must carry, and for any other row the next
writing load re-retires it, because the expiry is still in the past. So the KNOWN_GATES rows that one
bootstrap seeded together reached one timeout together, with no way to hold any of them and nothing
that said the date was coming.

These tests pin the drain and its voice. `renew` refuses without a reason and evidence, extends from
NOW so renewals cannot stack, revives a row its EXPIRY retired and never one a decision retired.
`switch_review.gate_expiry` names what the timeout is about to retire, or just retired, using the
same predicate `renew` enforces, and says so distinctly when there is nothing to name and when it
could not look. Every ledger here is a temporary file: no test may write the live one.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cadence_registry
import capabilities
import paths
import switch_review

DAY = 86400
TTL = capabilities.GATED_TTL_DAYS * DAY
GATE = "role-redirect"  # a KNOWN_GATES row: gated, so a LIVE row of it must carry next_transition


def _gate_row(**fields) -> dict:
    """A gate row as registration seeds it: declared fields, and `next_transition: retired`."""
    row = capabilities._blank_capability(GATE)
    row.update(json.loads(json.dumps(capabilities.KNOWN_GATES[GATE])))
    row.update({"next_transition": "retired", **fields})
    return row


def _plain_row(cap_id: str, **fields) -> dict:
    row = capabilities._blank_capability(cap_id)
    row.update({"status": "wired", **fields})
    return row


def _ledger(tmp_path: Path, rows: dict) -> Path:
    path = tmp_path / "capabilities.json"
    capabilities.save(rows, path)
    return path


def _raw(path: Path, cap_id: str) -> dict:
    return json.loads(path.read_text())["capabilities"][cap_id]


# ---------------------------------------------------------------------------
# The gap, as it stood: no way back out of an expiry retirement.
# ---------------------------------------------------------------------------


def test_the_old_way_back_out_of_an_expiry_retirement_does_not_work(tmp_path):
    ledger = _ledger(
        tmp_path,
        {
            GATE: _gate_row(expiry=100, activation_deadline=100, next_transition="retired"),
            "plain": _plain_row("plain", expiry=100),
        },
    )
    assert sorted(capabilities.sweep(ledger, now=200)) == ["plain", GATE]
    # A gate row cannot even be transitioned: the timeout cleared its next_transition.
    with pytest.raises(AssertionError, match="next_transition"):
        capabilities.transition(GATE, "observed", reason="by hand", path=ledger, timestamp=300)
    # Any other row can, and the next writing load retires it again: its expiry is still past.
    capabilities.transition("plain", "observed", reason="by hand", path=ledger, timestamp=300)
    assert capabilities.load(ledger)["plain"]["status"] == "retired"


# ---------------------------------------------------------------------------
# renew: refusals write nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "refs"),
    [
        ("", ["pr#1"]),
        ("   ", ["pr#1"]),
        ("still runs", []),
        ("still runs", None),
        ("still runs", ["", "  "]),
        (capabilities.RENEW_REASON_PLACEHOLDER, ["pr#1"]),
        ("still runs", [capabilities.RENEW_EVIDENCE_PLACEHOLDER]),
    ],
)
def test_renew_refuses_without_a_reason_and_evidence_and_writes_nothing(tmp_path, reason, refs):
    now = int(time.time())
    ledger = _ledger(tmp_path, {GATE: _gate_row(expiry=now + DAY, activation_deadline=now + DAY)})
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="needs"):
        capabilities.renew(GATE, reason=reason, evidence_refs=refs, path=ledger, timestamp=now)
    assert ledger.read_bytes() == before, "a refused renewal wrote the ledger"


def test_renew_refuses_what_it_cannot_honestly_hold(tmp_path):
    now = 10_000_000
    ledger = _ledger(
        tmp_path,
        {
            "no-expiry": _plain_row("no-expiry"),
            "superseded": _plain_row("superseded", status="superseded", expiry=now - DAY),
            "has-successor": _plain_row("has-successor", expiry=now + DAY, successor="v2"),
            "far-future": _plain_row("far-future", expiry=now + TTL + DAY),
            "decided": _plain_row("decided", expiry=now + DAY),
        },
    )
    capabilities.transition("decided", "retired", reason="no longer wanted", path=ledger)
    before = ledger.read_bytes()
    for cap_id, words in (
        ("no-expiry", "no expiry"),
        ("superseded", "superseded"),
        ("has-successor", "successor"),
        ("far-future", "would not extend"),
        ("decided", "retired by a decision"),
        ("absent", "unknown capability"),
    ):
        with pytest.raises(ValueError, match=words):
            capabilities.renew(
                cap_id, reason="hold", evidence_refs=["pr#1"], path=ledger, timestamp=now
            )
    assert ledger.read_bytes() == before


# ---------------------------------------------------------------------------
# renew: what a renewal does.
# ---------------------------------------------------------------------------


def test_renew_extends_from_now_and_records_the_decision(tmp_path):
    now = int(time.time())
    old = now + 2 * DAY
    ledger = _ledger(
        tmp_path,
        {GATE: _gate_row(expiry=old, activation_deadline=old, last_invocation=now - DAY)},
    )
    out = capabilities.renew(
        GATE, reason="runs every tick", evidence_refs=["pr#1", "pr#1", " run:7 "], path=ledger
    )
    row = _raw(ledger, GATE)
    assert out["renewed"] and out["revived_from"] is None, out
    assert now + TTL <= row["expiry"] <= int(time.time()) + TTL, row["expiry"]
    assert row["activation_deadline"] == row["expiry"], "a deadline seeded with the expiry moves"
    assert row["status"] == capabilities.KNOWN_GATES[GATE]["status"]
    event = row["event_history"][-1]
    assert event["type"] == capabilities.RENEWAL_EVENT, event
    assert event["reason"] == "runs every tick"
    assert event["evidence_refs"] == ["pr#1", "run:7"], "refs are stripped and deduplicated"
    assert event["previous_expiry"] == old and event["previous_activation_deadline"] == old
    assert event["observed"]["last_invocation"] == now - DAY, event["observed"]
    # And it HOLDS through the real writing load, which is where the timeout lives.
    assert capabilities.load(ledger)[GATE]["status"] == capabilities.KNOWN_GATES[GATE]["status"]


def test_a_renewal_revives_a_row_its_expiry_retired_through_the_real_load(tmp_path):
    """The drain must run while the gate is CLOSED, or the first missed date closes it for good."""
    ledger = _ledger(
        tmp_path, {GATE: _gate_row(expiry=100, activation_deadline=100, next_transition="retired")}
    )
    assert capabilities.load(ledger)[GATE]["status"] == "retired"  # the timeout, as it runs live
    timeout = capabilities.expiry_retirement(_raw(ledger, GATE))
    assert timeout and timeout["next_transition_was"] == "retired", timeout

    out = capabilities.renew(
        GATE, reason="still runs every redirect sweep", evidence_refs=["ledger:x"], path=ledger
    )
    declared = capabilities.KNOWN_GATES[GATE]["status"]
    assert out["revived_from"] == "retired" and out["status"] == declared, out
    row = _raw(ledger, GATE)
    assert row["next_transition"] == "retired", "the cleared next_transition came back"
    back = [e for e in row["event_history"] if e["type"] == "transition"][-1]
    assert (back["from"], back["to"]) == ("retired", declared), back
    # Live on every read path, including the writing load that retired it in the first place.
    assert capabilities.load(ledger)[GATE]["status"] == declared
    assert capabilities.load_declared(ledger)[GATE]["status"] == declared
    assert capabilities.expiry_retirement(_raw(ledger, GATE)) is None


def test_revival_restores_the_status_the_timeout_interrupted_not_a_default(tmp_path):
    ledger = _ledger(tmp_path, {"plain": _plain_row("plain", status="canary", expiry=100)})
    capabilities.sweep(ledger, now=200)
    out = capabilities.renew("plain", reason="hold", evidence_refs=["pr#1"], path=ledger)
    assert out["status"] == "canary", out
    # A row with an expiry retires at it unless renewed, so with nothing recorded that is the truth.
    assert _raw(ledger, "plain")["next_transition"] == "retired"


def test_renewals_never_stack(tmp_path):
    t0 = 50_000_000
    ledger = _ledger(tmp_path, {"plain": _plain_row("plain", expiry=t0 + DAY)})
    capabilities.renew("plain", reason="hold", evidence_refs=["a"], path=ledger, timestamp=t0)
    assert _raw(ledger, "plain")["expiry"] == t0 + TTL
    with pytest.raises(ValueError, match="would not extend"):
        capabilities.renew("plain", reason="again", evidence_refs=["b"], path=ledger, timestamp=t0)
    capabilities.renew(
        "plain", reason="a day on", evidence_refs=["c"], path=ledger, timestamp=t0 + DAY
    )
    assert _raw(ledger, "plain")["expiry"] == t0 + DAY + TTL, "from NOW, never old expiry + TTL"


def test_an_activation_deadline_of_its_own_is_left_alone(tmp_path):
    t0 = 50_000_000
    ledger = _ledger(
        tmp_path, {"plain": _plain_row("plain", expiry=t0 + DAY, activation_deadline=t0 + 5 * DAY)}
    )
    capabilities.renew("plain", reason="hold", evidence_refs=["a"], path=ledger, timestamp=t0)
    row = _raw(ledger, "plain")
    assert row["activation_deadline"] == t0 + 5 * DAY, row
    assert "previous_activation_deadline" not in row["event_history"][-1]


def test_a_decision_is_never_renewed_even_after_an_earlier_timeout(tmp_path):
    ledger = _ledger(tmp_path, {"plain": _plain_row("plain", expiry=100)})
    capabilities.sweep(ledger, now=200)
    capabilities.renew("plain", reason="hold", evidence_refs=["a"], path=ledger, timestamp=300)
    capabilities.transition(
        "plain", "retired", reason="no longer wanted", path=ledger, timestamp=400
    )
    with pytest.raises(ValueError, match="retired by a decision"):
        capabilities.renew("plain", reason="undo", evidence_refs=["b"], path=ledger, timestamp=500)


def test_the_cli_renews_with_exit_0_and_refuses_with_exit_1(tmp_path):
    now = int(time.time())
    ledger = _ledger(tmp_path, {"plain": _plain_row("plain", expiry=now + DAY)})
    env = {**os.environ, "ORCH_CAPABILITIES_PATH": str(ledger)}
    script = str(paths.MODULE_DIR / "capabilities.py")

    def cli(*args: str) -> tuple[int, dict]:
        done = subprocess.run(
            [sys.executable, script, "renew", *args], env=env, capture_output=True, text=True
        )
        return done.returncode, json.loads(done.stdout)

    before = ledger.read_bytes()
    rc, out = cli("--name", "plain", "--reason", "hold", "--evidence-ref", " ")
    assert rc == 1 and out["renewed"] is False and "evidence" in out["refused"], out
    assert ledger.read_bytes() == before
    rc, out = cli("--name", "plain", "--reason", "hold", "--evidence-ref", "pr#1")
    assert rc == 0 and out["renewed"] is True, out
    assert _raw(ledger, "plain")["expiry"] >= now + TTL


def test_the_notice_command_is_the_cli_and_its_placeholders_are_refused(tmp_path):
    command = capabilities.renew_command("plain")
    assert command.startswith("python3 ") and " renew --name plain " in command, command
    now = int(time.time())
    ledger = _ledger(tmp_path, {"plain": _plain_row("plain", expiry=now + DAY)})
    env = {**os.environ, "ORCH_CAPABILITIES_PATH": str(ledger)}
    # Pasted unedited, the command must refuse: a placeholder is not evidence. Run with THIS
    # interpreter, so the test is about the command's arguments and not about whichever python3
    # the runner's PATH happens to resolve.
    pasted = shlex.quote(sys.executable) + command.removeprefix("python3")
    done = subprocess.run(pasted, shell=True, env=env, capture_output=True, text=True)
    assert done.returncode == 1, done
    assert json.loads(done.stdout)["renewed"] is False


# ---------------------------------------------------------------------------
# switch_review.gate_expiry: the FYI notice.
# ---------------------------------------------------------------------------

NOW = 1_800_000_000


def _expired(cap_id: str, days_ago: float, reason: str, **fields) -> dict:
    ts = int(NOW - days_ago * DAY)
    return _plain_row(
        cap_id,
        status="retired",
        expiry=ts,
        event_history=[
            {
                "timestamp": ts,
                "type": "transition",
                "from": "shadow",
                "to": "retired",
                "reason": reason,
            }
        ],
        **fields,
    )


def _mixed(tmp_path: Path) -> Path:
    window = switch_review.GATE_EXPIRY_NOTICE_DAYS
    timeout = capabilities.EXPIRY_RETIREMENT_REASON
    return _ledger(
        tmp_path,
        {
            "soon": _plain_row("soon", expiry=NOW + 3 * DAY, last_invocation=NOW - DAY),
            "edge": _plain_row("edge", expiry=NOW + window * DAY),
            "later": _plain_row("later", expiry=NOW + (window + 1) * DAY),
            "successor-soon": _plain_row("successor-soon", expiry=NOW + DAY, successor="v2"),
            "lapsed": _expired("lapsed", 2, timeout),
            "decided": _expired("decided", 2, "no longer wanted"),
            "long-gone": _expired("long-gone", window + 1, timeout),
            "no-expiry": _plain_row("no-expiry"),
        },
    )


def test_the_notice_names_what_the_timeout_will_retire_and_just_retired(tmp_path):
    ledger = _mixed(tmp_path)
    before = ledger.read_bytes()
    got = switch_review.gate_expiry(now=NOW, path=ledger)
    assert ledger.read_bytes() == before, "the notice wrote the ledger it reads"
    assert got["status"] == "ok"
    assert [r["capability_id"] for r in got["expiring"]] == ["successor-soon", "soon", "edge"]
    assert [r["capability_id"] for r in got["lapsed"]] == ["lapsed"]
    assert got["rows_with_expiry"] == 4, "the whole denominator: every live row with an expiry"
    assert got["next_expiry"]["capability_id"] == "later", got["next_expiry"]
    soon = got["expiring"][1]
    assert soon["days_left"] == 3.0 and soon["last_invocation_on"] == capabilities.utc_date(
        NOW - DAY
    )
    assert soon["renew"] == capabilities.renew_command("soon")
    text = "\n".join(switch_review.format_gate_expiry(got))
    for name in ("soon", "edge", "lapsed", "successor-soon"):
        assert name in text, (name, text)
    assert "decided" not in text and "long-gone" not in text and "later  " not in text, text
    assert "not renewable: its successor v2" in text, text


def test_the_notice_and_renew_share_one_predicate(tmp_path):
    """`renewable` must mean exactly "renew accepts it", proved by renewing every listed row."""
    ledger = _mixed(tmp_path)
    got = switch_review.gate_expiry(now=NOW, path=ledger)
    listed = got["expiring"] + got["lapsed"]
    assert any(
        not r["renewable"] for r in listed
    ), "need a listed row renew refuses, or this is weak"
    assert got["renewable"] == sum(1 for r in listed if r["renewable"]) == 3, got["renewable"]
    for row in listed:
        if row["renewable"]:
            capabilities.renew(
                row["capability_id"], reason="hold", evidence_refs=["t"], path=ledger, timestamp=NOW
            )
        else:
            with pytest.raises(ValueError):
                capabilities.renew(
                    row["capability_id"],
                    reason="hold",
                    evidence_refs=["t"],
                    path=ledger,
                    timestamp=NOW,
                )
    # Once renewed, a row leaves the notice: the drain is visible in the very next report.
    after = switch_review.gate_expiry(now=NOW, path=ledger)
    assert [r["capability_id"] for r in after["expiring"]] == ["successor-soon"], after["expiring"]
    assert after["lapsed"] == []


def test_drained_unmeasured_and_empty_each_say_something_different(tmp_path):
    later = _ledger(tmp_path, {"later": _plain_row("later", expiry=NOW + 40 * DAY)})
    drained = "\n".join(
        switch_review.format_gate_expiry(switch_review.gate_expiry(now=NOW, path=later))
    )
    assert "nothing expires within" in drained and "the next is later on" in drained, drained

    empty = _ledger(tmp_path, {"no-expiry": _plain_row("no-expiry")})
    none = "\n".join(
        switch_review.format_gate_expiry(switch_review.gate_expiry(now=NOW, path=empty))
    )
    assert "no live ledger row carries an expiry" in none, none

    missing = switch_review.gate_expiry(now=NOW, path=tmp_path / "absent.json")
    assert missing["status"] == "unknown" and not (tmp_path / "absent.json").exists(), missing
    unread = tmp_path / "unreadable.json"
    unread.write_text("{not json")
    broken = switch_review.gate_expiry(now=NOW, path=unread)
    assert broken["status"] == "unknown" and "unreadable" in broken["measurement"], broken
    texts = {
        "drained": drained,
        "none": none,
        "missing": "\n".join(switch_review.format_gate_expiry(missing)),
        "broken": "\n".join(switch_review.format_gate_expiry(broken)),
    }
    assert "NOT MEASURED" in texts["missing"] and "NOT MEASURED" in texts["broken"]
    assert "NOT MEASURED" not in texts["drained"] and "NOT MEASURED" not in texts["none"]
    assert len(set(texts.values())) == 4, "two different states rendered the same words"


def _report(gate_expiry: dict | None) -> str:
    rep = {
        "generated_at": 0,
        "review_days": switch_review.REVIEW_DAYS,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "stale_runners": [],
        "mirror_drift": {"status": "ok"},
        "raise_count": 0,
    }
    if gate_expiry is not None:
        rep["gate_expiry"] = gate_expiry
    return switch_review.format_report(rep)


def test_nothing_due_is_never_printed_over_an_expiry_or_an_unmeasured_ledger(tmp_path):
    listed = switch_review.gate_expiry(now=NOW, path=_mixed(tmp_path))
    assert "Nothing due" not in _report(listed)
    assert "Nothing due" not in _report(
        switch_review.gate_expiry(now=NOW, path=tmp_path / "x.json")
    )
    clean = tmp_path / "clean"
    clean.mkdir()
    drained = switch_review.gate_expiry(
        now=NOW, path=_ledger(clean, {"later": _plain_row("later", expiry=NOW + 40 * DAY)})
    )
    assert "Nothing due" in _report(drained)
    assert "Nothing due" in _report(None), "a report from before the notice existed still reads"


def test_the_notice_is_fyi_only(tmp_path, monkeypatch):
    """It never counts toward an owner question, and a review renews nothing."""
    # Every input the sweep takes from the machine besides the ledger: the process table, the exec
    # mirror, GitHub, the Brain and the heartbeat.
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **_: {"suspect": False})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {"suspect": False})
    monkeypatch.setattr(switch_review, "_capability_heartbeat", lambda *_a, **_k: None)
    ledger = _mixed(tmp_path)
    before = ledger.read_bytes()
    rep = switch_review.review(now=NOW, env={}, path=ledger)
    assert rep["gate_expiry"]["expiring"], "fixture lists nothing, so this proves nothing"
    switches = len(rep["held_off"]) + len(rep["on_but_idle"])
    assert rep["raise_count"] == switches
    assert len(switch_review.raise_questions(rep, dry_run=True)["raised"]) == switches
    assert ledger.read_bytes() == before, "a review renewed or retired something"


def test_the_window_covers_the_cadence_that_reads_it():
    """Measuring window vs draining window: the notice must outlast the gap between two reviews.

    The step runs every `cadence_days`; a window shorter than that can let an expiry land between
    two reviews and never be named, and one shorter than two gaps loses it to a single missed run.
    """
    step = cadence_registry.STEP_BY_KEY["switch-review"]
    assert switch_review.GATE_EXPIRY_NOTICE_DAYS >= 2 * step["cadence_days"], step
    assert (
        switch_review.GATE_EXPIRY_NOTICE_DAYS < capabilities.GATED_TTL_DAYS
    ), "a window as long as the TTL would list every gate from the day it is registered"
