from __future__ import annotations

import os

import pytest

import capabilities
import capability_lifecycle
import capability_targets


@pytest.fixture
def completed_children() -> dict[int, str]:
    children = {
        17: "ir-and-miner",
        18: "role",
        19: "workflow",
        20: "skill",
        21: "playbook",
        22: "gate",
        23: "lifecycle",
    }
    if os.environ.get("ORCH_TEST_MISSING_GATE_CHILD") == "1":
        children.pop(22)
    return children


def test_epic_requires_all_compile_targets_and_lifecycle(
    completed_children: dict[int, str]
) -> None:
    assert completed_children.get(22) == "gate", "compiler epic missing acceptance-gate child"
    assert set(completed_children) == set(range(17, 24))
    # The COMPILE targets are the contract: every kind the compiler emits must have a runtime
    # binding and a lifecycle contract, and vice versa.
    assert set(capability_targets.TARGET_KINDS) == set(capabilities.COMPILE_TARGET_KINDS)
    assert set(capability_lifecycle.TARGET_CONTRACTS) == set(capabilities.COMPILE_TARGET_KINDS)
    # The LEDGER additionally carries adoption-only kinds for capabilities nothing compiles
    # (module-backed lanes migrated into the ledger). Those must NEVER gain a runtime binding —
    # capability_targets has nothing to bind for them.
    assert set(capabilities.TARGET_KINDS) == (
        set(capabilities.COMPILE_TARGET_KINDS) | set(capabilities.ADOPTION_ONLY_KINDS))
    assert not (set(capabilities.ADOPTION_ONLY_KINDS) & set(capability_targets.TARGET_KINDS)), \
        "an adoption-only kind must not be compilable/bindable"

