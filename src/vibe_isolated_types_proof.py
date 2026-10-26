"""Offline trusted-code import proof, never a provider loader or OS sandbox.

Native mode imports only public response types. AgentLoop/formatter methods are
SHA-pinned AST extractions, NOT an import or construction of AgentLoop. Python
import/audit controls constrain this trusted experiment, not hostile native code.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

INTERPRETER = Path("/opt/anaconda3/bin/python3.12")
STDLIB = Path("/opt/anaconda3/lib/python3.12")
PACKAGES = Path("/Users/teacher/.local/share/uv/tools/mistral-vibe/lib/python3.12/site-packages")
PUBLIC = (
    "vibe",
    "vibe.core",
    "vibe.core.types",
    "vibe.core.experiments",
    "vibe.core.experiments.models",
    "vibe.core.telemetry",
    "vibe.core.telemetry.types",
)
DEPENDENCIES = (
    "pydantic",
    "pydantic_core",
    "typing_extensions",
    "annotated_types",
    "typing_inspection",
)
METHOD_FILES = {
    "agent_loop.py": "a144802d59503e030508f9a8e2c8168fc4c9c40e5ec348eef5ce86fe4463a256",
    "llm/format.py": "bd6a7243f7965f588a5536535a751220142b80e7acabdd0a9df4786afccb7560",
}
MODES = ("native", "sdk", "config", "tool", "network", "process", "cwd", "home")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(source: str, class_name: str, method: str) -> str:
    tree = ast.parse(source)
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(
        n
        for n in owner.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method
    )
    return (
        "\n".join(line[4:] for line in source.splitlines()[node.lineno - 1 : node.end_lineno])
        + "\n"
    )


def code_files(root: Path) -> Any:
    if root.is_file():
        yield root
        return
    for folder, directories, names in os.walk(root):
        directories[:] = [d for d in directories if d not in ("site-packages", "__pycache__")]
        for name in names:
            yield Path(folder) / name


def prepare_manifest() -> dict[str, Any]:
    """Read code bytes only; no package imports, settings, credentials or CLI calls."""
    files: dict[str, str] = {}
    roots = [STDLIB]
    roots += [PACKAGES / name for name in DEPENDENCIES if (PACKAGES / name).is_dir()]
    roots += [
        PACKAGES / (name + ".py") for name in DEPENDENCIES if (PACKAGES / (name + ".py")).is_file()
    ]
    roots += [
        PACKAGES / name.replace(".", "/") / "__init__.py"
        for name in PUBLIC
        if (PACKAGES / name.replace(".", "/") / "__init__.py").is_file()
    ]
    roots += [
        PACKAGES / (name.replace(".", "/") + ".py")
        for name in PUBLIC
        if (PACKAGES / (name.replace(".", "/") + ".py")).is_file()
    ]
    for root in roots:
        for path in code_files(root):
            if path.is_file() and path.suffix in (".py", ".so") and "__pycache__" not in path.parts:
                files[str(path.resolve())] = digest(path)
    for name, expected in METHOD_FILES.items():
        path = PACKAGES / "vibe/core" / name
        if digest(path) != expected:
            raise ValueError("native method source drift")
        files[str(path)] = expected
    files[str(Path(__file__).resolve())] = digest(Path(__file__))
    return {
        "interpreter": str(INTERPRETER),
        "interpreter_sha256": digest(INTERPRETER),
        "files": files,
    }


class PinnedSource(importlib.machinery.SourceFileLoader):
    expected: str

    def get_code(self, fullname: str) -> Any:
        # Compile verified source directly: -B alone does not prevent stale pyc reads.
        data = self.get_data(self.path)
        if hashlib.sha256(data).hexdigest() != self.expected:
            raise ImportError("source drift before compilation")
        return compile(data, self.path, "exec", dont_inherit=True)


class Guard(importlib.abc.MetaPathFinder):
    def __init__(self, pins: dict[str, str]):
        self.pins = pins
        self.loaded: dict[str, dict[str, str]] = {}

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        allowed = fullname in PUBLIC or any(
            fullname == p or fullname.startswith(p + ".") for p in DEPENDENCIES
        )
        if (
            fullname.split(".")[0] not in sys.stdlib_module_names
            and fullname != "_sysconfigdata__darwin_darwin"
            and not allowed
        ):
            raise ImportError("offline proof forbidden import: " + fullname)
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin in (None, "built-in", "frozen"):
            return spec
        origin = str(Path(spec.origin).resolve())
        if self.pins.get(origin) != digest(Path(origin)):
            raise ImportError("unreviewed import bytes: " + fullname)
        self.loaded[fullname] = {"path": origin, "sha256": self.pins[origin]}
        if isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            loader = PinnedSource(fullname, origin)
            loader.expected = self.pins[origin]
            spec.loader = loader
        return spec


def audit(event: str, args: tuple[Any, ...], pins: dict[str, str]) -> None:
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event in (
        "os.system",
        "os.posix_spawn",
        "pty.spawn",
    ):
        raise PermissionError("offline proof blocked event: " + event)
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        mode = args[1]
        flags = args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int)
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        ):
            raise PermissionError("offline proof blocked file mutation")
        path = Path(os.fsdecode(args[0])).resolve()
        if str(path) not in pins:
            raise PermissionError("offline proof blocked non-code read")
        if path.suffix in (".pth", ".pyc"):
            raise PermissionError("offline proof blocked site/cache read")


def boundary() -> dict[str, Any]:
    # Actual native public types; no SDK import, factory, configuration or tool discovery.
    native_types = importlib.import_module("vibe.core.types")
    AgentStats, LLMChunk, LLMMessage, LLMUsage = (
        getattr(native_types, name) for name in ("AgentStats", "LLMChunk", "LLMMessage", "LLMUsage")
    )

    namespace: dict[str, Any] = {
        "LLMChunk": LLMChunk,
        "LLMMessage": LLMMessage,
        "time": time,
        "build_attachment_counts": lambda *a, **k: {},
        "RefusalError": type("RefusalError", (Exception,), {}),
        "AgentLoopLLMResponseError": ValueError,
    }
    for name in (
        "_should_raise_rate_limit_error",
        "_is_context_too_long_error",
        "_is_non_retryable_error",
    ):
        namespace[name] = lambda e: False
    methods = {}
    for file, cls, name in [
        ("agent_loop.py", "AgentLoop", "_chat"),
        ("agent_loop.py", "AgentLoop", "_update_stats"),
        ("llm/format.py", "APIToolFormatHandler", "process_api_response_message"),
    ]:
        path = PACKAGES / "vibe/core" / file
        if digest(path) != METHOD_FILES[file]:
            raise ValueError("native method source drift")
        source = extract(path.read_text(), cls, name)
        exec(
            compile(
                "from __future__ import annotations\n" + source, str(path) + ":" + name, "exec"
            ),
            namespace,
        )
        methods[name] = hashlib.sha256(source.encode()).hexdigest()
    message = LLMMessage(role="assistant", content="inert completion")
    usage = LLMUsage(prompt_tokens=3, completion_tokens=2)
    chunk = LLMChunk(message=message, usage=usage)
    calls = []

    class Backend:
        async def complete(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return chunk

    model = SimpleNamespace(
        alias="inert-only", name="inert-only", temperature=1.0, supports_images=False
    )
    formatter = SimpleNamespace(
        get_available_tools=lambda _: None,
        get_tool_choice=lambda: None,
        process_api_response_message=lambda value: namespace["process_api_response_message"](
            None, value
        ),
    )
    state = SimpleNamespace(
        config=SimpleNamespace(
            get_active_model=lambda: model,
            get_provider_for_model=lambda _: SimpleNamespace(name="inert"),
        ),
        _build_backend_metadata=lambda: SimpleNamespace(
            call_type="inert", message_id="inert", model_dump=lambda **k: {}
        ),
        format_handler=formatter,
        tool_manager=None,
        _last_user_message=lambda: None,
        telemetry_client=SimpleNamespace(send_request_sent=lambda **k: None),
        messages=[],
        backend=Backend(),
        _messages_for_backend=lambda _: [],
        _get_extra_headers=lambda _: {},
        stats=AgentStats(),
    )
    state._update_stats = lambda **kwargs: namespace["_update_stats"](state, **kwargs)
    coroutine = namespace["_chat"](state, max_tokens=7)
    try:
        coroutine.send(None)
    except StopIteration as done:
        result = done.value
    else:
        coroutine.close()
        raise AssertionError("unexpected asynchronous IO suspension")
    assert isinstance(result, LLMChunk) and isinstance(result.usage, LLMUsage)
    assert state.stats.session_prompt_tokens == 3 and state.stats.session_completion_tokens == 2
    assert len(calls) == 1 and calls[0]["max_tokens"] == 7 and calls[0]["tools"] is None
    try:
        LLMChunk(message=message, usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
    except ValueError:
        invalid_usage_rejected = True
    else:
        raise AssertionError("non-native invalid usage accepted")
    return {
        "native_public_types": True,
        "native_usage_validation": invalid_usage_rejected,
        "inert_backend_calls": len(calls),
        "method_sha256": methods,
    }


def child(payload: dict[str, Any]) -> dict[str, Any]:
    if not (
        sys.flags.isolated == 1 and sys.flags.no_site == 1 and sys.flags.dont_write_bytecode == 1
    ):
        raise RuntimeError("fresh -I -S -B required")
    if set(os.environ) - {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "VIBE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PYDANTIC_DISABLE_PLUGINS",
    }:
        raise RuntimeError("unexpected environment keys")
    if sys.pycache_prefix != str(Path(os.environ["HOME"]) / "empty-pycache"):
        raise RuntimeError("fresh empty pycache prefix required")
    manifest = payload["manifest"]
    if (
        str(Path(sys.executable).resolve()) != str(INTERPRETER.resolve())
        or digest(INTERPRETER) != manifest["interpreter_sha256"]
    ):
        raise RuntimeError("interpreter drift")
    if os.environ.get("PYDANTIC_DISABLE_PLUGINS") != "__all__":
        raise RuntimeError("Pydantic plugin discovery must be disabled")
    if manifest["files"].get(str(Path(__file__).resolve())) != digest(Path(__file__)):
        raise RuntimeError("proof source drift")
    guard = Guard(manifest["files"])
    sys.meta_path.insert(0, guard)
    sys.addaudithook(lambda event, args: audit(event, args, manifest["files"]))
    preloaded = {}
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin and Path(origin).is_file():
            resolved = str(Path(origin).resolve())
            if manifest["files"].get(resolved) != digest(Path(resolved)):
                raise RuntimeError("unreviewed startup module: " + name)
            preloaded[name] = {"path": resolved, "sha256": manifest["files"][resolved]}
    mode = payload["mode"]
    if mode in ("sdk", "config", "tool", "cwd"):
        __import__(
            {
                "sdk": "mistralai",
                "config": "vibe.core.config",
                "tool": "vibe.core.tools",
                "cwd": "cwd_canary",
            }[mode]
        )
    elif mode == "home":
        Path(os.environ["HOME"], "credential_canary").read_text()
    elif mode == "network":
        import socket

        socket.socket()
    elif mode == "process":
        subprocess.Popen(["/nonexistent-offline-canary"])
    elif mode != "native":
        raise ValueError("unknown mode")
    result = boundary()
    for record in guard.loaded.values():
        if digest(Path(record["path"])) != record["sha256"]:
            raise RuntimeError("closure changed during proof")
    assert "site" not in sys.modules
    assert not any(name == "mistralai" or name.startswith("mistralai.") for name in sys.modules)
    assert "vibe.core.agent_loop" not in sys.modules
    return {
        **result,
        "startup_import_closure": preloaded,
        "import_closure": guard.loaded,
        "site_imported": False,
        "observed_sys_path": sys.path,
        "observed_pycache_prefix": sys.pycache_prefix,
        "flags": {
            "isolated": sys.flags.isolated,
            "no_site": sys.flags.no_site,
            "dont_write_bytecode": sys.flags.dont_write_bytecode,
        },
        "full_agent_loop_imported": False,
        "full_agent_loop_constructed": False,
        "native_extension_os_dependencies_pinned": False,
        "native_provider_loader": "absent",
        "telemetry": "inert_injected_callback_only",
        "os_sandbox": False,
        "sdk_provider_calls": 0,
        "activation": False,
        "qualification": "unverified_offline_component_only",
    }


def run_proof(manifest: dict[str, Any], mode: str = "native") -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("unknown mode")
    if (
        manifest["interpreter"] != str(INTERPRETER)
        or digest(INTERPRETER) != manifest["interpreter_sha256"]
    ):
        raise ValueError("interpreter mismatch")
    with tempfile.TemporaryDirectory(prefix="vibe-types-proof-") as private:
        root = Path(private)
        (root / "cwd_canary.py").write_text('raise AssertionError("cwd executed")')
        (root / "credential_canary").write_text("INERT_NOT_A_CREDENTIAL")
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "PYDANTIC_DISABLE_PLUGINS": "__all__",
            **{
                key: private
                for key in ("HOME", "VIBE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR")
            },
        }
        script = Path(__file__).resolve()
        if manifest["files"].get(str(script)) != digest(script):
            raise ValueError("proof source drift before spawn")
        bootstrap = f'import sys;sys.path[:]={list(map(str, (STDLIB, STDLIB / "lib-dynload", PACKAGES)))!r};import runpy;runpy.run_path({str(script)!r},run_name="__main__")'
        argv = [
            str(INTERPRETER),
            "-I",
            "-S",
            "-B",
            "-X",
            f"pycache_prefix={root / 'empty-pycache'}",
            "-c",
            bootstrap,
            "--child",
        ]
        process = subprocess.run(
            argv,
            input=json.dumps({"manifest": manifest, "mode": mode}),
            text=True,
            capture_output=True,
            cwd=root,
            env=env,
            timeout=30,
            check=False,
        )
        return {
            "mode": mode,
            "returncode": process.returncode,
            "argv": argv,
            "child": json.loads(process.stdout) if process.stdout else None,
            "stderr": process.stderr,
            "environment_keys": sorted(env),
            "environment": env,
            "private_cwd": str(root),
            "cache_files_after": [
                str(p.relative_to(root)) for p in (root / "empty-pycache").rglob("*") if p.is_file()
            ],
            "manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()
            ).hexdigest(),
        }


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        print(json.dumps(child(json.load(sys.stdin)), sort_keys=True))
    elif sys.argv[1:] == ["--prove-native-types"]:
        pins = prepare_manifest()
        print(
            json.dumps(
                {"manifest": pins, "proofs": [run_proof(pins, mode) for mode in MODES]},
                sort_keys=True,
            )
        )
    else:
        raise SystemExit("Explicit --prove-native-types required; no provider loader exists.")
