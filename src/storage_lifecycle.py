"""Run-owned verification storage: byte admission, private temp, terminal retirement.

Reservations never expire or imply reclaimed APFS blocks. Only the owning supervisor can
finish them. Interrupted/inspection-denied runs remain charged until explicit reconciliation.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

GIB = 1024**3
STRICT_RELEASE_FLOOR = 27 * GIB
# Conservative legacy bootstrap: the October 20 shared-volume decline is NOT an
# exclusive owned measurement. Keep its reserve until reviewed calibration can
# bound transient fixture/copy growth; terminal or sampled sizes cannot lower it.
INITIAL_VERIFY_PEAK = 29_490_778_112 - 5_630_369_792
OBSERVED_OPERATING_MARGIN = 5_630_369_792
JSON = dict[str, Any]


class StorageBlocked(RuntimeError):
    pass


OWNED_SAMPLE_CONTRACT = "owned-tree-sampled-allocation/v1"
SAMPLE_INTERVAL_SECONDS = 30.0
SAMPLE_SCAN_SECONDS = 1.0
SAMPLE_MAX_ENTRIES = 100_000


# Reviewed engineering forecast, NOT an exclusive or mathematical peak bound.
# Four post-861 strict PASS windows (before staging through child terminal/probe)
# declined 5094690816,4512907264,4578033664,6811734016 shared-volume bytes.
# Concurrent deletions can mask growth. Keep the existing 5630369792 policy
# uncertainty reserve; it is not a measured OS minimum. Terminal sizes are NOT peaks.
# Reviewers must refresh this declared producer manifest when adding/changing
# amplifying fixture/copy producers, including newly introduced unlisted producers.
REVIEWED_COLD_VERSION = "post861-exec-mirror-engineering/v1"
REVIEWED_WINDOW_BYTES = 6_811_734_016
REVIEWED_STAGING_BYTES = 611_594_240
REVIEWED_EXTERNAL_COPY_SHA256 = "1335374937c9eca5a9221ba1ad43535f23eaa84e447c8853cb0eec53a4ce8a70"
REVIEWED_RECEIPTS = (
    "d65f2f16d337bfd5d6854ebeb66e0bbef78bfd5b6a80f59cf44ce56cc360b401",
    "e9c6f38f765dd61d670c6bdb89ed3d4dd503e796ee99814a71e9e6a18251cdd2",
    "ec983a54e3593414d830b5f6ace89d80901847a92a9be6b9d50fc5200302432c",
    "88923538611de7ea9cdf0830abb8f53a893f0e02ce21c35342906d68ec4eb3fb",
)
REVIEWED_PRODUCERS = {
    "scripts/build_exec_mirror.sh": "b426986ee2d1eedeb0b17e6b736320d75fae98b83419ad475c7a4e02caaca162",
    "scripts/verify_before_sync.sh": "042ef69a438f1ae12ed87ee1e633a18e96d3fe8f02575bfbc999d9f5edb5b80c",
    "scripts/install_verified_snapshot.py": "978f7a66668f9dde869bd9b0f9803a9e3a54aa4c97d10adf759626f84ca66b3c",
    "scripts/incumbent_copy_guard.sh": "88a6cff25827fbf019c7827822483a7eaa8a092ef7fc03ccc32b96fa9423a0b7",
    "tests/test_closer_native_observer.py": "898ecf104971c4cc357b86015ff748a353166acbfbb0dab6485a28aea4319b64",
    "tests/test_closer_native_collector.py": "a392951df7d9d66aa0b852662b7b8b1573229f8696e5ba4388983855a88815c5",
    "tests/mirror_generation_imports_witness.py": "58aff9902b4ef7ed30b39a893dd59363f6dcd9803fd849bcb2124bcde411e6fc",
    "tests/test_mirror_generation_imports.sh": "3d7ce7d2463d6ccc6d6000618fedd052f13c3fb86dcaf5e99bbf230a72a393b3",
    "tests/test_mirror_product_witnesses.py": "224edde6214d2207f76f0ef996aabb3df1e8e6aaa4f891aee5f0fbc9a77329e6",
    "tests/conftest.py": "8878537123d9ae5336c376ad0dbd83d0582cdbeabc8a3e1130b879ce91365b86",
    "tests/test_closer_runtime_distribution.py": "ac7bc41878542728c0b020641fa38ddf69cdb6089a26644fa4dff6ac8c06a088",
    "tests/test_build_exec_mirror.py": "48a48be1de6bfff9bcd79c49c3f6693781ea686effea290b454546586dc7c00b",
    "tests/test_exec_mirror_shape.py": "d56f78032832b792162099374766243348b40baa01b8150029697db932bb6e95",
    "src/closer_campaign.py": "40a3086b2537507fb7e4c3570c9d4f646ed0e7ace59408e1fb6fb8443db899b8",
    "src/closer_distribution_package.py": "a1118593333d700c3acf1f78319dc1ec684e4bec823e741fce66471257a645ee",
}


def _producer_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise StorageBlocked("unavailable declared cold-plan producer")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _archived_producers_match(source: Path) -> bool:
    """The copier archives tests/scripts from HEAD, not their working-tree bytes."""
    names = [name for name in REVIEWED_PRODUCERS if name.startswith(("tests/", "scripts/"))]
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-tree", "-rz", "HEAD", "--", *names],
            capture_output=True,
            check=True,
            timeout=10,
        )
        found = set()
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            name = raw_name.decode()
            if name not in names or mode not in (b"100644", b"100755") or kind != b"blob":
                return False
            path = source / name
            digest = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
            if digest.hexdigest() != oid.decode():
                return False
            found.add(name)
        return found == set(names)
    except (OSError, ValueError, UnicodeDecodeError, subprocess.SubprocessError):
        return False


def reviewed_cold_plan(source: Path, profile: str, maximum_mb: int) -> JSON | None:
    """Only a matching reviewed producer/mode gets the explicit engineering plan.

    Current host semantics are an explicit qualification assumption; old receipts
    did not independently record OS versions. Source identity remains separately
    bound by normal verification. This is not a claim about all future test code.
    """
    if (
        profile != "EXEC MIRROR"
        or maximum_mb != 50
        or (platform.system(), platform.release(), platform.machine())
        != ("Darwin", "25.6.0", "arm64")
        or sys.version_info[:2] != (3, 12)
    ):
        return None
    try:
        producers = {name: _producer_digest(source / name) for name in REVIEWED_PRODUCERS}
        copier = Path(
            os.environ.get("ORCH_SYNC_SCRIPT", str(Path.home() / ".codex/bin/orch-sync-mirror.sh"))
        ).expanduser()
        if (
            producers != REVIEWED_PRODUCERS
            or _producer_digest(copier) != REVIEWED_EXTERNAL_COPY_SHA256
            or not _archived_producers_match(source)
        ):
            return None
    except OSError:
        return None
    except StorageBlocked:
        return None
    contract = {
        "version": REVIEWED_COLD_VERSION,
        "producer_sha256": producers,
        "external_copy_sha256": REVIEWED_EXTERNAL_COPY_SHA256,
        "mode": profile,
        "max_dir_mb": maximum_mb,
        "os": ["Darwin", "25.6.0", "arm64"],
        "python": [3, 12],
        "shared_window_bytes": REVIEWED_WINDOW_BYTES,
        "baseline_staging_bytes": REVIEWED_STAGING_BYTES,
        "operating_policy_reserve_bytes": OBSERVED_OPERATING_MARGIN,
    }
    return {
        "kind": "reviewed_engineering_estimate",
        "measured_peak": False,
        "contract": contract,
        "profile_key": hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
        "receipt_sha256": list(REVIEWED_RECEIPTS),
        "shared_window_bytes": REVIEWED_WINDOW_BYTES,
        "baseline_staging_bytes": REVIEWED_STAGING_BYTES,
        "runtime_component_bytes": REVIEWED_WINDOW_BYTES - REVIEWED_STAGING_BYTES,
        "operating_policy_reserve_bytes": OBSERVED_OPERATING_MARGIN,
        "baseline_total_bytes": REVIEWED_WINDOW_BYTES + OBSERVED_OPERATING_MARGIN,
        "coverage": "declared producers only; newly introduced amplification requires manifest review",
    }


def _reviewed_command_matches(args: argparse.Namespace) -> bool:
    """Bind the estimated workload, not just a source directory/profile claim."""
    command = args.command
    source = args.source.resolve()
    if (
        len(command) < 3
        or command[0] != "bash"
        or Path(command[1]).resolve() != source / "scripts/verify_before_sync.sh"
        or Path(command[-1]).resolve() != source
    ):
        return False
    options = command[2:-1]
    if options[:1] == ["--snapshot-out"]:
        if len(options) < 2 or not options[1]:
            return False
        options = options[2:]
        if options[:1] == ["--digest-out"]:
            if len(options) < 2 or not options[1]:
                return False
            options = options[2:]
    if options:
        return False
    if (
        (os.environ.get("VERIFY_BEFORE_SYNC_TREE") or "EXEC MIRROR") != args.profile
        or (os.environ.get("VERIFY_BEFORE_SYNC_MAX_DIR_MB") or "50") != str(args.max_dir_mb)
        or os.environ.get("PYTEST_ADDOPTS")
        or os.environ.get("PYTEST_DEBUG_TEMPROOT")
    ):
        return False
    registry = os.environ.get("ORCH_REVIEW_REGISTRY_SNAPSHOT")
    if not registry or Path(registry).is_symlink() or not Path(registry).is_file():
        # A future network registry payload has no local prospective size proof.
        return False
    default = Path.home() / ".codex/orchestrator"
    if (
        Path(os.environ.get("ORCH_LOCAL_RUNTIME") or default).resolve() != args.runtime.resolve()
        or Path(os.environ.get("ORCH_STATE_DIR") or default).resolve()
        != args.state_source.resolve()
    ):
        return False
    # The reviewed shell prepends this directory before selecting its Python.
    local = Path.home() / ".local/bin"
    path = os.environ.get("PATH", "")
    if local.is_dir():
        path = str(local) + os.pathsep + path
    python = shutil.which(os.environ.get("PYTHON") or "python3", path=path)
    return python is not None and Path(python).resolve() == Path(sys.executable).resolve()


def _qualified_cold_plan(args: argparse.Namespace) -> JSON | None:
    if not args.local_strict or not _reviewed_command_matches(args):
        return None
    return reviewed_cold_plan(args.source, args.profile, args.max_dir_mb)


def measurement_profile(source: Path, profile: str, source_identity: str, volume: Path) -> JSON:
    """Bind fixture/copy bytes and OS allocation semantics, not a mutable label.

    The verifier's existing source identity covers its copy inputs, including test
    fixtures. Exact source changes deliberately invalidate calibration reuse.
    """
    producers: dict[str, str | None] = {}
    for name in (
        "scripts/verify_before_sync.sh",
        "scripts/build_exec_mirror.sh",
        "src/verify.py",
        "src/storage_lifecycle.py",
        "pyproject.toml",
    ):
        path = source / name
        if not path.is_file() or path.is_symlink():
            producers[name] = None
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
        producers[name] = digest.hexdigest()
    contract = {
        "schema": "storage-fixture-copy-profile/v1",
        "profile": profile,
        "fixture_copy_identity": source_identity,
        "producer_sha256": producers,
        "os": [platform.system(), platform.release(), platform.machine()],
        "python": list(sys.version_info[:2]),
        "allocation_unit_bytes": os.statvfs(volume).f_frsize,
        "measurement_contract": OWNED_SAMPLE_CONTRACT,
    }
    return {
        **contract,
        "sha256": hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
    }


class OwnedHighwater:
    """Bounded, non-following metadata samples; never claim an exhaustive peak.

    A racing/inaccessible entry invalidates scan completeness. Even all complete
    scans remain samples: producer teardown between scans is not observable.
    """

    def __init__(self, root: Path, identity: tuple[int, int], token: str):
        self.root, self.identity, self.token = root, identity, token
        self.next_sample = 0.0
        self.result: JSON = {
            "schema": OWNED_SAMPLE_CONTRACT,
            "complete_samples": 0,
            "failed_samples": 0,
            "allocated_highwater_bytes": 0,
            "logical_highwater_bytes": 0,
            "errors": [],
            "peak_bound_proven": False,
            "scope": "total-owned-including-staging",
            "observation": "sampled owned allocation lower bound; not physical peak or reclaimed bytes",
        }

    def sample(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now < self.next_sample:
            return False
        self.next_sample = now + SAMPLE_INTERVAL_SECONDS
        try:
            initial = self.root.lstat()
            if self.root.is_symlink() or (initial.st_dev, initial.st_ino) != self.identity:
                raise StorageBlocked("owned sample identity changed")
            marker = self.root / "owner.json"
            if marker.is_symlink() or json.loads(marker.read_text()).get("token") != self.token:
                raise StorageBlocked("owned sample token changed")
            seen = {(initial.st_dev, initial.st_ino)}
            allocated, logical, count = initial.st_blocks * 512, 0, 1

            def failed(error):
                raise error

            for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=failed):
                for name in dirs + files:
                    count += 1
                    if count > SAMPLE_MAX_ENTRIES or time.monotonic() - now > SAMPLE_SCAN_SECONDS:
                        raise StorageBlocked("owned sample scan bound reached")
                    info = (Path(directory) / name).lstat()
                    if info.st_dev != initial.st_dev:
                        raise StorageBlocked("owned sample crosses filesystem")
                    key = (info.st_dev, info.st_ino)
                    if key not in seen:
                        seen.add(key)
                        allocated += info.st_blocks * 512
                        if stat.S_ISREG(info.st_mode):
                            logical += info.st_size
            final = self.root.lstat()
            if (final.st_dev, final.st_ino) != self.identity:
                raise StorageBlocked("owned sample identity changed during scan")
            self.result["complete_samples"] += 1
            self.result["allocated_highwater_bytes"] = max(
                self.result["allocated_highwater_bytes"], allocated
            )
            self.result["logical_highwater_bytes"] = max(
                self.result["logical_highwater_bytes"], logical
            )
        except (OSError, ValueError, TypeError, AttributeError, StorageBlocked) as error:
            self.result["failed_samples"] += 1
            if len(self.result["errors"]) < 10:
                self.result["errors"].append(type(error).__name__ + ":" + str(error)[:256])
        return True


def owned_agent_tmpdir(agent: str, env: dict | None = None) -> Path | None:
    """Route only native temporary output into a positively owned active job.

    No context preserves the historical default. A partial/invalid claim refuses
    before provider launch; it never converts shared caches or credentials into
    disposable storage. This validator grants no retirement or action authority.
    """
    supplied = os.environ if env is None else env
    names = ("ORCH_STORAGE_RUN_ROOT", "ORCH_STORAGE_RUN_ID", "ORCH_STORAGE_OWNER_TOKEN")
    if not any(name in supplied for name in names):
        return None
    if (
        not isinstance(agent, str)
        or not agent
        or len(agent) > 32
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in agent)
    ):
        raise StorageBlocked("invalid native temporary agent identity")
    if any(
        not isinstance(supplied.get(name), str) or not supplied[name]
        for name in (*names, "ORCH_STORAGE_STATE_DIR")
    ):
        raise StorageBlocked("incomplete owned storage context")
    root = Path(supplied[names[0]])
    control = Path(supplied["ORCH_STORAGE_STATE_DIR"])
    job, token = supplied[names[1]], supplied[names[2]]
    if not job.isalnum() or len(job) > 64 or len(token) > 256:
        raise StorageBlocked("invalid owned storage context identity")

    def private_directory(path):
        if not path.is_absolute() or path.resolve() != path:
            raise StorageBlocked("aliased owned storage directory")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise StorageBlocked("owned storage directory must remain private")
        return fd, info

    def read_record(fd, name, maximum):
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise StorageBlocked("owned storage marker must remain private")
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise StorageBlocked("owned storage marker exceeds bound")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise StorageBlocked("invalid owned storage marker")
        return result

    try:
        root_fd, root_info = private_directory(root)
        try:
            marker = read_record(root_fd, "owner.json", 4096)
            if (
                marker.get("job") != job
                or not isinstance(marker.get("token"), str)
                or not hmac.compare_digest(marker["token"], token)
            ):
                raise StorageBlocked("owned storage marker identity differs")
            control_fd, _ = private_directory(control)
            try:
                lock_fd = os.open(
                    "lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=control_fd
                )
                try:
                    lock_info = os.fstat(lock_fd)
                    if (
                        not stat.S_ISREG(lock_info.st_mode)
                        or lock_info.st_uid != os.getuid()
                        or stat.S_IMODE(lock_info.st_mode) != 0o600
                    ):
                        raise StorageBlocked("owned storage ledger lock must remain private")
                    fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    record = read_record(control_fd, "reservations.json", 8 * 1024 * 1024)
                finally:
                    os.close(lock_fd)
            finally:
                os.close(control_fd)
            row = record.get("jobs", {}).get(job)
            if (
                not isinstance(row, dict)
                or row.get("status") not in {"admitted", "staged"}
                or row.get("root") != str(root)
                or row.get("root_identity") != [root_info.st_dev, root_info.st_ino]
                or row.get("volume_device") != root_info.st_dev
                or row.get("owner_token_sha256") != hashlib.sha256(token.encode()).hexdigest()
            ):
                raise StorageBlocked("owned storage reservation differs or is uncertain")
            parent_fd = root_fd
            opened = []
            validated = []
            selected_path = root
            try:
                for part in ("tmp", "native-tmp", agent):
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                    opened.append(fd)
                    info = os.fstat(fd)
                    if (
                        info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                        or info.st_dev != root_info.st_dev
                    ):
                        raise StorageBlocked(
                            "native temporary directory ownership or filesystem differs"
                        )
                    selected_path = selected_path / part
                    validated.append((selected_path, (info.st_dev, info.st_ino)))
                    parent_fd = fd
                for path, identity in validated:
                    info = path.lstat()
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or (info.st_dev, info.st_ino) != identity
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                    ):
                        raise StorageBlocked("native temporary directory changed during validation")
                current = root.stat(follow_symlinks=False)
                if root.is_symlink() or (current.st_dev, current.st_ino) != (
                    root_info.st_dev,
                    root_info.st_ino,
                ):
                    raise StorageBlocked("owned storage root changed during validation")
            finally:
                for fd in reversed(opened):
                    os.close(fd)
        finally:
            os.close(root_fd)
        return root / "tmp" / "native-tmp" / agent
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise StorageBlocked(
            "owned native temporary routing refused: " + type(error).__name__
        ) from error


def available(path: Path) -> int:
    value = os.statvfs(path)
    return value.f_bavail * value.f_frsize


def write_json(path: Path, value: JSON) -> None:
    fd, name = tempfile.mkstemp(prefix=".storage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def footprint(
    path: Path,
    *,
    stop_after: int | None = None,
    deduplicate: bool = True,
    allocated_limit: bool = False,
    reject_links: bool = False,
) -> JSON:
    """Metadata only, no symlink traversal; deduplicate hardlinks within this estimate.

    Logical bytes cover a copy that materializes sparse/shared extents. st_blocks alone
    cannot predict either new-copy cost or physical recovery on APFS.
    """
    allocated = logical = 0
    seen: set[tuple[int, int]] = set()
    pending = [path]
    while pending:
        entry = pending.pop()
        value = entry.lstat()
        if reject_links and stat.S_ISLNK(value.st_mode):
            raise StorageBlocked(
                "copied state contains a symlink; isolation needs an explicit copy plan"
            )
        key = (value.st_dev, value.st_ino)
        if deduplicate and key in seen:
            continue
        seen.add(key)
        allocated += value.st_blocks * 512
        if stat.S_ISREG(value.st_mode):
            logical += value.st_size
        elif stat.S_ISDIR(value.st_mode):
            pending.extend(entry.iterdir())
        if (
            stop_after is not None
            and (allocated if allocated_limit else max(allocated, logical)) > stop_after
        ):
            return {"allocated": allocated, "logical": logical, "over_limit": True}
    return {"allocated": allocated, "logical": logical, "over_limit": False}


def copy_bytes(path: Path, *, reject_links: bool = False) -> int:
    # Plain cp may materialize every hardlink and shared/sparse extent. Inventory
    # deduplication must not understate that prospective allocation.
    value = footprint(path, deduplicate=False, reject_links=reject_links)
    return max(value["allocated"], value["logical"])


def staging_bytes(
    source: Path, runtime: Path, state: Path, maximum: int, *, exclude: Path | None = None
) -> int:
    # Two simultaneous source trees (mirror and copy-contract comparison). Publication
    # must be on this filesystem, so moving the verified mirror adds no third full copy.
    modules = source / "src" if (source / "src").is_dir() else source
    selected = [p for p in modules.glob("*.py") if not p.name.startswith(".")]
    selected += [
        source / name
        for name in (
            "orchestrate.sh",
            "pyproject.toml",
            ".coveragerc",
            ".verify-floor.json",
            "CLAUDE.md",
            "AGENTS.md",
            "ORCHESTRATOR.md",
            "IMPROVEMENT_BACKLOG.md",
            "orchestrator-loop.svg",
            "experiments/hypotheses.json",
            "experiments/features.json",
            "experiments/repo_knowledge.json",
            "data/feedback-snapshot.json",
            "config/coverage-baseline.json",
            "config/model_epochs.json",
        )
    ]
    if any(p.is_symlink() for p in selected):
        raise StorageBlocked("copy-contract source symlink needs an explicit footprint")
    total = 2 * sum(copy_bytes(p) for p in selected if p.exists())
    archived = ("tests", "scripts", "docs", "campaigns", ".github", ".gitignore", "ruff.toml")
    if (source / ".git").exists():
        # These inputs are read from Git objects by build_exec_mirror, including
        # files absent/online-only in the working tree. No content hydration.
        result = subprocess.run(
            ["git", "-C", str(source), "ls-tree", "-rlz", "HEAD", "--", *archived],
            capture_output=True,
            check=True,
            timeout=30,
        )
        block = os.statvfs(source).f_frsize
        for raw_entry in result.stdout.split(b"\0"):
            if raw_entry:
                size = int(raw_entry.split(b"\t", 1)[0].split()[3])
                total += 2 * ((size + block - 1) // block + 1) * block
    else:
        # Also usable with tiny standalone fixtures/custom copy producers.
        total += 2 * sum(copy_bytes(source / name) for name in archived if (source / name).exists())

    # The state copier recurses through feedback irrespective of the directory
    # cap, and always brings the model catalog even when agent-runtime is large.
    def state_bytes(root: Path) -> int:
        subtotal = 0
        for entry in root.iterdir():
            if entry.name == ".storage-lifecycle" or entry == exclude:
                continue
            if (
                exclude is not None
                and exclude.resolve().is_relative_to(entry.resolve())
                and entry.name != "feedback"
            ):
                raise StorageBlocked(
                    "storage control nested in copied state; choose a separate control directory"
                )
            if entry.is_symlink():
                raise StorageBlocked("state-copy symlink needs an explicit copy footprint")
            if entry.is_dir() and entry.name == "feedback":
                subtotal += state_bytes(entry)
                continue
            value = footprint(
                entry, stop_after=maximum if entry.is_dir() else None, allocated_limit=True
            )
            if not value["over_limit"]:
                subtotal += copy_bytes(entry, reject_links=True)
        return subtotal

    for root in dict.fromkeys((runtime, state)):
        if root.is_dir():
            total += state_bytes(root)
    extra = [runtime / "agent-runtime/gemini/advertised-models.json"]
    extra += [
        Path(os.environ[name])
        for name in ("ORCH_CAPABILITIES_PATH", "ORCH_FEEDBACK_DB")
        if name in os.environ
    ]
    extra += [
        source / "experiments" / name
        for name in ("features.json", "repo_knowledge.json", "hypotheses.json")
    ]
    registry = os.environ.get("ORCH_REVIEW_REGISTRY_SNAPSHOT")
    if registry:
        entry = Path(registry)
        if entry.is_symlink() or not entry.is_file():
            raise StorageBlocked("captured copy registry unavailable or symlinked")
        # The reviewed copier writes mirror and runtime registry destinations.
        total += 2 * copy_bytes(entry)
    for entry in extra:
        if entry.is_symlink():
            raise StorageBlocked("external copied input is a symlink")
        if entry.is_file():
            total += copy_bytes(entry)
    return total


class Reservations:
    def __init__(
        self,
        directory: Path,
        volume: Path,
        *,
        floor: int,
        margin: int = 0,
        free: Callable[[Path], int] = available,
    ):
        self.directory = directory
        self.volume = volume
        self.floor = floor
        self.margin = margin
        self.free = free
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise StorageBlocked("storage state directory is a symlink")
        attributes = directory.stat()
        if attributes.st_uid != os.getuid() or stat.S_IMODE(attributes.st_mode) & 0o077:
            raise StorageBlocked("storage state must be owner-private; permissions unchanged")

    @contextlib.contextmanager
    def locked(self) -> Iterator[JSON]:
        descriptor = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            path = self.directory / "reservations.json"
            if path.is_symlink():
                raise StorageBlocked("reservation state is a symlink")
            try:
                data = json.loads(path.read_text()) if path.exists() else {"jobs": {}, "peaks": {}}
            except json.JSONDecodeError as error:
                raise StorageBlocked("unreadable reservation state; preserved") from error
            if not isinstance(data.get("jobs"), dict) or not isinstance(data.get("peaks"), dict):
                raise StorageBlocked("invalid storage reservation state; preserved")
            if not isinstance(data.get("owned_measurements", {}), dict):
                raise StorageBlocked("invalid owned measurement state; preserved")
            for key, record in data.get("owned_measurements", {}).items():
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("contract"), dict)
                    or record["contract"].get("sha256") != key
                    or type(record.get("allocated_highwater_bytes")) is not int
                    or record["allocated_highwater_bytes"] < 0
                ):
                    raise StorageBlocked("invalid owned measurement record; preserved")
            if any(not isinstance(row, dict) for row in data["jobs"].values()) or any(
                type(value) is not int or value < 0 for value in data["peaks"].values()
            ):
                raise StorageBlocked("invalid reservation/profile evidence; preserved")
            yield data
            write_json(path, data)
        finally:
            os.close(descriptor)

    def admit(
        self,
        job: str,
        profile: str,
        staging: int,
        planned_peak: int,
        *,
        owner_token: str,
        scope: str | None = None,
        measurement_contract: JSON | None = None,
    ) -> JSON:
        if not job.isalnum() or len(job) > 64:
            raise ValueError("invalid storage job identity")
        if min(staging, planned_peak) < 0:
            raise ValueError("negative storage forecast")
        if measurement_contract is not None and (
            not isinstance(measurement_contract, dict)
            or not isinstance(measurement_contract.get("sha256"), str)
            or len(measurement_contract["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in measurement_contract["sha256"])
        ):
            raise StorageBlocked("invalid measurement contract")
        with self.locked() as data:
            if job in data["jobs"] or (self.directory / "receipts" / f"{job}.json").exists():
                raise StorageBlocked("existing job reservation; reconcile without replay")
            if scope and any(row.get("scope") == scope for row in data["jobs"].values()):
                raise StorageBlocked(
                    "verification owner already claimed this source/release scope; reconcile without duplicate launch"
                )
            rows = list(data["jobs"].values())
            if any(
                row.get("volume_device") is not None and type(row["volume_device"]) is not int
                for row in rows
            ):
                raise StorageBlocked("invalid reservation filesystem identity; preserved")
            if any(type(row.get("remaining")) is not int or row["remaining"] < 0 for row in rows):
                raise StorageBlocked("invalid reservation bytes; no admission")
            device = self.volume.stat().st_dev
            # Unknown legacy ownership remains conservatively charged; a known
            # different device does not spend this filesystem's available bytes.
            remaining = [
                row["remaining"] for row in rows if row.get("volume_device") in (None, device)
            ]
            # Legacy peaks are retained conservative estimates, not measurements.
            calibration = data.get("owned_measurements", {}).get(
                measurement_contract["sha256"] if measurement_contract else "", {}
            )
            if calibration and calibration.get("contract") != measurement_contract:
                raise StorageBlocked("owned calibration contract mismatch; preserved")
            sampled = calibration.get("allocated_highwater_bytes", 0)
            if type(sampled) is not int or sampled < 0:
                raise StorageBlocked("invalid owned calibration; preserved")
            # A total-owned sample can only conservatively RAISE the runtime
            # reserve; it includes staging and is never labelled runtime growth.
            peak = max(planned_peak, data["peaks"].get(profile, 0), sampled)
            required = max(self.floor, peak + self.margin) + sum(remaining) + staging
            free = self.free(self.volume)
            if free < required:
                raise StorageBlocked(
                    json.dumps(
                        {
                            "status": "prepare_storage",
                            "available_bytes": free,
                            "required_bytes": required,
                            "deficit_bytes": required - free,
                            "floor_bytes": self.floor,
                            "operating_margin_bytes": self.margin,
                            "other_reserved_bytes": sum(remaining),
                            "staging_bytes": staging,
                            "peak_bytes": peak,
                        }
                    )
                )
            row = {
                "profile": profile,
                "measurement_contract": measurement_contract,
                "calibration_status": "insufficient_calibration",
                "calibration_requirement": "reviewed transient fixture/copy peak bound; samples can only raise reserve",
                "planned_peak_bytes": planned_peak,
                "sampled_owned_highwater_bytes": sampled,
                "sampled_scope": "total-owned-including-staging; conservative extra charge if larger than planned runtime reserve",
                "staging": staging,
                "peak": peak,
                "remaining": staging + peak,
                "status": "admitted",
                "available_before": free,
                "owner_pid": os.getpid(),
                "owner_token_sha256": hashlib.sha256(owner_token.encode()).hexdigest(),
                "scope": scope,
                "volume_device": device,
            }
            data["jobs"][job] = row
            return dict(row)

    def bind(self, job: str, **metadata: Any) -> None:
        with self.locked() as data:
            data["jobs"][job].update(metadata)

    def staged(self, job: str, owner_token: str) -> None:
        with self.locked() as data:
            row = data["jobs"][job]
            if row["owner_token_sha256"] != hashlib.sha256(owner_token.encode()).hexdigest():
                raise StorageBlocked("staging owner token does not match")
            if row["status"] not in ("admitted", "staged"):
                raise StorageBlocked("cannot stage an uncertain or terminal reservation")
            if row.get("measurement_contract") is not None and row.get("root"):
                checkpoint = OwnedHighwater(
                    Path(row["root"]), tuple(row["root_identity"]), owner_token
                )
                checkpoint.sample(force=True)
                row["staged_owned_checkpoint"] = checkpoint.result
            row.update(status="staged", remaining=row["peak"])

    def finish(self, job: str, receipt: JSON, *, terminal: bool) -> None:
        with self.locked() as data:
            row = data["jobs"][job]
            receipt = {**row, **receipt, "available_after": self.free(self.volume)}
            receipts = self.directory / "receipts"
            receipts.mkdir(exist_ok=True, mode=0o700)
            write_json(receipts / f"{job}.json", receipt)
            if terminal:
                # Never relabel planned reservations or shared-volume decline as
                # measured owned growth. Preserve all pre-existing legacy estimates.
                contract = row.get("measurement_contract")
                sample = receipt.get("owned_highwater")
                checkpoint = row.get("staged_owned_checkpoint")
                if isinstance(sample, dict) and isinstance(checkpoint, dict):
                    sample = {
                        **sample,
                        "complete_samples": sample.get("complete_samples", 0)
                        + checkpoint.get("complete_samples", 0),
                        "failed_samples": sample.get("failed_samples", 0)
                        + checkpoint.get("failed_samples", 0),
                        "allocated_highwater_bytes": max(
                            sample.get("allocated_highwater_bytes", 0),
                            checkpoint.get("allocated_highwater_bytes", 0),
                        ),
                    }
                if (
                    isinstance(contract, dict)
                    and isinstance(sample, dict)
                    and sample.get("schema") == OWNED_SAMPLE_CONTRACT
                    and type(sample.get("complete_samples")) is int
                    and sample["complete_samples"] > 0
                    and type(sample.get("failed_samples")) is int
                    and sample["failed_samples"] == 0
                    and receipt.get("source_unchanged") is True
                    and receipt.get("exit_code") == 0
                    and type(sample.get("allocated_highwater_bytes")) is int
                    and sample["allocated_highwater_bytes"] >= 0
                ):
                    measurements = data.setdefault("owned_measurements", {})
                    old = measurements.get(contract["sha256"], {})
                    measurements[contract["sha256"]] = {
                        "contract": contract,
                        "allocated_highwater_bytes": max(
                            old.get("allocated_highwater_bytes", 0),
                            sample["allocated_highwater_bytes"],
                        ),
                        "receipt": str(receipts / f"{job}.json"),
                        "peak_bound_proven": False,
                        "scope": "total-owned-including-staging",
                        "eligible_to_lower_reserve": False,
                    }
                del data["jobs"][job]
            else:
                row.update(status="uncertain", receipt=str(receipts / f"{job}.json"))


def idle(root: Path) -> None:
    """Independent process/open-file self-visibility, not just absence of matches."""
    mine = str(os.getpid())
    ps = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rows = [line.strip().split(None, 1) for line in ps.stdout.splitlines() if line.strip()]
    if not any(row[0] == mine for row in rows):
        raise StorageBlocked("process inspection is blind to supervisor")
    if any(len(row) == 2 and row[0] != mine and str(root) in row[1] for row in rows):
        raise StorageBlocked("process references owned storage")
    with (root / "owner.json").open("rb"):
        result = subprocess.run(
            ["lsof", "-nP", "-F", "pfn"], capture_output=True, text=True, timeout=45
        )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise StorageBlocked("open-file inspection failed")
    pid = ""
    visible = False
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n"):
            path = line[1:]
            if pid == mine and path == str(root / "owner.json"):
                visible = True
            if pid != mine and (path == str(root) or path.startswith(str(root) + "/")):
                raise StorageBlocked("open file in owned storage")
    if not visible:
        raise StorageBlocked("open-file inspection is blind to supervisor")


def retire(
    root: Path,
    identity: tuple[int, int],
    token: str,
    probe: Callable[[Path], None] = idle,
    *,
    measurement: JSON | None = None,
) -> list[str]:
    current = root.lstat()
    if root.is_symlink() or (current.st_dev, current.st_ino) != identity:
        raise StorageBlocked("owned directory identity changed")
    marker = root / "owner.json"
    if marker.is_symlink() or json.loads(marker.read_text()).get("token") != token:
        raise StorageBlocked("owned directory token changed")
    probe(root)
    # The root is private and exclusively owned. rmtree does not follow file/directory
    # symlinks; use Python's descriptor-relative implementation rather than shell rm.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise StorageBlocked("descriptor-relative retirement unavailable")
    # Measure during the mandatory ownership/filesystem scan, never another walk.
    entries, allocated, logical = 1, current.st_blocks * 512, 0
    seen = {(current.st_dev, current.st_ino)}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            if info.st_dev != current.st_dev:
                raise StorageBlocked("nested filesystem is not retirement authority")
            entries += 1
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                allocated += info.st_blocks * 512
                if stat.S_ISREG(info.st_mode):
                    logical += info.st_size
    if measurement is not None:
        measurement.update(
            {
                "entries": entries,
                "unique_inodes": len(seen),
                "allocated_bytes": allocated,
                "logical_bytes": logical,
                "observation": "terminal owned-tree metadata; hardlinks deduplicated; not peak growth or physical reclaimed bytes",
            }
        )
    final = root.lstat()
    if (
        root.is_symlink()
        or (final.st_dev, final.st_ino) != identity
        or marker.is_symlink()
        or json.loads(marker.read_text()).get("token") != token
    ):
        raise StorageBlocked("ownership changed during retirement inspection")
    quarantine = root.with_name(root.name + ".retiring")
    if quarantine.exists() or quarantine.is_symlink():
        raise StorageBlocked("existing retirement attempt; reconcile without replay")
    root.rename(quarantine)
    moved = quarantine.lstat()
    if (moved.st_dev, moved.st_ino) != identity or quarantine.is_symlink():
        raise StorageBlocked("retirement identity changed; retained without deletion")
    preserved: list[str] = []

    def preserve_denied(_operation: Any, path: str, error: BaseException) -> None:
        if isinstance(error, PermissionError):
            preserved.append(path)
            return  # Never retry, chmod, or force a permission-denied entry.
        if isinstance(error, OSError) and error.errno in (errno.ENOTEMPTY, errno.EEXIST):
            if any(Path(item).is_relative_to(path) for item in preserved):
                return  # Its recorded preserved child makes this ancestor nonempty.
        raise error

    shutil.rmtree(quarantine, onexc=preserve_denied)
    if preserved:
        # Keep a recovery identity on the residual container after writable
        # siblings were retired. All denied entries remain exactly where reported.
        write_json(quarantine / "owner.json", {"token": token, "preserved_paths": preserved})
    return preserved


def identity(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    return result.stdout.strip()


def run_job(
    args: argparse.Namespace,
    *,
    free: Callable[[Path], int] = available,
    probe: Callable[[Path], None] = idle,
) -> int:
    parent = args.temp_parent.resolve()
    cold_plan = _qualified_cold_plan(args)
    # The old 27 GiB floor already included the pre-861 fixture working demand.
    # Replace it ONLY for the exact reviewed contract, never by current free space.
    floor = STRICT_RELEASE_FLOOR if args.local_strict and cold_plan is None else 0
    margin = OBSERVED_OPERATING_MARGIN if args.local_strict else 0
    manager = Reservations(args.state_dir, parent, floor=floor, margin=margin, free=free)
    job = uuid.uuid4().hex
    token = uuid.uuid4().hex
    for flag in ("--snapshot-out", "--digest-out"):
        if flag in args.command:
            destination = Path(args.command[args.command.index(flag) + 1]).resolve()
            while not destination.exists():
                destination = destination.parent
            if destination.stat().st_dev != parent.stat().st_dev:
                raise StorageBlocked(
                    "cross-filesystem publication needs a separate destination reserve"
                )
    forecast = staging_bytes(
        args.source,
        args.runtime,
        args.state_source,
        args.max_dir_mb * 1024**2,
        exclude=manager.directory,
    )
    actual_staging_forecast = forecast
    if cold_plan is not None:
        # The observed full window already contains baseline staging. Charge it
        # once, then charge all additional prospective copied bytes in full.
        forecast = max(forecast, cold_plan["baseline_staging_bytes"])
        peak = max(args.peak_bytes, cold_plan["runtime_component_bytes"])
        reservation_profile = args.profile + ":" + cold_plan["profile_key"]
    else:
        peak = max(args.peak_bytes, INITIAL_VERIFY_PEAK if args.local_strict else 0)
        reservation_profile = args.profile
    source_command = ["bash", args.command[1], "--identity", str(args.source)]
    before = identity(source_command)
    owner_key = os.environ.get("VERIFY_BEFORE_SYNC_OWNER_KEY")
    claim = (
        ["publication", owner_key]
        if owner_key
        else ["source", str(args.runtime.resolve()), args.profile, before]
    )
    scope = hashlib.sha256(json.dumps(claim).encode()).hexdigest()
    contract = (
        measurement_profile(args.source, args.profile, before, parent)
        if args.local_strict
        else None
    )
    if cold_plan is not None and _qualified_cold_plan(args) != cold_plan:
        raise StorageBlocked("reviewed storage producer contract changed before admission")
    admitted = manager.admit(
        job,
        reservation_profile,
        forecast,
        peak,
        owner_token=token,
        scope=scope,
        measurement_contract=contract,
    )
    # A shared byte ledger admits one aggregate demand under one lock. This separate
    # per-job lock remains held through the child, evidence retention and cleanup.
    try:
        lock = os.open(
            manager.directory / f"{job}.owner", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
        )
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError:
        manager.finish(job, {"status": "not_started_owner_lock_failed"}, terminal=True)
        raise
    root: Path | None = None
    root_id: tuple[int, int] | None = None
    child: subprocess.Popen[Any] | None = None
    spawn_attempted = False
    terminal = False
    no_writers = False
    sampler: OwnedHighwater | None = None
    receipt: JSON = {
        "job": job,
        "source": str(args.source),
        "available_before": admitted["available_before"],
        "resource_plan": cold_plan
        or {
            "kind": "legacy_conservative_forecast",
            "floor_bytes": floor,
            "reason": "no matching reviewed producer/mode/platform contract",
        },
        "actual_staging_forecast_bytes": actual_staging_forecast,
    }
    try:
        root = Path(tempfile.mkdtemp(prefix="storage-verify-", dir=parent)).resolve()
        root_id = (root.stat().st_dev, root.stat().st_ino)
        write_json(root / "owner.json", {"job": job, "token": token})
        manager.bind(
            job,
            root=str(root),
            root_identity=list(root_id),
            resource_plan=receipt["resource_plan"],
            actual_staging_forecast_bytes=actual_staging_forecast,
        )
        (root / "tmp").mkdir(mode=0o700)
        tmp_attributes = (root / "tmp").stat()
        tmp_id = (tmp_attributes.st_dev, tmp_attributes.st_ino)
        write_json(root / "tmp" / "owner.json", {"job": job, "token": token})
        manager.bind(job, tmp_identity=list(tmp_id))
        (root / "staging").mkdir()
        manager.bind(job, source_command=source_command, source_identity=before)
        receipt.update(root=str(root), source_identity=before)
        if args.local_strict:
            sampler = OwnedHighwater(root, root_id, token)
            sampler.sample(force=True)
            manager.bind(job, owned_highwater=sampler.result)
        env = {
            **os.environ,
            "ORCH_STORAGE_RUN_ID": job,
            "ORCH_STORAGE_OWNER_TOKEN": token,
            "ORCH_STORAGE_RUN_ROOT": str(root),
            "ORCH_STORAGE_STATE_DIR": str(manager.directory),
            "TMPDIR": str(root / "tmp"),
            "VERIFY_BEFORE_SYNC_TMP_PARENT": str(root / "staging"),
        }
        if cold_plan is not None and _qualified_cold_plan(args) != cold_plan:
            raise StorageBlocked("reviewed storage producer contract changed before child launch")
        # Constructor failure can follow a fork. From this point, child=None
        # never proves no spawn; uncertainty must preserve storage and charge.
        spawn_attempted = True
        child = subprocess.Popen(
            [
                "bash",
                "-c",
                'export ORCH_STORAGE_ENTRY_PID=$$; exec "$@"',
                "storage-entry",
                *args.command,
            ],
            env=env,
            start_new_session=True,
        )
        manager.bind(job, child_pgid=child.pid)
        lowest = admitted["available_before"]
        warned = False
        while child.poll() is None:
            if sampler is not None and sampler.sample():
                # Preserve pre-teardown samples even if the supervisor later dies.
                # Interrupted ownership stays charged; this is not a terminal proof.
                manager.bind(job, owned_highwater=sampler.result)
            lowest = min(lowest, free(parent))
            if not warned and free(parent) < margin + forecast:
                print(
                    json.dumps(
                        {
                            "storage_pressure": True,
                            "job": job,
                            "available_bytes": free(parent),
                            "launch_floor_bytes": floor,
                            "operating_margin_bytes": margin,
                        }
                    ),
                    flush=True,
                )
                warned = True
            time.sleep(0.25)  # in-run observation, not a scheduler or a run deadline
        rc = child.returncode
        assert rc is not None
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            terminal = True
        if not terminal:
            raise StorageBlocked("descendant group still exists; retain storage and reservation")
        probe(root)
        no_writers = True
        if sampler is not None:
            sampler.sample(force=True)
        stable = identity(source_command) == before
        receipt.update(
            exit_code=rc,
            source_unchanged=stable,
            shared_volume_decline_bytes=max(
                0, admitted["available_before"] - min(lowest, free(parent))
            ),
            owned_highwater=sampler.result if sampler is not None else None,
            observation="shared-volume decline is diagnostic only; concurrent activity is not owned growth",
        )
        logs = sorted((root / "staging").glob("verify-before-sync.*/verify.log"))
        if logs:
            with logs[-1].open("rb") as stream:
                stream.seek(max(0, logs[-1].stat().st_size - 65536))
                receipt["diagnostic_tail"] = stream.read().decode(errors="replace")
        # Persist provenance/failure evidence BEFORE retiring any disposable bulk.
        target = root if rc == 0 else root / "tmp"
        target_id = root_id if rc == 0 else tmp_id
        receipt["retirement_path"] = str(target.with_name(target.name + ".retiring"))
        manager.finish(job, {**receipt, "status": "terminal_evidence_saved"}, terminal=False)
        if stable and os.environ.get("VERIFY_BEFORE_SYNC_KEEP") != "1":

            def checked_idle(path: Path) -> None:
                nonlocal no_writers
                no_writers = False
                probe(path)
                no_writers = True

            receipt["terminal_owned_footprint"] = {}
            preserved = retire(
                target,
                target_id,
                token,
                checked_idle,
                measurement=receipt["terminal_owned_footprint"],
            )
            receipt["preserved_permission_paths"] = preserved
            receipt["status"] = (
                ("retired_with_permission_remnants" if preserved else "retired")
                if rc == 0
                else "fixtures_retired_failure_retained"
            )
        else:
            receipt["status"] = "retained_requested" if stable else "retained_source_changed"
        manager.finish(job, receipt, terminal=True)
        print(
            json.dumps(
                {
                    "storage_job": job,
                    "status": receipt["status"],
                    "available_bytes": free(parent),
                    "receipt": str(manager.directory / "receipts" / f"{job}.json"),
                }
            ),
            flush=True,
        )
        return rc
    except BaseException as exc:
        # No signal escalation, alternate deletion, expiry or blind replay. A dead
        # supervisor does not prove that descendants stopped using its files.
        if sampler is not None:
            receipt["owned_highwater"] = sampler.result
        receipt["spawn_attempted"] = spawn_attempted
        if not spawn_attempted:
            # No constructor was entered. Retire only our authenticated root
            # through the ordinary independent probe/ownership checks.
            receipt.update(
                status="not_started", error=type(exc).__name__, root=str(root) if root else None
            )
            if root is None:
                manager.finish(job, receipt, terminal=True)
                raise
            try:
                if root_id is None:
                    raise StorageBlocked("pre-spawn root identity unavailable")

                def pre_spawn_idle(path: Path) -> None:
                    nonlocal no_writers
                    no_writers = False
                    probe(path)
                    no_writers = True

                receipt["terminal_owned_footprint"] = {}
                preserved = retire(
                    root,
                    root_id,
                    token,
                    pre_spawn_idle,
                    measurement=receipt["terminal_owned_footprint"],
                )
                receipt.update(
                    preserved_permission_paths=preserved,
                    status=(
                        "not_started_retired_with_permission_remnants"
                        if preserved
                        else "not_started_retired"
                    ),
                )
            except BaseException as cleanup_error:
                receipt.update(
                    status="not_started_cleanup_pending", cleanup_error=type(cleanup_error).__name__
                )
            manager.finish(job, receipt, terminal=no_writers)
            raise
        receipt.update(
            status="cleanup_pending" if no_writers else "retained_uncertain",
            error=type(exc).__name__,
            root=str(root) if root else None,
        )
        # Failed deletion does not reserve future growth after positive terminality:
        # existing remnants are already reflected in actual free bytes. Uncertain
        # children keep their reservation; never infer death from elapsed time.
        manager.finish(job, receipt, terminal=terminal and no_writers)
        raise
    finally:
        os.close(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--runtime", type=Path, required=True)
    run.add_argument("--state-source", type=Path, required=True)
    run.add_argument("--temp-parent", type=Path, required=True)
    run.add_argument("--max-dir-mb", type=int, default=50)
    run.add_argument("--peak-bytes", type=int, default=0)
    run.add_argument("--profile", required=True)
    run.add_argument("--local-strict", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    staged = sub.add_parser("staged")
    staged.add_argument("--state-dir", type=Path, required=True)
    staged.add_argument("--job", required=True)
    args = parser.parse_args()
    if args.action == "staged":
        Reservations(args.state_dir, Path.cwd(), floor=STRICT_RELEASE_FLOOR).staged(
            args.job, os.environ["ORCH_STORAGE_OWNER_TOKEN"]
        )
        return 0
    args.command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if len(args.command) < 2 or args.command[0] != "bash":
        parser.error("run requires -- bash verify_before_sync.sh [arguments]")
    return run_job(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (StorageBlocked, OSError, subprocess.SubprocessError) as error:
        print(f"storage admission/retirement refused: {error}", file=sys.stderr)
        raise SystemExit(78) from error
