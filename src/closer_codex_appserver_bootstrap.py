"""Unwired finite native-protocol DATA seam; no process, account client or turn writer.

Only a future actual owned transport can supply observation provenance. This pure
component never authenticates a peer, issues source facts or grants inference.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ProtocolHeld(RuntimeError):
    pass


_STEPS = (
    "initialize",
    "initialized",
    "account/read",
    "account/rateLimits/read",
    "thread/start",
    "account/read",
    "account/rateLimits/read",
)
_NOTIFICATIONS = frozenset(
    {
        "thread/started",
        "account/updated",
        "account/rateLimits/updated",
        "configWarning",
        "remoteControl/status/changed",
    }
)


def _need(value, code):
    if not value:
        raise ProtocolHeld(code)


def _pairs(values):
    result = {}
    for key, value in values:
        _need(key not in result, "duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    raise ProtocolHeld("nonfinite_json")


def _text(value):
    return type(value) is str and bool(value) and len(value.encode()) <= 1024 and "\0" not in value


def _remote_disabled(value):
    """Closed native remote-control notice; enabled/unknown states always hold."""
    _need(
        set(value) == {"method", "params", "emittedAtMs"}
        and value["method"] == "remoteControl/status/changed"
        and type(value["emittedAtMs"]) is int
        and 0 <= value["emittedAtMs"] <= 2**63 - 1,
        "native_remote_control_notice_invalid",
    )
    params = value["params"]
    _need(
        type(params) is dict
        and set(params) == {"status", "serverName", "installationId", "environmentId"}
        and params["status"] == "disabled"
        and _text(params["serverName"])
        and _text(params["installationId"])
        and (params["environmentId"] is None or _text(params["environmentId"])),
        "native_remote_control_not_disabled",
    )


class BootstrapProtocol:
    """Non-authorizing codec/state machine. No I/O and no source/turn capability."""

    _steps = _STEPS

    def __init__(
        self,
        *,
        model,
        provider,
        effort,
        permission_profile,
        cwd,
        workspace_roots,
        deadline,
        max_frame_bytes,
        max_total_bytes,
        max_notifications,
    ):
        _need(
            _text(model)
            and provider == "openai"
            and _text(effort)
            and effort in {"low", "medium", "high", "xhigh"},
            "selection_invalid",
        )
        _need(
            _text(permission_profile)
            and _text(cwd)
            and cwd.startswith("/")
            and type(workspace_roots) is list
            and bool(workspace_roots)
            and all(_text(root) and root.startswith("/") for root in workspace_roots),
            "layout_invalid",
        )
        _need(
            type(deadline) in (int, float) and math.isfinite(deadline) and deadline > 0,
            "deadline_invalid",
        )
        _need(
            type(max_frame_bytes) is int
            and 0 < max_frame_bytes <= 65536
            and type(max_total_bytes) is int
            and max_frame_bytes <= max_total_bytes <= 4 * 1024 * 1024
            and type(max_notifications) is int
            and 0 <= max_notifications <= 128,
            "bounds_invalid",
        )
        self._model, self._provider, self._effort = model, provider, effort
        self._permission = permission_profile
        self._cwd, self._roots = cwd, tuple(workspace_roots)
        self._deadline, self._frame_limit = deadline, max_frame_bytes
        self._total_limit, self._notification_limit = max_total_bytes, max_notifications
        self._index, self._id, self._notifications, self._total = 0, 0, 0, 0
        self._pending, self._held = None, None
        self._buffer = b""
        self._account = None
        self._native_account_id = None
        self._thread = None
        self._ready = False

    def _fail(self, code):
        self._held = code
        self._ready = False
        raise ProtocolHeld(code)

    def _live(self, now):
        if self._held is not None:
            raise ProtocolHeld(self._held)
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < self._deadline:
            self._fail("original_window_exhausted")

    def _count(self, count):
        self._total += count
        if self._total > self._total_limit:
            self._fail("total_bytes_exhausted")

    def next_frame(self, *, now):
        self._live(now)
        if self._pending is not None:
            self._fail("response_pending_no_retry")
        if self._index == len(self._steps):
            # There is intentionally NO positive writer or turn-permit issuer.
            self._fail("final_admission_unavailable")
        method = self._steps[self._index]
        if method == "initialized":
            message = {"method": method}
        else:
            self._id += 1
            if method == "initialize":
                params = {
                    "clientInfo": {"name": "source-coding-bootstrap", "version": "1"},
                    "capabilities": {
                        "experimentalApi": True,
                        "explicitGatewayOauth": True,
                    },
                }
            elif method == "account/read":
                params = {"refreshToken": False}
            elif method == "account/rateLimits/read":
                params = {
                    "supportsLunaReserve": False,
                    "excludeResetCreditDetails": True,
                }
            else:
                params = {
                    "model": self._model,
                    "modelProvider": self._provider,
                    "allowProviderModelFallback": False,
                    "permissions": self._permission,
                    "runtimeWorkspaceRoots": list(self._roots),
                    "approvalPolicy": "never",
                    "ephemeral": True,
                    "config": {"model_reasoning_effort": self._effort},
                }
                # No cwd key: actual process cwd must be independently source-bound.
                # No legacy sandbox, arbitrary config, trust write or model input.
            message = {"id": self._id, "method": method, "params": params}
        raw = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(raw) > self._frame_limit:
            self._fail("frame_size_exhausted")
        self._count(len(raw))
        if method != "initialized":
            self._pending = (self._id, method, self._index)
        self._index += 1
        return raw

    def feed(self, raw, *, now):
        self._live(now)
        if type(raw) is not bytes:
            self._fail("native_bytes_required")
        self._count(len(raw))
        self._buffer += raw
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if len(line) + 1 > self._frame_limit:
                self._fail("frame_size_exhausted")
            try:
                value = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=_pairs,
                    parse_constant=_constant,
                )
                self._accept(value)
            except ProtocolHeld as error:
                self._fail(str(error))
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                self._fail("native_frame_invalid")
        if len(self._buffer) > self._frame_limit:
            self._fail("frame_size_exhausted")

    def _accept(self, value):
        _need(type(value) is dict, "native_object_required")
        if "method" in value:
            _need("id" not in value, "native_server_request_unsupported")
            _need(value.get("method") != "configWarning", "native_config_warning")
            _need(value.get("method") in _NOTIFICATIONS, "native_notification_unsupported")
            if value["method"] == "remoteControl/status/changed":
                _remote_disabled(value)
            self._notifications += 1
            _need(
                self._notifications <= self._notification_limit,
                "notification_limit_exhausted",
            )
            if value["method"] == "account/updated" and self._account is not None:
                raise ProtocolHeld("account_changed_no_retry")
            return
        _need(
            set(value) <= {"id", "result", "error", "jsonrpc"}
            and ("jsonrpc" not in value or value["jsonrpc"] == "2.0"),
            "native_response_shape_invalid",
        )
        _need(
            self._pending is not None
            and type(value.get("id")) is int
            and value["id"] == self._pending[0],
            "native_response_identity_changed",
        )
        _need(
            "error" not in value and "result" in value and type(value["result"]) is dict,
            "native_error_or_result_invalid",
        )
        _, method, index = self._pending
        result = value["result"]
        if method == "account/read":
            account = result.get("account")
            _need(
                type(account) is dict
                and account.get("type") == "chatgpt"
                and result.get("requiresOpenaiAuth") is True,
                "native_account_kind_unavailable",
            )
            routing = result.get("workspaceRouting")
            route_account = None
            if routing is not None:
                _need(
                    type(routing) is dict and _text(routing.get("chatgptAccountId")),
                    "native_account_routing_invalid",
                )
                route_account = routing["chatgptAccountId"]
            self._account = route_account or ""  # no email/plan/token/config retained
        elif method == "account/rateLimits/read":
            identity = result.get("accountId")
            # Presence of either boolean is the native current-auth match signal.
            # Its value is NOT a subscription/credit admission policy.
            _need(
                type(result.get("ordinaryUsageAllowed")) is bool and _text(identity),
                "native_identity_match_unavailable",
            )
            _need(
                self._account is not None and (not self._account or identity == self._account),
                "native_account_routing_changed",
            )
            if self._native_account_id is None:
                self._native_account_id = identity
            else:
                _need(identity == self._native_account_id, "native_account_changed")
            if index == len(self._steps) - 1:
                self._ready = True
        elif method == "thread/start":
            active = result.get("activePermissionProfile")
            thread = result.get("thread")
            _need(
                type(thread) is dict
                and _text(thread.get("id"))
                and result.get("model") == self._model
                and result.get("modelProvider") == self._provider
                and result.get("reasoningEffort") == self._effort
                and result.get("cwd") == self._cwd
                and result.get("runtimeWorkspaceRoots") == list(self._roots)
                and result.get("approvalPolicy") == "never"
                and type(active) is dict
                and active.get("id") == self._permission
                and (active.get("extends") is None or _text(active.get("extends")))
                and type(result.get("sandbox")) is dict,
                "native_thread_selection_changed",
            )
            self._thread = {
                "id": thread["id"],
                "model": result["model"],
                "model_provider": result["modelProvider"],
                "effort": result["reasoningEffort"],
                "cwd": result["cwd"],
                "workspace_roots": list(result["runtimeWorkspaceRoots"]),
                "active_permission_profile": {
                    "id": active["id"],
                    "extends": active.get("extends"),
                },
            }
            # Native profile/sandbox selection metadata is NOT confinement proof.
        self._pending = None

    def snapshot(self):
        return {
            "schema": "codex-bootstrap-parsed-data/v1",
            "authority": False,
            "ready_for_source_admission": self._ready and self._held is None,
            "native_claim_identity_match_present": self._native_account_id is not None,
            "native_account_claim_sha256": (
                hashlib.sha256(
                    json.dumps(
                        {
                            "schema": "untrusted-native-account-claim/v1",
                            "account_id": self._native_account_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if self._native_account_id is not None
                else None
            ),
            "native_thread_selection": copy.deepcopy(self._thread),
            "confinement": "unsupported",
            "source_transport_binding": "unissued",
            "turn_authorized": False,
            "held_code": self._held,
            "counted_protocol_bytes": self._total,
            "notifications": self._notifications,
        }


class AccountMetadataProtocol(BootstrapProtocol):
    """No-thread account codec; DATA never registers or authorizes a consumer."""

    _steps = ("initialize", "initialized", "account/read", "account/rateLimits/read")

    def __init__(self, *, deadline, max_frame_bytes=65536, max_total_bytes=131072):
        super().__init__(
            model="metadata-only",
            provider="openai",
            effort="low",
            permission_profile="unissued-metadata-only",
            cwd="/",
            workspace_roots=["/"],
            deadline=deadline,
            max_frame_bytes=max_frame_bytes,
            max_total_bytes=max_total_bytes,
            max_notifications=16,
        )

    def _accept(self, value):
        if type(value) is dict and "method" in value:
            _need(
                value.get("method")
                in {
                    "account/updated",
                    "account/rateLimits/updated",
                    "configWarning",
                    "remoteControl/status/changed",
                },
                "metadata_notification_unsupported",
            )
        super()._accept(value)

    def snapshot(self):
        value = super().snapshot()
        value["schema"] = "codex-account-metadata-data/v1"
        value["metadata_complete"] = value.pop("ready_for_source_admission")
        value["ready_for_source_admission"] = False
        return value


class TurnResponseData:
    """Bounded native response DATA only; never emits requests or admission."""

    _IGNORED = frozenset(
        {
            "thread/status/changed",
            "turn/started",
            "item/started",
            "item/completed",
            "thread/tokenUsage/updated",
            "turn/diff/updated",
            "item/commandExecution/outputDelta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/textDelta",
        }
    )

    def __init__(self, *, thread_id, max_bytes):
        _need(
            _text(thread_id) and type(max_bytes) is int and max_bytes > 0,
            "turn_data_bounds_invalid",
        )
        self.thread_id, self.limit = thread_id, max_bytes
        self.buffer, self.count, self.turn_id = b"", 0, None
        self.output, self.complete, self.held = [], False, None

    def feed(self, raw):
        if self.held:
            raise ProtocolHeld(self.held)
        try:
            _need(
                type(raw) is bytes and bool(raw) and not self.complete,
                "turn_bytes_unavailable",
            )
            self.count += len(raw)
            _need(self.count <= self.limit, "turn_bytes_exhausted")
            self.buffer += raw
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                value = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=_pairs,
                    parse_constant=_constant,
                )
                self._accept(value)
        except (
            ProtocolHeld,
            ValueError,
            UnicodeError,
            TypeError,
            RecursionError,
        ) as error:
            self.held = str(error) if isinstance(error, ProtocolHeld) else "turn_frame_invalid"
            self.output.clear()
            raise ProtocolHeld(self.held) from None

    def _accept(self, value):
        _need(type(value) is dict and "error" not in value, "turn_frame_failed")
        if "id" in value:
            _need(
                type(value["id"]) is int
                and value["id"] == 1000
                and "method" not in value
                and self.turn_id is None,
                "turn_response_identity_invalid",
            )
            result = value.get("result")
            _need(
                type(result) is dict and type(result.get("turn")) is dict,
                "turn_response_invalid",
            )
            turn_id = result["turn"].get("id")
            _need(_text(turn_id), "turn_response_identity_invalid")
            self.turn_id = turn_id
            return
        method, params = value.get("method"), value.get("params")
        _need(type(params) is dict and type(method) is str, "turn_notification_invalid")
        _need(method not in {"account/updated", "configWarning"}, "turn_context_changed")
        if method == "remoteControl/status/changed":
            _remote_disabled(value)
            return
        if method == "item/agentMessage/delta":
            _need(
                self.turn_id is not None
                and params.get("threadId") == self.thread_id
                and params.get("turnId") == self.turn_id
                and type(params.get("delta")) is str,
                "turn_message_identity_invalid",
            )
            self.output.append(params["delta"])
        elif method == "turn/completed":
            turn = params.get("turn")
            _need(
                type(turn) is dict
                and self.turn_id is not None
                and params.get("threadId") == self.thread_id
                and turn.get("id") == self.turn_id
                and turn.get("status") == "completed",
                "turn_completion_unavailable",
            )
            self.complete = True
        else:
            _need(method in self._IGNORED, "turn_notification_unsupported")

    def text(self):
        _need(self.complete and self.held is None, "turn_completion_unavailable")
        return "".join(self.output)
