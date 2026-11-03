"""Source class policy, not installed tool/catalog or confinement qualification.

Only Black/Ruff rails already recorded by this repository are compiled. A model
can select a candidate operation, never supply commands or required-check facts.
No live CI collector, installed binary, source finding registration or root
callback is registered here. Missing facts remain UNMET; docformatter's version
pin alone supplies neither an invocation nor an independent check contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import closer_local_tool_confinement as files

SCHEMA = "closer-mechanical-class-policy/v1"
TOOL_CHECK_SCHEMA = "closer-required-tool-check/v1"
PIN_FILE = ".github/workflows/autofix-versions.env"
SOURCE_FILES = (PIN_FILE, "pyproject.toml", "ruff.toml", "scripts/ci_lint_baseline.py")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OPTIONS = {
    "format": (
        "black",
        ("--line-length", "100"),
        ("--check", "--line-length", "100"),
        "pyproject.toml",
    ),
    "lint_fix": ("ruff", ("check", "--fix"), ("check", "--output-format", "concise"), "ruff.toml"),
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ClassPolicy:
    operation: str
    tool: str
    version: str
    source_refs: tuple[tuple[str, str], ...]
    schema: str = SCHEMA

    def descriptor(self):
        return asdict(self)

    def argv(self, executable: str, workspace: Path, *, check=False):
        family, write, readonly, config = _OPTIONS[self.operation]
        if family != self.tool:
            raise ValueError("class_policy_tool_differs")
        return (executable, *(readonly if check else write), "--config", str(workspace / config))


def _read_source(path):
    path = files._canonical(path, directory=False)
    before = files._identity(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if files._stat_identity(os.fstat(stream.fileno())) != before:
            raise ValueError("source_policy_open_identity_changed")
        raw = stream.read(256 * 1024 + 1)
        if (
            len(raw) > 256 * 1024
            or files._stat_identity(os.fstat(stream.fileno())) != before
            or files._identity(path) != before
        ):
            raise ValueError("source_policy_read_identity_changed")
    return raw


def compile_catalog(source_root: Path):
    """Read fixed source files only; never locate/probe an installed executable."""
    root = source_root.resolve(strict=True)
    source = {name: _read_source(root / name) for name in SOURCE_FILES}
    refs = tuple((name, hashlib.sha256(source[name]).hexdigest()) for name in SOURCE_FILES)
    pins = {}
    for line in source[PIN_FILE].decode("utf-8").splitlines():
        if line.startswith(("BLACK_VERSION=", "RUFF_VERSION=", "DOCFORMATTER_VERSION=")):
            name, version = line.split("=", 1)
            if name in pins or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
                raise ValueError("managed_tool_pin_invalid")
            pins[name] = version
    black = tomllib.loads(source["pyproject.toml"].decode("utf-8"))["tool"]["black"]
    ruff = tomllib.loads(source["ruff.toml"].decode("utf-8"))
    baseline = source["scripts/ci_lint_baseline.py"].decode("utf-8")
    if (
        black.get("line-length") != 100
        or "tests/rail_exercises" not in black.get("force-exclude", "")
        or ruff.get("line-length") != 100
        or ruff.get("target-version") != "py313"
        or ruff.get("force-exclude") is not True
        or not {"tests/rail_exercises", ".workflows-lib"}.issubset(ruff.get("extend-exclude", []))
        or set(ruff.get("lint", {}).get("select", [])) != {"E4", "E7", "E9", "F", "I"}
        or "GATE_LINE_LENGTH = 100" not in baseline
    ):
        raise ValueError("source_formatter_lint_rails_changed")
    # Recheck after reads: a compiled source descriptor never mixes revisions.
    if refs != tuple((name, files._hash(root / name)) for name in SOURCE_FILES):
        raise ValueError("source_policy_changed_during_read")
    policies = tuple(
        ClassPolicy(op, family, pins[family.upper() + "_VERSION"], refs)
        for op, (family, *_rest) in _OPTIONS.items()
    )
    return policies, {"docstring_fix": "source_command_and_independent_check_rail_UNMET"}


def policy_from_descriptor(value):
    if not isinstance(value, Mapping) or set(value) != {
        "operation",
        "tool",
        "version",
        "source_refs",
        "schema",
    }:
        raise ValueError("source_class_policy_invalid")
    operation = value["operation"]
    if (
        operation not in _OPTIONS
        or value["schema"] != SCHEMA
        or value["tool"] != _OPTIONS[operation][0]
    ):
        raise ValueError("source_class_policy_unsupported")
    refs = value["source_refs"]
    if (
        not isinstance(value["version"], str)
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value["version"])
        or not isinstance(refs, (list, tuple))
        or len(refs) != len(SOURCE_FILES)
        or any(
            not isinstance(row, (list, tuple))
            or len(row) != 2
            or row[0] != name
            or not isinstance(row[1], str)
            or not _SHA.fullmatch(row[1])
            for row, name in zip(refs, SOURCE_FILES)
        )
    ):
        raise ValueError("source_class_pin_or_refs_invalid")
    return ClassPolicy(
        operation, value["tool"], value["version"], tuple(tuple(row) for row in refs)
    )


def validate_tool_check_identity(test, plan):
    """Validate an OWNED registry entry, never a registration from plan JSON."""
    expected = {
        "schema",
        "test_id",
        "target",
        "head",
        "finding_id",
        "kind",
        "class_policy",
        "policy_sha256",
        "executable",
        "executable_sha256",
        "ci_finding",
    }
    if (
        not isinstance(test, Mapping)
        or set(test) != expected
        or test.get("schema") != TOOL_CHECK_SCHEMA
        or test.get("kind") != "tool_check"
    ):
        raise ValueError("registered_tool_check_identity_invalid")
    policy = policy_from_descriptor(test["class_policy"])
    if (
        policy.operation != plan.get("operation")
        or test.get("policy_sha256") != digest(test["class_policy"])
        or any(test.get(key) != plan.get(key) for key in ("target", "head", "finding_id"))
        or not isinstance(test.get("executable"), str)
        or not Path(test["executable"]).is_absolute()
        or not isinstance(test.get("executable_sha256"), str)
        or not _SHA.fullmatch(test["executable_sha256"])
    ):
        raise ValueError("source_tool_check_binding_changed")
    if any(
        path == ".workflows-lib" or path.startswith((".workflows-lib/", "tests/rail_exercises/"))
        for path in plan.get("paths", [])
    ):
        raise ValueError("source_protected_check_exclusion_path")
    finding = test["ci_finding"]
    if (
        not isinstance(finding, Mapping)
        or set(finding)
        != {
            "schema",
            "target",
            "head",
            "finding_id",
            "source_owner",
            "paths",
            "operation",
            "check_name",
            "run_id",
            "run_attempt",
            "check_id",
            "source_receipt_sha256",
            "collector_source_sha256",
        }
        or finding.get("schema") != "closer-collected-tool-finding/v1"
        or any(
            finding.get(k) != plan.get(k)
            for k in ("target", "head", "finding_id", "source_owner", "paths", "operation")
        )
        or finding.get("check_name")
        != ("python ci / lint-format" if policy.operation == "format" else "python ci / lint-ruff")
        or any(
            type(finding.get(k)) is not int or finding[k] < 1
            for k in ("run_id", "run_attempt", "check_id")
        )
        or any(
            not isinstance(finding.get(k), str) or not _SHA.fullmatch(finding[k])
            for k in ("source_receipt_sha256", "collector_source_sha256")
        )
    ):
        raise ValueError("source_CI_finding_path_check_binding_changed")
    return policy


def resolve_candidate(request, *, source_root: Path):
    """Expose available policy and exact missing owner facts without registering it.

    The current CI packet inventory has no authenticated lint finding/check
    producer with path/command identities. Hints cannot fill that source gap.
    """
    if not isinstance(request, Mapping):
        request = {}
    source_problem = []
    try:
        policies, missing = compile_catalog(source_root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        policies = ()
        missing = {"docstring_fix": "source_command_and_independent_check_rail_UNMET"}
        source_problem = [f"source_policy_UNMET:{type(exc).__name__}"]
    policy = next((row for row in policies if row.operation == request.get("operation")), None)
    return {
        "schema": "closer-mechanical-catalog-resolution/v1",
        "class_policy": policy.descriptor() if policy else None,
        "missing": source_problem
        + ([missing["docstring_fix"]] if request.get("operation") == "docstring_fix" else [])
        + [
            "authenticated_CI_finding_path_check_collector_UNMET",
            "exact_source_owner_registration_UNMET",
            "installed_binary_read_closure_UNMET",
            "independent_postimage_check_collector_UNMET",
            "installed_confinement_qualification_UNMET",
            "root_repair_callback_wiring_UNMET",
        ],
        "qualified": False,
        "authority": False,
    }
