"""Frozen pure mocked-native test definitions; no native/source authority stubs.

Unexecuted until protocol review and implementation. Supplied bytes are synthetic
native-protocol DATA, never an authenticated process observation or turn permit.
"""

import json

import pytest

from closer_codex_appserver_bootstrap import BootstrapProtocol, ProtocolHeld


def protocol():
    return BootstrapProtocol(
        model="codex-6.1-sol",
        provider="openai",
        effort="high",
        permission_profile="reviewed-existing-profile",
        cwd="/synthetic/workspace",
        workspace_roots=["/synthetic/workspace"],
        deadline=100.0,
        max_frame_bytes=4096,
        max_total_bytes=65536,
        max_notifications=16,
    )


def outgoing(p, expected, now=1.0):
    frame = json.loads(p.next_frame(now=now))
    assert frame["method"] == expected
    assert frame["method"] not in {
        "turn/start",
        "account/login/start",
        "config/value/write",
        "account/rateLimits/reset",
        "command/exec",
    }
    return frame


def respond(p, frame, result, now=1.0):
    p.feed((json.dumps({"id": frame["id"], "result": result}) + "\n").encode(), now=now)


def account():
    return {
        "account": {
            "type": "chatgpt",
            "email": "synthetic@example.invalid",
            "planType": "pro",
        },
        "requiresOpenaiAuth": True,
        "workspaceRouting": None,
    }


def rates(ordinary=False, account_id="synthetic-account"):
    return {
        "accountId": account_id,
        "ordinaryUsageAllowed": ordinary,
        "rateLimits": {},
        "rateLimitsByLimitId": {},
        "rateLimitResetCredits": None,
        "rateLimitUpsell": None,
    }


def native_thread():
    return {
        "thread": {"id": "synthetic-thread"},
        "model": "codex-6.1-sol",
        "modelProvider": "openai",
        "reasoningEffort": "high",
        "cwd": "/synthetic/workspace",
        "runtimeWorkspaceRoots": ["/synthetic/workspace"],
        "approvalPolicy": "never",
        "activePermissionProfile": {"id": "reviewed-existing-profile", "extends": None},
        "sandbox": {
            "type": "workspaceWrite",
            "writableRoots": ["/synthetic/workspace"],
            "networkAccess": False,
        },
    }


def through_identity(p, ordinary=False):
    frame = outgoing(p, "initialize")
    respond(p, frame, {"userAgent": "synthetic-native"})
    note = outgoing(p, "initialized")
    assert "id" not in note
    frame = outgoing(p, "account/read")
    assert frame["params"] == {"refreshToken": False}
    respond(p, frame, account())
    frame = outgoing(p, "account/rateLimits/read")
    assert frame["params"] == {
        "supportsLunaReserve": False,
        "excludeResetCreditDetails": True,
    }
    respond(p, frame, rates(ordinary))


@pytest.mark.parametrize("ordinary", [False, True])
def test_native_boolean_presence_is_identity_data_not_billing_gate(ordinary):
    p = protocol()
    through_identity(p, ordinary)
    frame = outgoing(p, "thread/start")
    assert "cwd" not in frame["params"] and "sandbox" not in frame["params"]
    assert frame["params"]["permissions"] == "reviewed-existing-profile"
    assert frame["params"]["allowProviderModelFallback"] is False
    respond(p, frame, native_thread())
    respond(p, outgoing(p, "account/read"), account())
    respond(p, outgoing(p, "account/rateLimits/read"), rates(ordinary))
    draft = p.snapshot()
    assert draft["authority"] is False and draft["ready_for_source_admission"] is True
    assert draft["native_claim_identity_match_present"] is True
    assert "email" not in json.dumps(draft) and "synthetic@example.invalid" not in json.dumps(draft)
    with pytest.raises(ProtocolHeld, match="final_admission_unavailable"):
        p.next_frame(now=1.0)


@pytest.mark.parametrize("ordinary", [None, 0, 1, "true"])
def test_absent_or_untyped_identity_match_does_not_become_ordinary_policy(ordinary):
    p = protocol()
    with pytest.raises(ProtocolHeld):
        through_identity(p, ordinary)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "different"),
        ("modelProvider", "custom"),
        ("reasoningEffort", "low"),
        ("cwd", "/foreign"),
        ("runtimeWorkspaceRoots", ["/foreign"]),
        ("activePermissionProfile", {"id": "foreign", "extends": None}),
    ],
)
def test_native_thread_selection_mismatch_holds_without_turn(field, value):
    p = protocol()
    through_identity(p)
    frame = outgoing(p, "thread/start")
    data = native_thread()
    data[field] = value
    with pytest.raises(ProtocolHeld):
        respond(p, frame, data)


def test_same_launch_account_change_cannot_reuse_bootstrap():
    p = protocol()
    through_identity(p)
    respond(p, outgoing(p, "thread/start"), native_thread())
    respond(p, outgoing(p, "account/read"), account())
    with pytest.raises(ProtocolHeld):
        respond(p, outgoing(p, "account/rateLimits/read"), rates(False, "foreign"))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":1,"id":1,"result":{}}\n',
        b"\xff\n",
        b'{"id":999,"result":{}}\n',
        b'{"id":true,"result":{}}\n',
        b'{"id":1,"result":{"v":NaN}}\n',
        b'{"id":1,"error":{"code":-1,"message":"synthetic native error"}}\n',
        b'{"id":7,"method":"account/chatgptAuthTokens/refresh","params":{}}\n',
    ],
)
def test_malformed_or_foreign_native_frame_fails_closed(raw):
    p = protocol()
    outgoing(p, "initialize")
    with pytest.raises(ProtocolHeld):
        p.feed(raw, now=1.0)


def test_fragmented_native_line_is_not_multiple_responses():
    p = protocol()
    frame = outgoing(p, "initialize")
    raw = (json.dumps({"id": frame["id"], "result": {}}) + "\n").encode()
    p.feed(raw[:7], now=1.0)
    p.feed(raw[7:], now=1.0)
    outgoing(p, "initialized")
    with pytest.raises(ProtocolHeld):
        p.feed(raw, now=1.0)


def test_original_window_and_size_limits_hold_without_retry():
    p = protocol()
    with pytest.raises(ProtocolHeld):
        p.next_frame(now=101.0)
    p = protocol()
    outgoing(p, "initialize")
    with pytest.raises(ProtocolHeld):
        p.feed(b"x" * 4097, now=1.0)


def test_notifications_are_bounded_and_account_update_invalidates_without_retry():
    p = protocol()
    outgoing(p, "initialize")
    for _ in range(16):
        p.feed(b'{"method":"account/rateLimits/updated","params":{}}\n', now=1.0)
    with pytest.raises(ProtocolHeld, match="notification_limit_exhausted"):
        p.feed(b'{"method":"account/rateLimits/updated","params":{}}\n', now=1.0)
    p = protocol()
    through_identity(p)
    with pytest.raises(ProtocolHeld, match="account_changed_no_retry"):
        p.feed(b'{"method":"account/updated","params":{}}\n', now=1.0)
    with pytest.raises(ProtocolHeld, match="account_changed_no_retry"):
        p.next_frame(now=1.0)


def test_total_protocol_bytes_include_bootstrap_requests():
    p = BootstrapProtocol(
        model="codex-6.1-sol",
        provider="openai",
        effort="high",
        permission_profile="reviewed-existing-profile",
        cwd="/synthetic/workspace",
        workspace_roots=["/synthetic/workspace"],
        deadline=100.0,
        max_frame_bytes=4096,
        max_total_bytes=4096,
        max_notifications=16,
    )
    outgoing(p, "initialize")
    with pytest.raises(ProtocolHeld, match="total_bytes_exhausted"):
        p.feed(b"x" * 4096, now=1.0)


def test_native_api_key_account_has_no_identity_projection():
    p = protocol()
    respond(p, outgoing(p, "initialize"), {})
    outgoing(p, "initialized")
    frame = outgoing(p, "account/read")
    with pytest.raises(ProtocolHeld, match="native_account_kind_unavailable"):
        respond(p, frame, {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True})
    assert p.snapshot()["turn_authorized"] is False


def test_no_arbitrary_rpc_or_positive_turn_interface():
    p = protocol()
    assert not hasattr(p, "request") and not hasattr(p, "turn_start")
    assert p.snapshot()["authority"] is False and p.snapshot()["turn_authorized"] is False
    with pytest.raises(TypeError):
        p.next_frame(now=1.0, method="turn/start")


def test_observation_draft_is_copied_and_uses_untrusted_identity_domain():
    import hashlib

    p = protocol()
    through_identity(p)
    respond(p, outgoing(p, "thread/start"), native_thread())
    respond(p, outgoing(p, "account/read"), account())
    respond(p, outgoing(p, "account/rateLimits/read"), rates(False))
    draft = p.snapshot()
    draft["native_thread_selection"]["model"] = "forged"
    assert p.snapshot()["native_thread_selection"]["model"] == "codex-6.1-sol"
    approved_domain = hashlib.sha256(
        json.dumps(
            {"schema": "managed-native-account/v1", "account_id": "synthetic-account"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert p.snapshot()["native_account_claim_sha256"] != approved_domain
    assert p.snapshot()["source_transport_binding"] == "unissued"
    assert p.snapshot()["confinement"] == "unsupported" and p.snapshot()["turn_authorized"] is False


def test_initialization_uses_explicit_gateway_oauth_without_browser_fallback():
    p = protocol()
    frame = outgoing(p, "initialize")
    assert frame["params"]["capabilities"] == {
        "experimentalApi": True,
        "explicitGatewayOauth": True,
    }


def test_configuration_warning_is_a_fixed_hold_not_readiness():
    p = protocol()
    outgoing(p, "initialize")
    with pytest.raises(ProtocolHeld, match="native_config_warning"):
        p.feed(
            b'{"method":"configWarning","params":{"message":"synthetic-private-detail"}}\n',
            now=1.0,
        )
    snapshot = p.snapshot()
    assert snapshot["ready_for_source_admission"] is False
    assert snapshot["held_code"] == "native_config_warning"
    assert "synthetic-private-detail" not in json.dumps(snapshot)
    with pytest.raises(ProtocolHeld, match="native_config_warning"):
        p.next_frame(now=1.0)


def test_reasoning_override_is_a_single_source_supported_request_key():
    p = protocol()
    through_identity(p)
    frame = outgoing(p, "thread/start")
    assert frame["params"]["config"] == {"model_reasoning_effort": "high"}
    assert "cwd" not in frame["params"] and "sandbox" not in frame["params"]
