"""Explicit inert API/bootstrap controls; no installed Vibe, SDK or provider runs."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import non_claude_data_only_candidate as c

CALLS = []
RAW = {
    "model": c.MODEL,
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    "choices": [
        {
            "finish_reason": "stop",
            "message": {"content": '{"criterion_id":"C1","verdict":"UNKNOWN"}', "tool_calls": None},
        }
    ],
}


def init_sources(*sources, **kwargs):
    CALLS.append(("sources", sources, kwargs))


def make_config(**values):
    assert CALLS[0] == ("sources", (), {})
    CALLS.append(("config", values))
    return SimpleNamespace(**values)


def empty_hooks(**values):
    CALLS.append(("hooks", values))
    return SimpleNamespace(**values)


@dataclass
class Assistant:
    content: str
    stopped_by_middleware: bool = False


class Loop:
    def __init__(self, config, **kwargs):
        assert config.enabled_tools == [] and config.disabled_tools == ["*"]
        assert kwargs["agent_name"] == "default" and kwargs["max_turns"] == 1
        assert kwargs["headless"] and not kwargs["enable_streaming"]
        assert kwargs["hook_config_result"].hooks == []
        assert "previous_messages" not in kwargs
        self.backend = kwargs["backend"]
        self.tool_manager = SimpleNamespace(available_tools={})
        CALLS.append(("loop", dict(os.environ), str(Path.cwd())))

    async def act(self, prompt):
        CALLS.append(("prompt", prompt))
        chunk = await self.backend.complete()
        yield Assistant(chunk.message.content)

    async def aclose(self):
        CALLS.append(("closed",))


def backend_factory(observer):
    return c.InertObservingBackend(observer, RAW)


@pytest.fixture
def fixture(tmp_path):
    CALLS.clear()
    root = tmp_path / "imports"
    root.mkdir()
    (root / "stub.py").write_text("# inert import-root inventory\n")
    api = c.VibePublicAPI(init_sources, make_config, empty_hooks, Loop, Assistant)
    manifest = c.snapshot_runtime_manifest(
        interpreter=Path(sys.executable),
        import_roots=(root,),
        support_files=(Path(c.__file__), Path(__file__)),
        origin="offline_fixture",
    )
    plan = c.prepare_vibe_candidate(
        requested_model=c.MODEL,
        requested_effort=c.EFFORT,
        manifest=manifest,
        private_root=tmp_path / "private",
    )
    pins = c.ExecutionPins(
        tuple(
            (name, c.pin_object(getattr(api, name)))
            for name in c.VibePublicAPI.__dataclass_fields__
        ),
        c.pin_object(backend_factory),
    )
    return plan, api, pins


def execute(fixture, prompt=b"packet\n  "):
    plan, api, pins = fixture
    return c.run_vibe_candidate_inert(
        plan, prompt, api=api, backend_factory=backend_factory, execution_pins=pins
    )


def test_plan_emits_exact_isolation_stdin_config_and_never_native_authority(fixture):
    plan, _, _ = fixture
    doc = plan.to_dict()
    assert doc["argv"][1:4] == ["-I", "-S", "-B"]
    assert "pycache_prefix=" in doc["argv"][5]
    assert doc["argv"][-1] == "--candidate-bootstrap"
    assert doc["bootstrap_execution"] == "planned_not_executed_by_inert_driver"
    assert doc["inert_guard_scope"] == "trusted_same_process_tcp_connect_and_dns_only"
    assert doc["isolated_imports_proven"] is doc["kernel_sandbox"] is False
    assert doc["config"]["mcp_servers"] == [] and not doc["config"]["enable_experimental_hooks"]
    assert not doc["config"]["enable_connectors"] and not doc["config"]["include_project_context"]
    assert not doc["config"]["include_prompt_detail"] and not doc["config"]["experiments"]["enable"]
    assert doc["config"]["models"][0]["name"] == c.MODEL
    assert doc["config"]["providers"][0]["api_base"] == "https://api.mistral.ai/v1"
    assert doc["config_sha256"] == c.digest(doc["config"])
    assert doc["manifest_sha256"] == c.digest(doc["manifest"])
    assert doc["production_admission"] is doc["activation"] is False
    assert doc["backend_gate"] == "unimplemented_real_provider"
    assert not any(
        name == "vibe" or name.startswith(("vibe.", "mistralai")) for name in sys.modules
    )


def test_driver_zero_sources_precedes_config_and_uses_mandatory_observing_backend(
    fixture, monkeypatch
):
    monkeypatch.setenv("MISTRAL_API_KEY", "ambient-secret-must-not-enter")
    result = execute(fixture, b"snowman \xe2\x98\x83\n  ")
    assert result["status"] == "offline_fixture_unverified"
    assert not result["provider_attested"] and not result["activation"]
    assert result["bootstrap_execution"] == "planned_not_executed_by_inert_driver"
    assert result["inert_guard_scope"] == "trusted_same_process_tcp_connect_and_dns_only"
    assert result["isolated_imports_proven"] is result["kernel_sandbox"] is False
    assert result["observed_model"] == c.MODEL and result["observed_effort"] == "UNKNOWN"
    assert CALLS[0] == ("sources", (), {})
    assert next(row[1] for row in CALLS if row[0] == "prompt") == "snowman ☃\n  "
    env = next(row[1] for row in CALLS if row[0] == "loop")
    assert "MISTRAL_API_KEY" not in env and "ambient-secret-must-not-enter" not in json.dumps(env)
    assert os.environ["MISTRAL_API_KEY"] == "ambient-secret-must-not-enter"
    config = Path(fixture[0].to_dict()["private_config_path"])
    assert config.stat().st_mode & 0o777 == 0o600
    assert CALLS[-1] == ("closed",)


def network_config(**kwargs):
    socket.create_connection(("127.0.0.1", 1))
    return make_config(**kwargs)


def test_fixture_tcp_dns_guard_precedes_config_and_environment_is_restored(fixture):
    plan, api, pins = fixture
    api = replace(api, VibeConfig=network_config)
    pins = replace(
        pins,
        api=tuple(
            (name, c.pin_object(getattr(api, name)))
            for name in c.VibePublicAPI.__dataclass_fields__
        ),
    )
    before = dict(os.environ)
    with pytest.raises(RuntimeError, match="TCP-connect/DNS forbidden"):
        execute((plan, api, pins))
    assert dict(os.environ) == before


@pytest.mark.parametrize("change", ["added_file", "changed_file", "support_file", "interpreter"])
def test_manifest_inventory_and_interpreter_drift_refuse(fixture, tmp_path, change):
    plan, api, pins = fixture
    if change == "added_file":
        (Path(plan.manifest.import_roots[0]) / "hostile.py").write_text("raise RuntimeError()")
    elif change == "changed_file":
        (Path(plan.manifest.import_roots[0]) / "stub.py").write_text("# changed")
    elif change == "support_file":
        plan = replace(plan, manifest=replace(plan.manifest, support_files=()))
    else:
        plan = replace(
            plan,
            manifest=replace(
                plan.manifest, interpreter=replace(plan.manifest.interpreter, sha256="0" * 64)
            ),
        )
    with pytest.raises(ValueError):
        execute((plan, api, pins))
    assert CALLS == []


def test_config_and_factory_identity_drift_cannot_change_imports_or_agent(fixture):
    plan, api, pins = fixture
    altered = json.loads(plan.config_json)
    altered["agent_paths"] = ["/ambient/agents"]
    with pytest.raises(ValueError):
        execute((replace(plan, config_json=json.dumps(altered)), api, pins))
    with pytest.raises(ValueError):
        execute(
            (
                plan,
                api,
                replace(pins, backend_factory=replace(pins.backend_factory, qualname="other")),
            )
        )
    assert CALLS == []


@pytest.mark.parametrize(
    "raw",
    [
        {"choices": []},
        {"model": "mistral-vibe-cli-latest", "choices": []},
        {"model": c.MODEL, "choices": [{"finish_reason": "length", "message": {}}]},
        {"model": c.MODEL, "choices": [{"finish_reason": "stop", "message": {"tool_calls": [{}]}}]},
    ],
)
def test_observer_never_infers_model_alias_or_accepts_tools_or_incomplete_response(raw):
    with pytest.raises(ValueError):
        c.RawResponseObserver(c.MODEL).observe(raw)


def test_observer_does_not_promote_requested_or_extra_effort_and_refuses_multiple_responses():
    observer = c.RawResponseObserver(c.MODEL)
    observer.observe({**RAW, "reasoning_effort": "high"})
    with pytest.raises(ValueError):
        observer.observe(RAW)


@pytest.mark.parametrize(
    "text", ["```json\n{}\n```", "{}\n{}", "[]", '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}']
)
def test_strict_final_requires_one_semantic_object(text):
    with pytest.raises(ValueError):
        c.strict_final(text)


def test_fixed_model_effort_and_installed_import_roots_cannot_be_overridden(fixture, tmp_path):
    plan, _, _ = fixture
    with pytest.raises(ValueError):
        c.prepare_vibe_candidate(
            requested_model="mistral-vibe-cli-latest",
            requested_effort="high",
            manifest=plan.manifest,
            private_root=tmp_path / "different",
        )
    with pytest.raises(ValueError):
        c.snapshot_runtime_manifest(
            interpreter=Path(sys.executable),
            import_roots=(tmp_path,),
            support_files=(),
            origin="installed_candidate",
        )
    with pytest.raises(NotImplementedError, match="not implemented"):
        c.load_installed_provider_backend()


class ToolsVisible(Loop):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.tool_manager.available_tools = {"bash": object()}


def test_nonempty_tool_inventory_stops_before_act_and_closes_loop(fixture):
    plan, api, pins = fixture
    api = replace(api, AgentLoop=ToolsVisible)
    pins = replace(
        pins,
        api=tuple(
            (name, c.pin_object(getattr(api, name)))
            for name in c.VibePublicAPI.__dataclass_fields__
        ),
    )
    with pytest.raises(ValueError, match="tool-free"):
        execute((plan, api, pins))
    assert not any(row[0] == "prompt" for row in CALLS)
    assert CALLS[-1] == ("closed",)


def test_native_effort_mapping_is_source_present_but_not_live_observed(fixture):
    plan, _, _ = fixture
    control = plan.to_dict()["native_effort_control"]
    assert control["config_field"] == "models[0].thinking"
    assert control["configured_value"] == "high"
    assert control["native_request_field"] == "reasoning_effort"
    assert control["status"] == "present_configured_not_live_observed"
    assert control["request_actually_sent"] is False
    assert execute(fixture)["observed_effort"] == "UNKNOWN"


def test_directory_symlink_and_nonprivate_runtime_overlap_refuse(fixture, tmp_path):
    plan, _, _ = fixture
    imports = Path(plan.manifest.import_roots[0])
    with pytest.raises(ValueError, match="overlap"):
        c.prepare_vibe_candidate(
            requested_model=c.MODEL,
            requested_effort=c.EFFORT,
            manifest=plan.manifest,
            private_root=imports / "private",
        )
    (imports / "outside").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        c.verify_manifest(plan.manifest)


def test_emitted_selftest_runs_inert_controls(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["candidate", "--selftest"])
    c.main()
    result = json.loads(capsys.readouterr().out)
    assert result["synthetic_pipeline_controls_only"] is True
    assert len(result["checked_assertions"]) >= 6 and all(result["checked_assertions"].values())
    assert result["activation"] is False


def test_manifest_cannot_name_credential_or_arbitrary_support_files(fixture, tmp_path, monkeypatch):
    plan, _, _ = fixture
    secret = tmp_path / "auth.json"
    secret.write_text("synthetic secret fixture; must not be opened")
    original = c.pin_file

    def guarded(path):
        assert path != secret, "arbitrary secret file was read"
        return original(path)

    monkeypatch.setattr(c, "pin_file", guarded)
    bad = replace(
        plan.manifest,
        support_files=(*plan.manifest.support_files, c.FilePin(str(secret), "0" * 64)),
    )
    with pytest.raises(ValueError, match="support pins"):
        c.verify_manifest(bad)


# Retained installed public boundary source: no native module/SDK import at test time.
# vibe/core/agent_loop.py:1529-1599; full file SHA256 a144802d59503e030508f9a8e2c8168fc4c9c40e5ec348eef5ce86fe4463a256
NATIVE_CHAT_SHA256 = "17eb05a4d1f3fab3f9a04c63b6cf8ba1a9c448ad073c7dd1dfe224a23fc4dec3"
NATIVE_CHAT = 'async def _chat(\n    self, max_tokens: int | None = None, model_override: ModelConfig | None = None\n) -> LLMChunk:\n    active_model = model_override or self.config.get_active_model()\n    provider = self.config.get_provider_for_model(active_model)\n    backend_metadata = self._build_backend_metadata()\n\n    available_tools = self.format_handler.get_available_tools(self.tool_manager)\n    tool_choice = self.format_handler.get_tool_choice()\n\n    last_user_message = self._last_user_message()\n    self.telemetry_client.send_request_sent(\n        model=active_model.alias,\n        nb_context_chars=sum(len(m.content or "") for m in self.messages),\n        nb_context_messages=len(self.messages),\n        nb_prompt_chars=len(last_user_message.content or "")\n        if last_user_message\n        else 0,\n        call_type=backend_metadata.call_type,\n        message_id=backend_metadata.message_id,\n        attachment_counts=build_attachment_counts(\n            last_user_message, supports_images=active_model.supports_images\n        ),\n    )\n\n    try:\n        start_time = time.perf_counter()\n        result = await self.backend.complete(\n            model=active_model,\n            messages=self._messages_for_backend(active_model),\n            temperature=active_model.temperature,\n            tools=available_tools,\n            tool_choice=tool_choice,\n            extra_headers=self._get_extra_headers(provider),\n            max_tokens=max_tokens,\n            metadata=backend_metadata.model_dump(exclude_none=True),\n        )\n        end_time = time.perf_counter()\n\n        if result.usage is None:\n            raise AgentLoopLLMResponseError(\n                "Usage data missing in non-streaming completion response"\n            )\n        self._update_stats(usage=result.usage, time_seconds=end_time - start_time)\n\n        if result.correlation_id:\n            self.telemetry_client.last_correlation_id = result.correlation_id\n\n        processed_message = self.format_handler.process_api_response_message(\n            result.message\n        )\n        self.messages.append(processed_message)\n        if result.stop and result.stop.is_refusal:\n            raise _refusal_error(provider.name, active_model.name, result)\n        return LLMChunk(\n            message=processed_message, usage=result.usage, stop=result.stop\n        )\n\n    except Exception as e:\n        if isinstance(e, RefusalError):\n            raise\n        if _should_raise_rate_limit_error(e):\n            raise RateLimitError(provider.name, active_model.name) from e\n        if _is_context_too_long_error(e):\n            raise ContextTooLongError(provider.name, active_model.name) from e\n        if _is_non_retryable_error(e):\n            raise\n\n        raise RuntimeError(\n            f"API error from {provider.name} (model: {active_model.name}): {e}"\n        ) from e\n'
# vibe/core/llm/format.py:78-101; full file SHA256 bd6a7243f7965f588a5536535a751220142b80e7acabdd0a9df4786afccb7560
NATIVE_FORMAT_SHA256 = "293f88d5a45720e64aa04bded98407b493a94b6fd02cc0ca5193a332b67b811a"
NATIVE_FORMAT = 'def process_api_response_message(self, message: Any) -> LLMMessage:\n    clean_message = {\n        "role": message.role,\n        "content": message.content,\n        "reasoning_content": getattr(message, "reasoning_content", None),\n        "reasoning_state": getattr(message, "reasoning_state", None),\n        "reasoning_signature": getattr(message, "reasoning_signature", None),\n    }\n\n    if message.tool_calls:\n        clean_message["tool_calls"] = [\n            {\n                "id": tc.id,\n                "index": tc.index,\n                "type": "function",\n                "function": {\n                    "name": tc.function.name,\n                    "arguments": tc.function.arguments,\n                },\n            }\n            for tc in message.tool_calls\n        ]\n\n    return LLMMessage.model_validate(clean_message)\n'


def retained_native_boundary(backend):
    """Execute exact retained native attribute access/control flow with inert dependencies."""
    for source, sha in [(NATIVE_CHAT, NATIVE_CHAT_SHA256), (NATIVE_FORMAT, NATIVE_FORMAT_SHA256)]:
        assert hashlib.sha256(source.encode()).hexdigest() == sha
    namespace = {
        "time": time,
        "LLMChunk": SimpleNamespace,
        "LLMMessage": SimpleNamespace(model_validate=lambda values: SimpleNamespace(**values)),
        "build_attachment_counts": lambda *args, **kwargs: {},
        "AgentLoopLLMResponseError": ValueError,
        "RefusalError": type("RefusalError", (Exception,), {}),
        "_should_raise_rate_limit_error": lambda error: False,
        "_is_context_too_long_error": lambda error: False,
        "_is_non_retryable_error": lambda error: False,
    }
    exec(
        compile(
            "from __future__ import annotations\n" + NATIVE_CHAT + NATIVE_FORMAT,
            "<retained-native-public-boundary>",
            "exec",
        ),
        namespace,
    )
    model = SimpleNamespace(name=c.MODEL, alias="fixture", temperature=0.2, supports_images=False)
    provider = SimpleNamespace(name="fixture")
    messages = [SimpleNamespace(content="inert prompt")]
    stats = []
    formatter = SimpleNamespace(
        get_available_tools=lambda manager: [],
        get_tool_choice=lambda: "auto",
        process_api_response_message=lambda message: namespace["process_api_response_message"](
            None, message
        ),
    )
    session = SimpleNamespace(
        config=SimpleNamespace(
            get_active_model=lambda: model, get_provider_for_model=lambda model: provider
        ),
        _build_backend_metadata=lambda: SimpleNamespace(
            call_type="fixture", message_id=None, model_dump=lambda **kwargs: {}
        ),
        format_handler=formatter,
        tool_manager=object(),
        messages=messages,
        _last_user_message=lambda: messages[0],
        telemetry_client=SimpleNamespace(send_request_sent=lambda **kwargs: None),
        backend=backend,
        _messages_for_backend=lambda model: messages,
        _get_extra_headers=lambda provider: {},
        _update_stats=lambda **kwargs: stats.append(kwargs),
    )
    result = asyncio.run(namespace["_chat"](session, max_tokens=512))
    return result, stats, messages


def test_inert_chunk_passes_retained_native_chat_and_formatter_boundary():
    observer = c.RawResponseObserver(c.MODEL)
    chunk, stats, messages = retained_native_boundary(c.InertObservingBackend(observer, RAW))
    assert chunk.message.role == "assistant"
    assert chunk.message.content == RAW["choices"][0]["message"]["content"]
    assert chunk.usage.prompt_tokens == 3 and chunk.usage.completion_tokens == 2
    assert stats[0]["usage"] is chunk.usage and len(messages) == 2
    assert chunk.stop is None and observer.responses == 1


@pytest.mark.parametrize("missing", ["usage", "correlation_id", "stop", "role", "tool_calls"])
def test_retained_native_boundary_rejects_missing_native_chunk_fields(missing):
    class Broken:
        async def complete(self, **kwargs):
            chunk = await c.InertObservingBackend(c.RawResponseObserver(c.MODEL), RAW).complete()
            delattr(chunk.message if missing in ("role", "tool_calls") else chunk, missing)
            return chunk

    with pytest.raises(RuntimeError, match=missing):
        retained_native_boundary(Broken())


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": -1},
    ],
)
def test_inert_backend_requires_native_usage_without_fabrication(usage):
    observer = c.RawResponseObserver(c.MODEL)
    with pytest.raises(ValueError, match="native usage"):
        asyncio.run(c.InertObservingBackend(observer, {**RAW, "usage": usage}).complete())
    assert observer.responses == 0


def test_guard_scope_leaves_udp_existing_connection_processes_and_import_paths_untouched():
    originals = (socket.socket.sendto, socket.socket.sendall, subprocess.Popen, sys.path)
    with c._fixture_tcp_dns_guard():
        assert (
            socket.socket.sendto,
            socket.socket.sendall,
            subprocess.Popen,
            sys.path,
        ) == originals
        for operation, args in [
            (socket.getaddrinfo, ("invalid.test", 1)),
            (socket.create_connection, (("127.0.0.1", 1),)),
        ]:
            with pytest.raises(RuntimeError, match="TCP-connect/DNS forbidden"):
                operation(*args)
    assert (socket.socket.sendto, socket.socket.sendall, subprocess.Popen, sys.path) == originals
