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
    return problems


def load_runtime(manifest: dict):
    """Import the pinned closer_campaign by path; claims etc. resolve from the same dir."""
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
    if base not in sys.path:
        sys.path.insert(0, base)
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
    child = sub.add_parser("compat-child", help="bounded compatibility process (producer)")
    child.add_argument("--dir", required=True)
    child.add_argument("--role", required=True)
    child.add_argument("--manifest", required=True)
    args = p.parse_args(argv)
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
    if args.command == "run":
        print(json.dumps(runtime.tick(args.root, args.manifest), sort_keys=True))
        return 0
    return runtime.harness_child(args.dir, args.role, args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
