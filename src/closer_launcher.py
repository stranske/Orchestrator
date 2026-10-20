"""Thin launcher entrypoint for the closer campaign runtime.

It owns nothing but the pin check: it verifies the manifest's pinned runtime/core contract,
launcher version and package/state digests, then hands to the pinned `closer_campaign`
module. Any mismatch exits 78 with no effect. `compat-child` is the bounded compatibility
mode the receipt producer drives: scratch directory only, refuses to run with credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

LAUNCHER_VERSION = "1"
CONTRACT = "closer-operation/v1"
CREDENTIAL_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def pin_problems(manifest: dict, *, own_path: str | None = None) -> list[str]:
    """Everything that must hold before this launcher may start the runtime."""
    own = Path(own_path or __file__).resolve()
    problems = []
    pin = manifest.get("launcher") or {}
    if manifest.get("contract") != CONTRACT or pin.get("compatible_contract") != CONTRACT:
        problems.append("core contract differs from this launcher")
    if pin.get("version") != LAUNCHER_VERSION:
        problems.append("launcher version not pinned/compatible")
    if Path(pin.get("path", "")).resolve() != own or pin.get("sha256") != _sha(own):
        problems.append("launcher is not the pinned file")
    base = Path(manifest.get("module_dir", ""))
    if Path(manifest.get("module_path", "")).resolve() != (base / "closer_campaign.py").resolve():
        problems.append("runtime entrypoint is outside the pinned module set")
    required = {
        "closer_campaign.py",
        "closer_launcher.py",
        "closer_runtime_binding.py",
        "claims.py",
        "closer_release.py",
        "closer_dispatcher.py",
    }
    if not required.issubset(manifest.get("modules") or {}):
        problems.append("Runtime binding/entrypoint closure incomplete")
    for name, digest in (manifest.get("modules") or {}).items():
        try:
            if Path(name).name != name:
                problems.append("module path escapes pinned package")
                continue
            if _sha(base / name) != digest:
                problems.append(f"{name}: digest differs from manifest")
        except OSError:
            problems.append(f"{name}: unreadable")
    if manifest.get("state_digest") != _digest(manifest.get("state_versions")):
        problems.append("state digest does not match state versions")
    if manifest.get("runtime_digest") != _digest(manifest.get("runtime")):
        problems.append("runtime digest does not match runtime")
    core = (manifest.get("distribution") or {}).get("core") or {}
    if core.get("owner") != "Workflows":
        problems.append("distribution does not name Workflows as Core owner")
    if manifest.get("public_runtime_descriptor"):
        actual = {
            str(path.relative_to(base))
            for path in base.rglob("*")
            if path.is_file() and path.suffix in {".py", ".pyc", ".pyo"}
        }
        if actual != set(manifest.get("modules") or {}):
            problems.append("Runtime tree contains undeclared executable code")
    return problems


def agent_cli_descriptor_problems(manifest: dict) -> list[str]:
    """Check the closed public package using only stdlib before importing its code."""
    if "closer_agent_launchers.py" not in (manifest.get("modules") or {}):
        return ["agent CLI binding is not in the pinned module set"]
    if manifest.get("public_runtime_descriptor") != "closer-runtime-package.json":
        return ["agent CLI requires the closed public Runtime descriptor"]
    try:
        public = json.loads(
            (Path(manifest["module_dir"]) / "closer-runtime-package.json").read_text()
        )
        modules = public["modules"]
        expected = hashlib.sha256(
            json.dumps(
                [
                    "closer-runtime",
                    "Orchestrator",
                    "1",
                    LAUNCHER_VERSION,
                    CONTRACT,
                    public["candidate_sha"],
                    sorted(modules.items()),
                    hashlib.sha256(
                        json.dumps(
                            public["state_versions"], sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    public["supported_core"],
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            public.get("schema_version") != 1
            or public.get("package_kind") != "closer-runtime"
            or public.get("owner") != "Orchestrator"
            or public.get("contract") != CONTRACT
            or public.get("runtime_version") != "1"
            or (public.get("launcher") or {}).get("version") != LAUNCHER_VERSION
            or modules != manifest.get("modules")
            or public.get("candidate_sha") != manifest.get("candidate_sha")
            or public.get("state_versions") != manifest.get("state_versions")
            or public.get("package_digest") != expected
            or manifest.get("package_digest") != expected
        ):
            return ["agent CLI public Runtime descriptor differs from pinned host package"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ["agent CLI public Runtime descriptor unreadable or invalid"]
    return []


def load_runtime(manifest: dict):
    """Import the pinned closer_campaign by path; claims etc. resolve from the same dir."""
    sys.dont_write_bytecode = True
    base = str(Path(manifest["module_dir"]))
    for name in manifest["modules"]:
        loaded = sys.modules.get(Path(name).stem)
        if (
            loaded is not None
            and Path(getattr(loaded, "__file__", "")).resolve() != (Path(base) / name).resolve()
        ):
            raise RuntimeError(
                f"pinned Runtime dependency already loaded from another package: {name}"
            )
    sys.path[:] = [base] + [item for item in sys.path if item != base]
    spec = importlib.util.spec_from_file_location("closer_campaign", manifest["module_path"])
    if spec is None or spec.loader is None:
        raise RuntimeError("pinned Runtime has no supported Python loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules["closer_campaign"] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="verify the pin, then one bounded campaign tick")
    run.add_argument("--manifest", required=True)
    run.add_argument("--root", required=True)
    run.add_argument("--mechanism")
    observation = sub.add_parser(
        "cli-supervision-status", help="read-only retained CLI qualification evidence"
    )
    observation.add_argument("--manifest", required=True)
    supervisor = sub.add_parser("qualify-cli", help="bounded actual CLI no-effect qualification")
    supervisor.add_argument("--manifest", required=True)
    supervisor.add_argument("--agent", required=True)
    supervisor.add_argument("--profile-id", required=True)
    supervisor.add_argument("--timeout", type=int, default=60)
    supervisor.add_argument("--budget-seconds", type=int, required=True)
    qualification = sub.add_parser(
        "qualification-child", help="no-effect CLI lifecycle observation"
    )
    qualification.add_argument("--manifest", required=True)
    qualification.add_argument("--mechanism", required=True)
    qualification.add_argument("--request", required=True)
    qualification.add_argument("--role", choices=["first", "restart"], required=True)
    child = sub.add_parser("compat-child", help="bounded compatibility process (producer)")
    child.add_argument("--dir", required=True)
    child.add_argument("--role", required=True)
    child.add_argument("--manifest", required=True)
    agent_cli = sub.add_parser(
        "agent-cli",
        help="concrete per-agent CLI inventory/readiness/plan/run (closer_agent_launchers)",
    )
    agent_cli.add_argument("--manifest", required=True)
    agent_cli.add_argument("rest", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    if args.command == "agent-cli":
        manifest = json.loads(Path(args.manifest).read_text())
        problems = pin_problems(manifest)
        problems.extend(agent_cli_descriptor_problems(manifest))
        if problems:
            print(json.dumps({"launcher": "refused", "problems": problems}))
            return 78
        load_runtime(manifest)
        import closer_agent_launchers

        expected = (Path(manifest["module_dir"]) / "closer_agent_launchers.py").resolve()
        if Path(closer_agent_launchers.__file__).resolve() != expected:
            print(json.dumps({"launcher": "refused", "problems": ["agent CLI binding not pinned"]}))
            return 78
        rest = list(args.rest)
        if rest[:1] == ["--"]:
            rest = rest[1:]
        if rest and rest[0] in {"plan", "run"} and "--manifest" not in rest:
            rest.extend(["--manifest", args.manifest])
        return closer_agent_launchers.main(rest or ["--help"], required_manifest_path=args.manifest)
    manifest = json.loads(Path(args.manifest).read_text())
    problems = pin_problems(manifest)
    if args.command == "compat-child":
        if os.environ.get("ORCH_CLOSER_COMPAT") != "1" or any(
            os.environ.get(v) for v in CREDENTIAL_VARS
        ):
            problems.append("compatibility mode needs ORCH_CLOSER_COMPAT=1 and no credentials")
    if problems:
        print(json.dumps({"launcher": "refused", "problems": problems}))
        return 78
    runtime = load_runtime(manifest)
    from closer_mechanisms import CLI, MechanismError, mechanism, qualification_child

    try:
        selected = mechanism(manifest, getattr(args, "mechanism", None))
    except RuntimeError as exc:
        print(json.dumps({"launcher": "refused", "problems": [str(exc)]}))
        return 78
    if args.command == "cli-supervision-status":
        from closer_mechanisms import supervision_status

        try:
            status = supervision_status(args.manifest)
        except (MechanismError, FileNotFoundError) as exc:
            print(json.dumps({"launcher": "refused", "problems": [str(exc)]}))
            return 78
        print(json.dumps(status, sort_keys=True))
        return 0
    if args.command == "qualify-cli":
        from closer_mechanisms import supervise_cli

        try:
            result = supervise_cli(
                args.manifest,
                args.agent,
                args.profile_id,
                timeout=args.timeout,
                budget_seconds=args.budget_seconds,
            )
        except MechanismError as exc:
            print(json.dumps({"launcher": "refused", "problems": [str(exc)]}))
            return 78
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("passed") else 78
    if args.command == "qualification-child":
        if selected != CLI:
            return 78
        return qualification_child(manifest, args.request, args.role)
    if args.command == "run":
        keywords = {} if selected == "desktop-scheduler/v1" else {"mechanism": selected}
        print(json.dumps(runtime.tick(args.root, args.manifest, **keywords), sort_keys=True))
        return 0
    return runtime.harness_child(args.dir, args.role, args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
