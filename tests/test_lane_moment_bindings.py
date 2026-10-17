"""M4 restated (owner, 2026-10-17): judgment roles reach the lanes where their demand is.

Measured before this change: the four roles were bound only to the Claude `orchestrate` skill
(13 offers in 30 days) while 13,666 lane offers in 30 days went to ten other capabilities; their
consumers sat behind the tick's dispatch lane (off since 2026-09-03) and the monitor printed
`input_off` for one of them. Each binding below names a lane moment, a command and a pool.
"""

from __future__ import annotations

import json
import time

import capability_advisor as ca
import triage_shadow
import value_chain_monitor as vcm

ROLES = {"role-prompt", "role-triage", "role-redirect", "role-decomposer", "role-adjudicator"}


def test_each_role_is_bound_to_a_lane_moment_within_the_safe_zone():
    opener = ca.SURFACE_BINDINGS["opener-lane"]
    closer = ca.SURFACE_BINDINGS["closer-lane"]
    uploader = ca.SURFACE_BINDINGS["repo-review-uploader"]
    assert {"role-triage", "role-decomposer"} <= set(opener)
    assert "role-redirect" in closer and "redirect-plan" not in closer
    assert "role-adjudicator" in closer, "the dispute moment was already bound"
    assert set(uploader) == {"role-prompt"}
    for surface in ("opener-lane", "closer-lane"):
        assert 3 <= len(ca.SURFACE_BINDINGS[surface]) <= 7, surface
    # Every lane binding of a role names its moment, its command and its pool.
    assert "triage-ranking.json" in opener["role-triage"] and "--consume" in opener["role-triage"]
    assert "roles.py decompose" in opener["role-decomposer"]
    assert (
        "roles.py redirect" in closer["role-redirect"]
        and "redirect-sweep.json" in closer["role-redirect"]
    )
    assert "roles.py prompt --batch" in uploader["role-prompt"]
    for text in (
        opener["role-triage"],
        opener["role-decomposer"],
        closer["role-redirect"],
        uploader["role-prompt"],
    ):
        assert "Pool:" in text


def test_every_role_consumer_behind_a_switch_is_declared():
    """A role whose consumer runs only inside a switched lane names that switch, so the monitor
    prints input_off instead of not_acted_on when the switch is off; the adjudicator's consumer is
    the closer lane, which has no switch."""
    for role in ROLES - {"role-adjudicator"}:
        assert vcm.INPUT_FLAGS.get(role), role
    assert "role-adjudicator" not in vcm.INPUT_FLAGS
    assert vcm.SITUATION_QUERIES["role-redirect"] is vcm._stall_situations


def test_stall_population_is_read_from_the_sweep_log(tmp_path):
    now = int(time.time())
    log = tmp_path / "redirect-sweep.log"
    sweeps = [
        {
            "generated_at": now - 3600,
            "actionable_count": 2,
            "actionable": [{"target": "o/r#1"}, {"target": "o/r#2"}],
        },
        {"generated_at": now - 7200, "actionable_count": 1, "actionable": [{"target": "o/r#2"}]},
        {
            "generated_at": now - 200 * 86400,
            "actionable_count": 1,
            "actionable": [{"target": "o/r#9"}],
        },
        {"generated_at": now - 60, "actionable_count": 0, "actionable": []},
    ]
    log.write_text("\n".join(json.dumps(s, indent=2) for s in sweeps) + "\nnot json\n")
    targets = vcm._stall_sweep_targets(log, now - vcm.WINDOW_DAYS * 86400, now)
    assert targets == {"o/r#1", "o/r#2"}, "distinct actionable targets inside the window"
    assert vcm._stall_situations({"stall_targets": targets}) == 2
    assert vcm._stall_sweep_targets(tmp_path / "missing.log", 0, now) is None
    assert vcm._stall_situations({"stall_targets": None}) is None, "unread is never zero"


def test_triage_line_prints_its_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    (tmp_path / "capability-program").mkdir()
    line = triage_shadow.summary_line()
    assert line.startswith("triage shadow:")
    assert f"pool followed 0 of {triage_shadow.FOLLOWED_POOL}" in line
    assert triage_shadow.FOLLOWED_POOL == 30
