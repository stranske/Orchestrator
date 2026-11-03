"""Internal necessary read plans reuse real validators/Store, never model plans."""

import copy

import pytest
from test_closer_evidence_recovery import LogGitHub, log_request
from test_closer_pre_model_readiness import code_only_packet

import closer_evidence as evidence
import closer_evidence_recovery as recovery
import closer_semantic_assessment as semantic


def screen_for(tmp_path, *, source_missing=False):
    snap, packet = code_only_packet(tmp_path)
    if source_missing:
        snap["sources"] = []
    screen = evidence.action_evidence_readiness(
        snap, packet, "collect_evidence", retained_dir=tmp_path / "code-only"
    )
    return snap, packet, screen


def test_current_necessary_readiness_rail_has_valid_internal_plan_no_model_fields(tmp_path):
    snap, packet, screen = screen_for(tmp_path, source_missing=True)
    plan = semantic.host_collection_plan(snap, packet, screen)
    requests = recovery.validate_requests(plan, snap, packet, screen)
    assert requests
    assert all(
        row["necessity"] == "necessary" and row["origin"] == "readiness_rail" for row in requests
    )
    assert all(row["section"] in recovery.SECTIONS for row in requests)
    assert plan["authority"] is False
    assert not {"classification", "substantive_needs", "decision", "findings"} & set(plan)


def test_optional_enrichment_never_promoted_by_semantic_gap(tmp_path):
    snap, packet, screen = screen_for(tmp_path)
    assert screen["evaluation_ready"]
    assert any(row["necessity"] != "necessary" for row in screen["requirements"])
    plan = semantic.host_collection_plan(snap, packet, screen)
    assert plan["evidence_requests"] == []
    basis = {"source": snap["sources"][0]["target"], "criterion": snap["sources"][0]["body"]}
    assert (
        semantic.host_collection_plan(snap, packet, screen, basis=basis)["evidence_requests"] == []
    )
    optional = next(
        row
        for row in screen["requirements"]
        if row["necessity"] != "necessary" and row["section"] in recovery.SECTIONS
    )
    forged_upgrade = {**optional, "necessity": "necessary"}
    with pytest.raises(ValueError, match="optional enrichment"):
        semantic.host_collection_plan(snap, packet, screen, existing_requests=[forged_upgrade])


def test_existing_exact_original_job_request_is_revalidated_and_basis_bound(tmp_path):
    snap, packet, screen = screen_for(tmp_path)
    original = log_request(snap)
    normalized = recovery.validate_requests({"evidence_requests": [original]}, snap, packet, screen)
    basis = original["basis"]
    plan = semantic.host_collection_plan(
        snap, packet, screen, existing_requests=normalized, basis=basis
    )
    assert recovery.validate_requests(plan, snap, packet, screen) == normalized
    assert plan["evidence_requests"][0]["locator"] == {"kind": "job_log", "job_id": 88}
    wrong = {"source": "foreign/repo#4", "criterion": "A foreign source obligation"}
    with pytest.raises(ValueError, match="basis_unavailable"):
        semantic.host_collection_plan(
            snap, packet, screen, existing_requests=normalized, basis=wrong
        )


@pytest.mark.parametrize(
    "fault", ["arbitrary_locator", "missing_basis", "foreign_basis", "missing_question"]
)
def test_existing_request_cannot_bypass_old_locator_and_original_scope_guards(tmp_path, fault):
    snap, packet, screen = screen_for(tmp_path)
    original = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, packet, screen
    )[0]
    if fault == "arbitrary_locator":
        original["locator"]["path"] = "/unbounded"
    elif fault == "missing_basis":
        original["basis"] = {}
    elif fault == "foreign_basis":
        original["basis"] = {"source": "foreign/repo#4", "criterion": "foreign"}
    else:
        original["question"] = ""
    with pytest.raises(ValueError):
        semantic.host_collection_plan(snap, packet, screen, existing_requests=[original])


def test_old_store_blocks_unchanged_no_progress_and_ignores_clock_rotation(tmp_path):
    snap, packet, screen = screen_for(tmp_path, source_missing=True)
    plan = semantic.host_collection_plan(snap, packet, screen)
    requests = recovery.validate_requests(plan, snap, packet, screen)
    store = recovery.RecoveryStore(tmp_path / "journal")
    first = store.begin(snap, packet, screen)
    assert first
    store.finish(
        snap,
        first,
        prep={"packet_ref": {"sha256": "component-identity-only"}},
        screen=screen,
        requests=requests,
        outcome="no_progress",
        causes=[],
        plan=plan,
        actual={},
    )
    assert store.begin(snap, packet, screen) is None
    changed_clock = copy.deepcopy(packet)
    changed_clock["sections"]["elapsed"]["sha256"] = "a different wall-clock observation"
    assert store.begin(snap, changed_clock, screen) is None
    assert store.latest(snap)["plan_actual"].get("run_id") is None


@pytest.mark.parametrize("wrong", [None, "head", "pr"])
def test_real_existing_collector_binds_exact_job_read_to_original_delivery(tmp_path, wrong):
    snap, packet, screen = screen_for(tmp_path)
    normalized = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, packet, screen
    )
    plan = semantic.host_collection_plan(snap, packet, screen, existing_requests=normalized)
    selected = recovery.validate_requests(plan, snap, packet, screen)
    fake = LogGitHub(snap, wrong_head=wrong == "head", wrong_pr=wrong == "pr")
    rows = recovery.collect_job_logs(fake, snap, selected, tmp_path / "logs")
    assert len(rows) == 1
    assert rows[0]["status"] == ("complete" if wrong is None else "unavailable")
    if wrong is not None:
        assert not any(path.endswith("/logs") for path in fake.reads)


def test_exact_collection_branch_uses_old_store_before_read_port_and_no_model_route(tmp_path):
    import ast
    import time
    from pathlib import Path

    import closer_dispatcher as closer

    snap, packet, screen = screen_for(tmp_path, source_missing=True)
    root = ast.parse(Path(closer.__file__).read_text())
    outer = next(
        node
        for node in root.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_staged_pipeline"
    )
    interpret = next(
        node
        for node in outer.body
        if isinstance(node, ast.FunctionDef) and node.name == "interpret"
    )
    branch = next(
        node
        for node in interpret.body
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Attribute) and child.attr == "host_collection_plan"
            for child in ast.walk(node)
        )
    )
    first_model_route = next(
        node.lineno
        for node in ast.walk(interpret)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_pipeline_low_route"
    )
    assert branch.lineno < first_model_route
    pending = next(
        node
        for node in outer.body
        if isinstance(node, ast.FunctionDef) and node.name == "evidence_pending"
    )
    wrapper = ast.parse("def exercise():\n    pass\n").body[0]
    wrapper.body = [copy.deepcopy(branch)]
    wrapper = ast.fix_missing_locations(wrapper)
    store = recovery.RecoveryStore(tmp_path / "branch-journal")
    calls = []

    def read_boundary(
        target, context, prep, snapshot, selected_packet, readiness, plan, attempt, actual
    ):
        assert actual == {}  # no invented provider/run identity
        validated = recovery.validate_requests(plan, snapshot, selected_packet, readiness)
        assert validated and store.blocked(snapshot, selected_packet, readiness)
        calls.append(validated)
        store.finish(
            snapshot,
            attempt,
            prep=prep,
            screen=readiness,
            requests=validated,
            outcome="no_progress",
            causes=[],
            plan=plan,
            actual=actual,
        )
        return {
            "status": "pending",
            "authority": False,
            "component_scope": "collection I/O boundary only",
        }

    namespace = {
        **vars(closer),
        "collection_mode": True,
        "admitted_snapshot": snap,
        "raw_packet": packet,
        "readiness": screen,
        "recovery_store": store,
        "target": snap["target"],
        "context": {"deadline": time.time() + 10},
        "collect_or_defer": read_boundary,
        "prep": {
            "proposal": {"action": "collect_evidence"},
            "revision": snap["revision"],
            "recovery_requests": [],
            "packet_ref": {"sha256": "component-only"},
        },
    }
    exec(
        compile(
            ast.Module(body=[copy.deepcopy(pending), wrapper], type_ignores=[]),
            "actual-host-collection-branch",
            "exec",
        ),
        namespace,
    )
    assert namespace["exercise"]()["status"] == "pending"
    second = namespace["exercise"]()
    assert len(calls) == 1
    assert second["reason"] == "existing_necessary_collection_selector_or_new_progress_required"
    assert second["pending_evidence"]["readiness"]
    assert "unchanged no-progress input stays blocked" in second["next_trigger"]
