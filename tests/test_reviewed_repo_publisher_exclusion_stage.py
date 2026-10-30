"""Offline contracts for the reviewed-repo publisher exclusion stage."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest
from scripts import apply_reviewed_repo_publisher_exclusion_stage as apply_stage
from scripts import stage_reviewed_repo_publisher_exclusion as stage

SOURCE = Path("/Users/teacher/.codex/automations/reviewed-repo-automation-controller")


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def captured_publisher(tmp_path: Path) -> Path:
    root = tmp_path / "controller"
    root.mkdir()
    for name in ("controller.py", "demand_router.py", "accountability.py"):
        shutil.copy2(SOURCE / name, root / name)
    router = root / "demand_router.py"
    source = router.read_text()
    helper = Path(
        next(
            line.split("'", 2)[1]
            for line in source.splitlines()
            if line.startswith("_CONTINUITY_PATH")
        )
    )
    shutil.copy2(helper, root / helper.name)
    router.write_text(source.replace(str(helper), str(root / helper.name)))
    return root


def staged(tmp_path: Path) -> tuple[Path, dict]:
    root = captured_publisher(tmp_path)
    output = tmp_path / "stage"
    recipe = stage.stage(root, output)
    return output, recipe


def test_stage_is_create_only_and_records_preimages(tmp_path):
    output, recipe = staged(tmp_path)
    helper = recipe["operations"][0]
    assert helper["kind"] == "create_only"
    assert helper["target"] == f"delivery-continuity-{helper['sha256']}.py"
    assert [item["target"] for item in recipe["operations"]] == [
        helper["target"],
        "accountability.py",
        "demand_router.py",
    ]
    assert set(recipe["preimages"]) == {
        "controller.py",
        "demand_router.py",
        "accountability.py",
        next(name for name in recipe["preimages"] if name.startswith("delivery-continuity-")),
    }
    assert not (Path(recipe["controller_root"]) / helper["target"]).exists()
    assert (output / "payload" / helper["target"]).is_file()


def test_successor_refuses_excluded_cached_target_and_pr_target_before_mocked_gh(tmp_path):
    output, recipe = staged(tmp_path)
    helper = module(output / "payload" / recipe["operations"][0]["target"], "continuity_stage")
    calls = []

    class Inventory:
        @staticmethod
        def repo(node):
            return node["repository"]["nameWithOwner"]

        @staticmethod
        def scoped_blocker_states():
            return set(), set(), set()

        @staticmethod
        def run(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("excluded cached target reached mocked gh")

    router = types.SimpleNamespace(inventory=Inventory(), QUERY="... on PullRequest{}")
    router.pr_owner = lambda node: "closer"
    router.build_routes = lambda *args: (
        {"closer": [], "orphan": [], "maintenance": []},
        {},
        [],
        [],
    )
    state = {
        "targets": {},
        "delivery_ownership": {},
        "delivery_continuity": {
            "stranske/Collab-Admin#1": {"target": "stranske/Collab-Admin#1", "owner": "closer"},
            "stranske/Workflows#2": {
                "target": "stranske/Workflows#2",
                "pr_target": "stranske/Collab-Admin#1",
                "owner": "closer",
            },
        },
    }
    result = helper.prepare_controller(router, state, {"nodes": []}, 1.0, {}, (set(), set(), set()))
    assert calls == []
    assert result["rows"] == [] and result["carry"] == {}


def test_successor_filters_excluded_parent_references_and_unknown_due(tmp_path):
    output, _recipe = staged(tmp_path)
    accountability = module(output / "payload" / "accountability.py", "accountability_stage")
    pr = {
        "repository": {"nameWithOwner": "stranske/Workflows"},
        "number": 5,
        "body": "Fixes stranske/Collab-Admin#4 and stranske/Workflows#3",
        "closingIssuesReferences": {
            "nodes": [
                {"repository": {"nameWithOwner": "stranske/Collab-Admin"}, "number": 4},
                {"repository": {"nameWithOwner": "stranske/Workflows"}, "number": 3},
            ]
        },
    }
    assert accountability.issue_links(pr) == {"stranske/Workflows#3"}
    due = accountability.unknown_due(
        {
            "stranske/Collab-Admin#4": {"target": "stranske/Collab-Admin#4", "human_due_at": 0},
            "stranske/Workflows#3": {"target": "stranske/Workflows#3", "human_due_at": 0},
        },
        1.0,
        None,
        ["inventory incomplete"],
    )
    assert [row["target"] for row in due] == ["stranske/Workflows#3"]


def test_router_payload_filters_excluded_nodes_and_guard_rows(tmp_path, monkeypatch):
    output, _recipe = staged(tmp_path)
    inventory = types.ModuleType("controller")
    inventory.TARGETS = {
        lane: Path("/tmp") / lane
        for lane in ("opener", "closer", "orphan", "escalator", "maintenance")
    }
    inventory.labels = lambda node: set()
    inventory.repo = lambda node: node["repository"]["nameWithOwner"]
    inventory.is_release_pr = lambda node: False
    inventory.is_issue_linked_pr = lambda node: True
    inventory.is_generated_or_durable_pr = lambda node: False
    inventory.maintenance_category = lambda node: None
    inventory.parse_iso = lambda value: None
    maintenance = types.ModuleType("maintenance_admission")
    maintenance.repair_policy = lambda *args: None
    maintenance.budget = types.SimpleNamespace(summary=lambda: {})
    repo_campaigns = types.ModuleType("repo_campaigns")
    monkeypatch.setitem(sys.modules, "controller", inventory)
    monkeypatch.setitem(sys.modules, "maintenance_admission", maintenance)
    monkeypatch.setitem(sys.modules, "repo_campaigns", repo_campaigns)
    monkeypatch.setitem(sys.modules, "accountability", types.ModuleType("accountability"))
    router = module(output / "payload" / "demand_router.py", "router_stage")
    admin = {"repository": {"nameWithOwner": "stranske/Collab-Admin"}, "number": 1}
    guard_admin = {"repository": "stranske/Collab-Admin", "number": 1}
    guard = {"actionable_candidates": [guard_admin], "challenge_due": [guard_admin]}
    routes, observations, waits, ignored = router.build_routes([admin], guard, {}, {}, 1.0)
    assert all(not rows for rows in routes.values())
    assert observations == {} and waits == [] and ignored == []
    router.ROOT = tmp_path
    (tmp_path / "opener-assignment.json").write_text(
        '{"generated_at": "now", "targets": [{"target": "stranske/Collab-Admin#1"}], "errors": []}'
    )
    router.inventory.parse_iso = lambda _value: 1.0
    monkeypatch.setattr(router.time, "time", lambda: 2.0)
    assert not router.relay_eligible("opener")


def test_apply_is_atomic_cas_and_router_is_last(tmp_path):
    output, recipe = staged(tmp_path)
    result = apply_stage.apply(output)
    assert result["applied"] == [item["target"] for item in recipe["operations"]]
    root = Path(recipe["controller_root"])
    helper, accountability, router = recipe["operations"]
    assert hashlib.sha256((root / helper["target"]).read_bytes()).hexdigest() == helper["sha256"]
    assert (
        hashlib.sha256((root / accountability["target"]).read_bytes()).hexdigest()
        == accountability["sha256"]
    )
    assert hashlib.sha256((root / router["target"]).read_bytes()).hexdigest() == router["sha256"]


def test_apply_refuses_changed_preimage_without_overwriting(tmp_path):
    output, recipe = staged(tmp_path)
    root = Path(recipe["controller_root"])
    target = root / "accountability.py"
    target.write_text("concurrent owner bytes\n")
    with pytest.raises(apply_stage.ApplyHeld, match="canonical preimage changed"):
        apply_stage.apply(output)
    assert target.read_text() == "concurrent owner bytes\n"
    assert not (root / recipe["operations"][0]["target"]).exists()
