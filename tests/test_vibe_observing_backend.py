"""Offline pinned native boundary + inert SDK/HTTP controls; never import installed SDKs."""

import asyncio
import copy
import hashlib
import inspect
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

import vibe_observing_backend as v

# Retained MistralBackend public/native lifecycle and complete; no SDK import.
# Installed source SHA256 788fea10170a68bd70d8769066d56c1bce6d8820fd66e9e7236d43b0b551cf3a
NATIVE_SOURCE_SHA256 = "a686f01baf5e5a9ac655bc94cdd47bb62b358149569e597fb6e73b690f19e6ed"
NATIVE_SOURCE = 'def __init__(self, provider: ProviderConfig, timeout: float = 720.0) -> None:\n    self._client: Mistral | None = None\n    self._http_client: httpx.AsyncClient | None = None\n    self._provider = provider\n    self._mapper = MistralMapper()\n    self._api_key = (\n        os.getenv(self._provider.api_key_env_var)\n        if self._provider.api_key_env_var\n        else None\n    )\n\n    reasoning_field = getattr(provider, "reasoning_field_name", "reasoning_content")\n    if reasoning_field != "reasoning_content":\n        raise ValueError(\n            f"Mistral backend does not support custom reasoning_field_name "\n            f"(got \'{reasoning_field}\'). Mistral uses ThinkChunk for reasoning."\n        )\n\n    # Mistral SDK takes server URL without api version as input\n    server_url = get_server_url_from_api_base(self._provider.api_base)\n    if not server_url:\n        raise ValueError(\n            f"Invalid API base URL: {self._provider.api_base}. "\n            "Expected format: <server_url>/v<api_version>"\n        )\n    self._server_url = server_url\n    self._timeout = timeout\n    self._retry_config = self._build_retry_config()\n\nasync def __aenter__(self) -> MistralBackend:\n    self._client = self._create_mistral_client()\n    await self._client.__aenter__()\n    return self\n\nasync def __aexit__(\n    self,\n    exc_type: type[BaseException] | None,\n    exc_val: BaseException | None,\n    exc_tb: types.TracebackType | None,\n) -> None:\n    client = self._client\n    http_client = self._http_client\n    self._client = None\n    self._http_client = None\n    try:\n        if client is not None:\n            await client.__aexit__(\n                exc_type=exc_type, exc_val=exc_val, exc_tb=exc_tb\n            )\n    finally:\n        if http_client is not None:\n            await http_client.aclose()\n\nasync def aclose(self) -> None:\n    await self.__aexit__(None, None, None)\n\ndef _get_client(self) -> Mistral:\n    if self._client is None:\n        self._client = self._create_mistral_client()\n    return self._client\n\nasync def complete(\n    self,\n    *,\n    model: ModelConfig,\n    messages: Sequence[LLMMessage],\n    temperature: float,\n    tools: list[AvailableTool] | None,\n    max_tokens: int | None,\n    tool_choice: StrToolChoice | AvailableTool | None,\n    extra_headers: dict[str, str] | None,\n    metadata: dict[str, str] | None = None,\n) -> LLMChunk:\n    try:\n        reasoning_effort = _THINKING_TO_REASONING_EFFORT.get(model.thinking)\n        if reasoning_effort is not None:\n            temperature = 1.0\n\n        response = await self._get_client().chat.complete_async(\n            model=model.name,\n            messages=[self._mapper.prepare_message(msg) for msg in messages],\n            temperature=temperature,\n            tools=[self._mapper.prepare_tool(tool) for tool in tools]\n            if tools\n            else None,\n            max_tokens=max_tokens,\n            tool_choice=self._mapper.prepare_tool_choice(tool_choice)\n            if tool_choice\n            else None,\n            http_headers=extra_headers,\n            metadata=metadata,\n            stream=False,\n            reasoning_effort=reasoning_effort,\n        )\n\n        message = response.choices[0].message\n        parsed = (\n            self._mapper.parse_content(message.content)\n            if message and message.content\n            else ParsedContent(content="", reasoning_content=None)\n        )\n        return LLMChunk(\n            message=LLMMessage(\n                role=Role.assistant,\n                content=parsed.content,\n                reasoning_content=parsed.reasoning_content,\n                tool_calls=self._mapper.parse_tool_calls(message.tool_calls)\n                if message and message.tool_calls\n                else None,\n            ),\n            usage=LLMUsage(\n                prompt_tokens=response.usage.prompt_tokens or 0,\n                completion_tokens=response.usage.completion_tokens or 0,\n            ),\n        )\n\n    except SDKError as e:\n        raise BackendErrorBuilder.build_http_error(\n            provider=self._provider.name,\n            endpoint=self._server_url,\n            error=e,\n            model=model.name,\n            messages=messages,\n            temperature=temperature,\n            has_tools=bool(tools),\n            tool_choice=tool_choice,\n        ) from e\n    except httpx.RequestError as e:\n        raise BackendErrorBuilder.build_request_error(\n            provider=self._provider.name,\n            endpoint=self._server_url,\n            error=e,\n            model=model.name,\n            messages=messages,\n            temperature=temperature,\n            has_tools=bool(tools),\n            tool_choice=tool_choice,\n        ) from e\n'

RAW = {
    "model": v.MODEL,
    "choices": [
        {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "{}", "tool_calls": None},
        }
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
}
CALLS = []
BEHAVIOR = {}


def runtime_check():
    CALLS.append("runtime_check")


def route_check(server):
    assert server == v.SERVER
    CALLS.append("route_check")


def observe(raw):
    CALLS.append(("raw_observed", raw["model"]))


class Mapper:
    def prepare_message(self, message):
        return message

    def parse_content(self, content):
        CALLS.append("native_mapping")
        return SimpleNamespace(content=content, reasoning_content=None)


class Chunk(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(correlation_id=None, stop=None, **kwargs)


class SDKError(Exception):
    pass


# Run the actual retained native methods, with only their external dependencies inert.
assert hashlib.sha256(NATIVE_SOURCE.encode()).hexdigest() == NATIVE_SOURCE_SHA256
NATIVE = {
    "os": SimpleNamespace(getenv=lambda name: "synthetic-inert-key"),
    "MistralMapper": Mapper,
    "get_server_url_from_api_base": lambda url: v.SERVER if url == v.API_BASE else None,
    "_THINKING_TO_REASONING_EFFORT": {"high": "high"},
    "LLMChunk": Chunk,
    "LLMMessage": SimpleNamespace,
    "LLMUsage": SimpleNamespace,
    "Role": SimpleNamespace(assistant="assistant"),
    "ParsedContent": SimpleNamespace,
    "SDKError": SDKError,
    "httpx": SimpleNamespace(RequestError=SDKError),
}
exec(
    compile(
        "from __future__ import annotations\n" + NATIVE_SOURCE,
        "<retained-native-mistral-backend>",
        "exec",
    ),
    NATIVE,
)


class NativeBackend:
    __init__ = NATIVE["__init__"]
    __aenter__ = NATIVE["__aenter__"]
    __aexit__ = NATIVE["__aexit__"]
    aclose = NATIVE["aclose"]
    _get_client = NATIVE["_get_client"]
    complete = NATIVE["complete"]

    def _build_retry_config(self):
        return "native-configured-retry-sentinel-must-not-reach-SDK"


class SyncHTTP:
    def __init__(self, **kwargs):
        self.kwargs, self.closed = kwargs, False
        CALLS.append(("sync_http", kwargs))

    def close(self):
        self.closed = True
        CALLS.append("sync_closed")


class AsyncHTTP:
    def __init__(self, **kwargs):
        self.kwargs, self.closed = kwargs, False
        CALLS.append(("async_http", kwargs))

    async def aclose(self):
        self.closed = True
        CALLS.append("async_closed")

    async def send(self, request):
        for hook in self.kwargs["event_hooks"]["request"]:
            await hook(request)
        CALLS.append(("inert_send", request))
        response = SimpleNamespace(
            url=BEHAVIOR.get("response_url", v.ENDPOINT), status_code=BEHAVIOR.get("status", 200)
        )
        for hook in self.kwargs["event_hooks"]["response"]:
            await hook(response)
        return response


class RawResponse:
    def __init__(self, raw):
        self.raw = raw
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(**item["message"])) for item in raw["choices"]
        ]
        self.usage = SimpleNamespace(**raw["usage"])

    def model_dump(self, *, mode):
        assert mode == "json"
        return copy.deepcopy(self.raw)


class SDKChat:
    def __init__(self, client):
        self.client = client

    async def complete_async(self, **kwargs):
        CALLS.append(("sdk_call", kwargs))
        assert kwargs.pop("retries") is None
        if BEHAVIOR.get("hang"):
            await asyncio.Event().wait()
        if BEHAVIOR.get("fail"):
            raise RuntimeError("synthetic SDK failure")
        body = {
            k: value
            for k, value in kwargs.items()
            if k not in ("http_headers", "metadata") and value is not None
        }
        body.update(BEHAVIOR.get("body_update", {}))
        headers = {
            "authorization": "Bearer synthetic-inert-key",
            "content-type": "application/json",
        }
        headers.update(BEHAVIOR.get("headers", {}))
        payload = BEHAVIOR.get("body", json.dumps(body).encode())
        request = SimpleNamespace(
            url=BEHAVIOR.get("request_url", v.ENDPOINT),
            method=BEHAVIOR.get("method", "POST"),
            content=payload,
            headers=headers,
        )
        await self.client.send(request)
        if BEHAVIOR.get("duplicate_send"):
            await self.client.send(request)
        return RawResponse(copy.deepcopy(BEHAVIOR.get("raw", RAW)))


class SDK:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SDKChat(kwargs["async_client"])
        CALLS.append(("sdk_init", kwargs))

    async def __aenter__(self):
        CALLS.append("sdk_enter")
        return self

    async def __aexit__(self, **kwargs):
        CALLS.append("sdk_exit")


@pytest.fixture
def setup():
    CALLS.clear()
    BEHAVIOR.clear()
    bindings = v.BackendBindings(NativeBackend, SDK, AsyncHTTP, SyncHTTP)
    pins = v.pin_bindings(bindings)
    context = v.ReviewedTransportContext(
        runtime_check,
        route_check,
        v.pin_source(runtime_check),
        v.pin_source(route_check),
        "a" * 64,
        "b" * 64,
        origin="offline_fixture",
    )
    cls = v.build_observing_backend_type(bindings=bindings, pins=pins, context=context)
    limits = v.NativeRequestLimits(
        max_output_tokens=2048,
        timeout_ms=1000,
        max_request_bytes=8192,
        reviewed_contract_sha256="c" * 64,
    )
    return cls, limits, bindings, pins, context


def make_backend(setup, **overrides):
    cls, limits, *_ = setup
    provider = SimpleNamespace(
        name="mistral", api_base=v.API_BASE, api_key_env_var="MISTRAL_API_KEY", extra_headers={}
    )
    return cls(
        provider,
        raw_response_observer=observe,
        observer_pin=v.pin_source(observe),
        limits=overrides.get("limits", limits),
    )


def kwargs(setup, **overrides):
    values = dict(
        model=SimpleNamespace(name=v.MODEL, thinking="high"),
        messages=[{"role": "user", "content": "inert lossless prompt\n  "}],
        temperature=0.2,
        tools=[],
        tool_choice="auto",
        extra_headers=None,
        max_tokens=setup[1].max_output_tokens,
    )
    return {**values, **overrides}


def run(setup, **overrides):
    backend = make_backend(setup)

    async def drive():
        async with backend:
            result = await backend.complete(**kwargs(setup, **overrides))
            return result

    return asyncio.run(drive()), backend.observation_receipt()


def test_native_complete_preserves_raw_identity_before_mapping_and_explicit_limits(setup):
    chunk, receipt = run(setup)
    assert chunk.message.content == "{}" and chunk.usage.prompt_tokens == 20
    assert CALLS.index(("raw_observed", v.MODEL)) < CALLS.index("native_mapping")
    call = next(item[1] for item in CALLS if isinstance(item, tuple) and item[0] == "sdk_call")
    assert call["max_tokens"] == 2048 and call["reasoning_effort"] == "high"
    assert call["temperature"] == 1.0 and call["tools"] is call["tool_choice"] is None
    assert call["stream"] is False and call["messages"][0]["content"].endswith("\n  ")
    for name in ("sync_http", "async_http"):
        http = next(item[1] for item in CALLS if isinstance(item, tuple) and item[0] == name)
        assert http["verify"] and not http["trust_env"] and not http["follow_redirects"]
    sdk = next(item[1] for item in CALLS if isinstance(item, tuple) and item[0] == "sdk_init")
    assert sdk["timeout_ms"] == 1000 and sdk["retry_config"] is None
    assert sdk["client"] and sdk["async_client"] and sdk["debug_logger"].disabled
    assert receipt["observed_model"] == v.MODEL and receipt["observed_effort"] == "UNKNOWN"
    assert receipt["explicit_limits"]["max_output_tokens"] == 2048
    assert (
        receipt["activation"]
        is receipt["production_admission"]
        is receipt["provider_attested"]
        is False
    )
    assert receipt["no_paid_route"] == "UNVERIFIED" and receipt["actual_charge"] == "UNKNOWN"
    assert "synthetic-inert-key" not in json.dumps(receipt)
    assert "sync_closed" in CALLS and "async_closed" in CALLS


@pytest.mark.parametrize("value", [None, 0, True, -1, 512, 2049])
def test_missing_or_contradictory_output_bound_refused_before_sdk(setup, value):
    with pytest.raises(ValueError, match="output cap"):
        run(setup, max_tokens=value)
    assert not any(isinstance(item, tuple) and item[0] == "sdk_call" for item in CALLS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_output_tokens", 0),
        ("max_output_tokens", True),
        ("timeout_ms", 0),
        ("timeout_ms", float("nan")),
        ("timeout_ms", 1.5),
        ("max_request_bytes", -1),
        ("reviewed_contract_sha256", "missing"),
    ],
)
def test_limits_have_no_defaults_and_require_positive_native_units(setup, field, value):
    with pytest.raises(ValueError, match="explicit positive"):
        make_backend(setup, limits=replace(setup[1], **{field: value}))
    with pytest.raises(TypeError):
        v.NativeRequestLimits()


@pytest.mark.parametrize(
    "change",
    [
        {"request_url": "https://unreviewed.invalid/v1/chat/completions"},
        {"method": "GET"},
        {"headers": {"x-custom-route": "bad"}},
        {"body_update": {"model": "alias"}},
        {"body_update": {"reasoning_effort": "low"}},
        {"body_update": {"tools": [{}]}},
        {"body_update": {"max_tokens": 2047}},
        {"body_update": {"stream": True}},
        {"body": b'{"model":"a","model":"b"}'},
        {"body": b"x" * 8193},
    ],
)
def test_final_prepared_request_mutation_refused_before_inert_send(setup, change):
    BEHAVIOR.update(change)
    with pytest.raises(ValueError):
        run(setup)
    assert not any(isinstance(item, tuple) and item[0] == "inert_send" for item in CALLS)


@pytest.mark.parametrize(
    "change",
    [
        {"status": 302},
        {"status": 403},
        {"response_url": "https://unreviewed.invalid"},
        {"duplicate_send": True},
    ],
)
def test_redirect_error_and_repeated_http_request_stop_without_retry(setup, change):
    BEHAVIOR.update(change)
    with pytest.raises(ValueError):
        run(setup)
    assert len([item for item in CALLS if isinstance(item, tuple) and item[0] == "sdk_call"]) == 1
    assert len([item for item in CALLS if isinstance(item, tuple) and item[0] == "inert_send"]) == 1
    assert "native_mapping" not in CALLS


@pytest.mark.parametrize(
    "change",
    [
        {"model": "mistral-vibe-cli-latest"},
        {"model": None},
        {"choices": [{"finish_reason": "length", "message": {}}]},
        {"choices": [{"finish_reason": "stop", "message": {"tool_calls": [{}]}}]},
        {"usage": {"prompt_tokens": True, "completion_tokens": 3, "total_tokens": 23}},
        {"usage": {"prompt_tokens": 20, "completion_tokens": 2049, "total_tokens": 2069}},
    ],
)
def test_raw_model_usage_completion_failures_precede_native_mapping(setup, change):
    BEHAVIOR["raw"] = {**RAW, **change}
    with pytest.raises(ValueError):
        run(setup)
    assert "native_mapping" not in CALLS
    assert not any(isinstance(item, tuple) and item[0] == "raw_observed" for item in CALLS)


def test_timeout_closes_and_prevents_second_submission(setup):
    BEHAVIOR["hang"] = True
    backend = make_backend(setup, limits=replace(setup[1], timeout_ms=5))

    async def drive():
        async with backend:
            with pytest.raises(TimeoutError):
                await backend.complete(**kwargs(setup))
            with pytest.raises(ValueError, match="cannot submit again"):
                await backend.complete(**kwargs(setup))

    asyncio.run(drive())
    assert len([item for item in CALLS if isinstance(item, tuple) and item[0] == "sdk_call"]) == 1
    assert "sync_closed" in CALLS and "async_closed" in CALLS


def test_absent_context_or_pin_drift_refuses_without_creating_clients(setup):
    _, _, bindings, pins, context = setup
    with pytest.raises(ValueError, match="context"):
        v.build_observing_backend_type(bindings=bindings, pins=pins, context=None)
    with pytest.raises(ValueError, match="identity drift"):
        v.build_observing_backend_type(
            bindings=bindings, pins=replace(pins, components=()), context=context
        )
    with pytest.raises(ValueError, match="artifact binding"):
        v.build_observing_backend_type(
            bindings=bindings, pins=pins, context=replace(context, route_review_sha256="")
        )
    assert not any(isinstance(item, tuple) and item[0] == "sdk_init" for item in CALLS)


def test_module_import_has_no_sdk_or_live_default():
    assert not any(
        name == "vibe" or name.startswith(("vibe.", "mistralai", "httpx")) for name in sys.modules
    )
    assert (
        inspect.signature(v.NativeRequestLimits).parameters["max_output_tokens"].default
        is inspect.Parameter.empty
    )


@pytest.mark.parametrize(
    "change",
    [
        {"body_update": {"n": 2}},
        {"body_update": {"messages": [{"role": "user", "content": "changed"}]}},
        {"body_update": {"metadata": {"injected": "context"}}},
    ],
)
def test_sdk_hook_cannot_add_choices_or_alter_native_messages_metadata(setup, change):
    BEHAVIOR.update(change)
    with pytest.raises(ValueError):
        run(setup)
    assert not any(isinstance(item, tuple) and item[0] == "inert_send" for item in CALLS)


def test_sdk_failure_and_success_are_both_single_submission(setup):
    for failed in (False, True):
        BEHAVIOR["fail"] = failed
        backend = make_backend(setup)

        async def drive():
            async with backend:
                if failed:
                    with pytest.raises(RuntimeError, match="synthetic SDK failure"):
                        await backend.complete(**kwargs(setup))
                else:
                    await backend.complete(**kwargs(setup))
                with pytest.raises(ValueError, match="cannot submit again"):
                    await backend.complete(**kwargs(setup))

        asyncio.run(drive())
    assert len([item for item in CALLS if isinstance(item, tuple) and item[0] == "sdk_call"]) == 2


def test_emitted_selftest_never_constructs_sdk_http_or_native_backend(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["observer", "--selftest"])
    monkeypatch.setattr(
        v, "build_observing_backend_type", lambda **kwargs: pytest.fail("live factory reached")
    )
    v.main()
    output = json.loads(capsys.readouterr().out)
    assert output["synthetic_pipeline_controls_only"] and not output["default_live_backend"]
    assert len(output["checked_assertions"]) >= 7 and all(output["checked_assertions"].values())


# Retained mistralai/client/chat.py SHA256 9dd791daac83b51820aaea8fa23d8649806838f6157543b9f473d9c189121416
SDK_RETRY_SHA256 = "206dacf42b04f98b08ff0f645924462e146a04de481680dece7c602da0e52d1a"
SDK_RETRY = 'if retries == UNSET:\n    if self.sdk_configuration.retry_config is not UNSET:\n        retries = self.sdk_configuration.retry_config\n\nretry_config = None\nif isinstance(retries, utils.RetryConfig):\n    retry_config = (retries, ["429", "500", "502", "503", "504"])\n'


# Retained mistralai/client/basesdk.py SHA256 a13d2a4677d4adfbf5314f25e2eb1ad28f9e84e2997b2a45822588b6feccc22f
SDK_SEND_SHA256 = "21963ad8b0a8547cb20eaa2cfa386fdc478ebbada18be3602cb5a0cd6c7ef29d"
SDK_SEND = "if retry_config is not None:\n    http_res = await utils.retry_async(\n        do, utils.Retries(retry_config[0], retry_config[1])\n    )\nelse:\n    http_res = await do()\n"


def test_retained_sdk_none_disables_configured_retries_and_uses_single_do():
    class RetryConfig:
        pass

    unset = object()
    configured = RetryConfig()
    assert hashlib.sha256(SDK_RETRY.encode()).hexdigest() == SDK_RETRY_SHA256
    assert hashlib.sha256(SDK_SEND.encode()).hexdigest() == SDK_SEND_SHA256
    for supplied, expected in [(None, None), (unset, configured)]:
        namespace = {
            "UNSET": unset,
            "retries": supplied,
            "self": SimpleNamespace(sdk_configuration=SimpleNamespace(retry_config=configured)),
            "utils": SimpleNamespace(RetryConfig=RetryConfig),
        }
        exec(compile(SDK_RETRY, "<retained-sdk-retry-decision>", "exec"), namespace)
        assert namespace["retry_config"] == (
            None if expected is None else (expected, ["429", "500", "502", "503", "504"])
        )
    calls = []

    async def do():
        calls.append("do")
        return "inert-response"

    def forbidden_retry(*args):
        pytest.fail("SDK retry wrapper unexpectedly used")

    namespace = {
        "do": do,
        "retry_config": None,
        "utils": SimpleNamespace(retry_async=forbidden_retry, Retries=forbidden_retry),
    }
    source = (
        "async def invoke():\n"
        + "\n".join("    " + line for line in SDK_SEND.splitlines())
        + "\n    return http_res\n"
    )
    exec(compile(source, "<retained-sdk-send-decision>", "exec"), namespace)
    assert asyncio.run(namespace["invoke"]()) == "inert-response"
    assert calls == ["do"]
