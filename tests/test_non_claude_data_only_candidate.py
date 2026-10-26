"""Explicit inert API/bootstrap controls; no installed Vibe, SDK or provider runs."""

import json
import os
import socket
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import non_claude_data_only_candidate as c

CALLS = []
RAW = {
    "model": c.MODEL,
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


def test_network_is_denied_before_config_discovery_and_environment_restored(fixture):
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
    with pytest.raises(RuntimeError, match="network forbidden"):
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
