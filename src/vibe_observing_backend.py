"""Pinned, explicitly injected Vibe/Mistral passive response observer.

No installed SDK import, credential lookup, client creation or provider call occurs
on module import or by default. A future isolated loader must inject independently
pinned classes, runtime/route validators and a raw response observer. Endpoint
metadata is never subscription entitlement, no-PAYG approval or role authority.

Source audit (mistral-vibe2.15.0 / installed Mistral SDK): Vibe MistralBackend
mistral.py:265 creates an SDK client, :282 delegates complete_async, and :315
maps away response.model. This wrapper overrides client construction and thin
request guards; the original complete/mapping implementation is inherited.
chat.py:294-347 accepts retries=None. :448-467 inherits configured retries only
for UNSET and uses the retry wrapper only for RetryConfig; basesdk.py:384-390
otherwise calls do() once. SDK sdk.py:101-112 otherwise constructs BOTH default
HTTP clients, so both are explicitly supplied here. HTTPX _client.py:1691 runs
request event hooks after SDK request hooks, before network send; response hooks
run before redirect handling. trust_env=False and follow_redirects=False are
real constructor parameters, not guessed Vibe flags.

The passive SDK/HTTP callbacks below are unrelated to disabled Vibe agent hooks.
The SDK's own pinned hook registration still exists; final URL/body/header guards
reject unsafe request transformations, but cannot stop arbitrary hook side effects.
The future runtime validator must verify a fresh isolated interpreter with no
configured OpenTelemetry tracer/exporter before SDK construction and each call.
registration.py installs TracingHook; otel.py:420-444 uses the default NoOp only
when no application tracer is configured. Workflow hooks are config/operation
gated; chat is outside their operation sets. No SDK hook registry/global is
monkeypatched. None of these constraints is a kernel sandbox or billing proof.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MODEL = "mistral-medium-3.5"
EFFORT = "high"
SERVER = "https://api.mistral.ai"
API_BASE = SERVER + "/v1"
ENDPOINT = API_BASE + "/chat/completions"
PACKAGE = "/Users/teacher/.local/share/uv/tools/mistral-vibe/lib/python3.12/site-packages/"
AUDITED_SOURCES = {
    "vibe/core/llm/backend/mistral.py": "788fea10170a68bd70d8769066d56c1bce6d8820fd66e9e7236d43b0b551cf3a",
    "mistralai/client/chat.py": "9dd791daac83b51820aaea8fa23d8649806838f6157543b9f473d9c189121416",
    "mistralai/client/sdk.py": "8c7545c94d8b0851480fcffbdff9d1274f1f8176a669ceff96247c1688681a1b",
    "mistralai/client/basesdk.py": "a13d2a4677d4adfbf5314f25e2eb1ad28f9e84e2997b2a45822588b6feccc22f",
    "mistralai/client/models/chatcompletionresponse.py": "65035ca8418b465fd1edc201679b2545af827724395ebb8387c4a2ee78db114a",
    "httpx/_client.py": "c43f941baefe58c91e96d00039e1868fe719d91453026d7db1647194563bff8d",
    "mistralai/client/_hooks/registration.py": "62792ebd030d9d17c98a41c08832796cc89a7c7dd84dbac553551dceb75f74fe",
    "mistralai/client/_hooks/sdkhooks.py": "3ba644b5ac9ac59559e7dc707a82728e0ace93a0c5e8dad0b4f7930c6adf482a",
    "mistralai/client/models/chatcompletionrequest.py": "55e0e3d8a2d2d59c8ad69d39030b267d8e63a5eb1fc8a261f5ec7066f0dce7c4",
    "mistralai/client/utils/serializers.py": "f51e3cb15c2c752c369196991fadc46463eef372bc81a66a5ee3b7d66dc2804e",
    "mistralai/client/_hooks/tracing.py": "228a12b16fec184df365d2341d9d15cb8e69206de1f39b01cfa2228519c0ecfc",
    "mistralai/client/_hooks/traceparent.py": "a5c114058a4a96bf93f99aa4bfbeba3075b34240cace758c2e060d8aeeac603b",
    "mistralai/client/_hooks/workflow_encoding_hook.py": "a7ce8cf6302a82a0c98de169fe8aa18cab073f6833e67e6554b840d947697eb2",
    "mistralai/client/_hooks/custom_user_agent.py": "7077e9e3745cb0dbc7bacabc595c56ac24b7c3ea66cc9f2e36ebda31929db49f",
    "mistralai/client/_hooks/deprecation_warning.py": "7b210e7fbfa8f6eaaa356267b9f0f6457a77758ac65788a7f6aefac8b0b5ce88",
    "mistralai/extra/observability/otel.py": "3f117dffbbe398c9dde8c82874438732b00a49fad4f23e70dbf4eeb840b04716",
}


@dataclass(frozen=True)
class SourcePin:
    path: str
    sha256: str
    qualname: str = ""


def pin_source(value: Any) -> SourcePin:
    path = inspect.getsourcefile(value)
    if path is None:
        raise ValueError("injected component must have reviewable Python source")
    source = Path(path).resolve(strict=True)
    before = source.stat()
    data = source.read_bytes()
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("component source drift during hashing")
    return SourcePin(str(source), hashlib.sha256(data).hexdigest(), value.__qualname__)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class BackendBindings:
    base_backend: type
    sdk_factory: Callable
    async_http_factory: Callable
    sync_http_factory: Callable


@dataclass(frozen=True)
class BackendPins:
    components: tuple[tuple[str, SourcePin], ...]


def pin_bindings(bindings: BackendBindings) -> BackendPins:
    return BackendPins(
        tuple(
            (name, pin_source(getattr(bindings, name)))
            for name in BackendBindings.__dataclass_fields__
        )
    )


@dataclass(frozen=True)
class ReviewedTransportContext:
    """Trusted caller callbacks/artifact bindings, never claims read from model text.

    runtime_check must verify the isolated loader/import/native closure and clean
    allowlisted environment, actual isolated/no-site interpreter flags, fresh SDK hook
    state and a verified NoOp/Proxy OpenTelemetry tracer (no configured exporter).
    It must run before SDK construction and before each submission. route_check
    verifies the exact existing configured endpoint. Both
    return None or raise; neither is treated as account/paid-route attestation.
    """

    runtime_check: Callable[[], Any]
    route_check: Callable[[str], Any]
    runtime_check_pin: SourcePin
    route_check_pin: SourcePin
    runtime_manifest_sha256: str
    route_review_sha256: str
    origin: str = "reviewed_candidate"

    def check(self, server: str) -> None:
        if self.origin not in ("reviewed_candidate", "offline_fixture") or server != SERVER:
            raise ValueError("reviewed exact route/runtime context required")
        if not _sha(self.runtime_manifest_sha256) or not _sha(self.route_review_sha256):
            raise ValueError("runtime/route review artifact binding missing")
        if (
            pin_source(self.runtime_check) != self.runtime_check_pin
            or pin_source(self.route_check) != self.route_check_pin
        ):
            raise ValueError("context validator source identity drift")
        if self.runtime_check() is not None or self.route_check(server) is not None:
            raise ValueError(
                "context validators must verify or raise, not return approval booleans"
            )


def _validate_bindings(
    bindings: BackendBindings, pins: BackendPins, context: ReviewedTransportContext
) -> None:
    if not isinstance(context, ReviewedTransportContext):
        raise ValueError("explicit reviewed route/runtime context is required")
    context.check(SERVER)
    if not isinstance(bindings, BackendBindings) or pin_bindings(bindings) != pins:
        raise ValueError("injected backend/SDK/HTTP component identity drift")
    if context.origin == "reviewed_candidate":
        expected = {
            "base_backend": ("vibe/core/llm/backend/mistral.py", "MistralBackend"),
            "sdk_factory": ("mistralai/client/sdk.py", "Mistral"),
            "async_http_factory": ("httpx/_client.py", "AsyncClient"),
            "sync_http_factory": ("httpx/_client.py", "Client"),
        }
        for name, pin in pins.components:
            relative, qualname = expected[name]
            if pin != SourcePin(PACKAGE + relative, AUDITED_SOURCES[relative], qualname):
                raise ValueError("installed injected class differs from audited source")
        # Additional request/retry/schema/hook sources must be covered by the
        # caller's closure, and are independently rechecked here without imports.
        for relative, expected_sha in AUDITED_SOURCES.items():
            if hashlib.sha256(Path(PACKAGE + relative).read_bytes()).hexdigest() != expected_sha:
                raise ValueError("audited request/retry/hook source drift")


def _object(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    dump = getattr(raw, "model_dump", None)
    if not callable(dump):
        raise ValueError("SDK raw completed response cannot be observed")
    value = dump(mode="json")
    if not isinstance(value, dict):
        raise ValueError("SDK raw response is not an object")
    return value


@dataclass(frozen=True)
class NativeRequestLimits:
    """Explicit future reviewed contract; bytes never imply a token budget.

    Positive integer fields map to SDK max_tokens, timeout_ms and a separate
    serialized-request byte ceiling. No model-context capacity is inferred here.
    The review hash is lineage, not authorization or account/payment evidence.
    """

    max_output_tokens: int
    timeout_ms: int
    max_request_bytes: int
    reviewed_contract_sha256: str

    def validate(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_output_tokens, self.timeout_ms, self.max_request_bytes)
        ) or not _sha(self.reviewed_contract_sha256):
            raise ValueError(
                "explicit positive native limits and reviewed contract binding required"
            )
        # SDK basesdk.py converts integer milliseconds to float seconds.
        try:
            finite_timeout = math.isfinite(self.timeout_ms / 1000)
        except OverflowError:
            finite_timeout = False
        if not finite_timeout:
            raise ValueError("native timeout conversion is nonfinite")


def _request_fields(values: dict, limits: NativeRequestLimits) -> None:
    limits.validate()
    if (
        values.get("model") != MODEL
        or values.get("reasoning_effort") != EFFORT
        or values.get("tools") is not None
        or values.get("tool_choice") is not None
        or values.get("stream") is not False
        or values.get("temperature") != 1.0
    ):
        raise ValueError("exact model/high/nonstream/no-tools request contract differs")
    maximum = values.get("max_tokens")
    if type(maximum) is not int or maximum != limits.max_output_tokens:
        raise ValueError("exact explicit native max_tokens required")


def _headers(values: Any) -> dict | None:
    if values is None:
        return None
    if not isinstance(values, dict):
        raise ValueError("native header input must be an object")
    allowed = {"user-agent", "x-affinity"}
    if any(not isinstance(k, str) or k.lower() not in allowed for k in values):
        raise ValueError("custom endpoint/security headers forbidden")
    for name, value in values.items():
        if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid native metadata header")
        if name.lower() == "user-agent" and value != "mistral-client-python/Mistral-Vibe/2.15.0":
            raise ValueError("native user agent differs from pinned Vibe version")
        if name.lower() == "x-affinity" and re.fullmatch("[0-9a-fA-F-]{36}", value) is None:
            raise ValueError("native affinity identity malformed")
    return dict(values)


@dataclass
class _State:
    context: ReviewedTransportContext
    pins: BackendPins
    observer: Callable[[dict], None]
    observer_pin: SourcePin
    limits: NativeRequestLimits
    complete_started: bool = False
    sdk_started: bool = False
    http_requests: int = 0
    responses: int = 0
    served_model: str | None = None
    maximum: int | None = None
    usage: dict = field(default_factory=dict)
    messages_sha256: str | None = None
    metadata_sha256: str | None = None

    def check(self) -> None:
        self.context.check(SERVER)
        self.limits.validate()
        if pin_source(self.observer) != self.observer_pin:
            raise ValueError("raw response observer identity drift")

    def observe(self, response: Any) -> None:
        self.check()
        raw = _object(response)
        if raw.get("model") != MODEL or type(raw.get("model")) is not str or self.responses:
            raise ValueError(
                "raw served model missing/mismatch/multiple; aliases are not observations"
            )
        choices = raw.get("choices")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or choices[0].get("finish_reason") != "stop"
        ):
            raise ValueError("completed raw response required")
        if choices[0].get("message", {}).get("tool_calls"):
            raise ValueError("raw response attempted tools")
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("raw usage missing")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage.get(name)) is not int or usage[name] < 0:
                raise ValueError("raw token counter malformed")
        if self.maximum is None or usage["completion_tokens"] > self.maximum:
            raise ValueError("reported output tokens exceed exact request bound")
        self.observer(raw)  # observe BEFORE inherited native mapping discards model
        self.responses, self.served_model, self.usage = (
            1,
            raw["model"],
            {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        )

    async def request_guard(self, request: Any) -> None:
        self.check()
        if self.http_requests:
            raise ValueError("second HTTP request refused; no retry/fallback")
        if str(request.url) != ENDPOINT or request.method != "POST":
            raise ValueError("final prepared URL/method differs from exact reviewed route")
        header_names = {str(k).lower() for k in request.headers}
        allowed = {
            "host",
            "accept",
            "content-type",
            "content-length",
            "accept-encoding",
            "connection",
            "authorization",
            "user-agent",
            "x-affinity",
        }
        if not header_names <= allowed:
            raise ValueError("unexpected final egress header")
        body = request.content
        if not isinstance(body, bytes) or not 0 < len(body) <= self.limits.max_request_bytes:
            raise ValueError("bounded serialized JSON request body required")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate request key")
                result[key] = value
            return result

        def nonfinite(_value):
            raise ValueError("nonfinite prepared request value")

        values = json.loads(body, object_pairs_hook=unique, parse_constant=nonfinite)
        if not isinstance(values, dict):
            raise ValueError("request is not an object")
        _request_fields(values, self.limits)
        allowed_body = {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "stream",
            "reasoning_effort",
            "tools",
            "tool_choice",
            "metadata",
        }
        if any(value is not None for key, value in values.items() if key not in allowed_body):
            raise ValueError("unreviewed prepared request field")
        if (
            _digest(values.get("messages")) != self.messages_sha256
            or _digest(values.get("metadata")) != self.metadata_sha256
        ):
            raise ValueError("serialized messages/metadata differ from native request")
        if values.get("max_tokens") != self.maximum:
            raise ValueError("serialized output bound differs")
        self.http_requests += 1

    async def response_guard(self, response: Any) -> None:
        if (
            str(response.url) != ENDPOINT
            or type(response.status_code) is not int
            or not 200 <= response.status_code < 300
        ):
            raise ValueError("redirect/error/unexpected response route refused")

    def sync_request_guard(self, _request: Any) -> None:
        raise ValueError("synchronous SDK requests forbidden")

    def receipt(self) -> dict:
        return {
            "status": "passive_observer_unverified",
            "activation": False,
            "production_admission": False,
            "provider_attested": False,
            "origin": self.context.origin,
            "requested_model": MODEL,
            "requested_effort": EFFORT,
            "native_effort_control": "reasoning_effort=high configured",
            "observed_model": self.served_model,
            "observed_effort": "UNKNOWN",
            "completed_raw_responses": self.responses,
            "http_request_hook_events": self.http_requests,
            "provider_http_attempt_count": "UNKNOWN",
            "requested_max_tokens": self.maximum,
            "explicit_limits": asdict(self.limits),
            "completion_deadline_scope": "one cooperative async completion; no outer retry",
            "external_supervisor_still_required": True,
            "sdk_hook_isolation": "required_external_fresh_runtime_validation",
            "native_runtime_proof_measured_here": False,
            "tracing_state": "expected_noop_not_observed_by_this_helper",
            "usage": dict(self.usage),
            "quota_balance": "UNKNOWN",
            "actual_charge": "UNKNOWN",
            "subscription_entitlement": "UNKNOWN",
            "no_paid_route": "UNVERIFIED",
            "endpoint_metadata_is_account_approval": False,
            "missing_evidence": [
                "isolated_sdk_loader_and_native_runtime_proof_unmeasured",
                "no_configured_tracer_state_not_measured_by_helper",
                "exact_server_id_acceptance_and_existing_account_route_unprobed",
                "subscription_no_paid_route_and_quota_balance_unverified",
                "observed_served_effort_UNKNOWN",
            ],
            "runtime_manifest_sha256": self.context.runtime_manifest_sha256,
            "route_review_sha256": self.context.route_review_sha256,
            "component_pins_sha256": _digest(asdict(self.pins)),
        }


class _ChatProxy:
    def __init__(self, chat: Any, state: _State):
        self._chat, self._state = chat, state

    async def complete_async(self, **kwargs):
        self._state.check()
        if self._state.sdk_started:
            raise ValueError("second SDK submission refused")
        _request_fields(kwargs, self._state.limits)
        if set(kwargs) - {
            "model",
            "messages",
            "temperature",
            "tools",
            "max_tokens",
            "tool_choice",
            "http_headers",
            "metadata",
            "stream",
            "reasoning_effort",
        }:
            raise ValueError("unreviewed SDK request override")
        _headers(kwargs.get("http_headers"))

        # SDK serializer drops None values from message models. Compare that
        # source-backed canonical JSON shape after SDK request hooks as well.
        def without_none(value):
            if isinstance(value, dict):
                return {k: without_none(v) for k, v in value.items() if v is not None}
            if isinstance(value, list):
                return [without_none(v) for v in value]
            return value

        self._state.messages_sha256 = _digest(without_none(kwargs.get("messages")))
        self._state.metadata_sha256 = _digest(kwargs.get("metadata"))
        self._state.sdk_started = True
        self._state.maximum = kwargs["max_tokens"]
        raw = await self._chat.complete_async(**kwargs, retries=None)
        self._state.observe(raw)
        return raw  # unchanged; preserve inherited native response/error mapping


class _ClientProxy:
    def __init__(self, client: Any, state: _State):
        self._client = client
        self.chat = _ChatProxy(client.chat, state)

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, **kwargs):
        return await self._client.__aexit__(**kwargs)


def build_observing_backend_type(
    *, bindings: BackendBindings, pins: BackendPins, context: ReviewedTransportContext
) -> type:
    """Concrete subclass factory; injected components only, no installed imports.

    Called by a future pinned loader after independent runtime/route review. It
    cannot attest subscription/no-PAYG or enable candidate/role admission.
    """
    _validate_bindings(bindings, pins, context)
    base: Any = bindings.base_backend

    class ObservingMistralBackend(base):
        def __init__(
            self,
            provider: Any,
            *,
            raw_response_observer: Callable[[dict], None],
            observer_pin: SourcePin,
            limits: NativeRequestLimits,
        ):
            _validate_bindings(bindings, pins, context)
            if (
                getattr(provider, "name", None) != "mistral"
                or getattr(provider, "api_base", None) != API_BASE
                or getattr(provider, "api_key_env_var", None) != "MISTRAL_API_KEY"
                or getattr(provider, "extra_headers", {}) != {}
            ):
                raise ValueError("exact existing provider route/context required")
            if not isinstance(limits, NativeRequestLimits):
                raise ValueError("explicit reviewed native limits required")
            limits.validate()
            self._observation = _State(context, pins, raw_response_observer, observer_pin, limits)
            self._observation.check()
            self._observer_closed = False
            self._owned_sync_client = None
            self._client_created = False
            super().__init__(provider=provider, timeout=limits.timeout_ms / 1000)
            if self._server_url != SERVER:
                raise ValueError("native backend resolved an unexpected endpoint")

        def _create_mistral_client(self):
            _validate_bindings(bindings, pins, context)
            self._observation.check()
            if self._client_created or self._observer_closed:
                raise ValueError("SDK client recreation/fallback forbidden")
            if type(self._api_key) is not str or not self._api_key:
                raise ValueError(
                    "existing explicit key context absent; no SDK environment fallback"
                )
            self._client_created = True
            self._owned_sync_client = bindings.sync_http_factory(
                verify=True,
                trust_env=False,
                follow_redirects=False,
                event_hooks={"request": [self._observation.sync_request_guard]},
            )
            self._http_client = bindings.async_http_factory(
                verify=True,
                trust_env=False,
                follow_redirects=False,
                event_hooks={
                    "request": [self._observation.request_guard],
                    "response": [self._observation.response_guard],
                },
            )
            silent = logging.Logger("vibe-candidate-observer-private")
            silent.disabled = True
            client = bindings.sdk_factory(
                api_key=self._api_key,
                server_url=SERVER,
                client=self._owned_sync_client,
                async_client=self._http_client,
                timeout_ms=self._observation.limits.timeout_ms,
                retry_config=None,
                debug_logger=silent,
            )
            return _ClientProxy(client, self._observation)

        async def __aenter__(self):
            if self._observer_closed:
                raise ValueError("closed observing backend cannot be reused")
            try:
                return await super().__aenter__()
            except BaseException:
                await self.aclose()
                raise

        async def __aexit__(self, *args, **kwargs):
            try:
                return await super().__aexit__(*args, **kwargs)
            finally:
                self._observer_closed = True
                if self._owned_sync_client is not None:
                    self._owned_sync_client.close()
                    self._owned_sync_client = None

        async def complete(
            self,
            *,
            model,
            messages,
            temperature,
            tools,
            max_tokens,
            tool_choice,
            extra_headers,
            metadata=None,
        ):
            _validate_bindings(bindings, pins, context)
            self._observation.check()
            if self._observer_closed or self._observation.complete_started:
                raise ValueError("completed/failed/uncertain backend cannot submit again")
            if getattr(model, "name", None) != MODEL or getattr(model, "thinking", None) != EFFORT:
                raise ValueError("exact model/thinking preset required")
            if tools not in (None, []) or tool_choice not in (None, "auto"):
                raise ValueError("tool declarations/forced choice forbidden")
            _headers(extra_headers)
            if (
                type(max_tokens) is not int
                or max_tokens != self._observation.limits.max_output_tokens
            ):
                raise ValueError(
                    "requested native output cap absent or contradicts reviewed limits"
                )
            self._observation.complete_started = True
            try:
                return await asyncio.wait_for(
                    super().complete(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        tools=None,
                        max_tokens=max_tokens,
                        tool_choice=None,
                        extra_headers=extra_headers,
                        metadata=metadata,
                    ),
                    timeout=self._observation.limits.timeout_ms / 1000,
                )
            except BaseException:
                await self.aclose()
                raise

        async def complete_streaming(self, **_kwargs):
            raise ValueError("streaming unsupported by the fixed observing transport")

        def observation_receipt(self):
            return self._observation.receipt()

    return ObservingMistralBackend


def selftest() -> dict:
    """Emitted inert proxy controls; never constructs SDK/HTTP/provider clients."""
    from types import SimpleNamespace

    received = []

    def runtime_check():
        return None

    def route_check(server):
        if server != SERVER:
            raise ValueError("inert route differs")

    def observer(raw):
        received.append(raw["model"])

    context = ReviewedTransportContext(
        runtime_check,
        route_check,
        pin_source(runtime_check),
        pin_source(route_check),
        "a" * 64,
        "b" * 64,
        origin="offline_fixture",
    )
    limits = NativeRequestLimits(2048, 1000, 8192, "c" * 64)
    state = _State(context, BackendPins(()), observer, pin_source(observer), limits)
    raw = {
        "model": MODEL,
        "choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": None}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }

    class InertChat:
        async def complete_async(self, **values):
            assert values.pop("retries") is None
            request = SimpleNamespace(
                url=ENDPOINT, method="POST", headers={}, content=json.dumps(values).encode()
            )
            await state.request_guard(request)
            await state.response_guard(SimpleNamespace(url=ENDPOINT, status_code=200))
            return raw

    proxy = _ChatProxy(InertChat(), state)
    request = dict(
        model=MODEL,
        reasoning_effort=EFFORT,
        temperature=1.0,
        tools=None,
        tool_choice=None,
        stream=False,
        max_tokens=2048,
        messages=[{"role": "user", "content": "inert only"}],
    )
    observed = asyncio.run(proxy.complete_async(**request))
    refused = False
    try:
        asyncio.run(proxy.complete_async(**request))
    except ValueError:
        refused = True
    receipt = state.receipt()
    checks = {
        "exact_explicit_token_limit": receipt["requested_max_tokens"] == 2048,
        "raw_response_unchanged": observed is raw,
        "raw_model_observed": received == [MODEL],
        "one_prepared_request": state.http_requests == 1,
        "second_submission_refused": refused,
        "observed_effort_unknown": receipt["observed_effort"] == "UNKNOWN",
        "no_account_or_activation_authority": not receipt["activation"]
        and receipt["no_paid_route"] == "UNVERIFIED",
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    return {
        "checked_assertions": checks,
        "synthetic_pipeline_controls_only": True,
        "activation": False,
        "default_live_backend": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        print(json.dumps(selftest(), sort_keys=True))
        return
    print(
        json.dumps(
            {
                "status": "passive_observer_unverified",
                "activation": False,
                "installed_sdk_imports": 0,
                "default_live_backend": False,
                "route_metadata_is_account_approval": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
