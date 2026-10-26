"""Unverified Vibe data-only candidate; no executable real-provider default.

Read-only source audit: installed mistral-vibe 2.15.0, package root below.
_harness_manager.py:48-145,235+ accepts zero sources, eliminating user/project
config, tools, agents, prompts and skills discovery. tools/manager.py:101-157
imports custom modules before filtering, but zero sources plus empty explicit
paths leave only pinned builtins; :208-218 denies '*', and :442 rejects disabled
names. hooks/config.py:89 returns empty when disabled. system_prompt.py:330-396
separately gates skill/subagent/project/user instructions. AgentLoop's public
constructor :253 accepts an injected backend and empty hook result. This avoids
cli/cli.py:58-71, which strips stdin and loses it if /dev/tty cannot be opened.

ModelConfig.name (_settings.py:452) is unrestricted text and backend/mistral.py:300
forwards it unchanged. Explicit medium3.5 is expressible without changing the
provider, but installed defaults only attest the mutable CLI-latest mapping.
Same-account acceptance/entitlement/no-paid-route and observed effort remain
UNPROBED. backend/mistral.py:315-335 discards raw response.model: observe the raw
SDK response BEFORE mapping, never infer identity from a local alias.

Muse matrix: native empty preset is named at byte313267391; that is not proof
that later tools/MCP/hooks augmentation is absent. Settings primitives exist at
338668957(run.toolset),338643710+(MCP/hook admissions/fallback),338642898(project
configuration ceiling),338643361(allowed_models/model_fallback). Explicit false
session MCP values are described at317839530, distinct from global MCP servers.
Native payload SHA9fd40ae9c6dfb847e3ef58b61405c004d0ebbf19ceb0297be966f750b3b84aa0
can bypass shell wrapper auto-update (wrapper:1089-1123); augmentation/policy
precedence still needs source or offline qualification. No invented disable flag.

This module imports only the standard library. Preparing a plan never imports
Vibe/SDKs or reads credentials. Its explicit inert driver exercises the public
API contract under network denial. Installed backend binding remains an explicit
unimplemented gate; these controls cannot mint transport/role qualification.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import socket
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PACKAGE = Path("/Users/teacher/.local/share/uv/tools/mistral-vibe/lib/python3.12/site-packages")
INSTALLED_IMPORT_ROOTS = (
    "/opt/anaconda3/lib/python3.12",
    "/opt/anaconda3/lib/python3.12/lib-dynload",
    str(PACKAGE),
)
MODEL = "mistral-medium-3.5"
EFFORT = "high"
AUDITED_SOURCES = {
    "mistralai/client/models/chatcompletionresponse.py": "65035ca8418b465fd1edc201679b2545af827724395ebb8387c4a2ee78db114a",
    "vibe/core/config/_settings.py": "89f2e55d283c8334ea0ec563fd87865ca3f74e5cf41cc1e22768525181a599f8",
    "vibe/core/config/harness_files/_harness_manager.py": "45c6e2bfcfde03de571ab715ebb943fd9cbb669c6899549b392aaa2c260efec8",
    "vibe/core/tools/manager.py": "3d0bb39cb4c9460ed1ff736d64fd2ca396bb0b62feb528d88ad56de41b4fc431",
    "vibe/core/agents/models.py": "1a7d0eeacc4f9b75228391520d2727da36364bc9d25107450656ae8ba051ebba",
    "vibe/core/agents/manager.py": "2111855b9a8e44b0674040bd6bcaadaca5999e798fe19641e1cd1721e4f5f0f6",
    "vibe/core/hooks/config.py": "38eb5e2f8f0430512816731dba59ba03311c80381e89a48fa75dd83b1b32725f",
    "vibe/core/system_prompt.py": "ad7bf913cec0c8f7d64a92245620c25bb608d53004da41d8800c13924c076ec1",
    "vibe/core/llm/backend/mistral.py": "788fea10170a68bd70d8769066d56c1bce6d8820fd66e9e7236d43b0b551cf3a",
    "vibe/core/experiments/session.py": "2ba5f9427c127b69c2c56fe6c22519049d9e17c7936a665b852b7ecb2a73a5cb",
}
GAPS = (
    "real_provider_backend_binding_unimplemented",
    "isolated_installed_api_loader_not_live_verified",
    "native_request_output_bound_unwired",
    "raw_provider_observer_not_live_verified",
    "exact_server_model_id_acceptance_unprobed",
    "existing_context_no_paid_route_unprobed",
    "observed_effort_UNKNOWN",
    "native_dependency_closure_requires_independent_review",
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def strict_final(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode()) > 65536:
        raise ValueError("final text absent or oversized")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def refuse(_value):
        raise ValueError("nonfinite JSON value")

    value = json.loads(text, object_pairs_hook=unique, parse_constant=refuse)
    # Roundtrip refuses overflowed floats such as 1e999 as well.
    json.dumps(value, allow_nan=False)
    if not isinstance(value, dict):
        raise ValueError("one final JSON object required")
    return value


@dataclass(frozen=True)
class FilePin:
    path: str
    sha256: str


def pin_file(path: Path) -> FilePin:
    path = path.resolve(strict=True)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= 512 * 1024 * 1024:
        raise ValueError("pin requires a bounded regular file")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
        opened = os.fstat(stream.fileno())

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)

    if identity(before) != identity(opened) or identity(opened) != identity(path.stat()):
        raise ValueError("file changed during pinning")
    return FilePin(str(path), h.hexdigest())


@dataclass(frozen=True)
class TreePin:
    root: str
    files: tuple[FilePin, ...]


def _tree(root: Path) -> TreePin:
    root = root.resolve(strict=True)
    files = []
    if not root.is_dir():
        raise ValueError("import root is not a directory")

    def refuse_error(error):
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=refuse_error):
        # -S and explicit sys.path exclude the base interpreter's global site.
        dirs[:] = [
            d
            for d in sorted(dirs)
            if not (
                root == Path(INSTALLED_IMPORT_ROOTS[0])
                and Path(directory) == root
                and d == "site-packages"
            )
        ]
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise ValueError("runtime directory symlink requires a reviewed snapshot")
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("runtime tree symlink must be resolved in a reviewed snapshot")
            if path.is_file():
                files.append(pin_file(path))
    return TreePin(str(root), tuple(files))


@dataclass(frozen=True)
class RuntimeManifest:
    interpreter: FilePin
    import_roots: tuple[str, ...]
    trees: tuple[TreePin, ...]
    support_files: tuple[FilePin, ...]
    origin: str = "installed_candidate"


def _check_locations(
    interpreter: Path, roots: tuple[str, ...], support: tuple[Path, ...], origin: str
) -> None:
    if origin == "installed_candidate":
        if (
            roots != INSTALLED_IMPORT_ROOTS
            or str(interpreter.resolve()) != "/opt/anaconda3/bin/python3.12"
        ):
            raise ValueError("installed code roots/interpreter are fixed by source review")
    elif origin == "offline_fixture":
        temporary = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
        if (
            str(interpreter.resolve()) != str(Path(sys.executable).resolve())
            or not roots
            or any(not any(Path(root).is_relative_to(base) for base in temporary) for root in roots)
        ):
            raise ValueError(
                "fixture runtime requires the audit interpreter and private temporary code roots"
            )
    else:
        raise ValueError("manifest origin unknown")
    repo = Path(__file__).resolve().parent.parent
    for item in support:
        path = item.resolve()
        code = path.suffix == ".py" and path.is_relative_to(repo)
        native = (
            origin == "installed_candidate"
            and path.is_relative_to(Path("/opt/anaconda3/lib"))
            and path.suffix in (".so", ".dylib", ".zip")
        )
        if not (code or native):
            raise ValueError(
                "support pins may name reviewed code/native dependencies, not arbitrary files"
            )


def snapshot_runtime_manifest(
    *,
    interpreter: Path,
    import_roots: tuple[Path, ...],
    support_files: tuple[Path, ...],
    origin: str = "installed_candidate",
) -> RuntimeManifest:
    """Explicit code-root inventory, never called automatically by prepare."""
    roots = tuple(str(p.resolve(strict=True)) for p in import_roots)
    _check_locations(interpreter, roots, support_files, origin)
    if origin == "installed_candidate" and roots != INSTALLED_IMPORT_ROOTS:
        raise ValueError("installed candidate import roots are fixed by source review")
    if origin not in ("installed_candidate", "offline_fixture") or not roots:
        raise ValueError("explicit candidate/fixture manifest origin required")
    return RuntimeManifest(
        pin_file(interpreter),
        roots,
        tuple(_tree(Path(p)) for p in roots),
        tuple(pin_file(p) for p in support_files),
        origin,
    )


def verify_manifest(manifest: RuntimeManifest) -> None:
    if not isinstance(manifest, RuntimeManifest) or not manifest.import_roots:
        raise ValueError("typed explicit runtime manifest required")
    _check_locations(
        Path(manifest.interpreter.path),
        manifest.import_roots,
        tuple(Path(p.path) for p in manifest.support_files),
        manifest.origin,
    )
    if manifest.origin == "installed_candidate" and (
        manifest.import_roots != INSTALLED_IMPORT_ROOTS
        or manifest.interpreter.sha256
        != "d01b09973b5bd3ff617e7849ce9c0bdb8991256f495ce190720ccc976cdaaee7"
        or manifest.interpreter.path != "/opt/anaconda3/bin/python3.12"
    ):
        raise ValueError("unreviewed installed import roots/interpreter")
    if manifest.origin not in ("installed_candidate", "offline_fixture"):
        raise ValueError("manifest origin unknown")
    if tuple(t.root for t in manifest.trees) != manifest.import_roots:
        raise ValueError("runtime tree coverage differs")
    if pin_file(Path(manifest.interpreter.path)) != manifest.interpreter:
        raise ValueError("interpreter drift")
    for tree in manifest.trees:
        if _tree(Path(tree.root)) != tree:
            raise ValueError("runtime import/resource inventory drift")
    for pin in manifest.support_files:
        if pin_file(Path(pin.path)) != pin:
            raise ValueError("runtime/bootstrap/support source drift")
    pins = {p.path: p.sha256 for t in manifest.trees for p in t.files}
    pins.update({p.path: p.sha256 for p in manifest.support_files})
    own = pin_file(Path(__file__))
    if pins.get(own.path) != own.sha256:
        raise ValueError("candidate bootstrap itself must be pinned")
    if manifest.origin == "installed_candidate":
        for relative, expected in AUDITED_SOURCES.items():
            if pins.get(str(PACKAGE / relative)) != expected:
                raise ValueError("installed public API source is not the audited version")


def explicit_config() -> dict:
    return {
        "active_model": "reviewed-medium-3.5",
        "models": [
            {
                "name": MODEL,
                "alias": "reviewed-medium-3.5",
                "provider": "mistral",
                "thinking": EFFORT,
                "temperature": 1.0,
                "supports_images": False,
            }
        ],
        "providers": [
            {
                "name": "mistral",
                "api_base": "https://api.mistral.ai/v1",
                "api_key_env_var": "MISTRAL_API_KEY",
                "backend": "mistral",
            }
        ],
        "enabled_tools": [],
        "disabled_tools": ["*"],
        "tool_paths": [],
        "agent_paths": [],
        "skill_paths": [],
        "enabled_agents": ["default"],
        "installed_agents": [],
        "default_agent": "default",
        "mcp_servers": [],
        "connectors": [],
        "enable_connectors": False,
        "enable_experimental_hooks": False,
        "include_project_context": False,
        "include_prompt_detail": False,
        "include_commit_signature": False,
        "enable_telemetry": False,
        "experiments": {"enable": False, "api_host": "", "client_key": ""},
        "session_logging": {"enabled": False},
        "enable_update_checks": False,
        "enable_notifications": False,
        "enable_otel": False,
        "vibe_code_enabled": False,
        "voice_mode_enabled": False,
        "narrator_enabled": False,
    }


@dataclass(frozen=True)
class CandidatePlan:
    requested_model: str
    requested_effort: str
    manifest: RuntimeManifest
    private_root: str
    config_json: str
    contract_json: str

    def to_dict(self) -> dict:
        return strict_final(self.contract_json)


def prepare_vibe_candidate(
    *, requested_model: str, requested_effort: str, manifest: RuntimeManifest, private_root: Path
) -> CandidatePlan:
    if requested_model != MODEL or requested_effort != EFFORT:
        raise ValueError("only the fixed reviewed Medium3.5/high candidate is supported")
    verify_manifest(manifest)
    if not private_root.is_absolute():
        raise ValueError("private runtime root must be explicit and absolute")
    root = private_root.resolve()
    if any(
        root.is_relative_to(Path(p)) or Path(p).is_relative_to(root) for p in manifest.import_roots
    ):
        raise ValueError("private runtime must not overlap pinned import roots")
    config = explicit_config()
    paths = {
        k: str(root / leaf)
        for k, leaf in [
            ("HOME", "home"),
            ("VIBE_HOME", "vibe"),
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("TMPDIR", "tmp"),
        ]
    }
    bootstrap = (
        f"import sys;sys.path[:]={list(manifest.import_roots)!r};"
        f'import runpy;runpy.run_path({str(Path(__file__).resolve())!r},run_name="__main__")'
    )
    argv = [
        manifest.interpreter.path,
        "-I",
        "-S",
        "-B",
        "-X",
        f'pycache_prefix={root/"pycache"}',
        "-c",
        bootstrap,
        "--candidate-bootstrap",
    ]
    contract = {
        "schema": "non-claude-data-only-candidate/v1",
        "status": "candidate_unverified",
        "activation": False,
        "production_admission": False,
        "provider_attested": False,
        "requested_model": MODEL,
        "requested_effort": EFFORT,
        "observed_effort": "UNKNOWN",
        "native_effort_control": {
            "status": "present_configured_not_live_observed",
            "config_field": "models[0].thinking",
            "configured_value": "high",
            "native_request_field": "reasoning_effort",
            "source_mapping": "high -> high; backend/mistral.py:186-194,300-315",
            "request_actually_sent": False,
        },
        "argv": argv,
        "stdin": "lossless UTF-8 prompt bytes; never in argv",
        "cwd": str(root / "cwd"),
        "environment": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", **paths},
        "existing_secret_context": {
            "variable": "MISTRAL_API_KEY",
            "value": "REDACTED_REQUIRED_EXTERNAL_CONTEXT",
            "not_read_or_copied": True,
        },
        "private_config_path": str(root / "config" / "candidate.json"),
        "config": config,
        "config_sha256": digest(config),
        "manifest": asdict(manifest),
        "manifest_sha256": digest(asdict(manifest)),
        "backend_gate": "unimplemented_real_provider",
        "missing_evidence": list(GAPS),
    }
    return CandidatePlan(
        MODEL, EFFORT, manifest, str(root), json.dumps(config), json.dumps(contract)
    )


@dataclass(frozen=True)
class ObjectPin:
    source: FilePin
    qualname: str


def pin_object(value: Any) -> ObjectPin:
    source = inspect.getsourcefile(value)
    if source is None:
        raise ValueError("injected factory has no pinnable source")
    return ObjectPin(pin_file(Path(source)), value.__qualname__)


@dataclass(frozen=True)
class VibePublicAPI:
    init_harness_files_manager: Callable
    VibeConfig: Callable
    HookConfigResult: Callable
    AgentLoop: Callable
    AssistantEvent: type


@dataclass(frozen=True)
class ExecutionPins:
    api: tuple[tuple[str, ObjectPin], ...]
    backend_factory: ObjectPin


class RawResponseObserver:
    """Only raw SDK response.model is an observation; synthetic origin stays explicit."""

    def __init__(self, requested_model: str):
        self.requested_model = requested_model
        self.model: str | None = None
        self.responses = 0

    def observe(self, raw: dict) -> None:
        if raw.get("model") != self.requested_model or type(raw.get("model")) is not str:
            raise ValueError("raw served model absent or differs; aliases cannot substitute")
        if self.responses:
            raise ValueError("multiple/fallback responses unsupported in this first slice")
        choices = raw.get("choices")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or choices[0].get("finish_reason") != "stop"
        ):
            raise ValueError("completed raw response evidence missing")
        message = choices[0].get("message", {})
        if message.get("tool_calls"):
            raise ValueError("raw response attempted tools")
        self.model, self.responses = raw["model"], 1


class InertObservingBackend:
    """Fixture-only proxy demonstrates observation BEFORE Vibe-style mapping."""

    inert_fixture = True

    def __init__(self, observer: RawResponseObserver, raw: dict):
        self.observer, self.raw = observer, raw

    async def complete(self, **_kwargs):
        self.observer.observe(self.raw)
        return SimpleNamespace(
            message=SimpleNamespace(content=self.raw["choices"][0]["message"]["content"])
        )


def load_installed_provider_backend():
    raise NotImplementedError("verified raw-response observing provider backend is not implemented")


@contextlib.contextmanager
def _network_denied() -> Iterator[None]:
    def deny(*_args, **_kwargs):
        raise RuntimeError("network forbidden in candidate inert execution")

    patches: list[tuple[Any, str]] = [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ]
    originals = [getattr(obj, name) for obj, name in patches]
    for obj, name in patches:
        setattr(obj, name, deny)
    try:
        yield
    finally:
        for (obj, name), original in zip(patches, originals, strict=True):
            setattr(obj, name, original)


async def _drive(loop: Any, api: VibePublicAPI, prompt: str) -> str:
    finals = []
    try:
        async for event in loop.act(prompt):
            if not isinstance(event, api.AssistantEvent) or getattr(
                event, "stopped_by_middleware", False
            ):
                raise ValueError("nonfinal/tool/uncertain candidate event")
            finals.append(getattr(event, "content", None))
        if len(finals) != 1:
            raise ValueError("exactly one completed assistant response required")
        text = finals[0]
        if not isinstance(text, str):
            raise ValueError("final assistant text is absent")
        return text
    finally:
        await loop.aclose()
        telemetry = getattr(loop, "telemetry_client", None)
        if telemetry is not None:
            await telemetry.aclose()


def run_vibe_candidate_inert(
    plan: CandidatePlan,
    prompt: bytes,
    *,
    api: VibePublicAPI,
    backend_factory: Callable,
    execution_pins: ExecutionPins,
) -> dict:
    """Concrete public-API driver, explicitly inert and network-denied throughout.

    No installed SDK/API loader is provided. Factories are Python-object injection
    from the trusted caller, never import paths or names from candidate text.
    """
    fresh = prepare_vibe_candidate(
        requested_model=plan.requested_model,
        requested_effort=plan.requested_effort,
        manifest=plan.manifest,
        private_root=Path(plan.private_root),
    )
    if fresh != plan or not isinstance(prompt, bytes) or not 0 < len(prompt) <= 4096:
        raise ValueError("candidate/config/prompt binding drift")
    expected_api = tuple(
        (name, pin_object(getattr(api, name))) for name in VibePublicAPI.__dataclass_fields__
    )
    if execution_pins.api != expected_api or execution_pins.backend_factory != pin_object(
        backend_factory
    ):
        raise ValueError("public API or backend factory identity drift")
    pinned = {p for tree in plan.manifest.trees for p in tree.files} | set(
        plan.manifest.support_files
    )
    if (
        any(pin.source not in pinned for _, pin in expected_api)
        or execution_pins.backend_factory.source not in pinned
    ):
        raise ValueError("injected code is outside the pinned runtime manifest")
    contract = plan.to_dict()
    root = Path(plan.private_root)
    root.mkdir(mode=0o700)
    for path in [
        contract["cwd"],
        *[v for k, v in contract["environment"].items() if k.endswith("HOME") or k == "TMPDIR"],
    ]:
        Path(path).mkdir(mode=0o700)
    config_path = Path(contract["private_config_path"])
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(plan.config_json)
    old_env, old_cwd = dict(os.environ), Path.cwd()
    observer = RawResponseObserver(MODEL)
    try:
        os.environ.clear()
        os.environ.update(contract["environment"])
        os.chdir(contract["cwd"])
        with _network_denied():
            # This order is a source-backed fence, not a settings-file convention.
            api.init_harness_files_manager()
            config = api.VibeConfig(**strict_final(plan.config_json))
            hooks = api.HookConfigResult(hooks=[], issues=[])
            backend = backend_factory(observer)
            if type(backend) is not InertObservingBackend:
                raise ValueError("only explicit inert observing backend is implemented")
            loop = api.AgentLoop(
                config,
                agent_name="default",
                backend=backend,
                max_turns=1,
                enable_streaming=False,
                headless=True,
                hook_config_result=hooks,
            )
            if loop.tool_manager.available_tools:
                asyncio.run(loop.aclose())
                raise ValueError("tool-free candidate preflight failed")
            final_text = asyncio.run(_drive(loop, api, prompt.decode("utf-8", errors="strict")))
            final = strict_final(final_text)
        if observer.responses != 1:
            raise ValueError("mandatory raw-response observation missing")
        return {
            "status": "offline_fixture_unverified",
            "activation": False,
            "production_admission": False,
            "provider_attested": False,
            "final": final,
            "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
            "config_sha256": contract["config_sha256"],
            "manifest_sha256": contract["manifest_sha256"],
            "requested_model": MODEL,
            "requested_effort": EFFORT,
            "observed_model": observer.model,
            "identity_source": "synthetic_raw_response_fixture",
            "observed_effort": "UNKNOWN",
            "native_effort_control": contract["native_effort_control"],
            "missing_evidence": list(GAPS),
        }
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def selftest() -> dict:
    """Emitted inert controls; no installed API/SDK/provider import."""
    sources = []
    received = []

    class Event:
        def __init__(self, content):
            self.content = content

    def initialize(*values, **kwargs):
        sources.append((values, kwargs))

    def config(**values):
        assert sources == [((), {})]
        return SimpleNamespace(**values)

    def hooks(**values):
        return SimpleNamespace(**values)

    class Loop:
        def __init__(self, _config, **values):
            self.backend = values["backend"]
            self.tool_manager = SimpleNamespace(available_tools={})

        async def act(self, prompt):
            received.append(prompt)
            chunk = await self.backend.complete()
            yield Event(chunk.message.content)

        async def aclose(self):
            pass

    raw = {
        "model": MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"verdict":"UNKNOWN"}', "tool_calls": None},
            }
        ],
    }

    def backend(observer):
        return InertObservingBackend(observer, raw)

    api = VibePublicAPI(initialize, config, hooks, Loop, Event)
    with tempfile.TemporaryDirectory(prefix="vibe-candidate-inert-") as temp:
        root = Path(temp)
        imports = root / "imports"
        imports.mkdir()
        (imports / "__init__.py").write_text("")
        manifest = snapshot_runtime_manifest(
            interpreter=Path(sys.executable),
            import_roots=(imports,),
            support_files=(Path(__file__),),
            origin="offline_fixture",
        )
        plan = prepare_vibe_candidate(
            requested_model=MODEL,
            requested_effort=EFFORT,
            manifest=manifest,
            private_root=root / "private",
        )
        pins = ExecutionPins(
            tuple(
                (name, pin_object(getattr(api, name)))
                for name in VibePublicAPI.__dataclass_fields__
            ),
            pin_object(backend),
        )
        result = run_vibe_candidate_inert(
            plan, b"inert packet\n  ", api=api, backend_factory=backend, execution_pins=pins
        )
    checks = {
        "zero_sources_before_config": sources == [((), {})],
        "lossless_stdin": received == ["inert packet\n  "],
        "raw_model_observed_without_alias": result["observed_model"] == MODEL,
        "effort_not_inferred": result["observed_effort"] == "UNKNOWN",
        "cannot_activate": result["activation"] is False
        and result["production_admission"] is False,
        "synthetic_not_provider_attestation": result["provider_attested"] is False,
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    return {
        "synthetic_pipeline_controls_only": True,
        "checked_assertions": checks,
        "activation": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-bootstrap", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        print(json.dumps(selftest(), sort_keys=True))
        return
    print(
        json.dumps(
            {
                "status": "candidate_unverified",
                "activation": False,
                "production_admission": False,
                "backend_gate": "unimplemented_real_provider",
                "sdk_imports": 0,
                "candidate_bootstrap_requested": args.candidate_bootstrap,
                "missing_evidence": list(GAPS),
            }
        )
    )


if __name__ == "__main__":
    main()
