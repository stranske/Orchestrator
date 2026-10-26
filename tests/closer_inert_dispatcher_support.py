"""Test-only scoped caller inputs and genuine inert broker execution.

No result/origin/qualification validator is replaced. The sole executable is a
fixture-generated native-result emitter; ps is limited to this test/its children.
An explicit registration also admits the exact no-effect native host fixture wrapper.
The fixture's resolved Core source may answer one exact read-only HEAD query.
"""

import contextlib
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from test_closer_role_qualification import record

import adapters
import closer_claude_auth
import closer_data_only_execution as execution
import closer_dispatcher as closer
import closer_evidence as evidence
import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_role_qualification as roles
import dispatcher
import execution_profiles

REAL_POPEN, REAL_RUN = subprocess.Popen, subprocess.run
REAL_QUALIFIED = roles.is_runtime_qualified
REAL_EXECUTE = closer._execute_profile_route
REAL_STAGED = closer._run_staged_pipeline
REAL_GITHUB = closer.GitHub
REAL_BROKER = dispatcher.offload
REAL_COLLECT = evidence.collect_evidence
LOW = "claude-sonnet-5-5-low"
SECOND = "claude-sonnet-5-5-high-v2"


def fixture_api_transport(api, argv, *, read_text_fn=None, **kwargs):
    """Translate only the source GitHub.api protocol into explicit fixture I/O."""
    if (
        not isinstance(argv, (list, tuple))
        or len(argv) < 4
        or list(argv[:2]) != ["api", "--method"]
        or not isinstance(argv[2], str)
        or argv[2] not in {"GET", "POST", "PATCH", "PUT", "DELETE"}
        or not isinstance(argv[3], str)
        or not argv[3]
        or argv[3].startswith(("-", "/"))
        or "://" in argv[3]
        or any(ch.isspace() for ch in argv[3])
        or set(kwargs) - {"resource", "timeout_s", "raw_body"}
        or type(kwargs.get("raw_body", False)) is not bool
        or len(argv[4:]) % 2
    ):
        raise AssertionError("unsupported fixture GitHub argv or transport option")
    fields = {}
    for flag, value in zip(argv[4::2], argv[5::2], strict=True):
        if flag != "-f" or not isinstance(value, str) or "=" not in value:
            raise AssertionError("unsupported fixture GitHub field option")
        name, field = value.split("=", 1)
        if not name or name in fields:
            raise AssertionError("ambiguous fixture GitHub field")
        fields[name] = field
    method, endpoint = argv[2:4]
    if kwargs.get("raw_body"):
        if method != "GET" or fields or read_text_fn is None:
            raise AssertionError("explicit fixture GET text reader required")
        body = read_text_fn(endpoint)
    else:
        body = api(endpoint, method=method, fields=fields or None)
    return 0, body  # The unchanged source GitHub.api validates body/status/type.


def fixture_core_head_binding():
    """Bind the existing _core fixture's physical resolver, never arbitrary Git."""
    from closer_core_support import core_source

    supplied = os.environ.get("CLOSER_CORE_PACKAGE")
    source = Path(supplied).expanduser() if supplied else core_source()
    try:
        source = source.resolve(strict=True)
    except OSError:
        return None, None
    if (
        not (source / "scripts/closer_runtime.py").is_file()
        or (source / "closer-core-package.json").is_file()
        or not (source / ".git").exists()
    ):
        return None, None
    expected = os.environ.get("CLOSER_CORE_SOURCE_REVISION")
    if expected is not None:
        assert len(expected) == 40 and all(ch in "0123456789abcdef" for ch in expected)
    return ("git", "-C", str(source), "rev-parse", "HEAD"), expected


class InertCaller:
    def __init__(self, world, monkeypatch):
        self.world, self.patch = world, monkeypatch
        self.core_head_query, self.core_head_revision = fixture_core_head_binding()
        self.results, self.prompts = [], []
        self.timeout_seconds = 45
        self.children = {os.getpid()}
        self.control = world.root / "inert-response.json"
        script = world.cli.read_text()
        script = script.replace(
            "mode='success'",
            "control=json.loads(Path("
            + repr(str(self.control))
            + ").read_text())\nmode=control.get('mode','success')",
        )
        script = script.replace(
            "final=json.dumps({'classification':'relevant','authority':False,'evidence':[]})",
            "final=control['output']",
        )
        script = script.replace(
            "if mode=='resumed':row['resumed']=True",
            "if mode=='resumed':row['resumed']=True\nif mode=='native_error':row.update(is_error=True,subtype='error_during_execution')",
        )
        script = script.replace(
            "print(json.dumps(row))",
            "\nif control.get('exit',0):\n print(control.get('error','inert failure'),file=sys.stderr)\n if final: print(json.dumps(row))\n raise SystemExit(control['exit'])\nprint(json.dumps(row))",
        )
        world.cli.write_text(script)
        self.binary = hashlib.sha256(world.cli.read_bytes()).hexdigest()
        self.control.write_text(json.dumps({"output": "{}"}))
        self.activate()

    def activate(self):
        patch, world = self.patch, self.world
        patch.setitem(sys.modules, "dispatcher", dispatcher)
        patch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", self.binary)
        patch.setattr(execution, "CLAUDE_BINARY_SHA256", self.binary)
        patch.setattr(roles, "is_runtime_qualified", REAL_QUALIFIED)
        records = {}
        for profile_id in (LOW, SECOND):
            profile = execution_profiles.get_profile(profile_id)
            for task in ("closer_interpret", "closer_exception"):
                descriptor = closer._controlled_model_contract(task)
                name = profile_id + ":" + task
                value = record(profile, roles.TASK_ROLES[task])
                value.update(
                    qualification_id=name,
                    qualified_at=time.time() - 10,
                    expires_at=time.time() + 600,
                    execution_limits={
                        **world.args["data_only_limits"],
                        "role": roles.TASK_ROLES[task],
                        "model_contract_sha256": descriptor["contract_sha256"],
                        "timeout_seconds": self.timeout_seconds,
                    },
                    production_model_contract=descriptor,
                )
                value["confinement"]["adapter_build_sha256"] = self.binary
                records[name] = value
        patch.setattr(roles, "ROLE_QUALIFICATIONS", records)
        patch.setattr(
            closer_claude_auth,
            "borrow_claude_oauth_token",
            lambda **kw: world.args["trusted_auth_supplier"](),
        )
        patch.setattr(closer, "_execute_profile_route", REAL_EXECUTE)
        patch.setitem(sys.modules, "dispatcher", dispatcher)
        patch.setattr(subprocess, "Popen", self.popen)
        patch.setattr(subprocess, "run", self.run)
        if not (Path(os.environ["HANDOFF_DIR"]) / "capacity.json").exists():
            self.seed_capacity()

    def seed_capacity(self):
        handoff = Path(os.environ["HANDOFF_DIR"])
        handoff.mkdir(parents=True, exist_ok=True)
        profiles = {
            p: {"state": "ok", "capacity_pool_ids": ["claude-subscription"]} for p in (LOW, SECOND)
        }
        (handoff / "capacity.json").write_text(
            json.dumps(
                {
                    "generated_at": time.time(),
                    "agents": {"claude": {"state": "ok"}},
                    "profiles": profiles,
                    "pools": {"claude-subscription": {"state": "ok"}},
                }
            )
        )

    def register_native_fixture(self, manifest, manifest_path):
        """Opt in only the existing private host fixture's exact rendered wrapper."""
        import pwd

        import closer_native_observer as native

        root = self.world.root
        assert root.is_absolute() and root.resolve(strict=True) == root
        account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        assert root != account and not account.is_relative_to(root)
        for name in (".codex", ".config", ".aws", ".agents", ".ssh", ".gnupg"):
            assert not root.is_relative_to(account / name)
            assert not root.is_relative_to((account / name).resolve())
        home = root / "home"
        assert Path(os.environ["HOME"]) == home
        wrapper = home / ".codex/bin/cursor-lane.sh"
        manifest_path = Path(manifest_path)
        assert manifest_path == root / "manifest.json"
        contract = manifest["mechanism_contract"]
        assert contract["wrapper"]["path"] == str(wrapper)
        assert Path(contract["observation_root"]) == root / "native-observations"
        assert Path(contract["campaign_root"]) == root / "campaigns"
        assert Path(manifest["module_dir"]) == root / "modules"
        assert Path(manifest["launcher"]["path"]) == root / "modules/closer_launcher.py"
        for path in (
            wrapper,
            manifest_path,
            Path(contract["observation_root"]),
            Path(contract["campaign_root"]),
            Path(manifest["module_dir"]),
        ):
            self._native_fixture_physical(root, path)
        source = '#!/bin/bash\nset -euo pipefail\nLANE="${1:?}"\ncase "$LANE" in\ncloser|opener) ;;\n*) exit 2 ;;\nesac\n\nexport PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"\n'
        source += "echo FORBIDDEN_LEGACY_PROVIDER >&2\nexit 99\n"
        expected = native.render_cursor_wrapper(
            source,
            manifest_path=manifest_path,
            campaign_root=root / "campaigns",
            interpreter=Path(contract["interpreter"]["path"]),
            launcher=root / "modules/closer_launcher.py",
        ).encode()
        raw, identity = self._native_fixture_file(root, wrapper, 128 * 1024)
        assert raw == expected and hashlib.sha256(raw).hexdigest() == contract["wrapper"]["sha256"]
        manifest_bytes, _ = self._native_fixture_file(root, manifest_path, 512 * 1024)
        assert json.loads(manifest_bytes) == manifest
        import closer_mechanisms as mechanisms

        observer = Path(mechanisms.__file__).resolve(strict=True)
        observer_bytes, _ = self._native_fixture_file(observer.parent, observer, 512 * 1024)
        assert (
            hashlib.sha256(observer_bytes).hexdigest()
            == manifest["modules"]["closer_mechanisms.py"]
        )
        assert Path(sys.executable).resolve(strict=True) == Path(contract["interpreter"]["path"])
        source_root = Path(closer.__file__).resolve(strict=True).parent
        module_pins = []
        total = 0
        assert isinstance(manifest["modules"], dict) and manifest["modules"]
        assert {entry.name for entry in (root / "modules").iterdir()} == set(manifest["modules"])
        assert manifest["launcher"]["sha256"] == manifest["modules"]["closer_launcher.py"]
        for name, expected_sha in sorted(manifest["modules"].items()):
            assert isinstance(name, str) and Path(name).name == name and name.endswith(".py")
            reviewed = source_root / name
            private = root / "modules" / name
            reviewed_bytes, _ = self._native_fixture_file(source_root, reviewed, 4 * 1024 * 1024)
            private_bytes, _ = self._native_fixture_file(root, private, 4 * 1024 * 1024)
            total += len(reviewed_bytes) + len(private_bytes)
            assert total <= 32 * 1024 * 1024
            assert hashlib.sha256(reviewed_bytes).hexdigest() == expected_sha
            assert private_bytes == reviewed_bytes
            module_pins.append((private, reviewed, expected_sha, len(reviewed_bytes)))
        self.native_runs = {}
        self.native_fixture = {
            "argv": ("/bin/bash", str(wrapper), "closer"),
            "root": root,
            "home": home,
            "wrapper": wrapper,
            "wrapper_sha256": hashlib.sha256(raw).hexdigest(),
            "wrapper_identity": identity,
            "manifest": manifest_path,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "observation_root": root / "native-observations",
            "binding": native.binding(manifest),
            "module_pins": module_pins,
            "observer": observer,
            "observer_sha256": hashlib.sha256(observer_bytes).hexdigest(),
            "interpreter": sys.executable,
            "wrapper_argv_sha256": mechanisms._argv_digest(["/bin/bash", str(wrapper), "closer"]),
            "entry_argv_sha256": mechanisms._argv_digest(
                native._entry_argv(manifest, manifest_path)
            ),
        }

    @staticmethod
    def _native_fixture_physical(root, path):
        assert path.is_absolute() and path.is_relative_to(root)
        assert path.resolve(strict=True) == path
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            assert not current.is_symlink()

    @classmethod
    def _native_fixture_file(cls, root, path, limit):
        import stat

        cls._native_fixture_physical(root, path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            assert stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and 0 < meta.st_size <= limit
            raw = stream.read(limit + 1)
            assert len(raw) == meta.st_size
        return raw, (meta.st_dev, meta.st_ino)

    @staticmethod
    def _native_startup_setting(key):
        return key.startswith("PYTHON") or key in {
            "__PYVENV_LAUNCHER__",
            "BASH_ENV",
            "ENV",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
        }

    @contextlib.contextmanager
    def native_fixture_environment(self):
        """Scope startup isolation to this explicit no-effect fixture invocation."""
        with self.patch.context() as isolated:
            for key in list(os.environ):
                if self._native_startup_setting(key):
                    isolated.delenv(key)
            isolated.setenv("PYTHONNOUSERSITE", "1")
            yield

    def _native_fixture_modules_match(self):
        registered = self.native_fixture
        modules = registered["root"] / "modules"
        self._native_fixture_physical(registered["root"], modules)
        if {entry.name for entry in modules.iterdir()} != {
            pin[0].name for pin in registered["module_pins"]
        }:
            return False
        for private, reviewed, expected_sha, size in registered["module_pins"]:
            private_bytes, _ = self._native_fixture_file(registered["root"], private, size)
            reviewed_bytes, _ = self._native_fixture_file(reviewed.parent, reviewed, size)
            if (
                private_bytes != reviewed_bytes
                or hashlib.sha256(reviewed_bytes).hexdigest() != expected_sha
            ):
                return False
        return True

    def _native_fixture_options(self, options):
        """Recheck exact private bytes immediately before the unchanged Popen."""
        registered = self.native_fixture
        if (
            set(options) - {"env", "stdout", "stderr", "text", "shell"}
            or options.get("stdout") != subprocess.PIPE
            or options.get("stderr") != subprocess.PIPE
            or options.get("text") is not True
        ):
            return False
        env = options.get("env")
        if not isinstance(env, dict) or env.get("HOME") != str(registered["home"]):
            return False
        if env.get("PYTHONNOUSERSITE") != "1":
            return False
        if any(
            self._native_startup_setting(key)
            and (key not in {"PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"} or value != "1")
            for key, value in env.items()
        ):
            return False
        try:
            root = registered["root"]
            if not self._native_fixture_modules_match():
                return False
            self._native_fixture_physical(root, registered["home"])
            raw, identity = self._native_fixture_file(root, registered["wrapper"], 128 * 1024)
            if (
                identity != registered["wrapper_identity"]
                or hashlib.sha256(raw).hexdigest() != registered["wrapper_sha256"]
            ):
                return False
            raw, _ = self._native_fixture_file(root, registered["manifest"], 512 * 1024)
            if hashlib.sha256(raw).hexdigest() != registered["manifest_sha256"]:
                return False
            request = Path(env.get("CLOSER_NATIVE_QUALIFICATION_REQUEST", ""))
            if (
                request.parent.parent != registered["observation_root"]
                or request.name != "request.json"
                or len(request.parent.name) != 32
                or any(ch not in "0123456789abcdef" for ch in request.parent.name)
            ):
                return False
            raw, _ = self._native_fixture_file(root, request, 32 * 1024)
            value = json.loads(raw)
            return (
                value.get("schema") == "native-wrapper-qualification-request/v1"
                and value.get("nonce") == request.parent.name
                and value.get("binding") == registered["binding"]
                and type(value.get("action_deadline")) in (int, float)
                and type(value.get("hard_deadline")) in (int, float)
                and math.isfinite(value["action_deadline"])
                and math.isfinite(value["hard_deadline"])
                and time.monotonic() < value["action_deadline"] < value["hard_deadline"]
                and value["hard_deadline"] - time.monotonic() <= 120
            )
        except (OSError, ValueError, TypeError, AssertionError):
            return False

    @staticmethod
    def _native_birth(value):
        return (
            isinstance(value, str)
            and 0 < len(value) <= 128
            and all(32 <= ord(ch) < 127 for ch in value)
        )

    def _native_observation_targets(self):
        """Metadata candidates only; the unchanged OS reader must prove ownership."""
        registered = getattr(self, "native_fixture", None)
        if registered is None:
            return {}
        targets = {}
        for wrapper_pid, launch in self.native_runs.items():
            try:
                if time.monotonic() >= launch["action_deadline"]:
                    continue
                raw, _ = self._native_fixture_file(registered["root"], launch["request"], 32 * 1024)
                if hashlib.sha256(raw).hexdigest() != launch["request_sha256"]:
                    continue
                targets[wrapper_pid] = {"birth": None, "hard_deadline": launch["hard_deadline"]}
                startup_path = launch["request"].parent / "startup.json"
                if not startup_path.exists():
                    continue
                raw, _ = self._native_fixture_file(registered["root"], startup_path, 32 * 1024)
                startup = json.loads(raw)
                wrapper, entry = startup.get("wrapper"), startup.get("entry")
                if not (
                    startup.get("schema") == "native-wrapper-lifecycle/v1"
                    and startup.get("nonce") == launch["request"].parent.name
                    and startup.get("binding") == registered["binding"]
                    and startup.get("actor") == "cursor"
                    and startup.get("source") == "owned-native-wrapper-process"
                    and startup.get("desktop_qualified") is False
                    and startup.get("scheduler_origin") == "unobserved"
                    and isinstance(wrapper, dict)
                    and isinstance(entry, dict)
                    and type(wrapper.get("pid")) is int
                    and wrapper["pid"] == wrapper_pid
                    and wrapper.get("argv_sha256") == registered["wrapper_argv_sha256"]
                    and entry.get("argv_sha256") == registered["entry_argv_sha256"]
                    and wrapper.get("source") == entry.get("source") == "os-byte-argv"
                    and type(entry.get("pid")) is int
                    and 1 < entry["pid"] <= 2**31 - 1
                    and entry["pid"] != wrapper_pid
                    and self._native_birth(wrapper.get("started"))
                    and self._native_birth(entry.get("started"))
                ):
                    continue
                targets[wrapper_pid] = {
                    "birth": wrapper["started"],
                    "hard_deadline": launch["hard_deadline"],
                }
                targets[entry["pid"]] = {
                    "birth": entry["started"],
                    "hard_deadline": launch["hard_deadline"],
                }
            except (OSError, ValueError, TypeError, AssertionError):
                continue
        return targets

    def _native_observation_command(self, command):
        registered = getattr(self, "native_fixture", None)
        if registered is None:
            return False
        try:
            targets = self._native_observation_targets()
            if (
                len(command) == 7
                and command[:2] == ["ps", "-p"]
                and command[2].isdigit()
                and command[3:] == ["-o", "ppid=", "-o", "lstart="]
            ):
                return int(command[2]) in targets
            if len(command) != 7 or command[:4] != [
                registered["interpreter"],
                "-I",
                str(registered["observer"]),
                "--owned-argv",
            ]:
                return False
            pid, birth, seconds = command[4:]
            if (
                not pid.isdigit()
                or int(pid) not in targets
                or not self._native_birth(birth)
                or birth != targets[int(pid)]["birth"]
            ):
                return False
            remaining = float(seconds)
            if not math.isfinite(remaining) or not 0 < remaining <= min(
                5, targets[int(pid)]["hard_deadline"] - time.monotonic()
            ):
                return False
            raw, _ = self._native_fixture_file(
                registered["observer"].parent, registered["observer"], 512 * 1024
            )
            return hashlib.sha256(raw).hexdigest() == registered["observer_sha256"]
        except (OSError, ValueError, TypeError, AssertionError):
            return False

    def _native_observation_options(self, options):
        if set(options) - {
            "capture_output",
            "stdout",
            "stderr",
            "text",
            "timeout",
            "check",
            "shell",
        }:
            return False
        if options.get("text") is not True:
            return False
        timeout = options.get("timeout")
        if timeout is not None and (
            type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5
        ):
            return False
        return not any(
            name in os.environ
            for name in (
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
                "DYLD_INSERT_LIBRARIES",
                "DYLD_LIBRARY_PATH",
            )
        )

    def allowed(self, command):
        if not isinstance(command, (list, tuple)) or not all(
            isinstance(arg, str) for arg in command
        ):
            return False
        command = list(command)
        return command and (
            (self.core_head_query is not None and tuple(command) == self.core_head_query)
            or (
                getattr(self, "native_fixture", None) is not None
                and tuple(command) == self.native_fixture["argv"]
            )
            or self._native_observation_command(command)
            or command[0] == str(self.world.cli)
            or (
                Path(command[0]).name == "ps"
                and command[1:2] == ["-p"]
                and len(command) == 5
                and command[2].isdigit()
                and int(command[2]) in self.children
                and command[3:] == ["-o", "lstart="]
            )
        )

    def subprocess_allowed(self, command, options):
        if options.get("shell", False) is not False or not self.allowed(command):
            return False
        if (
            getattr(self, "native_fixture", None) is not None
            and tuple(command) == self.native_fixture["argv"]
        ):
            return self._native_fixture_options(options)
        if self._native_observation_command(list(command)):
            return self._native_observation_options(options)
        if self.core_head_query is not None and tuple(command) == self.core_head_query:
            # No alternate executable, cwd or injected Git environment may turn
            # the exact metadata query into a different repository/operation.
            return all(options.get(name) is None for name in ("cwd", "env", "executable"))
        return True

    def popen(self, command, **kwargs):
        assert self.subprocess_allowed(command, kwargs), ("forbidden subprocess", command)
        native_launch = None
        if (
            getattr(self, "native_fixture", None) is not None
            and tuple(command) == self.native_fixture["argv"]
        ):
            request_path = Path(kwargs["env"]["CLOSER_NATIVE_QUALIFICATION_REQUEST"])
            raw, _ = self._native_fixture_file(self.native_fixture["root"], request_path, 32 * 1024)
            native_launch = {
                "request": request_path,
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "action_deadline": json.loads(raw)["action_deadline"],
                "hard_deadline": json.loads(raw)["hard_deadline"],
            }
        child = REAL_POPEN(command, **kwargs)
        if native_launch is not None:
            self.native_runs[child.pid] = native_launch
        if command[0] == str(self.world.cli) or (
            getattr(self, "native_fixture", None) is not None
            and tuple(command) == self.native_fixture["argv"]
        ):
            self.children.add(child.pid)
        return child

    def run(self, command, **kwargs):
        assert self.subprocess_allowed(command, kwargs), ("forbidden subprocess", command)
        result = REAL_RUN(command, **kwargs)
        if (
            self.core_head_query is not None
            and tuple(command) == self.core_head_query
            and self.core_head_revision is not None
            and result.returncode == 0
            and result.stdout is not None
        ):
            observed = result.stdout
            if isinstance(observed, bytes):
                observed = observed.decode("ascii")
            assert observed.strip() == self.core_head_revision, "fixture Core HEAD differs from pin"
        return result

    def response(self, result, agent, prompt, **kwargs):
        assert agent == "claude" and kwargs["profile_id"] in (LOW, SECOND)
        self.prompts.append(prompt)
        control = {
            "output": result.get("output", ""),
            "exit": result.get("exit", 0),
            "error": result.get("output") or result.get("error", "inert failure"),
        }
        if result.get("outcome_uncertain") and not result.get("completion_persistence"):
            control.update(exit=0, mode="uncertain")
        if result.get("raw_exit") == 0 and result.get("exit") == 70:
            control["exit"] = 0
        if result.get("_native_mode"):
            control.update(mode=result["_native_mode"], exit=0)
        self.control.write_text(json.dumps(control))
        if result.get("rate_incident_evidence"):
            control["error"] = "quota exceeded: usage limit reached"
            if result.get("raw_exit") == 0:
                control["output"] = "OFFLOAD_INCOMPLETE: quota exceeded: usage limit reached"
            self.control.write_text(json.dumps(control))
        with self.patch.context() as local:
            if result.get("completion_persistence"):
                import sqlite3

                def busy(*args, **kwargs):
                    raise sqlite3.OperationalError("database is locked")

                local.setattr(dispatcher.feedback, "complete_profile_attempt", busy)
                local.setattr(dispatcher.feedback, "complete_profile_attempt_unresolved", busy)
            actual = REAL_BROKER(agent, prompt, **kwargs)
        self.results.append(actual)
        return actual

    def frontier(self, snapshots, backend=None):
        bindings = {}
        for target, snap in snapshots.items():
            if backend:
                observed = backend.observe(target)
                action_id = policy.action_identity(
                    target, snap["revision"], observed["action"], observed["operation_binding"]
                )
            else:
                action_id = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()
            bindings[target] = {"revision": snap["revision"], "action_id": action_id}
        return policy.make_frontier(
            list(snapshots),
            owner="inert-fixture-owner",
            frontier_id="inert-fixture-frontier",
            bindings=bindings,
        )

    def bind_worker(self, backend):
        self.activate()
        if "claude-sonnet-5-5-low" not in json.loads(
            (Path(os.environ["HANDOFF_DIR"]) / "capacity.json").read_bytes()
        ).get("profiles", {}):
            self.seed_capacity()
        original = backend.worker

        def worker(target, action, snap, operation, **kwargs):
            if action == "repair":
                return original(target, action, snap, operation, **kwargs)
            packet = self.world.root / (
                "advisory-" + hashlib.sha256(target.encode()).hexdigest() + ".json"
            )
            packet.write_text(
                json.dumps(
                    {
                        "target": target,
                        "revision": snap["revision"],
                        "sources": snap.get("sources", []),
                    }
                )
            )
            context = {
                **(kwargs.pop("advisory_context", None) or {}),
                "packet_ref": {
                    "path": str(packet),
                    "sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
                },
            }
            op = {**operation, "finite_frontier": self.frontier({target: snap})}
            return original(target, action, snap, op, advisory_context=context, **kwargs)

        backend.worker = worker
        return backend

    def temporary_campaign_guard(self):
        """Create only beneath this supplied physical pytest root, never OS home."""
        import pwd
        import stat

        root = self.world.root
        assert root.is_absolute() and root.resolve(strict=True) == root
        home = root / "private-home"
        assert Path(os.environ["HOME"]) == home and home.resolve() == home
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        # CI's legitimate private pytest roots can be below account-home/work.
        # Forbid the account root/ancestors and canonical live settings instead.
        assert root != account_home and not account_home.is_relative_to(root)
        assert home != account_home
        guard = home / ".codex/automations/model-allocation/assess.py"
        assert guard.is_relative_to(root)
        for name in (".codex", ".config", ".aws", ".agents", ".ssh", ".gnupg"):
            canonical = account_home / name
            for protected in (canonical, canonical.resolve()):
                assert not any(path.is_relative_to(protected) for path in (root, home, guard))
        current = Path(guard.anchor)
        for component in guard.parts[1:]:
            current = current / component
            assert not current.is_symlink()
            if current.exists():
                assert current.resolve(strict=True) == current
        guard.parent.mkdir(parents=True, exist_ok=True)
        assert guard.parent.resolve(strict=True) == guard.parent
        data = b"def enforce_campaign_budget(args):\n    args.timeout=min(args.timeout,30)\n"
        if getattr(self, "created_guard", None) == guard:
            assert stat.S_ISREG(guard.stat(follow_symlinks=False).st_mode)
            assert guard.read_bytes() == data
            return guard
        fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.created_guard = guard
        return guard

    def staged(
        self,
        args,
        targets,
        backend,
        deadline,
        *,
        deterministic=False,
        coordinator=False,
        snapshot_fn=None,
        read_text_fn=None,
    ):
        """Use the unchanged CLI backend constructor for finite model positives."""
        snapshots = {
            target: (
                copy.deepcopy(snapshot_fn(target))
                if snapshot_fn is not None
                else closer.GitHub(deadline=deadline).snapshot(target)
            )
            for target in targets
        }
        if deterministic:
            return REAL_STAGED(args, targets, backend, deadline)
        import argparse
        from types import SimpleNamespace

        import closer_resources as resources

        self.temporary_campaign_guard()
        module = sys.modules["closer_pipeline"]
        runner = module.run_pipeline
        for key, value in vars(pipeline).items():
            if not hasattr(module, key):
                setattr(module, key, value)
        captured = {}

        def scoped(path, selected, **callbacks):
            actual_backend = dict(
                zip(
                    callbacks["prepare"].__code__.co_freevars,
                    (cell.cell_contents for cell in callbacks["prepare"].__closure__),
                )
            )["backend"]
            captured["backend"] = actual_backend
            if coordinator:
                # Preserve the CLI-registered callback identities. The actual
                # coordinator opens and owns its shard scopes and leases.
                captured["result"] = runner(path, selected, **callbacks)
                return captured["result"]
            import contextlib

            root = resources.open_root(path, pipeline_schema=pipeline.SCHEMA)
            with contextlib.ExitStack() as stack:
                leases = {}
                for target in selected:
                    lease = resources.acquire(
                        root,
                        target,
                        owner="inert-stage-fixture",
                        finite_frontier=admitted.finite_frontier,
                        request_deadline=time.time() + 30,
                    )
                    assert lease is not None
                    stack.callback(lease.release)
                    leases[target] = lease
                for stage in ("prepare", "interpret", "exception", "act", "reconcile"):
                    if stage not in callbacks:
                        continue
                    original = callbacks[stage]

                    def invoke(target, context, fn=original):
                        shard = resources.shard_dir(root, target) / "state.json"
                        scoped = {
                            **context,
                            "work_key": context.get("work_key", "inert-work-" + target),
                        }
                        scoped.setdefault("state_path", str(shard))
                        scoped.setdefault(
                            "action_state_path",
                            str(
                                shard.parent
                                / "state.actions"
                                / (pipeline._digest(target) + ".json")
                            ),
                        )
                        with resources.fenced(leases[target]):
                            return fn(target, scoped)

                    callbacks[stage] = invoke
                captured["result"] = runner(path, selected, **callbacks)
                return captured["result"]

        assignment = self.world.root / "pipeline-assignment.json"
        assignment.write_text(json.dumps({"targets": [{"target": target} for target in targets]}))
        dispositions = self.world.root / "pipeline-dispositions.json"
        dispositions.write_text(json.dumps(backend.dispositions))
        admitted = SimpleNamespace(
            **{
                **vars(args),
                "assignment": str(assignment),
                "budget_seconds": None,
                "finite_frontier": self.frontier(snapshots, backend),
                "request_timeout_seconds": 60.0,
                "coordinator_mode": "resource_scoped",
                "pipeline": True,
                "apply": True,
                "workers": True,
                "worker_timeout": 60,
                "runtime_manifest": None,
                "dispositions": str(dispositions),
                "workspaces": None,
            }
        )
        with self.patch.context() as local:
            local.setattr(closer, "GitHub", REAL_GITHUB)
            local.setattr(
                REAL_GITHUB,
                "snapshot",
                lambda self, target: copy.deepcopy(
                    snapshot_fn(target) if snapshot_fn is not None else snapshots[target]
                ),
            )
            local.setattr(
                closer.gh_capacity,
                "gh_run",
                lambda argv, **kwargs: fixture_api_transport(
                    backend.gh.api,
                    argv,
                    read_text_fn=read_text_fn or getattr(backend.gh, "read_text", None),
                    **kwargs,
                ),
            )
            local.setattr(closer, "known_workspaces", lambda targets: backend.workspaces)
            local.setattr(module, "run_pipeline", scoped)
            with resources.coordination_admission(
                closer._pipeline_state_path(args.state), targets, mode="resource_scoped"
            ) as admission:
                admitted._coordination_admission = admission
                closer._main_admitted(
                    admitted,
                    argparse.ArgumentParser(),
                    {target: {"target": target} for target in targets},
                    targets,
                )
        actual = captured["backend"]
        backend.dispositions = actual.dispositions
        backend.validated_nonpass_waits = actual.validated_nonpass_waits
        return captured["result"]

    def packet(self, snap, root):
        import io
        import zipfile

        stream = io.BytesIO()
        criterion = closer.requirements(snap["sources"][0]["body"])[0]
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "junit.xml", '<testsuite tests="1"><testcase name="' + criterion + '"/></testsuite>'
            )
        number = int(snap["target"].split("#")[1])

        def api(endpoint, **kwargs):
            if "/compare/" in endpoint:
                return {
                    "status": "ahead",
                    "files": [
                        {
                            "filename": "behavior.py",
                            "status": "added",
                            "patch": "+def preserve_behavior(): return True",
                            "sha": "c" * 40,
                        }
                    ],
                }
            if "/actions/runs?" in endpoint:
                return {"workflow_runs": [{"id": 7, "head_sha": snap["head"]}]}
            if endpoint.endswith("/runs/7"):
                return {
                    "id": 7,
                    "run_attempt": 2,
                    "head_sha": snap["head"],
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [{"number": number}],
                }
            if endpoint.endswith("/runs/7/artifacts?per_page=100"):
                return {
                    "artifacts": [
                        {"id": 9, "name": "tests", "size_in_bytes": 100, "expired": False}
                    ]
                }
            if "/artifacts?" in endpoint:
                return {"artifacts": []}
            if "/actions/runs/" in endpoint:
                return {
                    "id": int(endpoint.rsplit("/", 1)[1]),
                    "run_attempt": 1,
                    "head_sha": snap["head"],
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [{"number": number}],
                }
            raise AssertionError(endpoint)

        packet = REAL_COLLECT(snap, api, root, artifact_fetch=lambda *_a, **_k: stream.getvalue())
        path = Path(root) / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
        raw = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]["raw"]
        proof = {
            "evidence_sha256": raw["sha256"],
            "artifact_id": 9,
            "run_id": 7,
            "run_attempt": 2,
            "head_sha": snap["head"],
            "kind": "junit",
            "receipt_path": "junit.xml",
            "excerpt": criterion,
            "relevance": "Actual passing testcase exercises " + criterion,
        }
        rows = [
            {
                "source": source["target"],
                "criterion": value,
                "result": "PASS",
                "evidence": [raw],
                "proofs": [proof],
            }
            for source in snap["sources"]
            for value in [source["body"], *closer.requirements(source["body"])]
        ]
        proposal = {
            "target": snap["target"],
            "head": snap["head"],
            "base": snap["base"],
            "classification": "relevant",
            "substantive_needs": [],
            "findings": [],
            "decision": {"criteria": rows},
        }
        return (
            packet,
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            proposal,
        )

    def interpretation(self, snap, packet, ref, proposal, *, native_mode=None):
        import closer_model_contracts as model
        import closer_resources as resources

        readiness = evidence.action_evidence_readiness(
            snap, packet, "collect_evidence", retained_dir=Path(ref["path"]).parent.parent
        )
        prompt = model.render_classification_prompt(
            {
                "target": snap["target"],
                "head": snap["head"],
                "base": snap["base"],
                "packet_ref": ref,
                "snapshot_revision": snap["revision"],
                "manifest": packet["manifest"],
                "section_refs": packet["sections"],
                "data_only_evidence": closer.data_only_evidence_context(
                    packet, readiness=readiness, snapshot=snap
                ),
            },
            source_rows=packet["sections"]["source"]["content"],
            diff_rows={},
            review_rows=[],
            artifact_rows={},
            data_only=True,
            readiness=readiness,
        )
        frontier = self.frontier({snap["target"]: snap})
        with resources.pool_slot(
            self.world.root / "pools",
            hashlib.sha256(b"claude-subscription").hexdigest(),
            1,
            deadline=time.time() + 10,
            finite_frontier=True,
            reservation={
                "reservation_id": "original-act-fixture",
                "target": snap["target"],
                "frontier_id": frontier["frontier_id"],
                "agent": "claude",
                "profile_id": LOW,
            },
        ):
            result = closer._controlled_offload(
                agent="claude",
                prompt=prompt,
                profile_id=LOW,
                task_type="closer_interpret",
                operation_id="original-act-interpretation",
                frontier=frontier,
                target=snap["target"],
                basis={
                    "target": snap["target"],
                    "revision": snap["revision"],
                    "head": snap["head"],
                    "base": snap["base"],
                    "packet_sha256": ref["sha256"],
                },
                deadline=time.time() + 30,
                offload=lambda agent, prompt, **kwargs: self.response(
                    {"exit": 0, "output": json.dumps(proposal), "_native_mode": native_mode},
                    agent,
                    prompt,
                    **kwargs,
                ),
                mode="assess",
            )
        if native_mode is not None:
            return result
        assert result["exit"] == 0, result
        return {
            "actual": closer._worker_actual(result),
            "proposal": closer._worker_proposal(result, agent="claude"),
            "requested_agent": "claude",
            "requested_profile": LOW,
            "substantive_needs": [],
        }
