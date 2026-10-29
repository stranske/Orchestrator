"""Orchestrator-owned Closer Runtime/Launcher distribution package.

Public descriptors are relocatable and contain no host paths. Host bindings
align with campaign.make_manifest fields but stay outside published archives.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from closer_campaign import PINNED_MODULES, module_closure

CONTRACT = "closer-operation/v1"
RUNTIME_VERSION = "1"
LAUNCHER_VERSION = "1"
OWNER = "Orchestrator"
PACKAGE_KIND = "closer-runtime"
PUBLIC_SCHEMA = 1

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

# Extend the terminal distribution unit's API with the actual production Runtime
# closure. All code remains Orchestrator-owned; Core is independently pinned.

RUNTIME_PINNED_MODULES = module_closure(
    Path(__file__).resolve().parent,
    (
        *PINNED_MODULES,
        "closer_distribution_package.py",
        "closer_agent_launchers.py",
        "closer_launch_preflight.py",
        "closer_launch_operator.py",
        "closer_native_readmission.py",
        "closer_retained_reconciliation.py",
        "closer_host_restore_contract.py",
        "closer_native_wrapper_install.py",
        "closer_native_source_upgrade.py",
    ),
)


class DistributionPackageError(RuntimeError):
    """Invalid runtime package or incompatible Core pin."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _now() -> float:
    return time.time()


def module_digests(
    module_dir: Path, names: Sequence[str] = RUNTIME_PINNED_MODULES
) -> dict[str, str]:
    module_dir = module_dir.resolve()
    out: dict[str, str] = {}
    for name in names:
        path = module_dir / name
        if not path.is_file():
            raise DistributionPackageError(f"missing runtime module: {name}")
        out[name] = sha256_file(path)
    return out


def compute_runtime_package_digest(
    *,
    candidate_sha: str,
    modules: Mapping[str, str],
    state_versions: Mapping[str, int],
    supported_core: Mapping[str, Any],
) -> str:
    return sha256_json(
        [
            PACKAGE_KIND,
            OWNER,
            RUNTIME_VERSION,
            LAUNCHER_VERSION,
            CONTRACT,
            candidate_sha,
            sorted(modules.items()),
            sha256_json(dict(state_versions)),
            supported_core,
        ]
    )


def build_public_runtime_descriptor(
    module_dir: Path,
    *,
    candidate_sha: str,
    state_versions: dict[str, int],
    supported_core: dict[str, Any],
    modules: Sequence[str] = RUNTIME_PINNED_MODULES,
) -> dict[str, Any]:
    if not _SHA.fullmatch(candidate_sha):
        raise ValueError("candidate_sha must be a 40-char lowercase git SHA")
    digests = module_digests(module_dir, modules)
    if "closer_role_selection.py" in digests:
        import closer_role_selection as selection

        selection._policy_bytes(selection.policy_path(module_dir))
    launcher = {
        "version": LAUNCHER_VERSION,
        "module": "closer_launcher.py",
        "sha256": digests["closer_launcher.py"],
        "compatible_runtime": RUNTIME_VERSION,
        "compatible_contract": CONTRACT,
    }
    descriptor: dict[str, Any] = {
        "schema_version": PUBLIC_SCHEMA,
        "package_kind": PACKAGE_KIND,
        "owner": OWNER,
        "contract": CONTRACT,
        "candidate_sha": candidate_sha,
        "runtime_version": RUNTIME_VERSION,
        "modules": digests,
        "launcher": launcher,
        "state_versions": dict(state_versions),
        "state_digest": sha256_json(state_versions),
        "supported_core": dict(supported_core),
    }
    descriptor["package_digest"] = compute_runtime_package_digest(
        candidate_sha=candidate_sha,
        modules=digests,
        state_versions=state_versions,
        supported_core=supported_core,
    )
    return descriptor


def validate_public_runtime_descriptor(descriptor: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if descriptor.get("schema_version") != PUBLIC_SCHEMA:
        problems.append("unsupported public schema_version")
    if descriptor.get("package_kind") != PACKAGE_KIND:
        problems.append("package_kind mismatch")
    if descriptor.get("owner") != OWNER:
        problems.append("owner must be Orchestrator")
    if descriptor.get("contract") != CONTRACT:
        problems.append("contract mismatch")
    if descriptor.get("runtime_version") != RUNTIME_VERSION:
        problems.append("unsupported runtime_version")
    if not _SHA.fullmatch(str(descriptor.get("candidate_sha", ""))):
        problems.append("invalid candidate_sha")
    modules = descriptor.get("modules")
    if not isinstance(modules, dict) or set(modules) != set(RUNTIME_PINNED_MODULES):
        problems.append("modules set does not match pinned runtime modules")
    elif any(
        Path(name).name != name or not _DIGEST.fullmatch(str(digest))
        for name, digest in modules.items()
    ):
        problems.append("invalid module name/digest")
    launcher = descriptor.get("launcher") or {}
    if launcher.get("version") != LAUNCHER_VERSION:
        problems.append("launcher version mismatch")
    if launcher.get("compatible_contract") != CONTRACT:
        problems.append("launcher contract mismatch")
    if (
        launcher.get("module") != "closer_launcher.py"
        or launcher.get("compatible_runtime") != RUNTIME_VERSION
        or launcher.get("sha256") != (modules or {}).get("closer_launcher.py")
    ):
        problems.append("launcher differs from the owned Runtime entrypoint")
    core = descriptor.get("supported_core") or {}
    if core.get("owner") != "Workflows":
        problems.append("supported_core must name Workflows as owner")
    if core.get("contract") != CONTRACT:
        problems.append("supported_core contract mismatch")
    if not _DIGEST.fullmatch(str(core.get("package_digest", ""))):
        problems.append("supported_core package digest must be pinned")
    if descriptor.get("state_digest") != sha256_json(descriptor.get("state_versions") or {}):
        problems.append("state_digest does not bind state_versions")
    digest = descriptor.get("package_digest")
    if not _DIGEST.fullmatch(str(digest or "")):
        problems.append("invalid package_digest")
    elif not problems:
        expected = compute_runtime_package_digest(
            candidate_sha=str(descriptor["candidate_sha"]),
            modules=modules or {},
            state_versions=descriptor.get("state_versions") or {},
            supported_core=core,
        )
        if digest != expected:
            problems.append("package_digest does not match descriptor contents")
    return problems


def check_core_compatibility(
    runtime_descriptor: Mapping[str, Any],
    core_descriptor: Mapping[str, Any],
) -> list[str]:
    problems: list[str] = []
    want = runtime_descriptor.get("supported_core") or {}
    if core_descriptor.get("owner") != want.get("owner"):
        problems.append("core owner mismatch")
    if core_descriptor.get("contract") != want.get("contract"):
        problems.append("core contract mismatch")
    if want.get("core_version") and core_descriptor.get("core_version") != want.get("core_version"):
        problems.append("core_version incompatible")
    pinned = want.get("package_digest")
    if pinned and core_descriptor.get("package_digest") != pinned:
        problems.append("core package_digest does not match runtime pin")
    return problems


def materialize_runtime_tree(
    module_dir: Path,
    dest: Path,
    descriptor: Mapping[str, Any],
) -> Path:
    problems = validate_public_runtime_descriptor(descriptor)
    if problems:
        raise DistributionPackageError("; ".join(problems))
    import closer_role_selection as selection

    # The Python source pin contains the expected data SHA. Do not introduce an
    # unbound manifest field or a config directory inside the flat native image.
    policy = selection._policy_bytes(selection.policy_path(module_dir))
    dest = dest.resolve()
    if dest.exists():
        raise DistributionPackageError(
            "destination exists; use a fresh immutable Runtime directory"
        )
    dest.mkdir(parents=True)
    for name, digest in descriptor["modules"].items():
        src = module_dir / name
        target = dest / name
        shutil.copy2(src, target)
        if sha256_file(target) != digest:
            raise DistributionPackageError(f"materialized digest mismatch: {name}")
    (dest / selection.POLICY_FILENAME).write_bytes(policy)
    selection._policy_bytes(dest / selection.POLICY_FILENAME)
    (dest / "closer-runtime-package.json").write_text(
        json.dumps(dict(descriptor), indent=2, sort_keys=True) + "\n"
    )
    return dest


def host_binding_from_public(
    public: Mapping[str, Any],
    *,
    install_root: Path,
    campaign_root: str | Path | None = None,
    core_binding: Mapping[str, Any] | None = None,
    python: str | None = None,
    policy: Mapping[str, Any] | None = None,
    mechanism_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a public runtime descriptor into a campaign-style host manifest."""
    problems = validate_public_runtime_descriptor(public)
    if problems:
        raise DistributionPackageError("; ".join(problems))
    install_root = install_root.resolve()
    from closer_campaign import sha256_json as host_digest

    runtime = {
        "python": python or sys.executable,
        "version": RUNTIME_VERSION,
    }
    state_root = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex" / "orchestrator"))
    root = (
        Path(
            campaign_root
            or os.environ.get("ORCH_CLOSER_CAMPAIGN_DIR")
            or state_root / "closer-campaigns"
        )
        .expanduser()
        .resolve()
    )
    command = [
        runtime["python"],
        str(install_root / "closer_launcher.py"),
        "run",
        "--manifest",
        str(install_root / "host-manifest.json"),
        "--root",
        str(root),
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract": CONTRACT,
        "candidate_sha": public["candidate_sha"],
        "module_dir": str(install_root),
        "modules": dict(public["modules"]),
        "module_path": str(install_root / "closer_campaign.py"),
        "portable": dict(core_binding or {}),
        "policy": dict(policy or {}),
        "runtime": runtime,
        "runtime_digest": host_digest(runtime),
        "command": command,
        "command_digest": host_digest(command),
        "state_versions": dict(public["state_versions"]),
        "state_digest": host_digest(public["state_versions"]),
        "launcher": {
            "version": LAUNCHER_VERSION,
            "path": str(install_root / "closer_launcher.py"),
            "sha256": public["launcher"]["sha256"],
            "compatible_runtime": RUNTIME_VERSION,
            "compatible_contract": CONTRACT,
        },
        "distribution": {
            "schema": 1,
            "orchestrator": {
                "owner": OWNER,
                "runtime_version": RUNTIME_VERSION,
                "modules": dict(public["modules"]),
            },
            "launcher": public["launcher"],
            "core": {
                "owner": "Workflows",
                "contract": CONTRACT,
                "pinned": (core_binding or {}).get("package_digest"),
            },
        },
        "package_digest": public["package_digest"],
        "created_at": _now(),
        "public_runtime_descriptor": "closer-runtime-package.json",
    }
    if mechanism_contract is not None:
        manifest["mechanism_contract"] = dict(mechanism_contract)
        from closer_mechanisms import CLI, mechanism

        if mechanism(manifest) == CLI:
            manifest["command"].extend(["--mechanism", CLI])
            manifest["command_digest"] = host_digest(manifest["command"])
    return manifest


def write_host_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n")


def campaign_adapter_fields(host_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Fields campaign.make_manifest consumers read; omits absolute module_dir."""
    return {
        "schema_version": host_manifest.get("schema_version"),
        "contract": host_manifest.get("contract"),
        "candidate_sha": host_manifest.get("candidate_sha"),
        "package_digest": host_manifest.get("package_digest"),
        "state_digest": host_manifest.get("state_digest"),
        "distribution": host_manifest.get("distribution"),
        "launcher": host_manifest.get("launcher"),
    }


def _selftest() -> int:
    failures = 0
    module_dir = Path(__file__).resolve().parent
    core_pin = {
        "owner": "Workflows",
        "contract": CONTRACT,
        "core_version": "1",
        "package_digest": "a" * 64,
    }
    d = build_public_runtime_descriptor(
        module_dir,
        candidate_sha="b" * 40,
        state_versions={"campaign": 1},
        supported_core=core_pin,
    )
    if validate_public_runtime_descriptor(d):
        print("FAIL: valid descriptor rejected")
        failures += 1
    bad = dict(d)
    bad["state_versions"] = {"campaign": 99}
    if not validate_public_runtime_descriptor(bad):
        print("FAIL: state change should invalidate digest")
        failures += 1
    print(f"selftest: {failures} failures")
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--module-dir", type=Path, default=Path(__file__).resolve().parent)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--candidate-sha", required=True)
    b.add_argument("--core-digest", required=True)
    b.set_defaults(func=_cmd_build)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


def _cmd_build(args: argparse.Namespace) -> int:
    supported_core = {
        "owner": "Workflows",
        "contract": CONTRACT,
        "core_version": "1",
        "package_digest": args.core_digest,
    }
    descriptor = build_public_runtime_descriptor(
        args.module_dir,
        candidate_sha=args.candidate_sha,
        state_versions={"campaign": 1},
        supported_core=supported_core,
    )
    materialize_runtime_tree(args.module_dir, args.out, descriptor)
    print(json.dumps({"package_digest": descriptor["package_digest"]}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest())
    raise SystemExit(main())
