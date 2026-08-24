#!/usr/bin/env python3
"""Bounded, idempotent GitHub Actions artifact-ingestion bridge for consumer-sync shadow capability."""

from __future__ import annotations

import argparse
import base64
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import capabilities
import consumer_sync_shadow

# Global mock hook for testing
GH_COMMAND_MOCK: Callable[[list[str]], Any] | None = None


def _stable_hash(namespace: str, value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(namespace.encode() + b"\0" + encoded).hexdigest()


def _content_hash(path: Path) -> str:
    if path.is_file():
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    rows: list[dict[str, str]] = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            rows.append(
                {
                    "path": child.relative_to(path).as_posix(),
                    "sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
                }
            )
    return _stable_hash("consumer-sync-directory", rows)


def run_gh(args: list[str]) -> str:
    _gh_throttle("core")
    if GH_COMMAND_MOCK is not None:
        val = GH_COMMAND_MOCK(args)
        if isinstance(val, bytes):
            return val.decode("utf-8")
        return str(val)
    env = dict(os.environ)
    if "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env:
        token_path = Path.home() / ".codex/credentials/gh_cli_token"
        if token_path.is_file():
            try:
                env["GH_TOKEN"] = token_path.read_text().strip()
            except Exception:
                pass
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise Exception(f"gh command failed: {r.stderr}")
    return r.stdout


def run_gh_bytes(args: list[str]) -> bytes:
    _gh_throttle("core")
    if GH_COMMAND_MOCK is not None:
        val = GH_COMMAND_MOCK(args)
        if isinstance(val, bytes):
            return val
        return str(val).encode("utf-8")
    env = dict(os.environ)
    if "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env:
        token_path = Path.home() / ".codex/credentials/gh_cli_token"
        if token_path.is_file():
            try:
                env["GH_TOKEN"] = token_path.read_text().strip()
            except Exception:
                pass
    r = subprocess.run(args, capture_output=True, env=env)
    if r.returncode != 0:
        raise Exception(f"gh command failed: {r.stderr.decode('utf-8', errors='ignore')}")
    return r.stdout


# Test-only injected registry
TEST_REGISTRY: list[str] | None = None


def make_tree_responses(
    files: dict[str, str | bytes], *, executable: frozenset[str] = frozenset()
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Test support: a virtual repo as GitHub would serve it (tree response + blob responses).

    Blob ids are REAL git object ids, so `blob_sha256`'s integrity check exercises the same path it
    does against GitHub rather than being bypassed in tests.
    """
    tree: list[dict[str, Any]] = []
    blobs: dict[str, dict[str, Any]] = {}
    directories: set[str] = set()
    for path, content in sorted(files.items()):
        data = content.encode("utf-8") if isinstance(content, str) else content
        sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        tree.append(
            {
                "path": path,
                "mode": "100755" if path in executable else "100644",
                "type": "blob",
                "sha": sha,
                "size": len(data),
            }
        )
        blobs[sha] = {
            "sha": sha,
            "size": len(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }
        parts = path.split("/")[:-1]
        for depth in range(len(parts)):
            directories.add("/".join(parts[: depth + 1]))
    for directory in sorted(directories):
        tree.append(
            {
                "path": directory,
                "mode": "040000",
                "type": "tree",
                "sha": hashlib.sha1(directory.encode()).hexdigest(),
            }
        )
    return {"sha": "0" * 40, "truncated": False, "tree": tree}, blobs


def _gh_throttle(resource: str) -> None:
    try:
        import gh_capacity

        gh_capacity.throttle_if_enabled(resource)
    except Exception:
        pass


def safe_extract_zip(
    zip_bytes: bytes,
    dest_dir: Path,
    *,
    max_total_bytes: int = 50 * 1024 * 1024,
    max_entry_bytes: int = 20 * 1024 * 1024,
):
    dest_dir = Path(dest_dir).resolve()

    # 1. Reject oversized zip bytes
    if len(zip_bytes) > max_total_bytes:
        raise ValueError(
            f"Archive zip_bytes size ({len(zip_bytes)}) exceeds max_total_bytes limit ({max_total_bytes})"
        )

    with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
        # 2. Reject oversized total uncompressed size
        total_uncompressed = sum(member.file_size for member in zf.infolist())
        if total_uncompressed > max_total_bytes:
            raise ValueError(
                f"Archive total uncompressed size ({total_uncompressed}) exceeds max_total_bytes ({max_total_bytes})"
            )

        seen_paths: set[str] = set()

        for member in zf.infolist():
            # 3. Reject oversized entries
            if member.file_size > max_entry_bytes:
                raise ValueError(
                    f"Entry {member.filename} size ({member.file_size}) exceeds max_entry_bytes ({max_entry_bytes})"
                )

            # 4. Reject absolute / traversal paths
            filename = member.filename
            if filename.startswith("/") or ".." in Path(filename).parts:
                raise ValueError(f"Unsafe zip entry path (absolute/traversal): {filename}")

            # 5. Reject symlinks (Unix external attribute S_IFLNK)
            is_symlink = (member.external_attr >> 16) & 0o170000 == 0o120000
            if is_symlink:
                raise ValueError(f"Unsafe zip entry (symlink): {filename}")

            # Resolve target path and verify it doesn't escape dest_dir
            target_path = (dest_dir / filename).resolve()
            if not target_path.is_relative_to(dest_dir):
                raise ValueError(f"Unsafe zip entry: {filename}")

            # 6. Reject duplicate normalized paths
            norm_path = str(target_path)
            if norm_path in seen_paths:
                raise ValueError(f"Duplicate normalized path in zip: {filename}")
            seen_paths.add(norm_path)

            zf.extract(member, path=dest_dir)


# ARTIFACT CONTRACT. The producer (stranske/Workflows
# .github/workflows/health-69-consumer-sync-shadow-evidence.yml) uploads a whole DIRECTORY
# (`path: consumer-sync-shadow-evidence/`), so its member set grows every time a reporting step is
# added to that job. It gained completion-evidence.json, evidence-ledger.json,
# capabilities-state.json and runtime-report.json, and from 2026-08-12 stopped matching the pinned
# two-file set this consumer demanded — after which EVERY ingest raised and the promotion evidence
# for capability:reference-sync-hygiene-test-gate could never start accruing (subjects_seen stuck
# at 0 for a reason unrelated to the subject-identity wiring).
#
# So the contract is REQUIRED-PRESENT, not exact-match: the two files this consumer actually reads
# must be there, and extras are tolerated. Re-pinning today's six would only move the same break to
# the next reporting step the producer adds. Extras stay INERT — never parsed, never trusted, and
# only extracted under safe_extract_zip's traversal/symlink/size bounds — and are still refused
# when the NAME looks secret-bearing (same markers consumer_sync_shadow applies to run_ref) or when
# the count runs away, which would signal a producer regression rather than a new sidecar.
ARTIFACT_REQUIRED_MEMBERS = frozenset({"consumer-sync-plan.json", "handoff.json"})
ARTIFACT_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "api-key",
    "apikey",
    "credential",
)
ARTIFACT_MAX_EXTRA_MEMBERS = 24


def validate_artifact_members(infolist: list[zipfile.ZipInfo]) -> set[str]:
    """Enforce the producer artifact contract; return the member names.

    Raises ValueError on a missing required member, a non-regular-file member, a secret-bearing
    extra, or an extra count past the runaway bound.
    """
    names: set[str] = set()
    for m in infolist:
        is_dir = m.filename.endswith("/") or (m.external_attr & 0x10) != 0
        is_symlink = (m.external_attr >> 16) & 0o170000 == 0o120000
        if is_dir or is_symlink:
            raise ValueError(f"Artifact member {m.filename} must be a regular file")
        names.add(m.filename)

    missing = sorted(ARTIFACT_REQUIRED_MEMBERS - names)
    if missing:
        raise ValueError(f"Artifact is missing required member(s) {missing}; got: {sorted(names)}")

    extras = sorted(names - ARTIFACT_REQUIRED_MEMBERS)
    if len(extras) > ARTIFACT_MAX_EXTRA_MEMBERS:
        raise ValueError(
            f"Artifact carries {len(extras)} extra members, past the runaway bound "
            f"{ARTIFACT_MAX_EXTRA_MEMBERS}: {extras[:8]}..."
        )
    for extra in extras:
        lowered = extra.lower()
        for marker in ARTIFACT_SECRET_MARKERS:
            if marker in lowered:
                raise ValueError(
                    f"Artifact extra member looks secret-bearing ({marker!r}): {extra}"
                )
    return names


# TARGETED REPO READ. This used to download each consumer repo's whole zipball to hash the plan's
# targets. That is 3% signal: stranske/Fine-Art-Archive is 122MB of which 105MB is committed JPEGs
# under data/image_cache/ plus a committed node_modules/, and the ingest reads 3.8MB of it. The cap
# in safe_extract_zip refused it, and raising the cap would not have helped — the transport itself
# fails (measured 48MB in 10 minutes, ~80KB/s, then `stream error: ... CANCEL; received from peer`),
# so the fix would have been a 20-minute hang inside an hourly tick.
#
# Instead: ONE recursive tree call per repo yields every path, type, size and blob SHA, which
# already answers existence and file-vs-directory. Content is then fetched per blob and memoised on
# the blob SHA — git blob ids are content-addressed, so the memo is sound across repos AND across
# targets. Measured on the live cohort: 1,560 blob reads collapse to 311 distinct blobs (80% saved
# on the very first pass), and daily transfer drops from ~171MB to ~4.3MB.
#
# `observed_sha256` is hashed into the published effect_fingerprint, so it stays sha256-of-content.
# The blob SHA is a CACHE KEY only and is never substituted for the recorded evidence.
REPO_TREE_BLOB_MODES = frozenset({"100644", "100755"})
REPO_TREE_DIR_MODE = "040000"
REPO_READ_MAX_BLOB_BYTES = 20 * 1024 * 1024
REPO_READ_MAX_TOTAL_BYTES = 50 * 1024 * 1024
# Each blob read is one throttled `gh` call, and gh_capacity paces up to MAX_PACE_S=10s/call once
# core drops below 25% — so an unbounded fetch count is a latent multi-hour cron stall, not just a
# slow step. Measured steady state across the whole cohort is 311 distinct blobs; this bound leaves
# headroom and turns a runaway into a loud failure that retries next tick (per-repo state makes
# that resumable) instead of a hang.
REPO_READ_MAX_BLOB_FETCHES = 800
BLOB_DIGEST_CACHE_MAX = 4000


def fetch_repo_tree(repo: str, ref: str) -> dict[str, dict[str, Any]]:
    """One recursive tree call -> {path: node}. Fails closed if GitHub truncated the response.

    Truncation is the dangerous case, not the slow one: GitHub caps `recursive=1` at 100k entries /
    7MB, and a truncated tree would make present targets read as ABSENT — the classifier would then
    propose `create` for everything and that wrong answer would be recorded as evidence. So this
    raises rather than degrading.
    """
    raw = run_gh(["gh", "api", f"repos/{repo}/git/trees/{ref}?recursive=1"])
    data = json.loads(raw)
    if data.get("truncated"):
        raise ValueError(
            f"repo_tree_truncated:{repo}:{ref} — refusing to classify against a partial tree"
        )
    tree = data.get("tree")
    if not isinstance(tree, list):
        raise ValueError(f"repo_tree_missing_array:{repo}")
    nodes: dict[str, dict[str, Any]] = {}
    for node in tree:
        if not isinstance(node, dict):
            raise ValueError(f"repo_tree_invalid_node:{repo}")
        path = node.get("path")
        if not isinstance(path, str) or not path or _safe_tree_path(path) is None:
            raise ValueError(f"repo_tree_unsafe_path:{repo}:{path!r}")
        nodes[path] = node
    return nodes


def _safe_tree_path(path: str) -> str | None:
    if path.startswith("/") or ".." in path.split("/") or path.startswith("./"):
        return None
    return path


class BlobReader:
    """Fetch-and-memoise blob digests under an explicit, run-scoped fetch budget.

    `digests` is a PURE {git blob id -> sha256} map, safe to persist as-is: git blob ids are
    content-addressed, so an entry can never go stale and needs no invalidation. The fetch counter
    is kept here rather than in that map so nothing but digests is ever written to it.
    """

    def __init__(
        self,
        digests: dict[str, str] | None = None,
        *,
        max_fetches: int = REPO_READ_MAX_BLOB_FETCHES,
    ) -> None:
        self.digests = {} if digests is None else digests
        self.max_fetches = max_fetches
        self.fetches = 0
        self.hits = 0

    def sha256(self, repo: str, node: dict[str, Any]) -> str:
        return blob_sha256(repo, node, self)


def blob_sha256(repo: str, node: dict[str, Any], reader: BlobReader) -> str:
    """sha256 hexdigest of one blob's bytes, memoised on the content-addressed git blob id."""
    sha = str(node.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"invalid_blob_sha:{repo}:{sha!r}")
    cached = reader.digests.get(sha)
    if cached is not None:
        reader.hits += 1
        return cached

    if reader.fetches >= reader.max_fetches:
        raise ValueError(f"repo_read_blob_fetch_budget_exhausted:{repo}:{reader.max_fetches}")
    reader.fetches += 1

    size = node.get("size")
    if isinstance(size, int) and size > REPO_READ_MAX_BLOB_BYTES:
        raise ValueError(
            f"blob_exceeds_max_bytes:{repo}:{node.get('path')}:{size}>{REPO_READ_MAX_BLOB_BYTES}"
        )

    data = json.loads(run_gh(["gh", "api", f"repos/{repo}/git/blobs/{sha}"]))
    encoding = data.get("encoding")
    if encoding == "base64":
        content = base64.b64decode(data.get("content") or "")
    elif encoding == "utf-8":
        content = str(data.get("content") or "").encode("utf-8")
    else:
        raise ValueError(f"unsupported_blob_encoding:{repo}:{encoding!r}")
    if len(content) > REPO_READ_MAX_BLOB_BYTES:
        raise ValueError(f"blob_exceeds_max_bytes:{repo}:{sha}:{len(content)}")

    # Verify the git object id over the bytes we received. This is what makes the memo sound: the
    # key is only a valid cache key if it really addresses this content. (SHA-1 here is git's
    # object id, not a security primitive.)
    recomputed = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if recomputed != sha:
        raise ValueError(f"blob_content_sha_mismatch:{repo}:{sha}!={recomputed}")

    digest = hashlib.sha256(content).hexdigest()
    reader.digests[sha] = digest
    return digest


def _tree_node_kind(repo: str, path: str, node: dict[str, Any]) -> str:
    """'file' | 'dir'; raises on symlinks and submodules rather than guessing their hash.

    The zipball path hashed a symlink's TARGET bytes (``Path.is_file()`` follows links) while a git
    tree stores the link text, so the two reads would silently disagree. None exist in any target
    path across the live cohort today; if one appears, fail the repo instead of recording a hash
    whose meaning changed.
    """
    node_type = node.get("type")
    mode = str(node.get("mode") or "")
    if node_type == "tree" and mode == REPO_TREE_DIR_MODE:
        return "dir"
    if node_type == "blob" and mode in REPO_TREE_BLOB_MODES:
        return "file"
    raise ValueError(f"unsupported_tree_entry:{repo}:{path}:type={node_type}:mode={mode}")


def observed_targets_from_tree(
    repo: str,
    nodes: dict[str, dict[str, Any]],
    plan: dict[str, Any],
    *,
    reader: BlobReader,
) -> dict[str, str]:
    """Reproduce the zipball read's observed_targets from a tree, byte-for-byte.

    Directory hashes MUST equal what `_content_hash` produced from an extracted zipball, or every
    directory target flips to `update` on the cutover and pollutes the ledger. `_content_hash`
    walked `sorted(path.rglob("*"))`, and `Path` orders by COMPONENT TUPLE, not by flat string —
    so `a/deep/leaf.txt` sorts before `a-sibling.css` ('a' < 'a-sibling.css'), the opposite of what
    sorting the joined strings gives. The ledger's existing hashes were computed under that rule,
    so it is reproduced deliberately here rather than inferred; `test_targeted_read_matches_legacy_
    zipball_read` pins it and will fail loudly if a future Python changes pathlib's ordering.
    """
    directory_in_plan: dict[str, bool] = {}
    for entry in plan.get("entries", []):
        directory_in_plan.setdefault(entry["target"], entry["is_directory"])
    for removal in plan.get("removals", []):
        # A removal carries no is_directory; the zipball path defaulted it to False and raised on a
        # directory found there. Preserved.
        directory_in_plan.setdefault(removal["target"], False)

    observed: dict[str, str] = {}
    total_bytes = 0
    for target, is_dir_in_plan in directory_in_plan.items():
        node = nodes.get(target)
        if node is None:
            continue
        kind = _tree_node_kind(repo, target, node)
        if (kind == "dir") != bool(is_dir_in_plan):
            raise TypeError(
                f"Type mismatch for target {target}: plan expected "
                f"directory={is_dir_in_plan}, got directory={kind == 'dir'}"
            )

        if kind == "file":
            total_bytes += int(node.get("size") or 0)
            if total_bytes > REPO_READ_MAX_TOTAL_BYTES:
                raise ValueError(f"repo_read_exceeds_max_total_bytes:{repo}")
            observed[target] = "sha256:" + reader.sha256(repo, node)
            continue

        prefix = target + "/"
        rows: list[dict[str, str]] = []
        for path in sorted(
            (p for p in nodes if p.startswith(prefix)),
            key=lambda p: p.split("/"),  # component-wise, matching Path ordering
        ):
            child = nodes[path]
            if _tree_node_kind(repo, path, child) != "file":
                continue
            total_bytes += int(child.get("size") or 0)
            if total_bytes > REPO_READ_MAX_TOTAL_BYTES:
                raise ValueError(f"repo_read_exceeds_max_total_bytes:{repo}")
            rows.append(
                {
                    "path": path[len(prefix) :],
                    "sha256": reader.sha256(repo, child),
                }
            )
        observed[target] = _stable_hash("consumer-sync-directory", rows)
    return observed


# REPO HYGIENE. Not a new subsystem and deliberately not a registered capability: the tree call
# above already carries every path and size, so the measurement is a BYPRODUCT of work the ingest
# does anyway — no extra API call, no scheduled job, no second store, no lifecycle of its own. It
# rides the existing report artifact.
#
# What it is for: Fine-Art-Archive committed ~105MB of JPEGs under data/image_cache/ plus a
# node_modules/ tree, which is what made the old whole-repo read impossible. The targeted read makes
# the ingest immune to that, but the bloat is still real and still costs every OTHER consumer of
# those repos (clones, CI checkouts, agent worktrees). Naming it in the report is FYI-only — no
# queue, no approval, nothing that can accumulate.
# Regenerable with certainty: a cache or a local virtualenv is never deliberately committed.
HYGIENE_DEBRIS_DIRS = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".venv", "venv"}
)
# Depends on evidence: deps are debris UNLESS an ECOSYSTEM-MATCHED manifest sits beside them.
# Matching any manifest is wrong and was briefly wrong here — stranske/Template's root
# pyproject.toml (Python) would otherwise vouch for a JavaScript node_modules and hide 7.35MB of
# genuine debris behind a "maybe vendored" label.
HYGIENE_MANIFEST_FOR_DIR = {
    "node_modules": ("package.json",),
    "bower_components": ("bower.json",),
    "site-packages": ("requirements.txt", "pyproject.toml", "setup.py"),
}
# Deliberate often enough that recommending removal would be reckless: `vendor/` means "committed
# on purpose" by convention, and JS GitHub Actions ship a committed `dist/`.
HYGIENE_REVIEW_DIRS = frozenset({"vendor", "dist", "build", "target", ".next", ".nuxt"})
HYGIENE_DEPENDENCY_DIRS = (
    tuple(HYGIENE_DEBRIS_DIRS) + tuple(HYGIENE_MANIFEST_FOR_DIR) + tuple(HYGIENE_REVIEW_DIRS)
)
HYGIENE_LARGE_BLOB_BYTES = 5 * 1024 * 1024
HYGIENE_REPO_WARN_BYTES = 50 * 1024 * 1024
HYGIENE_MAX_REPORTED = 8


def repo_hygiene(repo: str, nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Measure committed bloat from the tree already in hand. Read-only, never raises."""
    blobs = [
        (path, int(node.get("size") or 0))
        for path, node in nodes.items()
        if node.get("type") == "blob"
    ]
    total = sum(size for _, size in blobs)
    present = set(nodes)

    dependency_dirs: dict[str, dict[str, int]] = {}
    for path, size in blobs:
        parts = path.split("/")
        for depth, part in enumerate(parts[:-1]):
            if part not in HYGIENE_DEPENDENCY_DIRS:
                continue
            root = "/".join(parts[: depth + 1])
            row = dependency_dirs.setdefault(root, {"bytes": 0, "files": 0})
            row["bytes"] += size
            row["files"] += 1
            break  # attribute to the OUTERMOST dependency dir only, never double-count

    # Directories holding large binaries. Count EVERY blob under such a directory, not only the
    # ones over the threshold — otherwise a 105MB image cache reports as 38MB and the number
    # understates the case it is making.
    large_dirs: dict[str, dict[str, int]] = {}
    for path, size in blobs:
        if size < HYGIENE_LARGE_BLOB_BYTES or "/" not in path:
            continue
        large_dirs.setdefault(path.rsplit("/", 1)[0], {"bytes": 0, "files": 0})
    for path, size in blobs:
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        if parent in large_dirs:
            large_dirs[parent]["bytes"] += size
            large_dirs[parent]["files"] += 1

    findings: list[dict[str, Any]] = []
    for root, row in sorted(dependency_dirs.items(), key=lambda kv: -kv[1]["bytes"]):
        # EVIDENCE, not assumption. A dependency dir with a sibling manifest may be deliberately
        # vendored — stranske/Template's .github/scripts/node_modules is declared by a
        # `"minimatch": "file:node_modules/minimatch"` dependency, copied into workflows-lib by
        # agents-auto-pilot.yml and allowlisted by agents-guard.yml. Untracking that would break
        # both. A dir with NO manifest anywhere is debris (a bare `npm install --no-save` that got
        # committed), and caches are debris by definition.
        parent = root.rsplit("/", 1)[0] if "/" in root else ""
        leaf = root.rsplit("/", 1)[-1]
        manifest = next(
            (
                m
                for m in (
                    (f"{parent}/" if parent else "") + name
                    for name in HYGIENE_MANIFEST_FOR_DIR.get(leaf, ())
                )
                if m in present
            ),
            None,
        )
        if leaf in HYGIENE_DEBRIS_DIRS:
            disposition = "untrack"
        elif leaf in HYGIENE_REVIEW_DIRS:
            disposition = "review_vendored"
        else:
            disposition = "untrack" if manifest is None else "review_vendored"
        findings.append(
            {
                "kind": "dependency_dir",
                "path": root,
                "bytes": row["bytes"],
                "files": row["files"],
                # Anchored to THIS path. A bare `node_modules/` pattern matches at every depth and
                # would swallow the vendored copy too — the precise pattern is the safe one.
                "gitignore": "/" + root + "/",
                "manifest": manifest,
                "disposition": disposition,
            }
        )
    for parent, row in sorted(large_dirs.items(), key=lambda kv: -kv[1]["bytes"]):
        if any(parent == f["path"] or parent.startswith(f["path"] + "/") for f in findings):
            continue  # already covered by a dependency-dir finding
        findings.append(
            {
                "kind": "large_binary_dir",
                "path": parent,
                "bytes": row["bytes"],
                "files": row["files"],
                "gitignore": "/" + parent + "/",
                "manifest": None,
                # Never assume large binaries are disposable — they may be the product. The owner
                # decides; this only puts the number in front of them.
                "disposition": "review_owner",
            }
        )

    findings = findings[:HYGIENE_MAX_REPORTED]
    reclaimable = sum(row["bytes"] for row in findings if row["disposition"] == "untrack")
    return {
        "schema": "orchestrator.consumer-sync-repo-hygiene/v1",
        "repository": repo.lower(),
        "tracked_bytes": total,
        "tracked_files": len(blobs),
        "oversized": total > HYGIENE_REPO_WARN_BYTES,
        "reclaimable_bytes": reclaimable,
        "reclaimable_pct": round(100.0 * reclaimable / total, 1) if total else 0.0,
        "findings": findings,
        "remediation": hygiene_remediation(repo, findings),
    }


def hygiene_remediation(repo: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    """The exact steps to fix it. Emitted as data, never executed — this bridge has no write authority."""
    untrack = [row for row in findings if row["disposition"] == "untrack"]
    review = [row for row in findings if row["disposition"] != "untrack"]
    if not findings:
        return {}
    out: dict[str, Any] = {
        "gitignore_lines": [row["gitignore"] for row in untrack],
        "untrack_commands": [f"git rm -r --cached {row['path']}" for row in untrack],
        "note": (
            "Untracking removes these from HEAD, so new clones and archives shrink; existing "
            "history still carries the blobs. Shrinking history needs git-filter-repo and a "
            "force-push, which rewrites every open PR's base — do that deliberately, separately."
        ),
    }
    if out["untrack_commands"]:
        out["untrack_commands"].append(
            "git commit -m 'chore: untrack generated dependency/cache dirs'"
        )
    if review:
        out["review_only"] = [
            {
                "path": row["path"],
                "bytes": row["bytes"],
                "why": (
                    f"declared by {row['manifest']} — may be deliberately vendored; "
                    "check what consumes it before touching"
                    if row.get("manifest")
                    else "large binary content; only the owner can say whether it is the product"
                ),
            }
            for row in review
        ]
    return out


# HYGIENE ESCALATION. The detector measured honestly and then wrote into a JSON file nobody reads
# daily — a finding that never reaches anyone is indistinguishable from no finding. This closes
# that loop, and it does so by WIRING EXISTING SURFACES rather than inventing a notifier:
# `feedback.record_owner_question` already provides the project's canonical non-blocking touchpoint
# (states the default it is proceeding on, dedupes while open, auto-ratifies at expiry so a backlog
# is structurally impossible), and `periodic_report.py` is already the documented check-in digest.
#
# Measured before building (the owner's weekly attention budget, shared across every system (see LOCAL_POLICY.md)):
#   * new findings fleet-wide: 8 debris-touching commits / 180 days across the 5-repo cohort,
#     clustered into ~4 events => ~0.67 findings/month.
#   * only JUDGMENT findings ask anything. `untrack` is machine-decidable (no manifest vouches for
#     it) and goes to the digest with exact commands; `review_vendored` is expected and never asks.
#     Historical `review_owner` rate above the materiality floor: 1 in 180 days => ~0.17/month.
#   * ~0.17 questions/month x ~2 min = ~0.3 min/month against ~130 min/month. Ratio ~0.003.
# The floor matters: without it this would ask about a 47KB vendor directory.
HYGIENE_ASK_MIN_BYTES = 25 * 1024 * 1024
HYGIENE_ASK_EXPIRES_DAYS = 30.0
HYGIENE_DIGEST_MIN_BYTES = 1 * 1024 * 1024


def _hygiene_size_band(size: int) -> str:
    """Coarse band so a standing finding is ONE question, not a new one on every byte change.

    `record_owner_question` keys on question text + scope, so putting exact bytes in the question
    would re-ask whenever the cache grew by a file. Banding means it only re-asks when the
    situation materially changes.
    """
    band = HYGIENE_ASK_MIN_BYTES
    return f"{(size // band) * band // (1024 * 1024)}MB+"


def hygiene_escalation(report_repos: dict[str, Any]) -> dict[str, Any]:
    """Turn hygiene findings into a digest plus the judgment-only questions. Pure; records nothing."""
    digest: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    for repo, row in sorted(report_repos.items()):
        hygiene = row.get("hygiene") or {}
        for finding in hygiene.get("findings") or []:
            size = int(finding.get("bytes") or 0)
            disposition = finding.get("disposition")
            if disposition == "untrack" and size >= HYGIENE_DIGEST_MIN_BYTES:
                digest.append(
                    {
                        "repository": repo,
                        "path": finding["path"],
                        "bytes": size,
                        "action": "untrack",
                        "command": f"git rm -r --cached {finding['path']}",
                        "gitignore": finding["gitignore"],
                    }
                )
            elif disposition == "review_owner" and size >= HYGIENE_ASK_MIN_BYTES:
                # The ONLY thing a human is asked. Machine evidence cannot settle whether large
                # binary content is the product or residue — but silence must still be safe, so
                # the default is to change nothing.
                questions.append(
                    {
                        "repository": repo,
                        "path": finding["path"],
                        "bytes": size,
                        # Identity-bearing text: BAND only, no exact byte count and no file
                        # count. record_owner_question keys on this string, so any field that
                        # moves when one file is added would re-ask about a standing finding.
                        "question": (
                            f"{repo}: {finding['path']} holds {_hygiene_size_band(size)} of "
                            f"committed binary content. Is it the product, or regenerable "
                            f"residue that should be untracked?"
                        ),
                        "files": finding.get("files"),
                        "default_action": (
                            "leave it tracked and change nothing; re-ask only if it grows "
                            "materially"
                        ),
                        "options": ["keep (it is the product)", "untrack it", "untrack + ignore"],
                    }
                )
    return {
        "schema": "orchestrator.consumer-sync-hygiene-escalation/v1",
        "digest": digest,
        "questions": questions,
        "untrackable_bytes": sum(row["bytes"] for row in digest),
    }


def record_hygiene_escalation(escalation: dict[str, Any]) -> dict[str, Any]:
    """Record the judgment questions. Fail-open: an escalation problem must never fail an ingest."""
    recorded: list[dict[str, Any]] = []
    for row in escalation.get("questions") or []:
        try:
            import feedback

            result = feedback.record_owner_question(
                row["question"],
                row["default_action"],
                repo=row["repository"],
                options=row["options"],
                expires_days=HYGIENE_ASK_EXPIRES_DAYS,
            )
            recorded.append({**result, "repository": row["repository"], "path": row["path"]})
        except Exception as exc:  # noqa: BLE001 - reporting must not break the read-only bridge
            print(f"Hygiene escalation skipped for {row['repository']}: {exc}", file=sys.stderr)
    return {"recorded": recorded, "asked": len(recorded)}


# Consumer repos that must NEVER be drift SUBJECTS, even though they are registered consumers.
#
# stranske/Orchestrator is the tool that HOSTS this drift detection. It was registered as consumer
# 14 on 2026-08-21 so the agent lanes can reach it, which also put it in
# REGISTERED_CONSUMER_REPOS — the list this module reads. Left alone it would become a subject of
# its own detector, and subject identity is what `capability:reference-sync-hygiene-test-gate`
# promotes on, so the tool could feed its own promotion gate. That is circular self-evidence, the
# same defect as attributing feature-building PRs as usage of the feature.
#
# It is currently excluded only INCIDENTALLY, because the cohort is intersected with the repo-review
# registry and Orchestrator is not in it yet. That protection disappears the moment the repo is
# added to the registry for review — which it should be. So the exclusion is made deliberate here.
NON_SUBJECT_CONSUMERS = frozenset({"stranske/orchestrator"})


def is_drift_subject(repo: str) -> bool:
    """May this registered consumer be a drift SUBJECT? Registration alone is not consent."""
    return str(repo or "").strip().lower() not in NON_SUBJECT_CONSUMERS


def get_maint_68_repos(fallback_allowed: bool = True) -> list[str]:
    _gh_throttle("core")
    try:
        res = run_gh(
            [
                "gh",
                "api",
                "repos/stranske/Workflows/contents/.github/workflows/maint-68-sync-consumer-repos.yml",
            ]
        )
        data = json.loads(res)
        content = base64.b64decode(data["content"]).decode("utf-8")
        repos = []
        in_repos = False
        for line in content.splitlines():
            if "REGISTERED_CONSUMER_REPOS:" in line:
                in_repos = True
                continue
            if in_repos:
                if (
                    line.strip()
                    and not line.startswith(" ")
                    and not line.startswith("-")
                    and ":" in line
                ):
                    break
                cleaned = line.strip().strip("-").strip().strip('"').strip("'").strip()
                if cleaned and "/" in cleaned:
                    repos.append(cleaned)
        if repos:
            return repos
        raise Exception("Registry file contents did not contain any valid consumer repositories")
    except Exception as e:
        if not fallback_allowed:
            raise Exception(f"Failed to load registry during active ingestion: {e}") from e
        print(
            f"Warning: failed to fetch maint-68 workflow from GitHub: {e}",
            file=sys.stderr,
        )

    return [
        "stranske/Template",
        "stranske/Ready",
        "stranske/Collab-Admin",
        "stranske/learning-management-system",
        "stranske/Fine-Art-Archive",
    ]


def load_state(state_file: Path) -> dict:
    if not state_file.is_file():
        return {"schema_version": 1, "records": {}, "exceptions": [], "blob_digests": {}}
    try:
        content = state_file.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as e:
        raise ValueError(f"Corrupt state file: {e}") from e

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported or missing state schema version: {data.get('schema_version')}"
        )

    if "records" not in data or "exceptions" not in data:
        raise ValueError("Corrupt state: missing records or exceptions field")
    # Added after the state file already existed in the field, so absence is normal, not corrupt.
    digests = data.get("blob_digests")
    data["blob_digests"] = digests if isinstance(digests, dict) else {}
    return data


def save_state(state: dict, state_file: Path):
    # Bound the digest cache. A miss only costs a refetch, so dropping the oldest insertions is
    # safe; the live cohort converges to ~350 entries, so this should never bind in practice.
    digests = state.get("blob_digests")
    if isinstance(digests, dict) and len(digests) > BLOB_DIGEST_CACHE_MAX:
        state["blob_digests"] = dict(list(digests.items())[-BLOB_DIGEST_CACHE_MAX:])

    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_suffix(".tmp")

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, state_file)


def discover_artifact() -> dict[str, Any] | None:
    res = run_gh(
        [
            "gh",
            "api",
            "repos/stranske/Workflows/actions/workflows/health-69-consumer-sync-shadow-evidence.yml/runs?status=success&per_page=20",
        ]
    )
    runs_data = json.loads(res)
    runs = runs_data.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("workflow_runs_response_missing_array")
    lookup_errors: list[str] = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("id"), int):
            continue
        run_id = run["id"]
        run_attempt = run.get("run_attempt", 1)
        expected_artifact_name = f"consumer-sync-shadow-evidence-{run_id}-{run_attempt}"

        try:
            res_art = run_gh(
                [
                    "gh",
                    "api",
                    f"repos/stranske/Workflows/actions/runs/{run_id}/artifacts",
                ]
            )
            artifacts_data = json.loads(res_art)
            artifacts = artifacts_data.get("artifacts", [])
        except Exception as exc:
            lookup_errors.append(f"run {run_id}: {exc}")
            continue

        for art in artifacts:
            if art.get("name") == expected_artifact_name and not art.get("expired", False):
                return {
                    "run_id": run_id,
                    "run_attempt": run_attempt,
                    "artifact_id": art["id"],
                    "artifact_name": expected_artifact_name,
                }
    if lookup_errors:
        raise RuntimeError("artifact_lookup_failed:" + "; ".join(lookup_errors[:3]))
    return None


def run_selftests():
    import io
    import tempfile

    # 1. Test exact directory/file hashing
    print("Running selftest: Directory hashing...", end="")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        f1 = tmp_path / "a.txt"
        f1.write_text("hello", encoding="utf-8")
        h1 = _content_hash(f1)
        expected_h1 = "sha256:" + hashlib.sha256(b"hello").hexdigest()
        assert h1 == expected_h1, f"Expected {expected_h1}, got {h1}"

        d1 = tmp_path / "dir"
        d1.mkdir()
        (d1 / "x.txt").write_text("x", encoding="utf-8")
        (d1 / "y.txt").write_text("y", encoding="utf-8")
        h_dir = _content_hash(d1)
        # Expected hash of directory: stable hash of sorted relative paths and their content sha256s
        rows = [
            {"path": "x.txt", "sha256": hashlib.sha256(b"x").hexdigest()},
            {"path": "y.txt", "sha256": hashlib.sha256(b"y").hexdigest()},
        ]
        expected_h_dir = _stable_hash("consumer-sync-directory", rows)
        assert h_dir == expected_h_dir, f"Expected {expected_h_dir}, got {h_dir}"
    print(" OK")

    # 2. Test unsafe zip variants and size bounds
    print("Running selftest: Unsafe zip variants and size bounds...", end="")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Test valid extraction
        valid_buf = io.BytesIO()
        with zipfile.ZipFile(valid_buf, "w") as zf:
            zf.writestr("file.txt", "content")
        dest = tmp_path / "dest"
        dest.mkdir()
        safe_extract_zip(valid_buf.getvalue(), dest)
        assert (dest / "file.txt").read_text() == "content"

        # Test traversal path (Zip Slip)
        bad_buf = io.BytesIO()
        with zipfile.ZipFile(bad_buf, "w") as zf:
            zf.writestr("../traversal.txt", "evil")
        try:
            safe_extract_zip(bad_buf.getvalue(), dest)
            raise AssertionError("Should have rejected traversal path")
        except ValueError as e:
            assert "absolute/traversal" in str(e)

        # Test absolute path
        bad_buf2 = io.BytesIO()
        with zipfile.ZipFile(bad_buf2, "w") as zf:
            zf.writestr("/absolute.txt", "evil")
        try:
            safe_extract_zip(bad_buf2.getvalue(), dest)
            raise AssertionError("Should have rejected absolute path")
        except ValueError as e:
            assert "absolute/traversal" in str(e)

        # Test symlink
        bad_buf3 = io.BytesIO()
        with zipfile.ZipFile(bad_buf3, "w") as zf:
            zi = zipfile.ZipInfo("symlink.txt")
            zi.external_attr = 0o120000 << 16  # S_IFLNK
            zf.writestr(zi, "target")
        try:
            safe_extract_zip(bad_buf3.getvalue(), dest)
            raise AssertionError("Should have rejected symlink")
        except ValueError as e:
            assert "symlink" in str(e)

        # Test duplicate normalized path
        bad_buf4 = io.BytesIO()
        with zipfile.ZipFile(bad_buf4, "w") as zf:
            zf.writestr("dup.txt", "content1")
            zf.writestr("dup.txt", "content2")
        try:
            safe_extract_zip(bad_buf4.getvalue(), dest)
            raise AssertionError("Should have rejected duplicate path")
        except ValueError as e:
            assert "Duplicate" in str(e)

        # Test oversized archive/entry
        try:
            safe_extract_zip(valid_buf.getvalue(), dest, max_total_bytes=2)
            raise AssertionError("Should have rejected oversized archive")
        except ValueError as e:
            assert "exceeds max_total_bytes" in str(e)
    print(" OK")

    # 3. Test caps validation
    print("Running selftest: Caps validation...", end="")
    try:
        main(["ingest", "--max-artifacts", "2"])
        raise AssertionError("Should have rejected max_artifacts > 1")
    except ValueError as e:
        assert "max-artifacts" in str(e)

    try:
        main(["ingest", "--max-repositories", "6"])
        raise AssertionError("Should have rejected max_repositories > 5")
    except ValueError as e:
        assert "max-repositories" in str(e)
    print(" OK")

    # 4. Test artifact member contract: required-present, extras tolerated but bounded and inert
    print("Running selftest: Artifact member contract...", end="")

    def _members(*names: str) -> list[zipfile.ZipInfo]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name in names:
                zf.writestr(name, "{}")
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            return zf.infolist()

    # The shape the producer uploads TODAY must be accepted, extras and all. This is exactly the
    # artifact that the pinned two-file set refused 29 times from 2026-08-12 onward.
    producer_members = (
        "consumer-sync-plan.json",
        "handoff.json",
        "evidence-ledger.json",
        "completion-evidence.json",
        "capabilities-state.json",
        "runtime-report.json",
    )
    assert validate_artifact_members(_members(*producer_members)) == set(producer_members)

    # The bare required pair still ingests, and a sidecar the producer has not invented yet must
    # not break ingest either — pinning today's six would only relocate the same failure.
    assert validate_artifact_members(_members("consumer-sync-plan.json", "handoff.json")) == set(
        ARTIFACT_REQUIRED_MEMBERS
    )
    assert "future-sidecar.json" in validate_artifact_members(
        _members(*producer_members, "future-sidecar.json")
    )

    # Required members stay mandatory, one at a time.
    for dropped in sorted(ARTIFACT_REQUIRED_MEMBERS):
        try:
            validate_artifact_members(_members(*[n for n in producer_members if n != dropped]))
            raise AssertionError(f"Should have rejected artifact missing {dropped}")
        except ValueError as e:
            assert "missing required member" in str(e), e

    # Secret-bearing extras are still refused by name (same markers consumer_sync_shadow applies
    # to run_ref); tolerating extras must not become tolerating credential exfiltration.
    for leak in ("gh-token.json", "SECRET-notes.txt", "deploy.credentials", "API-KEY"):
        try:
            validate_artifact_members(_members(*producer_members, leak))
            raise AssertionError(f"Should have rejected secret-bearing extra {leak}")
        except ValueError as e:
            assert "secret-bearing" in str(e), e

    # Non-regular members are still refused.
    for name, attr in (("nested/", 0), ("link.json", 0o120000 << 16)):
        odd_buf = io.BytesIO()
        with zipfile.ZipFile(odd_buf, "w") as zf:
            for member in producer_members:
                zf.writestr(member, "{}")
            zi = zipfile.ZipInfo(name)
            zi.external_attr = attr
            zf.writestr(zi, "")
        with zipfile.ZipFile(io.BytesIO(odd_buf.getvalue())) as zf:
            try:
                validate_artifact_members(zf.infolist())
                raise AssertionError(f"Should have rejected non-regular member {name}")
            except ValueError as e:
                assert "must be a regular file" in str(e), e

    # A runaway extra count reads as a producer regression, not as a new sidecar.
    try:
        validate_artifact_members(
            _members(
                *producer_members,
                *[f"extra-{i}.json" for i in range(ARTIFACT_MAX_EXTRA_MEMBERS + 1)],
            )
        )
        raise AssertionError("Should have rejected runaway extra count")
    except ValueError as e:
        assert "runaway bound" in str(e), e
    print(" OK")

    # 5. Integration tests (E2E, preview, expired, corrupt state, hashing failure, exception no-record)
    print("Running selftest: Integration scenarios...", end="")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        state_dir = tmp_path / "state"
        ledger_path = tmp_path / "capabilities.json"
        capabilities.save({}, ledger_path)

        # Setup valid plan and handoff
        plan = {
            "schema": "workflows.consumer-sync-plan/v1",
            "version": 1,
            "manifest_sha256": "sha256:" + "a" * 64,
            "entries": [
                {
                    "section": "workflows",
                    "source": ".github/workflows/new.yml",
                    "resolved_source": "templates/consumer-repo/.github/workflows/new.yml",
                    "target": ".github/workflows/new.yml",
                    "description": "Fixture entry",
                    "sync_mode": None,
                    "is_directory": False,
                    "skip_repos": [],
                    "skip_reasons": {},
                    "overwrite_repos": [],
                    "template_sync": None,
                    "delivery": "copy",
                    "requires": [],
                    "content_sha256": "sha256:" + hashlib.sha256(b"desired").hexdigest(),
                    "effect_fingerprint": "sha256:" + "e" * 64,
                }
            ],
            "removals": [],
        }
        # Recalculate effect fingerprint to pass validation
        entry = plan["entries"][0]
        effect_fields = {
            k: entry[k] for k in entry if k not in ("effect_fingerprint", "description")
        }
        entry["effect_fingerprint"] = _stable_hash("consumer-sync-source-effect", effect_fields)
        plan["plan_id"] = _stable_hash("consumer-sync-plan", plan)

        handoff = {
            "schema": "workflows.consumer-sync-shadow-handoff/v1",
            "version": 1,
            "capability_id": "capability:reference-sync-hygiene-test-gate",
            "plan_schema": "workflows.consumer-sync-plan/v1",
            "plan_id": plan["plan_id"],
            "manifest_sha256": plan["manifest_sha256"],
            "entry_count": 1,
            "removal_count": 0,
            "plan_filename": "consumer-sync-plan.json",
            "run_ref": "github-actions:stranske/Workflows:12345:1",
            "supervision_mode": "shadow",
            "write_authority": False,
            "promotion_allowed": False,
            "effect_allowlist": ["create", "update", "remove", "skip", "no_change"],
            "kill_switch": "ORCH_REFERENCE_WORKFLOW_DISABLED=1",
            "consumer": "Orchestrator/consumer_sync_shadow.py",
        }
        handoff["handoff_id"] = _stable_hash("consumer-sync-shadow-handoff", handoff)

        # Mock artifact zip. Built as the SUPERSET the producer actually uploads — the reporting
        # sidecars are part of the fixture so the whole integration path below (preview, ingest,
        # idempotency) is the regression guard for the artifact contract, not just the unit block.
        valid_artifact_buf = io.BytesIO()
        with zipfile.ZipFile(valid_artifact_buf, "w") as zf:
            zf.writestr("consumer-sync-plan.json", json.dumps(plan))
            zf.writestr("handoff.json", json.dumps(handoff))
            zf.writestr("evidence-ledger.json", json.dumps([]))
            zf.writestr("completion-evidence.json", json.dumps({"status": "accepted"}))
            zf.writestr("capabilities-state.json", json.dumps({}))
            zf.writestr(
                "runtime-report.json",
                json.dumps({"schema": "workflows.consumer-sync-runtime-report/v1"}),
            )
        artifact_bytes = valid_artifact_buf.getvalue()

        # Mock repo contents, served the way the targeted read asks for them: one tree, then
        # blobs. No zipball is downloaded any more.
        repo_tree, repo_blobs = make_tree_responses({".github/workflows/new.yml": "current"})

        # Mock GH API command responses
        run_list = {"workflow_runs": [{"id": 12345, "run_attempt": 1, "conclusion": "success"}]}
        artifact_list = {
            "artifacts": [
                {
                    "id": 9999,
                    "name": "consumer-sync-shadow-evidence-12345-1",
                    "expired": False,
                }
            ]
        }

        global TEST_REGISTRY
        TEST_REGISTRY = ["stranske/Template"]

        def mock_gh(args: list[str]) -> str | bytes:
            cmd = " ".join(args)
            if "health-69-consumer-sync-shadow-evidence.yml/runs?status=success" in cmd:
                return json.dumps(run_list)
            if "runs/12345/artifacts" in cmd:
                return json.dumps(artifact_list)
            if "artifacts/9999/zip" in cmd:
                return artifact_bytes
            # Tree/blob routes must be matched BEFORE the bare repo-metadata route, which is a
            # prefix of both.
            if "repos/stranske/Template/git/trees/" in cmd:
                return json.dumps(repo_tree)
            if "repos/stranske/Template/git/blobs/" in cmd:
                return json.dumps(repo_blobs[cmd.rsplit("/", 1)[-1]])
            if "repos/stranske/Template" in cmd:
                return json.dumps({"default_branch": "main"})
            raise ValueError(f"Unexpected mock gh command: {cmd}")

        global GH_COMMAND_MOCK
        saved_gh_mock = GH_COMMAND_MOCK
        GH_COMMAND_MOCK = mock_gh

        try:
            # A. Test preview mode (no-write)
            rc_preview = main(
                [
                    "preview",
                    "--state-dir",
                    str(state_dir),
                    "--ledger",
                    str(ledger_path),
                    "--repository",
                    "stranske/Template",
                    "--mode",
                    "shadow",
                    "--now",
                    "1784894400",
                ]
            )
            assert rc_preview == 0
            assert not (state_dir / "consumer-sync-ingest-state.json").exists()
            assert not (state_dir / "consumer-sync-artifact-ingest-report.json").exists()
            assert not capabilities.load(ledger_path, create=False)

            # B. Test run-attempt mismatch (should reject)
            mismatch_handoff = dict(handoff)
            mismatch_handoff["run_ref"] = "github-actions:stranske/Workflows:12345:2"
            mismatch_handoff["handoff_id"] = _stable_hash(
                "consumer-sync-shadow-handoff", mismatch_handoff
            )

            mismatch_buf = io.BytesIO()
            with zipfile.ZipFile(mismatch_buf, "w") as zf:
                zf.writestr("consumer-sync-plan.json", json.dumps(plan))
                zf.writestr("handoff.json", json.dumps(mismatch_handoff))

            old_art_bytes = artifact_bytes
            artifact_bytes = mismatch_buf.getvalue()

            rc_mismatch = main(
                [
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--ledger",
                    str(ledger_path),
                    "--repository",
                    "stranske/Template",
                    "--mode",
                    "shadow",
                    "--now",
                    "1784894400",
                ]
            )
            assert rc_mismatch == 1, "Should have failed run-attempt mismatch"
            artifact_bytes = old_art_bytes

            # C. Test expired human-on-exception fails closed and does not write
            try:
                main(
                    [
                        "ingest",
                        "--state-dir",
                        str(state_dir),
                        "--ledger",
                        str(ledger_path),
                        "--repository",
                        "stranske/Template",
                        "--mode",
                        "human-on-exception",
                        "--expiry",
                        "2026-07-20",
                        "--now",
                        "1784894400",
                    ]
                )
                raise AssertionError("Should have failed human-on-exception expired")
            except ValueError as e:
                assert "Expiry date" in str(e)

            assert not (state_dir / "consumer-sync-ingest-state.json").exists()

            # C2. Boundary: `--expiry DATE` names the LAST authorised day, so the phase is still
            # live ON that day. `<=` burned the final day of every window in production ("Expiry
            # date 2026-07-25 must be in the future relative to current date 2026-07-25", 4x).
            rc_boundary = main(
                [
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--ledger",
                    str(ledger_path),
                    "--repository",
                    "stranske/Template",
                    "--mode",
                    "human-on-exception",
                    "--phase-id",
                    "importer-selftest-boundary",
                    "--expiry",
                    "2026-07-24",  # exactly the --now date
                    "--now",
                    "1784894400",  # 2026-07-24
                ]
            )
            assert rc_boundary == 0, "Expiry ON the current date must still be live"

            # ...while the day before it is genuinely past, so it still fails closed.
            try:
                main(
                    [
                        "ingest",
                        "--state-dir",
                        str(state_dir),
                        "--ledger",
                        str(ledger_path),
                        "--repository",
                        "stranske/Template",
                        "--mode",
                        "human-on-exception",
                        "--phase-id",
                        "importer-selftest-boundary",
                        "--expiry",
                        "2026-07-23",
                        "--now",
                        "1784894400",
                    ]
                )
                raise AssertionError("Should have failed on a past expiry date")
            except ValueError as e:
                assert "must be on or after" in str(e), e

            # D. Test corrupt state check
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file = state_dir / "consumer-sync-ingest-state.json"
            state_file.write_text("corrupt data", encoding="utf-8")
            try:
                main(
                    [
                        "ingest",
                        "--state-dir",
                        str(state_dir),
                        "--ledger",
                        str(ledger_path),
                        "--repository",
                        "stranske/Template",
                        "--mode",
                        "shadow",
                        "--now",
                        "1784894400",
                    ]
                )
                raise AssertionError("Should have failed on corrupt state")
            except ValueError as e:
                assert "Corrupt state" in str(e)

            state_file.unlink()

            # E. Test end-to-end idempotency
            rc_first = main(
                [
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--ledger",
                    str(ledger_path),
                    "--repository",
                    "stranske/Template",
                    "--mode",
                    "shadow",
                    "--now",
                    "1784894400",
                ]
            )
            assert rc_first == 0

            state_data = load_state(state_file)
            key = "9999:stranske/template:shadow:importer-shadow"
            assert state_data["records"][key]["status"] == "success"

            report_file = state_dir / "consumer-sync-artifact-ingest-report.json"
            assert report_file.exists()
            report_data = json.loads(report_file.read_text(encoding="utf-8"))
            assert report_data["repositories"]["stranske/template"]["status"] == "success"

            ledger_data = capabilities.load(ledger_path, create=False)
            cap = ledger_data[consumer_sync_shadow.CAPABILITY_ID]
            outcomes1 = len(cap.get("event_history") or [])

            # Run again (idempotent, skips)
            rc_second = main(
                [
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--ledger",
                    str(ledger_path),
                    "--repository",
                    "stranske/Template",
                    "--mode",
                    "shadow",
                    "--now",
                    "1784894401",
                ]
            )
            assert rc_second == 0

            ledger_data2 = capabilities.load(ledger_path, create=False)
            cap2 = ledger_data2[consumer_sync_shadow.CAPABILITY_ID]
            outcomes2 = len(cap2.get("event_history") or [])
            assert (
                outcomes1 == outcomes2
            ), f"Ledger events count changed: {outcomes1} vs {outcomes2}"

            state_data2 = load_state(state_file)
            assert len(state_data["records"]) == len(state_data2["records"])

            # F. Test target hashing/type error does not record success
            state_file.unlink()
            report_file.unlink()

            repo_tree, repo_blobs = make_tree_responses(
                {".github/workflows/new.yml/nested.txt": "x"}
            )

            rc_fail = main(
                [
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--ledger",
                    str(ledger_path),
                    "--repository",
                    "stranske/Template",
                    "--mode",
                    "shadow",
                    "--now",
                    "1784894400",
                ]
            )
            assert rc_fail == 1

            state_data3 = load_state(state_file)
            assert state_data3["records"][key]["status"] == "failed"

        finally:
            GH_COMMAND_MOCK = saved_gh_mock
            TEST_REGISTRY = None

    print(" OK")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    if "--selftest" in argv or (argv and argv[0] == "selftest"):
        try:
            run_selftests()
            print("All selftests passed successfully!")
            return 0
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"Selftest failed: {e}", file=sys.stderr)
            return 1

    if not argv or argv[0] not in ("preview", "ingest"):
        argv.insert(0, "ingest")

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview_parser = subparsers.add_parser("preview", help="Preview ingestion")
    ingest_parser = subparsers.add_parser("ingest", help="Run active ingestion")

    for sub in (preview_parser, ingest_parser):
        sub.add_argument("--state-dir", type=Path, default=Path.home() / ".codex/orchestrator")
        sub.add_argument(
            "--repository",
            action="append",
            dest="repositories",
            help="Explicit repositories to process",
        )
        sub.add_argument(
            "--mode",
            choices=("shadow", "human-on-exception"),
            default="shadow",
            help="Supervision mode",
        )
        sub.add_argument("--expiry", help="ISO expiry date for human-on-exception")
        sub.add_argument("--ledger", type=Path, default=capabilities.REG)
        sub.add_argument("--now", type=int, help="Simulated current timestamp")
        sub.add_argument(
            "--max-artifacts",
            type=int,
            default=1,
            help="Max artifacts to process (hard cap 1)",
        )
        sub.add_argument(
            "--max-repositories",
            type=int,
            default=5,
            help="Max repositories to process (hard cap 5)",
        )
        sub.add_argument("--phase-id", default="importer-shadow", help="Importer phase identifier")

    args = parser.parse_args(argv)
    command = args.command

    # 1. Validate max-artifacts and max-repositories caps
    max_artifacts = args.max_artifacts
    max_repositories = args.max_repositories
    if max_artifacts != 1:
        raise ValueError("max-artifacts must be exactly 1")
    if not 1 <= max_repositories <= 5:
        raise ValueError("max-repositories must be between 1 and 5")

    # 2. Kill Switch check
    if os.environ.get("ORCH_REFERENCE_WORKFLOW_DISABLED") == "1":
        print("Kill switch active: ORCH_REFERENCE_WORKFLOW_DISABLED=1. Exiting.")
        return 0

    write_enabled = command == "ingest"
    current_time = time.time() if args.now is None else float(args.now)
    current_date = datetime.datetime.fromtimestamp(current_time, tz=datetime.timezone.utc).date()

    supervision_mode = args.mode
    if supervision_mode == "human-on-exception":
        if not args.expiry:
            raise ValueError(
                "Expiry date must be explicitly specified when human-on-exception mode is selected."
            )
        try:
            expiry_date = datetime.date.fromisoformat(args.expiry)
        except ValueError:
            raise ValueError(f"Invalid ISO date format for expiry: {args.expiry}")

        # BOUNDARY. `--expiry DATE` names the last day the phase is authorised, so the phase is
        # still live ON that day; `<=` here burned the final day of every window (four production
        # refusals reading "Expiry date 2026-07-25 must be in the future relative to current date
        # 2026-07-25"). Only a date already PAST fails closed.
        if expiry_date < current_date:
            raise ValueError(
                f"Expiry date {args.expiry} must be on or after current date {current_date}."
            )

    phase_id = args.phase_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", phase_id):
        raise ValueError("phase-id must be a short, safe identifier")
    if supervision_mode == "human-on-exception" and not phase_id.startswith("importer-"):
        raise ValueError("human-on-exception phase-id must start with importer-")

    state_dir = args.state_dir
    state_file = state_dir / "consumer-sync-ingest-state.json"
    if write_enabled:
        state_dir.mkdir(parents=True, exist_ok=True)
        lock_file = state_dir / "consumer-sync-ingest.lock"
        lock_f = open(lock_file, "w")
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Lock acquired by another ingest process. Exiting.", file=sys.stderr)
            return 0
    state = load_state(state_file)

    # 4. Discover runs and artifacts
    art_meta = discover_artifact()
    if art_meta is None:
        print("No successful, non-expired consumer-sync-shadow-evidence artifacts found. Exiting.")
        return 0

    artifact_id = art_meta["artifact_id"]
    artifact_name = art_meta["artifact_name"]
    run_id = art_meta["run_id"]
    run_attempt = art_meta["run_attempt"]

    # 5. Cohort loading & Validation
    cohort = args.repositories
    if not cohort:
        # Load registry, failing closed without fallback during active ingest
        cohort = get_maint_68_repos(fallback_allowed=(not write_enabled))

    # Enforce cohort size bound/clamp
    cohort = cohort[:max_repositories]

    # Validate repositories format and membership in registry
    registry_repos = (
        TEST_REGISTRY
        if TEST_REGISTRY is not None
        else get_maint_68_repos(fallback_allowed=(not write_enabled))
    )
    registry_set = {r.lower() for r in registry_repos}

    # Registration makes a repo a sync TARGET; it does not make it a drift SUBJECT.
    cohort = [r for r in cohort if is_drift_subject(r)]
    validated_cohort = []
    for repo in cohort:
        if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            raise ValueError(f"Invalid repository format: {repo}")
        if repo.lower() not in registry_set:
            raise ValueError(f"Repository {repo} is not in the registered consumer repos list.")
        validated_cohort.append(repo)

    # Deduplicate case-insensitively while preserving order
    seen = set()
    deduped_cohort = []
    for repo in validated_cohort:
        if repo.lower() not in seen:
            seen.add(repo.lower())
            deduped_cohort.append(repo)

    needs_processing = []
    for repo in deduped_cohort:
        key = f"{artifact_id}:{repo.lower()}:{supervision_mode}:{phase_id}"
        if state.get("records", {}).get(key, {}).get("status") != "success":
            needs_processing.append(repo)

    if not needs_processing:
        print(
            f"All repositories in cohort already processed for artifact {artifact_name} in mode {supervision_mode} and phase {phase_id}."
        )
        return 0

    # Download artifact zip
    print(f"Downloading artifact: {artifact_name} (ID: {artifact_id})")
    try:
        zip_bytes = run_gh_bytes(
            [
                "gh",
                "api",
                f"repos/stranske/Workflows/actions/artifacts/{artifact_id}/zip",
            ]
        )
    except Exception as e:
        print(f"Error downloading artifact zip: {e}", file=sys.stderr)
        return 1

    # Artifact must carry consumer-sync-plan.json and handoff.json as regular files; see the
    # ARTIFACT CONTRACT note above for why extras are tolerated rather than pinned.
    with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
        member_names = validate_artifact_members(zf.infolist())
    extra_members = sorted(member_names - ARTIFACT_REQUIRED_MEMBERS)
    if extra_members:
        print(f"Artifact carries {len(extra_members)} ignored extra(s): {extra_members}")

    # Safely extract zip
    with tempfile.TemporaryDirectory() as art_temp_dir:
        art_path = Path(art_temp_dir)
        try:
            safe_extract_zip(zip_bytes, art_path)
        except Exception as e:
            print(f"Zip extraction failed/unsafe: {e}", file=sys.stderr)
            return 1

        plan_file = art_path / "consumer-sync-plan.json"
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error parsing consumer-sync-plan.json: {e}", file=sys.stderr)
            return 1

        handoff_file = art_path / "handoff.json"
        try:
            handoff_data = json.loads(handoff_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error parsing handoff.json: {e}", file=sys.stderr)
            return 1

        # Validate plan and handoff
        try:
            plan = consumer_sync_shadow.validate_consumer_sync_plan(plan)
            # The producer handoff supervision_mode is always "shadow"
            handoff = consumer_sync_shadow.validate_shadow_handoff(handoff_data, plan=plan)
        except Exception as e:
            print(f"Validation of plan/handoff failed: {e}", file=sys.stderr)
            return 1

        # Bind run_ref to run identity
        expected_run_ref = f"github-actions:stranske/Workflows:{run_id}:{run_attempt}"
        if handoff.get("run_ref") != expected_run_ref:
            print(
                f"Error: Handoff run_ref mismatch. Expected {expected_run_ref}, got {handoff.get('run_ref')}",
                file=sys.stderr,
            )
            return 1

        failures = []
        # Shared across the whole cohort AND across runs. Within one run the five consumer repos
        # carry near-identical template content, so 1,560 blob reads collapse to 311 distinct
        # blobs; persisting the map in the existing state file (no second store — git blob ids are
        # content-addressed, so entries never go stale and need no invalidation) takes the next
        # day's steady state to roughly zero fetches. That matters because every fetch is a
        # throttled call: at LOW budget gh_capacity paces up to 10s each.
        blob_reader = BlobReader(state.setdefault("blob_digests", {}))
        needs_processing_lower = {repo.lower() for repo in needs_processing}
        report_repos = {
            repo.lower(): {"status": "already_recorded"}
            for repo in deduped_cohort
            if repo.lower() not in needs_processing_lower
        }

        # Process each repository in needs_processing
        for repo in needs_processing:
            key = f"{artifact_id}:{repo.lower()}:{supervision_mode}:{phase_id}"
            print(f"Processing repository: {repo}")

            try:
                # Fetch the repo metadata/default branch explicitly
                try:
                    repo_meta_raw = run_gh(["gh", "api", f"repos/{repo}"])
                    repo_meta = json.loads(repo_meta_raw)
                    default_branch = repo_meta["default_branch"]
                except Exception as e:
                    raise Exception(
                        f"Failed to fetch metadata/default branch for {repo}: {e}"
                    ) from e

                # Read only the plan's targets out of that ref — never the whole repo.
                try:
                    tree_nodes = fetch_repo_tree(repo, default_branch)
                except Exception as e:
                    raise Exception(
                        f"Failed to read tree for {repo} ref {default_branch}: {e}"
                    ) from e

                hygiene = repo_hygiene(repo, tree_nodes)

                # Hashing failures propagate as exceptions and fail the repo
                observed_targets = observed_targets_from_tree(
                    repo, tree_nodes, plan, reader=blob_reader
                )

                # Classify shadow drift
                result = consumer_sync_shadow.classify_shadow_drift(
                    plan, repository=repo, observed_targets=observed_targets
                )

                # Successful and fully read-only classification assertion
                if result.get("side_effects_performed") != []:
                    raise Exception("classification_returned_side_effects")

                if not write_enabled:
                    print(
                        f"[preview] Would record shadow result for {repo} with proposals: {len(result['proposals'])}"
                    )
                    report_repos[repo.lower()] = {
                        "status": "preview_success",
                        "proposals_count": len(result["proposals"]),
                        "hygiene": hygiene,
                    }
                    continue

                # Record shadow result with phase provenance
                evidence_artifact_ref = (
                    f"consumer-sync-{supervision_mode}-{phase_id}:{artifact_name}"
                )
                event_ref = f"{result['result_id']}:{phase_id}"

                receipt = consumer_sync_shadow.record_shadow_result(
                    result,
                    ledger_path=args.ledger,
                    timestamp=int(current_time),
                    supervision_mode=supervision_mode,
                    evidence_artifact_ref=evidence_artifact_ref,
                    event_ref=event_ref,
                    phase_id=phase_id,
                )

                action_counts: dict[str, int] = {}
                for proposal in result["proposals"]:
                    action = proposal["action"]
                    action_counts[action] = action_counts.get(action, 0) + 1

                # Save success state
                state["records"][key] = {
                    "artifact_id": artifact_id,
                    "artifact_name": artifact_name,
                    "repository": repo.lower(),
                    "supervision_mode": supervision_mode,
                    "phase_id": phase_id,
                    "run_id": run_id,
                    "run_attempt": run_attempt,
                    "handoff_id": handoff["handoff_id"],
                    "result_id": result["result_id"],
                    "default_branch": default_branch,
                    "observed_target_count": len(observed_targets),
                    "action_counts": action_counts,
                    "status": "success",
                    "receipt": receipt,
                    "timestamp": int(current_time),
                }
                save_state(state, state_file)
                # Greppable success line: the log previously had no token distinguishing an
                # accepted ingest from a silent no-op, so "zero accepted ingests" and "nothing
                # ran" looked identical in it.
                print(
                    f"Successfully processed and recorded result for {repo}. "
                    f"accepted receipt {receipt.get('receipt_id')} "
                    f"status={receipt.get('status')} "
                    f"evidence_status={receipt.get('evidence_status')} "
                    f"subject_id={result.get('repository')}"
                )

                report_repos[repo.lower()] = {
                    "status": "success",
                    "receipt": receipt,
                    "proposals_count": len(result["proposals"]),
                    "action_counts": action_counts,
                    "result_id": result["result_id"],
                    "default_branch": default_branch,
                    "hygiene": hygiene,
                }

            except Exception as e:
                print(f"Error processing {repo}: {e}", file=sys.stderr)
                failures.append((repo, e))
                report_repos[repo.lower()] = {"status": "failed", "error": str(e)}

                if write_enabled:
                    # Save failure state
                    state["records"][key] = {
                        "artifact_id": artifact_id,
                        "artifact_name": artifact_name,
                        "repository": repo.lower(),
                        "supervision_mode": supervision_mode,
                        "phase_id": phase_id,
                        "status": "failed",
                        "error": str(e),
                        "timestamp": int(current_time),
                    }
                    # Bound exceptions log
                    state["exceptions"].append(
                        {
                            "timestamp": int(current_time),
                            "artifact_id": artifact_id,
                            "repository": repo.lower(),
                            "supervision_mode": supervision_mode,
                            "phase_id": phase_id,
                            "error": str(e),
                        }
                    )
                    state["exceptions"] = state["exceptions"][-50:]
                    save_state(state, state_file)

        if write_enabled:
            # Close the loop on the hygiene findings: digest for the machine-decidable ones, an
            # auto-expiring owner question only for the judgment calls. Both surfaces already
            # exist; this only feeds them.
            escalation = hygiene_escalation(report_repos)
            escalation["recording"] = record_hygiene_escalation(escalation)

            status_counts: dict[str, int] = {}
            for row in report_repos.values():
                status = str(row.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            report_data = {
                "schema": "orchestrator.consumer-sync-artifact-ingest-report/v1",
                "generated_at": int(current_time),
                "supervision_mode": supervision_mode,
                "phase_id": phase_id,
                "phase_expires_on": args.expiry,
                "write_authority": False,
                "promotion_allowed": False,
                "remote_mutations_performed": [],
                "limits": {
                    "max_artifacts": max_artifacts,
                    "max_repositories": max_repositories,
                },
                "artifact_name": artifact_name,
                "artifact_id": artifact_id,
                "run_ref": expected_run_ref,
                "handoff_id": handoff["handoff_id"],
                "summary": status_counts,
                "repositories": report_repos,
                "promotion_dashboard": consumer_sync_shadow.promotion_dashboard(
                    ledger_path=args.ledger, now=int(current_time)
                ),
                "hygiene_escalation": escalation,
            }
            report_file = state_dir / "consumer-sync-artifact-ingest-report.json"
            tmp_report = report_file.with_suffix(".tmp")
            with open(tmp_report, "w", encoding="utf-8") as f:
                json.dump(report_data, f, indent=2, sort_keys=True)
            os.chmod(tmp_report, 0o600)
            os.replace(tmp_report, report_file)

        if failures:
            print(
                f"Ingestion finished with {len(failures)} repository failure(s).",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
