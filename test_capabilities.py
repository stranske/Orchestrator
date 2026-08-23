import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import capabilities
import env_prereq


@pytest.fixture
def active_capability_fixture():
    cap = capabilities._blank_capability("active-fixture")
    cap.update(
        {
            "status": "active",
            "owner": "orchestrator",
            "matcher": {"kind": "task_type", "equals": "implement"},
            "entrypoint": "worker.py",
            "trigger_cadence": "per dispatch",
            "output_artifact": "result.json",
            "downstream_consumer": "outcomes.py",
            "learning_sink": "feedback.outcomes",
            "activation_evidence": {
                probe: {
                    "passed": True,
                    "checked_at": 100,
                    "ref": f"fixture:{probe}",
                }
                for probe in capabilities.ACTIVE_PROBES
            },
            "last_invocation": 100,
            "last_match": 99,
            "last_success": 100,
            "outcome_links": ["run-1"],
            "expiry": int(time.time()) + 3600,
            "kill_switch": "ORCH_FIXTURE=0",
            "rollback": {"transition": "retired"},
        }
    )
    return cap


def test_active_requires_entrypoint(active_capability_fixture):
    capabilities.validate_capability(active_capability_fixture)
    active_capability_fixture["entrypoint"] = None
    with pytest.raises(AssertionError, match="active capability missing entrypoint"):
        capabilities.validate_capability(active_capability_fixture)


def test_conservative_migration_does_not_infer_activation(tmp_path):
    features = tmp_path / "features.json"
    ledger = tmp_path / "capabilities.json"
    features.write_text(
        json.dumps(
            {
                "old-feature": {
                    "problem": "legacy",
                    "maturity": "hardened",
                    "module": "old.py",
                }
            }
        )
    )
    result = capabilities.migrate_features_to_capabilities(features, ledger, now=100)
    migrated = result["capabilities"]
    assert migrated["old-feature"]["status"] == "generated"
    assert migrated["old-feature"]["last_match"] is None
    assert migrated["old-feature"]["last_invocation"] is None
    assert migrated["old-feature"]["last_success"] is None
    assert migrated["old-feature"]["outcome_links"] == []
    assert all(cap["status"] != "active" for cap in migrated.values())
    assert migrated["range-lane-rollout"]["status"] == "canary"
    assert (
        migrated["range-lane-rollout"]["flags_defaults"]["orchestrate_default"][
            "ORCH_RANGE_LANE_ROLLOUT"
        ]
        == "1"
    )
    assert migrated["range-lane-rollout"]["next_transition"] == "retired"
    assert migrated["range-lane-rollout"]["expiry"] > 100


def test_unknown_heartbeat_fails_closed(tmp_path):
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)
    with pytest.raises(ValueError, match="unknown capability"):
        capabilities.heartbeat("missing", "match", path=ledger)
    assert json.loads(ledger.read_text())["capabilities"] == {}


def test_concurrent_heartbeats_retain_every_event(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("concurrent")
    cap.update({"status": "wired", "entrypoint": "worker.py"})
    capabilities.register("concurrent", cap, ledger)

    def record(index):
        capabilities.heartbeat(
            "concurrent",
            "match",
            ref=f"subject-{index}",
            timestamp=index + 1,
            path=ledger,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(record, range(40)))
    stored = capabilities.load(ledger)
    events = [e for e in stored["concurrent"]["event_history"] if e["type"] == "match"]
    assert len(events) == 40
    assert {e["ref"] for e in events} == {f"subject-{i}" for i in range(40)}


def test_expiry_persists_retirement_and_history(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("expiring")
    cap.update(
        {
            "status": "canary",
            "expiry": 100,
            "rollback": {"transition": "shadow"},
        }
    )
    capabilities.save({"expiring": cap}, ledger)
    assert capabilities.sweep(ledger, now=100) == ["expiring"]
    raw = json.loads(ledger.read_text())["capabilities"]["expiring"]
    assert raw["status"] == "retired"
    assert raw["event_history"][-1]["to"] == "retired"


def test_expiry_retires_wired_candidates(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("wired-expiring")
    cap.update({"status": "wired", "expiry": 100, "next_transition": "retired"})
    capabilities.save({"wired-expiring": cap}, ledger)
    assert capabilities.sweep(ledger, now=100) == ["wired-expiring"]
    assert capabilities.load(ledger)["wired-expiring"]["status"] == "retired"


def test_illegal_transition_is_rejected(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("candidate")
    capabilities.save({"candidate": cap}, ledger)
    with pytest.raises(ValueError, match="illegal capability transition"):
        capabilities.transition("candidate", "active", reason="skip gates", path=ledger)


def test_outcome_event_creates_durable_edge(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("wired")
    cap.update({"status": "wired", "entrypoint": "worker.py"})
    capabilities.save({"wired": cap}, ledger)
    capabilities.heartbeat("wired", "outcome", ref="run-42", path=ledger)
    stored = capabilities.load(ledger)["wired"]
    assert stored["outcome_links"] == ["run-42"]
    assert stored["event_history"][-1]["ref"] == "run-42"


def test_out_of_order_heartbeat_cannot_regress_last_event(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("ordered")
    capabilities.save({"ordered": cap}, ledger)
    capabilities.heartbeat("ordered", "match", timestamp=200, path=ledger)
    capabilities.heartbeat("ordered", "match", timestamp=100, path=ledger)
    stored = capabilities.load(ledger)["ordered"]
    assert stored["last_match"] == 200
    assert [event["timestamp"] for event in stored["event_history"][-2:]] == [200, 100]


def test_save_rejects_malformed_active_record(tmp_path, active_capability_fixture):
    active_capability_fixture["last_match"] = None
    with pytest.raises(AssertionError, match="active capability missing last_match"):
        capabilities.save(
            {"active-fixture": active_capability_fixture},
            tmp_path / "capabilities.json",
        )


def test_activation_probe_requires_a_durable_reference(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("probe-fixture")
    cap.update({"status": "canary", "entrypoint": "worker.py"})
    capabilities.save({"probe-fixture": cap}, ledger)
    with pytest.raises(ValueError, match="requires an evidence ref"):
        capabilities.record_probe(
            "probe-fixture", "producer_probe", passed=True, ref="", path=ledger
        )
    capabilities.record_probe(
        "probe-fixture",
        "producer_probe",
        passed=True,
        ref="artifact:producer-check.json",
        timestamp=101,
        path=ledger,
    )
    stored = capabilities.load(ledger)["probe-fixture"]
    assert stored["activation_evidence"]["producer_probe"]["ref"] == (
        "artifact:producer-check.json"
    )


def test_inventory_is_generated_from_ledger(tmp_path):
    ledger = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("visible")
    capabilities.save({"visible": cap}, ledger)
    text = capabilities.format_inventory(capabilities.summary(ledger))
    assert "Source:" in text
    assert "| visible | observed |" in text


def test_existing_ledger_reconciles_new_code_declarations(tmp_path):
    ledger = tmp_path / "capabilities.json"
    capabilities.save({}, ledger)
    loaded = capabilities.load(ledger)
    assert set(capabilities.KNOWN_GATES) <= set(loaded)
    assert loaded["role-prompt"]["status"] == "wired"
    assert loaded["role-prompt"]["last_invocation"] is None


def test_liveness_classifications_use_capability_events():
    base = capabilities._blank_capability("fixture")

    gated = {**base, "status": "wired", "gate_reason": "flag off"}
    assert capabilities.classify_liveness(gated, now=100) == "deliberately_gated"

    assert capabilities.classify_liveness(base, now=100) == "no_matching_work"

    matched = {**base, "status": "wired", "last_match": 90}
    assert capabilities.classify_liveness(matched, now=100) == "matched_not_invoked"

    invoked = {**matched, "last_invocation": 95}
    assert capabilities.classify_liveness(invoked, now=100) == "invoked_without_outcomes"

    dry = {
        **invoked,
        "last_success": 96,
        "outcome_links": ["run-1"],
        "event_history": [{"type": "outcome", "timestamp": 96, "ref": "run-1"}],
    }
    assert capabilities.classify_liveness(dry, now=100) == "wired_but_dry"

    # A FRESH INVOCATION MUST NOT RE-LATCH THE MEASUREMENT-GAP LABEL. This assertion used to
    # demand "invoked_without_outcomes" here, encoding the latched-metric bug that
    # classify_liveness deliberately fixed: outcomes legitimately lag invocations (a run has to
    # merge, then survive a durability window), so "newest invocation is newer than newest
    # outcome" was unescapable for anything invoked often -- role-triage runs ~98x/week and stayed
    # flagged as a measurement gap while holding 12 linked terminal outcomes. Presence of outcome
    # evidence decides, not its recency. Do not restore the old expectation; fix the code's comment
    # first if you think this is wrong.
    reinvoked = {**dry, "last_invocation": 99}
    assert capabilities.classify_liveness(reinvoked, now=100) == "wired_but_dry"

    # ...and the same shape with the outcome evidence REMOVED is still a genuine gap, so the fix
    # did not simply delete the class.
    no_outcome_evidence = {
        **reinvoked,
        "outcome_links": [],
        "event_history": [],
        "last_success": None,
    }
    assert (
        capabilities.classify_liveness(no_outcome_evidence, now=100) == "invoked_without_outcomes"
    )

    gated_but_broken = {**gated, "last_match": 90}
    assert capabilities.classify_liveness(gated_but_broken, now=100) == "matched_not_invoked"

    stale = {**dry, "status": "active", "last_success": 1}
    assert capabilities.classify_liveness(stale, now=20 * 86400) == "stale_active"


def test_gate_blocks_execution_is_opt_in_and_narrow():
    """A gate that blocks the DELIVERING path is not a measurement gap — but only when declared.

    16 of 39 ledger capabilities carry a `gate_reason`. Simply checking `deliberately_gated` before
    `invoked_without_outcomes` would reclassify all of them, including `issue-readiness`, whose gate
    covers only its label WRITES while `classify_issue` runs every day and genuinely influences what
    the opener picks. So the rule is opt-in: absent the declaration, behaviour is unchanged.
    """
    base = capabilities._blank_capability("gated-fixture")
    base.update(
        {
            "status": "shadow",
            "gate_reason": "switch is off",
            "last_invocation": 95,
            "last_match": 90,
            "event_history": [],
        }
    )

    # Undeclared: still a measurement question, exactly as before.
    assert capabilities.classify_liveness(base, now=100) == "invoked_without_outcomes"
    # Declared: the honest answer is that it cannot run at all.
    assert (
        capabilities.classify_liveness({**base, "gate_blocks_execution": True}, now=100)
        == "deliberately_gated"
    )
    # The flag alone is not enough — it needs a real gate and a gateable status.
    assert (
        capabilities.classify_liveness(
            {**base, "gate_blocks_execution": True, "gate_reason": None}, now=100
        )
        == "invoked_without_outcomes"
    )

    # From here the check reads the LIVE ledger, which is machine-local state: `issue-readiness`
    # is registered by running the system, not by checking out the tree, so on a machine that has
    # never run it there is nothing to classify. Skip with the row named — never silently pass.
    env_prereq.require(
        env_prereq.ledger_rows_absent(
            "thompson-hybrid-routing", "range-lane-rollout", "issue-readiness", "role-triage"
        )
    )

    # `load_declared`, not `load(create=False)`: `gate_blocks_execution` is a DECLARATION-owned
    # field, so a raw read answers with whatever is on disk at that instant. Both of these rows had
    # it reconciled mid-suite on 2026-08-21 (08:15:07), which is how this file produced a red that
    # vanished on re-run. See capabilities.load_declared.
    ledger = capabilities.load_declared(capabilities.REG)
    # The two capabilities whose gate blocks the delivering path, and nothing else.
    #
    # `issue-readiness` is DELIBERATELY NOT in this set, and was removed from the expectation on
    # 2026-08-22 after it had been failing against the live ledger. capabilities.py's own comment at
    # the gate_blocks_execution check is explicit about why: its gate "covers only its LABEL WRITES
    # while the assessment runs every day and really does influence what the opener picks". Marking
    # it gate-blocking would reclassify a capability that genuinely delivers, which is the opposite
    # of what the flag is for. The test asserted the mechanism's inverse; the ledger was right.
    #
    # Membership is pinned on purpose. `gate_blocks_execution` suppresses the
    # `invoked_without_outcomes` measurement question, so a capability acquiring it silently stops
    # being asked whether its outcomes link — that must require a visible test change.
    declared = {k for k, v in ledger.items() if v.get("gate_blocks_execution")}
    assert declared == {"thompson-hybrid-routing", "range-lane-rollout"}, declared
    for cid in declared:
        assert capabilities.classify_liveness(ledger[cid]) == "deliberately_gated", cid
    # NARROWNESS: `role-triage` is gated too and must not be swept up — it runs constantly and
    # holds real outcome evidence, so this rule must leave it alone.
    triage = ledger["role-triage"]
    assert triage.get("gate_reason") and not triage.get("gate_blocks_execution"), triage
    assert triage.get("outcome_links"), "control case lost its evidence; pick another"


def test_evidence_gate_kind_is_not_blanket_observer():
    """The fix for one mislabeled capability must not hide the next real gap.

    `live-keepalive-supervisor` and `redirect-apply-bootstrap` both declared
    `matcher.kind == "evidence_gate"`, and they are opposites: the supervisor plans and reports
    ("no live action"), while the bootstrap applies a redirect plan that dispatches a run which
    terminates. So the kind does NOT carry observer-ness, and adding it to
    OBSERVER_MATCHER_KINDS — the cheap way to clear a measurement gap — would permanently excuse a
    capability that is supposed to produce outcomes. The supervisor was fixed by correcting its
    matcher to the cadence step that actually triggers it, not by widening the set.
    """
    assert "evidence_gate" not in capabilities.OBSERVER_MATCHER_KINDS
    # THE RACE THIS AVOIDS, once measured: on 2026-08-21 this test failed once inside a verify.py
    # run and passed on every re-run. `matcher` is declaration-owned, and the supervisor's was still
    # the old `evidence_gate` on disk when the suite read it — reconciliation rewrote it to
    # `tick_phase` at 08:07:28, mid-suite (the row's own `declaration_reconciled` event records it).
    # A raw `create=False` read asks "which side of that write did I land on?"; this asks the
    # question the test actually means, and still writes nothing.
    #
    # `observing` is a verdict about RECORDED HISTORY: a supervisor row that has never been
    # invoked on this machine classifies `deliberately_gated`, correctly. So the prerequisite is
    # the invocation history, and its absence is named rather than asserted around.
    env_prereq.require(
        env_prereq.ledger_rows_absent("live-keepalive-supervisor", "redirect-apply-bootstrap"),
        env_prereq.ledger_invocation_history_absent("live-keepalive-supervisor"),
    )
    ledger = capabilities.load_declared(capabilities.REG)

    supervisor = ledger["live-keepalive-supervisor"]
    assert supervisor["matcher"]["kind"] == "tick_phase", supervisor["matcher"]
    assert capabilities.is_observer(supervisor)
    assert capabilities.classify_liveness(supervisor) == "observing", supervisor

    # ...and the delivering sibling must still be answerable for an outcome.
    bootstrap = ledger["redirect-apply-bootstrap"]
    assert not capabilities.is_observer(bootstrap), bootstrap["matcher"]
    advice = capabilities.unblock(bootstrap, liveness="invoked_without_outcomes")
    assert "MEASUREMENT gap" in advice["action"], advice


def test_observers_are_not_a_measurement_gap():
    """A report cannot merge a PR, so demanding a delivery outcome from one is a category error.

    Eight of the sixteen capabilities in the `invoked_without_outcomes` bucket were cadence steps
    and feedback-event recorders: `capability-activation-audit`, `switch-review`, `feedback-store`
    and friends. None can ever produce a merged PR with a durability verdict, so that liveness class
    was a state they could never leave, and the advice attached to it — "fix outcome linkage" —
    described work that does not exist for them. Same shape as asking a transport capability a
    task_type question.

    The classification is DERIVED from `matcher.kind`, which already carries it. `target_kind` does
    not: it reads "module" for 32 of 38 capabilities and says nothing about delivery.
    """
    base = {"status": "wired", "last_invocation": 95, "event_history": []}

    observer = {**base, "matcher": {"kind": "tick_phase", "name": "x"}}
    assert capabilities.is_observer(observer)
    assert capabilities.classify_liveness(observer, now=100) == "observing"

    # ...and the advice must not send anyone chasing linkage that cannot exist.
    advice = capabilities.unblock(observer, liveness="observing")
    assert advice["feed"] is False, advice
    assert "not chase" in advice["action"].lower(), advice
    # And a delivery-linked capability must still get the linkage advice, not the observer's pass.
    delivery_advice = capabilities.unblock(
        {**base, "matcher": {"kind": "transport", "name": "offload"}},
        liveness="invoked_without_outcomes",
    )
    assert "MEASUREMENT gap" in delivery_advice["action"], delivery_advice

    # A DELIVERY-LINKED capability with no outcome is still a real gap — the fix must not swallow it.
    delivery = {**base, "matcher": {"kind": "transport", "name": "offload"}}
    assert not capabilities.is_observer(delivery)
    assert capabilities.classify_liveness(delivery, now=100) == "invoked_without_outcomes"

    # An observer that has never run is not "observing" — absence of a run is not observation.
    never = {**observer, "last_invocation": None}
    assert capabilities.classify_liveness(never, now=100) != "observing"


def test_non_gate_declarations_are_code_seeded_and_read_only(tmp_path):
    """`KNOWN_DECLARATIONS` must reseed stripped rows WITHOUT writing, and must not seed gate machinery.

    The hazard this closes: `kill_switch_category`, `control_point` and `kill_switch_rationale` were
    applied straight to the running instance's `capabilities.json`. That made them machine-local —
    green on the instance where someone typed them, absent on a fresh checkout — so the two tests
    that pinned the kill-switch exemption set could only skip on CI, and the exemption set was never
    actually reviewed anywhere. Declaration-owned means code-owned; this is the same reconciliation
    that already owns `gate_blocks_execution`.

    `KNOWN_GATES` was the wrong home for them. It seeds a status, a GATED_TTL_DAYS expiry and
    `next_transition: retired`, so registering `feedback-store` there would assert an expiry that
    does not exist — the append-only write path is not scheduled to retire. Hence a sibling table
    carrying ONLY declaration-owned fields, consumed by the SAME loop: one mechanism, two sources.
    """
    fields = ("kill_switch_category", "control_point", "kill_switch_rationale")

    # A row that exists but carries none of the declarations — a fresh checkout, or drift.
    stripped = {
        cid: {**capabilities._blank_capability(cid), "status": "observed"}
        for cid in capabilities.KNOWN_DECLARATIONS
    }
    ledger = tmp_path / "capabilities.json"
    capabilities.save(stripped, ledger)
    on_disk = json.loads(ledger.read_text())["capabilities"]
    assert not any(
        f in on_disk[cid] for cid in stripped for f in fields
    ), "fixture was not stripped"

    loaded = capabilities.load_declared(ledger)
    for cid, declared in capabilities.KNOWN_DECLARATIONS.items():
        for field, value in declared.items():
            assert loaded[cid][field] == value, (cid, field, loaded[cid].get(field))

    # READ-ONLY. `load_declared` reconciles in memory; the periodic report and the admission tests
    # both read through it and must never mutate the shared live ledger.
    after = json.loads(ledger.read_text())["capabilities"]
    assert not any(f in after[cid] for cid in stripped for f in fields), "load_declared WROTE"

    # NOT gate machinery: a non-gate declaration must not acquire an expiry or a retirement plan.
    for cid, declared in capabilities.KNOWN_DECLARATIONS.items():
        assert cid not in capabilities.KNOWN_GATES, cid
        assert not (set(declared) - set(capabilities.DECLARATION_FIELDS)), (
            f"{cid} declares non-declaration-owned fields: "
            f"{sorted(set(declared) - set(capabilities.DECLARATION_FIELDS))}"
        )
        assert loaded[cid].get("expires_at") in (None, ""), (cid, loaded[cid].get("expires_at"))

    # And a writing load must reach the same state, so the two paths cannot disagree.
    written = capabilities.load(ledger, create=True)
    for cid, declared in capabilities.KNOWN_DECLARATIONS.items():
        for field, value in declared.items():
            assert written[cid][field] == value, (cid, field)


def test_verifying_the_system_never_writes_the_live_ledger():
    """No test or selftest may take a WRITING load of the shared live capability ledger.

    `load()` defaults to `create=True`, which RECONCILES AND PERSISTS. Nine test/selftest call sites
    read the live ledger that way — they only ever read, but the write happened anyway whenever the
    code tables and the ledger disagreed. So running `verify.py` mutated
    `$ORCH_STATE_DIR/capabilities.json`, the same file the hourly tick reads.

    That is not theoretical. On 2026-08-22 a deliberate break added `offload` to
    `KNOWN_DECLARATIONS`; the suite's writing load persisted the fake exemption onto the live
    `offload` row, and it survived the revert of the source — reconciliation only rewrites rows the
    code tables still declare, so a row that LEAVES the table keeps whatever was stamped on it. A
    verification run must not be able to do that. `load_declared()` reconciles in memory and writes
    nothing, which is what every one of those readers actually wanted.
    """
    import re

    # Built by concatenation so this file's own source cannot match the scan below.
    forbidden = re.compile(r"capabilities\.load\(" + r"(?:capabilities\.)?REG\b")

    # POSITIVE CONTROL: if the pattern stops matching, the scan is vacuous and this fails first.
    assert forbidden.search("x = capabilities.load(" + "capabilities.REG)")
    assert forbidden.search("x = capabilities.load(" + "REG)")
    assert not forbidden.search("x = capabilities.load_declared(" + "capabilities.REG)")

    root = pathlib.Path(__file__).resolve().parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not (path.name.startswith("test_") or "_selftest" in text):
            continue  # production code may legitimately persist; verification may not.
        for num, line in enumerate(text.splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{path.name}:{num}: {line.strip()}")
    assert not offenders, (
        "verification code takes a writing load of the live ledger; use load_declared():\n  "
        + "\n  ".join(offenders)
    )


def test_no_tick_producer_runs_above_the_heartbeat_export():
    """A heartbeat-emitting module must not be invoked before heartbeats are switched on.

    `capabilities.production_heartbeat` returns False immediately unless ORCH_CAPABILITY_HEARTBEATS=1
    is in the child's environment, and only orchestrate.sh exports it. Measured 2026-08-22: the
    export sat 19 lines BELOW `frontend_verify.py --doctor`, the frontend-verifier capability's only
    tick caller, and below `capacity.py`. Both reached their heartbeat call and recorded nothing, so
    `frontend-verifier` read `never fired` while working, and `windowed-capacity-policy`'s declared
    cadence ("capacity.build at the top of the tick") was false.

    Reachability could not see this — `capability_activation_audit.heartbeat_reachable` reported
    frontend-verifier `reachable`, correctly, because the CALL is reachable. Enablement is a
    separate question and this is the check that owns it.
    """
    import capability_activation_audit as audit

    # POSITIVE CONTROL, on synthetic text, so a parse that finds nothing cannot read as clean.
    control = audit.shell_heartbeat_gate(
        'python3 "$ORCH/early.py" --run\n'
        "export ORCH_CAPABILITY_HEARTBEATS=1\n"
        'python3 "$ORCH/late.py" --run\n'
    )
    assert [m for _, m in control["before"]] == ["early.py"], control
    assert [m for _, m in control["after"]] == ["late.py"], control

    gate = audit.heartbeat_env_gate()
    assert gate["anchor_present"], (
        f"orchestrate.sh no longer carries `{audit.HEARTBEAT_EXPORT_ANCHOR}`; the stored switch "
        "criteria cite that anchor because line numbers rot"
    )
    # BOTH numbers, per the latched-gate runtime rule: zero suppressed means nothing only if the
    # parse actually found invocations to classify.
    assert (
        gate["invocations_after"] > 0
    ), f"parsed no post-export invocations at all, so `suppressed_modules` proves nothing: {gate}"
    assert gate["suppressed_modules"] == [], (
        "these modules emit a capability heartbeat but are invoked above the "
        f"{audit.HEARTBEAT_ENV_FLAG} export, so they record nothing: "
        f"{gate['suppressed_by_driver']}"
    )
