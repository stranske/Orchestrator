from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_admission_continuity as continuity

TARGET = "owner/repo#12"
HEAD = "a" * 40
MERGE = "b" * 40


def snapshot(**changes):
    return {
        "target": TARGET,
        "pr_target": TARGET,
        "state": "MERGED",
        "head": HEAD,
        "merge": MERGE,
        "association_verified": True,
        **changes,
    }


def run(*, fresh=None, decision=None, old=None):
    return continuity.plan(
        {TARGET: old or {"target": TARGET, "owner": "closer"}},
        [],
        observe=lambda t: fresh or snapshot(),
        policy=lambda t, s: decision or {"owner": "closer", "held": False, "dependency": None},
        now=10,
    )


def test_merge_and_closed_source_do_not_retire():
    result = run(fresh=snapshot(source_state="CLOSED", workflow_conclusion="success"))
    assert result["rows"][0]["execution_state"] == "active"
    assert result["rows"][0]["merge"] == MERGE
    assert result["retired"] == []
    assert result["carry"][TARGET]["owner"] == "closer"


def test_no_native_merge_receipt_needed_for_external_merge_admission():
    result = run(decision={"owner": "orphan", "held": False, "dependency": None})
    assert result["rows"][0]["lane"] == "orphan"
    assert result["rows"][0]["pr_target"] == TARGET


@pytest.mark.parametrize(
    "changes",
    [
        {"association_verified": False},
        {"head": None},
        {"merge": None},
        {"pr_target": "owner/repo#13"},
    ],
)
def test_changed_or_incomplete_identity_cannot_activate(changes):
    result = run(
        fresh=snapshot(**changes), old={"target": TARGET, "owner": "closer", "pr_target": TARGET}
    )
    assert result["failures"]
    assert all(r["execution_state"] == "held" for r in result["rows"])


@pytest.mark.parametrize(
    "decision",
    [
        {"owner": "closer", "held": True, "dependency": None},
        {"owner": "closer", "held": False, "dependency": "source wait"},
        {"owner": None, "held": False, "dependency": None},
    ],
)
def test_current_hold_dependency_or_owner_removal_wins(decision):
    assert run(decision=decision)["rows"][0]["execution_state"] == "held"


def test_only_exact_authenticated_final_resolution_retires():
    final = {**snapshot(), "verified": True, "status": "resolved"}
    assert run(fresh=snapshot(final_resolution=final))["retired"]
    assert run(fresh=snapshot(final_resolution={**final, "head": "c" * 40}))["rows"]


def test_failed_item_does_not_hold_independent_item():
    result = continuity.plan(
        {TARGET: {"owner": "closer"}, "owner/repo#13": {"owner": "closer"}},
        [],
        observe=lambda t: snapshot(target=t, pr_target=t, association_verified=t != TARGET),
        policy=lambda t, s: {"owner": "closer", "held": False, "dependency": None},
        now=10,
    )
    assert {r["target"]: r["execution_state"] for r in result["rows"]} == {
        TARGET: "held",
        "owner/repo#13": "active",
    }


def test_existing_route_and_current_hold_are_never_overwritten():
    routes = {"closer": [{"target": TARGET, "execution_state": "held"}], "orphan": []}
    observations = {TARGET: {"target": TARGET, "execution_state": "held"}}
    ownership = {}
    continuity.merge_plan(run(), routes, observations, ownership, [])
    assert len(routes["closer"]) == 1
    assert observations[TARGET]["execution_state"] == "held"


def renderer():
    path = Path(__file__).parents[1] / "scripts/render_closer_admission_hook.py"
    spec = importlib.util.spec_from_file_location("render_admission", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renderer_stages_hooks_before_sole_writer_and_refuses_drift(tmp_path):
    module = Path(continuity.__file__)
    renderer_module = renderer()
    # Minimal actual controller call sequence; no GitHub/runtime effects.
    original = """import hashlib
from pathlib import Path
def fingerprint(pr, sig):
    value = []
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
def main(argv=None):
    with lock:
        routes, observations, waits, ignored = build_routes(
            prs, state)
        ownership = reconcile_ownership()
        records, recoveries, human_due, exclusions = accountability.evaluate(
            observations)
        if not errors:
            state["targets"] = observations
"""
    pin = hashlib.sha256(module.read_bytes()).hexdigest()
    rendered = renderer_module.render(original, module, pin)
    assert rendered.index("prepare_controller(") < rendered.index("= build_routes(")
    assert rendered.index("reconcile_ownership()") < rendered.index(".merge_plan(")
    assert rendered.index(".merge_plan(") < rendered.index("accountability.evaluate(")
    assert 'state["delivery_continuity"] = continuity["carry"]' in rendered
    assert original.count("delivery_continuity") == 0
    with pytest.raises(ValueError, match="hash"):
        renderer_module.render(original, module, "0" * 64)
    with pytest.raises(ValueError, match="shape"):
        renderer_module.render(rendered, module, pin)


def test_controller_adapter_uses_fresh_exact_query_and_normal_orphan_owner():
    seen = []
    node = {
        "__typename": "PullRequest",
        "number": 12,
        "repository": {"nameWithOwner": "owner/repo"},
        "state": "MERGED",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE},
    }

    def read(argv, timeout):
        import json

        query = argv[argv.index("-f") + 1]
        assert query.count("{") == query.count("}")
        seen.append(argv)
        return 0, json.dumps({"data": {"repository": {"issueOrPullRequest": node}}}), ""

    router = SimpleNamespace(
        QUERY="query{search{nodes{... on PullRequest{number repository{nameWithOwner} headRefOid}}}}",
        inventory=SimpleNamespace(
            repo=lambda n: n["repository"]["nameWithOwner"],
            run=read,
            scoped_blocker_states=lambda: (set(), set(), set()),
        ),
        pr_owner=lambda n: "orphan",
        build_routes=lambda *a: (
            {"orphan": [{"target": TARGET}]},
            {TARGET: {"execution_state": "active", "action": "normal", "fingerprint": "normal-fp"}},
            [],
            [],
        ),
    )
    result = continuity.prepare_controller(
        router, {"targets": {TARGET: {"head": HEAD, "delivery_owner": "orphan"}}}, {"nodes": []}, 10
    )
    assert len(seen) == 1
    assert result["rows"][0]["lane"] == "orphan"


def enrollment_world(tmp_path):
    import json

    state = tmp_path / "demand-state.json"
    state.write_text(json.dumps({"targets": {}, "delivery_ownership": {}, "unrelated": "preserve"}))
    proof = tmp_path / "frozen-locator.json"
    proof.write_text('{"target":"owner/repo#12"}')
    candidates = [
        {
            "target": TARGET,
            "pr_target": TARGET,
            "provenance_refs": [
                {"path": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
            ],
        }
    ]
    return state, proof, candidates


def test_backfill_enrollment_only_locators_then_fresh_policy_admission(tmp_path):
    import json

    state, _, candidates = enrollment_world(tmp_path)
    prepared = continuity.plan_enrollment(state, candidates)
    assert json.loads(state.read_text())["targets"] == {}
    continuity.apply_enrollment(state, prepared)
    actual = json.loads(state.read_text())
    assert actual["targets"] == {} and actual["delivery_ownership"] == {}
    assert actual["unrelated"] == "preserve"
    result = continuity.plan(
        actual["delivery_continuity"],
        [],
        observe=lambda t: snapshot(),
        policy=lambda *a: {"owner": "orphan", "held": False, "dependency": None},
        now=10,
    )
    assert result["rows"][0]["lane"] == "orphan"
    assert "execution_state" not in actual["delivery_continuity"][TARGET]


def test_enrollment_refuses_cas_race_provenance_drift_and_forged_authority(tmp_path):
    import copy
    import json

    state, proof, candidates = enrollment_world(tmp_path)
    prepared = continuity.plan_enrollment(state, candidates)
    initial = state.read_bytes()
    state.write_text(json.dumps({"changed": True}))
    with pytest.raises(ValueError, match="CAS"):
        continuity.apply_enrollment(state, prepared)
    state.write_bytes(initial)
    forged = copy.deepcopy(prepared)
    forged["locators"][TARGET]["execution_state"] = "active"
    with pytest.raises(ValueError, match="authority"):
        continuity.apply_enrollment(state, forged)
    assert state.read_bytes() == initial
    proof.write_text("changed")
    with pytest.raises(ValueError, match="provenance"):
        continuity.apply_enrollment(state, prepared)
    assert state.read_bytes() == initial


def test_enrollment_does_not_steal_controller_lock(tmp_path):
    import fcntl

    state, _, candidates = enrollment_world(tmp_path)
    prepared = continuity.plan_enrollment(state, candidates)
    initial = state.read_bytes()
    with (tmp_path / ".demand.lock").open("a+") as incumbent:
        fcntl.flock(incumbent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            continuity.apply_enrollment(state, prepared)
    assert state.read_bytes() == initial


def test_enrollment_enriches_missing_delivery_locator_preserving_provenance(tmp_path):
    import json

    state, _, candidates = enrollment_world(tmp_path)
    state.write_text(
        json.dumps(
            {
                "delivery_continuity": {
                    TARGET: {
                        "target": TARGET,
                        "pr_target": None,
                        "owner": "orphan",
                        "provenance": {"original": True},
                    }
                }
            }
        )
    )
    continuity.apply_enrollment(state, continuity.plan_enrollment(state, candidates))
    row = json.loads(state.read_text())["delivery_continuity"][TARGET]
    assert row["pr_target"] == TARGET
    assert row["owner"] == "orphan" and row["provenance"] == {"original": True}
    assert row["enrollment_provenance"][0]["refs"]


def actual_controller_functions(*, rendered=False):
    """Compile production pure functions only; never execute controller imports/main."""
    import ast
    import datetime
    import json

    source = (Path(__file__).parent / "fixtures/continuity_controller_contract.py.txt").read_text()
    if rendered:
        module = Path(continuity.__file__)
        source = renderer().render(source, module, hashlib.sha256(module.read_bytes()).hexdigest())
    tree = ast.parse(source)
    functions = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in {"signals", "fingerprint", "build_routes"}
    ]
    inventory = SimpleNamespace(
        repo=lambda n: n["repository"]["nameWithOwner"],
        labels=lambda n: [x["name"] for x in n.get("labels", {}).get("nodes", [])],
        iso_now=lambda: "2026-10-22T00:00:00Z",
        parse_iso=lambda value: (
            datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            if value
            else None
        ),
    )
    namespace = {
        "hashlib": hashlib,
        "json": json,
        "inventory": inventory,
        "STALL_SECONDS": 10800,
        "LANES": {
            "closer": None,
            "orphan": None,
            "escalator": None,
            "maintenance": None,
            "opener": None,
        },
        "classify": lambda *a: ("orphan", "normal exact-head gates", {}),
        "pr_owner": lambda pr: "orphan",
        "issue_owner": lambda item: "closer",
    }
    exec(
        compile(
            ast.Module(body=functions, type_ignores=[]), "actual-demand-router-functions", "exec"
        ),
        namespace,
    )
    return SimpleNamespace(**namespace)


def test_actual_controller_coverage_parks_continuity_and_body_change_reenters():
    import json

    router = actual_controller_functions()
    node = {
        "__typename": "PullRequest",
        "number": 12,
        "repository": {"nameWithOwner": "owner/repo"},
        "state": "MERGED",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE},
        "body": "original request",
    }
    router.QUERY = (
        "query{search{nodes{... on PullRequest{number repository{nameWithOwner} headRefOid body}}}}"
    )
    router.inventory.scoped_blocker_states = lambda: (set(), set(), set())
    router.inventory.run = lambda *a, **kw: (
        0,
        json.dumps({"data": {"repository": {"issueOrPullRequest": node}}}),
        "",
    )
    fp = router.fingerprint(node, {})
    state = {"targets": {TARGET: {"head": HEAD, "delivery_owner": "orphan", "fingerprint": fp}}}
    terminal = {
        "coverage_json": {
            TARGET: {
                "head": HEAD,
                "fingerprint": fp,
                "execution_error": None,
                "park_until_material_change": True,
            }
        }
    }
    result = continuity.prepare_controller(router, state, {"nodes": []}, 10, terminal)
    assert result["rows"][0]["execution_state"] == "held"
    assert result["rows"][0]["fingerprint"] == fp
    node["body"] = "changed original requirement"
    changed = continuity.prepare_controller(router, state, {"nodes": []}, 11, terminal)
    assert changed["rows"][0]["execution_state"] == "active"
    assert changed["rows"][0]["fingerprint"] != fp


def test_rendered_actual_controller_report_wakes_but_discussion_and_source_pause_do_not():
    import json

    router = actual_controller_functions(rendered=True)
    node = {
        "__typename": "PullRequest",
        "number": 12,
        "repository": {"nameWithOwner": "owner/repo"},
        "state": "MERGED",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE},
        "mergedAt": "2026-10-21T00:00:00Z",
        "body": "original",
        "comments": {"nodes": []},
        "closingIssuesReferences": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    router.QUERY = (
        "query{search{nodes{... on PullRequest{number repository{nameWithOwner} headRefOid body}}}}"
    )
    router.inventory.scoped_blocker_states = lambda: (set(), set(), set())
    router.inventory.run = lambda *a, **kw: (
        0,
        json.dumps({"data": {"repository": {"issueOrPullRequest": node}}}),
        "",
    )
    fp = router.fingerprint(node, {})
    state = {"targets": {TARGET: {"head": HEAD, "delivery_owner": "orphan", "fingerprint": fp}}}
    terminal = {
        "coverage_json": {
            TARGET: {
                "head": HEAD,
                "fingerprint": fp,
                "execution_error": None,
                "park_until_material_change": True,
            }
        }
    }
    baseline = continuity.prepare_controller(router, state, {"nodes": []}, 10, terminal)
    state["delivery_continuity"] = baseline["carry"]
    node["comments"]["nodes"].append(
        {
            "id": "discussion",
            "body": "status update",
            "createdAt": "2026-10-22T00:00:00Z",
            "author": {"login": "github-actions[bot]"},
        }
    )
    unrelated = continuity.prepare_controller(router, state, {"nodes": []}, 11, terminal)
    assert unrelated["rows"][0]["execution_state"] == "held"
    node["comments"]["nodes"].append(
        {
            "id": "report",
            "body": "## Provider Comparison Report\nCONCERNS",
            "createdAt": "2026-10-22T00:00:00Z",
            "author": {"login": "github-actions[bot]"},
        }
    )
    changed = continuity.prepare_controller(router, state, {"nodes": []}, 12, terminal)
    assert changed["rows"][0]["execution_state"] == "active"
    assert changed["rows"][0]["fingerprint"] != fp
    state["delivery_continuity"] = changed["carry"]
    newfp = changed["rows"][0]["fingerprint"]
    state["targets"][TARGET]["fingerprint"] = newfp
    terminal["coverage_json"][TARGET]["fingerprint"] = newfp
    unchanged = continuity.prepare_controller(router, state, {"nodes": []}, 13, terminal)
    assert unchanged["rows"][0]["execution_state"] == "held"
    state["targets"][TARGET] = unchanged["rows"][0]
    state["delivery_continuity"] = unchanged["carry"]
    rotated = continuity.prepare_controller(
        router, state, {"nodes": []}, 14, {"selected_target": "owner/repo#99"}
    )
    assert rotated["rows"][0]["execution_state"] == "held"
    assert rotated["rows"][0]["unchanged_since"] == unchanged["rows"][0]["unchanged_since"]
    assert rotated["rows"][0]["diagnosed_fingerprint"] == unchanged["rows"][0]["fingerprint"]
    node["closingIssuesReferences"]["nodes"] = [
        {
            "number": 11,
            "repository": {"nameWithOwner": "owner/repo"},
            "state": "CLOSED",
            "labels": {"nodes": [{"name": "agents:paused"}]},
        }
    ]
    paused = continuity.prepare_controller(router, state, {"nodes": []}, 14, {})
    assert paused["rows"][0]["execution_state"] == "held"


def test_rendered_integrity_failure_parks_own_carry_without_global_error(tmp_path):
    import ast
    import json

    module = tmp_path / "module.py"
    module.write_text("# valid pin at render time\n")
    original = (Path(__file__).parent / "fixtures/continuity_controller_contract.py.txt").read_text()
    rendered = renderer().render(original, module, hashlib.sha256(module.read_bytes()).hexdigest())
    tree = ast.parse(rendered)
    helper = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_continuity_held"
    ]
    namespace = {}
    exec(
        compile(ast.Module(body=helper, type_ignores=[]), "actual-rendered-fallback", "exec"),
        namespace,
    )
    state = {
        "targets": {TARGET: {"head": HEAD, "delivery_owner": "orphan"}},
        "delivery_ownership": {},
    }
    result = namespace["_continuity_held"](state, "source changed")
    assert result["rows"][0]["execution_state"] == "held"
    assert 'errors.append("Delivery continuity' not in rendered
    assert "warnings.append" in rendered
    assert json.dumps(state) == json.dumps(
        {"targets": {TARGET: {"head": HEAD, "delivery_owner": "orphan"}}, "delivery_ownership": {}}
    )


def test_actual_rendered_main_malformed_carry_keeps_unrelated_open_assignment(
    tmp_path, monkeypatch
):
    import argparse
    import ast
    import copy
    import fcntl
    import json
    import sys
    import time
    import types

    module_path = Path(continuity.__file__)
    contract = (Path(__file__).parent / "fixtures/continuity_controller_contract.py.txt").read_text()
    rendered = renderer().render(
        contract, module_path, hashlib.sha256(module_path.read_bytes()).hexdigest()
    )
    fake = types.ModuleType("_inert_actual_router")
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    root = tmp_path / "controller"
    root.mkdir()
    open_target = "owner/repo#13"
    node = {"number": 13, "repository": {"nameWithOwner": "owner/repo"}, "headRefOid": HEAD}
    state = {
        "delivery_continuity": {TARGET: 7},
        "targets": {},
        "delivery_ownership": {TARGET: {"owner": "orphan"}},
    }
    saved = {}
    lanes = {name: None for name in ["opener", "closer", "orphan", "escalator", "maintenance"]}
    actual = {
        name: {"model": "test", "reasoning_effort": "low", "status": "ACTIVE", "rrule": "unchanged"}
        for name in lanes
    }
    inventory = SimpleNamespace(
        repo=lambda n: n["repository"]["nameWithOwner"],
        ROOT=root.parent,
        labels=lambda node: [],
        scoped_blocker_states=lambda: (set(), set(), set()),
        parse_iso=lambda value: None,
        iso_now=lambda: "2026-10-22T00:00:00Z",
    )
    namespace = {
        "__name__": fake.__name__,
        "argparse": argparse,
        "fcntl": fcntl,
        "hashlib": hashlib,
        "json": json,
        "Path": Path,
        "time": time,
        "LANES": lanes,
        "ROOT": root,
        "STATE": root / "demand-state.json",
        "PLAN": root / "demand-plan.json",
        "STALL_SECONDS": 10800,
        "inventory": inventory,
        "load": lambda path, default: (
            copy.deepcopy(state) if path.name == "demand-state.json" else {}
        ),
        "save": lambda path, value: saved.__setitem__(path.name, copy.deepcopy(value)),
        "app_settings": lambda: actual,
        "collect": lambda: ({"nodes": [node], "issueCount": 1}, {"nodes": []}, {}, []),
        "running_lanes": lambda: set(),
        "running_lane_owners": lambda running: {},
        "classify": lambda *args: ("orphan", "normal open work", {}),
        "pr_owner": lambda pr: "orphan",
        "issue_owner": lambda item: "closer",
        "reconcile_ownership": lambda prs, issues, guard, routes, observations, now: ({}, []),
        "maintenance": SimpleNamespace(
            budget=SimpleNamespace(summary=lambda: {}),
            read_control=lambda: {},
            existing_worker=lambda: None,
        ),
        "maintenance_admissions": lambda *args: [],
        "accountability": SimpleNamespace(evaluate=lambda *args: ({}, [], [], [])),
        "ownership_gaps": lambda *args: [],
        "ownership_coverage_warnings": lambda gaps: [],
        "repo_maintenance_admissions": lambda *args: {"assignments": [], "errors": []},
        "error_freeze": lambda *args: {"lapsed": False},
        "desired_settings": lambda *args: actual,
        "error_freeze_message": lambda value: None,
        "frozen_lanes_message": lambda value: None,
        "budget_alerts": lambda *args: None,
        "write_operational_memory": lambda value: None,
    }
    fake.__dict__.update(namespace)
    exec(compile(ast.parse(rendered), "actual-rendered-router", "exec"), fake.__dict__)
    assert fake.main([]) == 0
    assert saved["demand-plan.json"]["errors"] == []
    assert saved["demand-plan.json"]["warnings"]
    assert [r["target"] for r in saved["orphan-assignment.json"]["targets"]] == [open_target]
    assert saved["demand-state.json"]["delivery_continuity"][TARGET] == 7
    held = saved["demand-plan.json"]["waiting"]
    assert any(r["target"] == TARGET and r["execution_state"] == "held" for r in held)


@pytest.mark.parametrize("which", ["challenge", "retry", "diagnosed_escalator"])
def test_source_alias_never_reroutes_challenged_or_ignored_delivery_owner(which):
    import json

    router = actual_controller_functions(rendered=True)
    source_target = "owner/repo#11"
    source = {
        "__typename": "Issue",
        "number": 11,
        "repository": {"nameWithOwner": "owner/repo"},
        "state": "CLOSED",
        "labels": {"nodes": []},
    }
    node = {
        "__typename": "PullRequest",
        "number": 12,
        "repository": {"nameWithOwner": "owner/repo"},
        "state": "MERGED",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE},
        "closingIssuesReferences": {"nodes": [source]},
        "body": "source request",
    }
    router.QUERY = (
        "query{search{nodes{... on PullRequest{number repository{nameWithOwner} headRefOid body}}}}"
    )

    def read(argv, **kwargs):
        result = source if argv[-1] == "number=11" else node
        return 0, json.dumps({"data": {"repository": {"issueOrPullRequest": result}}}), ""

    router.inventory.run = read
    router.pr_owner = lambda node: "orphan"
    # Existing actual build_routes retains an escalator classification; the
    # ordinary owner must not be reactivated by continuity's source alias.
    router.build_routes.__globals__["classify"] = lambda *args: (
        "escalator",
        "challenge current delivery",
        {},
    )
    state = {
        "delivery_continuity": {
            source_target: {"target": source_target, "pr_target": TARGET, "owner": "orphan"}
        },
        "targets": {source_target: {"head": HEAD, "delivery_owner": "orphan"}},
    }
    fp = router.fingerprint(node, {})
    terminal = (
        {
            "coverage_json": {
                source_target: {"head": HEAD, "fingerprint": fp, "execution_error": None}
            }
        }
        if which == "diagnosed_escalator"
        else {}
    )
    context = (
        set(),
        {TARGET} if which != "retry" else set(),
        {TARGET} if which == "retry" else set(),
    )
    result = continuity.prepare_controller(router, state, {"nodes": []}, 10, terminal, context)
    assert result["rows"][0]["target"] == source_target
    assert result["rows"][0]["execution_state"] == "held"
    assert result["rows"][0]["lane"] == "orphan"


def test_unowned_backfill_read_failure_retains_visible_requirement_without_fake_owner():
    def unavailable(target):
        raise RuntimeError("read timed out")

    result = continuity.plan(
        {TARGET: {"target": TARGET, "pr_target": TARGET}},
        [],
        observe=unavailable,
        policy=lambda *args: {},
        now=10,
    )
    assert result["rows"] == []
    failure = result["carry"][TARGET]["last_failure"]
    assert failure["reason"] == "read timed out"
    assert failure["requirement"] and failure["next_trigger"]
    assert result["failures"][0]["target"] == TARGET


def test_enrollment_state_and_provenance_reads_have_explicit_bounds(tmp_path, monkeypatch):
    state, proof, candidates = enrollment_world(tmp_path)
    monkeypatch.setattr(continuity, "MAX_DEMAND_STATE_BYTES", 8)
    with pytest.raises(ValueError, match="JSON size"):
        continuity.plan_enrollment(state, candidates)
    monkeypatch.setattr(continuity, "MAX_DEMAND_STATE_BYTES", 1024)
    monkeypatch.setattr(continuity, "MAX_PROVENANCE_BYTES", 8)
    with pytest.raises(ValueError, match="provenance.*bound"):
        continuity.plan_enrollment(state, candidates)
    # Huge-file refusal uses metadata before opening or reading payload bytes.
    with proof.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    with pytest.raises(ValueError, match="provenance.*bound"):
        continuity.plan_enrollment(state, candidates)


# Retained normal production classify contract (source e7bf8a31), no effects.
_NORMAL_CLASSIFY_SOURCE = 'def classify(pr, held, challenge, retry, now):\n    target = f"{inventory.repo(pr)}#{pr[\'number\']}"\n    sig = signals(pr)\n    attention = bool(set(inventory.labels(pr)) & {"agent:needs-attention", "autofix:escalated"})\n    if target in held:\n        return "held", "Confirmed human gate or unexpired scoped blocker", sig\n    if target in challenge or target in retry:\n        return "escalator", "Challenge unverified human claim or recover expired blocker", sig\n    draft_hold = pr.get("isDraft") and re.search(r"keep this pr.*draft|hold:|keep.*draft and unmerged", pr.get("body", ""), re.I)\n    if draft_hold and not sig["threads"]:\n        return "held", "Explicit draft hold; source evaluation prerequisites remain with their owner", sig\n    if pr.get("isDraft") and not attention:\n        return "held", "Draft retained until readiness or attention changes", sig\n    if not inventory.is_release_pr(pr) and inventory.is_generated_or_durable_pr(pr):\n        return "held", "Durable generated publication", sig\n    lane = "maintenance" if inventory.maintenance_category(pr) or maintenance.repair_policy(target, maintenance.budget.summary()) else (\n        "orphan" if inventory.is_release_pr(pr) or not inventory.is_issue_linked_pr(pr) else "closer")\n    if sig["failed"] or sig["threads"] or sig["incomplete"] or attention or pr.get("mergeStateStatus") == "DIRTY":\n        return lane, ("Classify existing failures against this shipment; deliver unaffected work and hand off incidental findings; no automatic source repair" if lane == "maintenance" else "Repair failed checks, conflicts, review debt, or diagnose incomplete evidence"), sig\n    pushed = inventory.parse_iso(sig["pushed_at"] or "")\n    if pushed is not None and now - pushed < 7 * 60:\n        return "waiting", "Await exact-head review floor; Luna rechecks readiness", sig\n    if sig["pending"]:\n        return "waiting", "Await checks; Luna observes completion", sig\n    return lane, "Inspect exact-head gates and merge, close, or record a concrete blocker", sig'


@pytest.mark.parametrize(
    "label,confirmed,expected",
    [
        ("needs-human", False, "active"),
        ("needs-human", True, "held"),
        ("hold", False, "held"),
        ("do-not-merge", False, "held"),
        ("agents:paused", False, "held"),
    ],
)
def test_normal_controller_machine_human_label_is_not_confirmed_human_hold(
    label, confirmed, expected
):
    import json
    import re

    router = actual_controller_functions(rendered=True)
    namespace = router.build_routes.__globals__
    router.inventory.is_release_pr = lambda node: False
    router.inventory.is_generated_or_durable_pr = lambda node: False
    router.inventory.maintenance_category = lambda node: False
    router.inventory.is_issue_linked_pr = lambda node: True
    namespace.update(
        re=re,
        maintenance=SimpleNamespace(
            repair_policy=lambda *args: None, budget=SimpleNamespace(summary=lambda: {})
        ),
    )
    exec(compile(_NORMAL_CLASSIFY_SOURCE, "normal-production-classify", "exec"), namespace)
    namespace["pr_owner"] = lambda node: "closer"
    router.pr_owner = namespace["pr_owner"]
    node = {
        "__typename": "PullRequest",
        "number": 12,
        "repository": {"nameWithOwner": "owner/repo"},
        "state": "MERGED",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE},
        "body": "Issue-linked original request",
        "labels": {"nodes": [{"name": label}]},
    }
    router.QUERY = (
        "query{search{nodes{... on PullRequest{number repository{nameWithOwner} headRefOid body}}}}"
    )
    router.inventory.run = lambda *a, **kw: (
        0,
        json.dumps({"data": {"repository": {"issueOrPullRequest": node}}}),
        "",
    )
    state = {"targets": {TARGET: {"head": HEAD, "delivery_owner": "closer"}}}
    result = continuity.prepare_controller(
        router, state, {"nodes": []}, 10, {}, ({TARGET} if confirmed else set(), set(), set())
    )
    assert result["rows"][0]["execution_state"] == expected
    assert result["rows"][0]["lane"] == "closer"
