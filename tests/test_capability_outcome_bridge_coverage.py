"""Additional coverage for capability_outcome_bridge.py uncovered lines.

Targets specific uncovered exception handling paths.
"""

from __future__ import annotations

import pytest

import capability_outcome_bridge as bridge


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Keep tests isolated from the live ledger."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_role_capability_ids_exception(monkeypatch):
    """_role_capability_ids handles exception when importing roles.

    Covers lines 71-72.
    """

    def raise_on_roles_import(name, *args, **kwargs):
        if name == "roles":
            raise ImportError("roles module not found")
        import builtins

        return builtins.__import__(name, *args, **kwargs)

    import builtins

    monkeypatch.setattr(builtins, "__import__", raise_on_roles_import)

    result = bridge._role_capability_ids(
        "role:test:run1", {"influenced_by_run_id": "role:test:run1"}
    )
    assert result == []


def test_role_capability_ids_no_match():
    """_role_capability_ids returns empty list when regex doesn't match.

    Covers line 66.
    """
    result = bridge._role_capability_ids("run1", {})
    assert result == []


def test_role_capability_ids_not_in_registry(monkeypatch):
    """_role_capability_ids returns empty list when role not in registry.

    Covers line 73.
    """
    mock_roles = type("MockRoles", (), {"ROLE_CAPABILITY_IDS": {"known": "cap-known"}})()

    import sys

    old_roles = sys.modules.get("roles")
    sys.modules["roles"] = mock_roles

    try:
        result = bridge._role_capability_ids(
            "role:unknown:run1", {"influenced_by_run_id": "role:unknown:run1"}
        )
        assert result == []
    finally:
        if old_roles:
            sys.modules["roles"] = old_roles
        else:
            sys.modules.pop("roles", None)


def test_tagged_capability_ids_json_parse_exception():
    """_tagged_capability_ids handles JSON parse exception.

    Covers lines 80-83.
    """
    row = {"capability_ids": "not valid json"}
    result = bridge._tagged_capability_ids("run1", row)
    assert result == []


def test_tagged_capability_ids_type_error():
    """_tagged_capability_ids handles TypeError when capability_ids is not a string.

    Covers lines 80-83 (the isinstance check handles non-string types).
    """
    row = {"capability_ids": 123}  # integer, not string
    result = bridge._tagged_capability_ids("run1", row)
    assert result == []


def test_tagged_capability_ids_empty_list():
    """_tagged_capability_ids handles empty list.

    Tests the normal path.
    """
    row = {"capability_ids": []}
    result = bridge._tagged_capability_ids("run1", row)
    assert result == []


def test_tagged_capability_ids_valid_json():
    """_tagged_capability_ids correctly parses valid JSON.

    Tests the normal path.
    """
    row = {"capability_ids": '["cap1", "cap2"]'}
    result = bridge._tagged_capability_ids("run1", row)
    assert result == ["cap1", "cap2"]


def test_fleet_deliverable_regex():
    """_FLEET_DELIVERABLE_RE matches valid deliverables.

    Tests the regex used in fleet deliverable extraction.
    """
    assert bridge._FLEET_DELIVERABLE_RE.fullmatch("repo/owner#123")
    assert bridge._FLEET_DELIVERABLE_RE.fullmatch("my-repo/my-owner#456")
    assert not bridge._FLEET_DELIVERABLE_RE.fullmatch("invalid")
    assert not bridge._FLEET_DELIVERABLE_RE.fullmatch("repo/owner")  # missing issue number


def test_fleet_deliverable_lowercase():
    """_fleet_deliverable lowercases the deliverable.

    Covers line 92.
    """
    result = bridge._fleet_deliverable("Repo/Owner#123")
    assert result == "repo/owner#123"


def test_fleet_deliverable_empty_string():
    """_fleet_deliverable returns empty string for non-matching input.

    Covers line 92.
    """
    result = bridge._fleet_deliverable("not a deliverable")
    assert result == ""


def test_fleet_deliverable_whitespace():
    """_fleet_deliverable strips whitespace.

    Covers line 91.
    """
    result = bridge._fleet_deliverable("  repo/owner#123  ")
    assert result == "repo/owner#123"
