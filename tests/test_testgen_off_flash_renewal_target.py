"""Freeze the #1039 closer canary target until owner-host enrollment lands.

This is not a qualification record. It only pins the live closer_exception
contract so router.py / dispatcher.py cannot drift again before the reviewed
Sonnet successor is appended (#1002 / #1022 / #1040). After main merged the
py313 catalog retarget (#1053), the fixture must track the live contract
(including closer_mechanical_catalog.py) and the current predecessor digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import closer_dispatcher as closer
import closer_role_qualification as roles
import execution_profiles

FIXTURE = (
    Path(__file__).parent
    / "fixtures/semantic_api_reference/current_testgen_off_flash_renewal_target_v1.json"
)
FIXTURE_SHA256 = "18e75a9d6dae190ee9d6edd56e5f2f20a1d16700d55d2cfab043d6b8a7a39681"
PREDECESSOR_ID = (
    "sonnet-current-role-qualification:"
    "c6a57e37d7839ca227306d33b94b0ce3672b1eb3260bded3363e61932c774a4a"
)


def _target():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    value = json.loads(raw)
    assert value["schema"] == "closer-testgen-off-flash-renewal-target/v1"
    assert value["status"] == "awaiting_owner_host_canary"
    assert value["issue"] == 1039
    return value


def test_testgen_off_flash_renewal_target_matches_live_contract():
    target = _target()
    live = closer._controlled_model_contract("closer_exception")
    assert live["contract_sha256"] == target["live_model_contract_sha256"]
    assert live["sources"] == target["sources"]
    assert target["sources"]["router.py"] == (
        "34f45b99276dfb4eeb274f840fe872c77881a4da5c1b91ee215c3475e66970f0"
    )
    assert target["sources"]["dispatcher.py"] == (
        "d27ed8a9d027d58f16703a9b20a2d90b200a2e03850e6e3f9c6f8f88f82ebc12"
    )


def test_testgen_off_flash_renewal_predecessor_bridge_remains_immutable():
    target = _target()
    bridge = roles.ROLE_QUALIFICATIONS[PREDECESSOR_ID]
    live = closer._controlled_model_contract("closer_exception")
    assert bridge["qualification_id"] == target["predecessor_qualification_id"]
    assert roles._source_digest(bridge) == target["predecessor_record_sha256"]
    # The historical bridge keeps the pre-#1039 router pin; only an append-only
    # successor may carry the frozen live contract above.
    assert bridge["production_model_contract"]["sources"]["router.py"] == (
        "7702a4d6d79d9c5ff13c10d90fa3d3b0c28b15884ed959be84abd228b037c01b"
    )
    assert bridge["production_model_contract"]["contract_sha256"] != (
        target["live_model_contract_sha256"]
    )
    assert bridge["production_model_contract"] != live
    assert PREDECESSOR_ID in roles.ROLE_QUALIFICATION_SUPERSEDES
    # Keep lifetime and profile eligibility positive: the refusal must come
    # from the source change, rather than the predecessor's eventual expiry.
    checked_at = bridge["qualified_at"] + 1
    profile = execution_profiles.get_profile(bridge["profile_identity"]["profile_id"])
    assert (
        roles.qualified_record(
            profile,
            roles.STRONG_CRITERION_ADJUDICATION,
            now=checked_at,
            records={PREDECESSOR_ID: bridge},
        )
        == bridge
    )
    assert roles.execution_source_support(
        bridge,
        bridge["profile_identity"],
        bridge["production_model_contract"],
        task_type="closer_exception",
        checked_at=checked_at,
    ) == {"limits": bridge["execution_limits"], "admission": None}
    with pytest.raises(ValueError, match="does not support this exact production source"):
        roles.execution_source_support(
            bridge,
            bridge["profile_identity"],
            live,
            task_type="closer_exception",
            checked_at=checked_at,
        )
    expected = {
        "profile_identity": bridge["profile_identity"],
        "task_type": "closer_exception",
        "production_model_contract": live,
        "limits": {
            **bridge["execution_limits"],
            "model_contract_sha256": target["live_model_contract_sha256"],
        },
    }
    assert not roles.execution_record_matches(bridge, expected, checked_at=checked_at)
    assert roles._source_digest(bridge) == target["predecessor_record_sha256"]
    if target["status"] == "awaiting_owner_host_canary":
        assert not any(
            record["production_model_contract"]["contract_sha256"]
            == target["live_model_contract_sha256"]
            for record in roles.ROLE_QUALIFICATIONS.values()
        )
