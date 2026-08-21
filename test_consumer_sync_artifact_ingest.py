from __future__ import annotations

import hashlib
import json
import zipfile
import io
from pathlib import Path

import pytest

import capabilities
import capability_outcome_bridge
import consumer_sync_artifact_ingest
import consumer_sync_shadow
from test_consumer_sync_shadow import stable_hash, valid_handoff, valid_plan


def make_zip(files_dict: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files_dict.items():
            if isinstance(content, str):
                zf.writestr(name, content.encode("utf-8"))
            else:
                zf.writestr(name, content)
    return buf.getvalue()


def test_safe_zip_extraction(tmp_path: Path) -> None:
    # Test valid zip
    valid_data = make_zip({"file.txt": "hello"})
    dest = tmp_path / "valid"
    dest.mkdir()
    consumer_sync_artifact_ingest.safe_extract_zip(valid_data, dest)
    assert (dest / "file.txt").read_text() == "hello"

    # Test Zip Slip attack zip (unsafe path)
    unsafe_data = make_zip({"../escaped.txt": "evil"})
    dest_unsafe = tmp_path / "unsafe"
    dest_unsafe.mkdir()
    with pytest.raises(ValueError, match="Unsafe zip entry"):
        consumer_sync_artifact_ingest.safe_extract_zip(unsafe_data, dest_unsafe)


def test_exact_hashing(tmp_path: Path) -> None:
    # Test file hash
    f = tmp_path / "test.txt"
    f.write_text("hello world\n", encoding="utf-8")
    expected_hash = "sha256:" + hashlib.sha256(b"hello world\n").hexdigest()
    assert consumer_sync_artifact_ingest._content_hash(f) == expected_hash

    # Test directory hash
    d = tmp_path / "dir"
    d.mkdir()
    (d / "b.txt").write_text("content b", encoding="utf-8")
    (d / "a.txt").write_text("content a", encoding="utf-8")

    rows = [
        {"path": "a.txt", "sha256": hashlib.sha256(b"content a").hexdigest()},
        {"path": "b.txt", "sha256": hashlib.sha256(b"content b").hexdigest()},
    ]
    expected_dir_hash = stable_hash("consumer-sync-directory", rows)
    assert consumer_sync_artifact_ingest._content_hash(d) == expected_dir_hash


def test_artifact_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup mock actions runs and artifacts
    run_list = {
        "workflow_runs": [
            {"id": 1001, "run_attempt": 2, "conclusion": "success"},
            {"id": 1002, "run_attempt": 1, "conclusion": "failure"},
            {"id": 1003, "run_attempt": 1, "conclusion": "success"},
        ]
    }

    def mock_gh(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "health-69-consumer-sync-shadow-evidence.yml/runs?status=success" in cmd:
            return json.dumps(run_list)
        if "runs/1001/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 2001,
                            "name": "consumer-sync-shadow-evidence-1001-2",
                            "expired": False,
                        }
                    ]
                }
            )
        if "runs/1003/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 2003,
                            "name": "consumer-sync-shadow-evidence-1003-1",
                            "expired": True,
                        }
                    ]
                }
            )
        raise ValueError(f"Unexpected mock gh command: {cmd}")

    monkeypatch.setattr(consumer_sync_artifact_ingest, "GH_COMMAND_MOCK", mock_gh)

    meta = consumer_sync_artifact_ingest.discover_artifact()
    assert meta is not None
    assert meta["run_id"] == 1001
    assert meta["run_attempt"] == 2
    assert meta["artifact_id"] == 2001
    assert meta["artifact_name"] == "consumer-sync-shadow-evidence-1001-2"


def test_run_attempt_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    ledger_path = tmp_path / "capabilities.json"
    capabilities.save({}, ledger_path)

    plan = valid_plan()
    handoff = valid_handoff(plan)
    # Correct run_ref format: github-actions:stranske/Workflows:12345:1
    handoff["run_ref"] = (
        "github-actions:stranske/Workflows:12345:2"  # Mismatch run attempt (expected 1)
    )
    handoff["handoff_id"] = stable_hash(
        "consumer-sync-shadow-handoff",
        {k: handoff[k] for k in handoff if k != "handoff_id"},
    )

    artifact_zip_bytes = make_zip(
        {
            "consumer-sync-plan.json": json.dumps(plan),
            "handoff.json": json.dumps(handoff),
        }
    )

    run_list = {
        "workflow_runs": [{"id": 12345, "run_attempt": 1, "conclusion": "success"}]
    }
    artifact_list = {
        "artifacts": [
            {
                "id": 9999,
                "name": "consumer-sync-shadow-evidence-12345-1",
                "expired": False,
            }
        ]
    }

    def mock_gh(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "health-69-consumer-sync-shadow-evidence.yml/runs?status=success" in cmd:
            return json.dumps(run_list)
        if "runs/12345/artifacts" in cmd:
            return json.dumps(artifact_list)
        if "artifacts/9999/zip" in cmd:
            return artifact_zip_bytes
        raise ValueError(f"Unexpected mock gh command: {cmd}")

    monkeypatch.setattr(consumer_sync_artifact_ingest, "GH_COMMAND_MOCK", mock_gh)
    monkeypatch.setattr(
        consumer_sync_artifact_ingest, "TEST_REGISTRY", ["stranske/template"]
    )

    # Ingestion must reject mismatched attempt and return exit code 1
    rc = consumer_sync_artifact_ingest.main(
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
            "100",
        ]
    )
    assert rc == 1


def test_unsafe_zip_variants(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()

    # Symlinks should be rejected
    symlink_buf = io.BytesIO()
    with zipfile.ZipFile(symlink_buf, "w") as zf:
        zi = zipfile.ZipInfo("symlink.txt")
        zi.external_attr = 0o120000 << 16  # S_IFLNK
        zf.writestr(zi, "target")
    with pytest.raises(ValueError, match="symlink"):
        consumer_sync_artifact_ingest.safe_extract_zip(symlink_buf.getvalue(), dest)

    # Duplicate normalized paths should be rejected
    dup_buf = io.BytesIO()
    with zipfile.ZipFile(dup_buf, "w") as zf:
        zf.writestr("dup.txt", "content1")
        zf.writestr("dup.txt", "content2")
    with pytest.raises(ValueError, match="Duplicate"):
        consumer_sync_artifact_ingest.safe_extract_zip(dup_buf.getvalue(), dest)


def test_size_bounds(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()

    valid_buf = io.BytesIO()
    with zipfile.ZipFile(valid_buf, "w") as zf:
        zf.writestr("file.txt", "content")

    with pytest.raises(ValueError, match="exceeds max_total_bytes"):
        consumer_sync_artifact_ingest.safe_extract_zip(
            valid_buf.getvalue(), dest, max_total_bytes=2
        )


def test_caps_validation() -> None:
    with pytest.raises(ValueError, match="max-artifacts"):
        consumer_sync_artifact_ingest.main(["ingest", "--max-artifacts", "2"])

    with pytest.raises(ValueError, match="max-repositories"):
        consumer_sync_artifact_ingest.main(["ingest", "--max-repositories", "6"])


def test_expired_phase_no_write(tmp_path: Path) -> None:
    # human-on-exception mode expired fails closed
    state_dir = tmp_path / "state"
    ledger_path = tmp_path / "capabilities.json"

    with pytest.raises(ValueError, match="Expiry date"):
        consumer_sync_artifact_ingest.main(
            [
                "ingest",
                "--state-dir",
                str(state_dir),
                "--ledger",
                str(ledger_path),
                "--mode",
                "human-on-exception",
                "--expiry",
                "2026-07-20",
                "--now",
                "1784894400",  # 2026-07-24 (expired!)
            ]
        )


PRODUCER_MEMBERS = (
    # What health-69-consumer-sync-shadow-evidence.yml actually uploads: it publishes a whole
    # directory, so this set GROWS whenever a reporting step is added to that job.
    "consumer-sync-plan.json",
    "handoff.json",
    "evidence-ledger.json",
    "completion-evidence.json",
    "capabilities-state.json",
    "runtime-report.json",
)


def _infolist(*names: str) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(make_zip({n: "{}" for n in names}))) as zf:
        return zf.infolist()


def test_artifact_contract_accepts_producer_superset() -> None:
    # The pinned two-file set refused this artifact 29 times from 2026-08-12 on, which is why
    # subjects_seen for capability:reference-sync-hygiene-test-gate could not leave 0.
    assert consumer_sync_artifact_ingest.validate_artifact_members(
        _infolist(*PRODUCER_MEMBERS)
    ) == set(PRODUCER_MEMBERS)

    # The bare required pair, and a sidecar the producer has not invented yet, both ingest.
    assert consumer_sync_artifact_ingest.validate_artifact_members(
        _infolist("consumer-sync-plan.json", "handoff.json")
    ) == set(consumer_sync_artifact_ingest.ARTIFACT_REQUIRED_MEMBERS)
    assert "future-sidecar.json" in consumer_sync_artifact_ingest.validate_artifact_members(
        _infolist(*PRODUCER_MEMBERS, "future-sidecar.json")
    )


@pytest.mark.parametrize(
    "dropped", sorted(consumer_sync_artifact_ingest.ARTIFACT_REQUIRED_MEMBERS)
)
def test_artifact_contract_requires_both_read_members(dropped: str) -> None:
    with pytest.raises(ValueError, match="missing required member"):
        consumer_sync_artifact_ingest.validate_artifact_members(
            _infolist(*[n for n in PRODUCER_MEMBERS if n != dropped])
        )


@pytest.mark.parametrize(
    "leak", ["gh-token.json", "SECRET-notes.txt", "deploy.credentials", "API-KEY"]
)
def test_artifact_contract_rejects_secret_bearing_extra(leak: str) -> None:
    # Tolerating extras must not become tolerating credential exfiltration.
    with pytest.raises(ValueError, match="secret-bearing"):
        consumer_sync_artifact_ingest.validate_artifact_members(
            _infolist(*PRODUCER_MEMBERS, leak)
        )


def test_artifact_contract_rejects_non_regular_and_runaway_members() -> None:
    for name, attr in (("nested/", 0), ("link.json", 0o120000 << 16)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for member in PRODUCER_MEMBERS:
                zf.writestr(member, "{}")
            info = zipfile.ZipInfo(name)
            info.external_attr = attr
            zf.writestr(info, "")
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            with pytest.raises(ValueError, match="must be a regular file"):
                consumer_sync_artifact_ingest.validate_artifact_members(zf.infolist())

    bound = consumer_sync_artifact_ingest.ARTIFACT_MAX_EXTRA_MEMBERS
    with pytest.raises(ValueError, match="runaway bound"):
        consumer_sync_artifact_ingest.validate_artifact_members(
            _infolist(*PRODUCER_MEMBERS, *[f"extra-{i}.json" for i in range(bound + 1)])
        )


def test_expiry_boundary_is_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `--expiry DATE` names the LAST authorised day, so the window is still live ON that day.
    # `<=` refused it in production: "Expiry date 2026-07-25 must be in the future relative to
    # current date 2026-07-25", four times.
    state_dir = tmp_path / "state"
    ledger_path = tmp_path / "capabilities.json"
    capabilities.save({}, ledger_path)

    monkeypatch.setattr(
        consumer_sync_artifact_ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps({"workflow_runs": []}),
    )

    def run(expiry: str) -> int:
        return consumer_sync_artifact_ingest.main(
            [
                "ingest",
                "--state-dir",
                str(state_dir),
                "--ledger",
                str(ledger_path),
                "--mode",
                "human-on-exception",
                "--phase-id",
                "importer-boundary",
                "--expiry",
                expiry,
                "--now",
                "1784894400",  # 2026-07-24
            ]
        )

    assert run("2026-07-24") == 0, "expiry ON the current date must still be live"
    assert run("2026-07-25") == 0

    with pytest.raises(ValueError, match="must be on or after"):
        run("2026-07-23")


def _legacy_observed_targets(repo_root: Path, plan: dict) -> dict[str, str]:
    """The pre-2026-08-20 zipball read, verbatim, kept as the equivalence reference.

    The targeted read has to reproduce this EXACTLY. If a directory hash differs by so much as an
    ordering rule, every directory target flips to `update` on the cutover and that drift is
    recorded as evidence — a silent, self-inflicted corruption of the ledger.
    """
    observed: dict[str, str] = {}
    targets = {e["target"] for e in plan["entries"]} | {
        r["target"] for r in plan["removals"]
    }
    for target in targets:
        target_abs_path = repo_root / target
        if not target_abs_path.exists():
            continue
        is_dir_in_plan = False
        for entry in plan["entries"]:
            if entry["target"] == target:
                is_dir_in_plan = entry["is_directory"]
                break
        if target_abs_path.is_dir() != is_dir_in_plan:
            raise TypeError(f"Type mismatch for target {target}")
        observed[target] = consumer_sync_artifact_ingest._content_hash(target_abs_path)
    return observed


# Names chosen to catch the ordering subtlety: '-' (0x2D) sorts before '/' (0x2F), so a sibling
# file and a subdirectory with a shared prefix interleave in a way naive per-directory recursion
# gets wrong. `sorted(rglob("*"))` compares whole path strings; the tree read sorts relative paths.
EQUIV_FILES = {
    ".github/workflows/new.yml": "hello old",
    ".github/workflows/obsolete.yml": "obsolete content",
    "design-system/tokens.json": '{"a": 1}',
    "design-system/a-sibling.css": "body{}",
    "design-system/a/nested.css": "nested",
    "design-system/a/deep/leaf.txt": "leaf",
    "design-system/z-last.txt": "z",
    "untouched/elsewhere.txt": "not a target",
}

EQUIV_PLAN = {
    "entries": [
        {"target": ".github/workflows/new.yml", "is_directory": False},
        {"target": "design-system", "is_directory": True},
        {"target": ".github/workflows/absent.yml", "is_directory": False},
    ],
    "removals": [{"target": ".github/workflows/obsolete.yml"}],
}


def test_targeted_read_matches_legacy_zipball_read(tmp_path: Path) -> None:
    # Legacy path: GitHub zipballs nest everything under one generated root directory.
    zip_bytes = make_zip(
        {f"stranske-Repo-abcdef/{p}": c for p, c in EQUIV_FILES.items()}
    )
    dest = tmp_path / "extracted"
    dest.mkdir()
    consumer_sync_artifact_ingest.safe_extract_zip(zip_bytes, dest)
    legacy = _legacy_observed_targets(dest / "stranske-Repo-abcdef", EQUIV_PLAN)

    # Targeted path: tree + blobs, no archive.
    tree, blobs = consumer_sync_artifact_ingest.make_tree_responses(EQUIV_FILES)
    nodes = {n["path"]: n for n in tree["tree"]}
    targeted = consumer_sync_artifact_ingest.observed_targets_from_tree(
        "stranske/Repo",
        nodes,
        EQUIV_PLAN,
        reader=consumer_sync_artifact_ingest.BlobReader(
            {
                sha: hashlib.sha256(
                    __import__("base64").b64decode(row["content"])
                ).hexdigest()
                for sha, row in blobs.items()
            }
        ),
    )

    assert targeted == legacy
    assert set(targeted) == {
        ".github/workflows/new.yml",
        ".github/workflows/obsolete.yml",
        "design-system",
    }, "absent targets must stay absent, non-targets must not leak in"


def test_tree_read_fails_closed_on_truncation_and_odd_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A truncated tree would make present targets read as ABSENT -> `create` proposals for
    # everything, recorded as evidence. Must raise, never degrade.
    monkeypatch.setattr(
        consumer_sync_artifact_ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps({"truncated": True, "tree": []}),
    )
    with pytest.raises(ValueError, match="repo_tree_truncated"):
        consumer_sync_artifact_ingest.fetch_repo_tree("stranske/Repo", "main")

    monkeypatch.setattr(
        consumer_sync_artifact_ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps(
            {
                "truncated": False,
                "tree": [
                    {"path": "../escape", "mode": "100644", "type": "blob", "sha": "0" * 40}
                ],
            }
        ),
    )
    with pytest.raises(ValueError, match="repo_tree_unsafe_path"):
        consumer_sync_artifact_ingest.fetch_repo_tree("stranske/Repo", "main")

    # Symlinks and submodules are refused rather than hashed under a changed meaning.
    for mode, node_type in (("120000", "blob"), ("160000", "commit")):
        nodes = {
            "t.yml": {"path": "t.yml", "mode": mode, "type": node_type, "sha": "0" * 40}
        }
        with pytest.raises(ValueError, match="unsupported_tree_entry"):
            consumer_sync_artifact_ingest.observed_targets_from_tree(
                "stranske/Repo",
                nodes,
                {"entries": [{"target": "t.yml", "is_directory": False}], "removals": []},
                reader=consumer_sync_artifact_ingest.BlobReader(),
            )


def test_blob_memo_is_content_addressed_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree, blobs = consumer_sync_artifact_ingest.make_tree_responses({"a.txt": "shared"})
    node = next(n for n in tree["tree"] if n["path"] == "a.txt")
    calls: list[str] = []

    def mock_gh(args: list[str]) -> str:
        cmd = " ".join(args)
        calls.append(cmd)
        return json.dumps(blobs[cmd.rsplit("/", 1)[-1]])

    monkeypatch.setattr(consumer_sync_artifact_ingest, "GH_COMMAND_MOCK", mock_gh)

    reader = consumer_sync_artifact_ingest.BlobReader()
    first = reader.sha256("stranske/A", node)
    assert first == hashlib.sha256(b"shared").hexdigest()
    # Same blob id in a DIFFERENT repo must not re-fetch — that is where the 80% saving comes from.
    second = reader.sha256("stranske/B", node)
    assert second == first
    assert len(calls) == 1, calls
    assert (reader.fetches, reader.hits) == (1, 1)

    # The per-run fetch budget turns a runaway into a loud failure, not a multi-hour cron stall
    # (gh_capacity paces up to 10s per call once core drops below 25%).
    broke = consumer_sync_artifact_ingest.BlobReader(max_fetches=0)
    with pytest.raises(ValueError, match="blob_fetch_budget_exhausted"):
        broke.sha256("stranske/A", node)

    # A blob whose bytes do not match the id it was fetched under is refused, so the memo key
    # cannot be poisoned.
    tampered = dict(blobs[node["sha"]])
    tampered["content"] = __import__("base64").b64encode(b"different").decode()
    monkeypatch.setattr(
        consumer_sync_artifact_ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps(tampered),
    )
    with pytest.raises(ValueError, match="blob_content_sha_mismatch"):
        consumer_sync_artifact_ingest.BlobReader().sha256("stranske/C", node)


def test_blob_digest_cache_round_trips_and_is_bounded(tmp_path: Path) -> None:
    # Persisting the digest map is what takes the next day's steady state to ~0 fetches. It rides
    # the existing state file rather than adding a store, so older state files must still load.
    state_file = tmp_path / "consumer-sync-ingest-state.json"
    legacy = {"schema_version": 1, "records": {}, "exceptions": []}
    state_file.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = consumer_sync_artifact_ingest.load_state(state_file)
    assert loaded["blob_digests"] == {}

    loaded["blob_digests"]["a" * 40] = "b" * 64
    consumer_sync_artifact_ingest.save_state(loaded, state_file)
    assert consumer_sync_artifact_ingest.load_state(state_file)["blob_digests"] == {
        "a" * 40: "b" * 64
    }

    bound = consumer_sync_artifact_ingest.BLOB_DIGEST_CACHE_MAX
    loaded["blob_digests"] = {f"{i:040x}": f"{i:064x}" for i in range(bound + 25)}
    consumer_sync_artifact_ingest.save_state(loaded, state_file)
    kept = consumer_sync_artifact_ingest.load_state(state_file)["blob_digests"]
    assert len(kept) == bound
    # A dropped entry only costs a refetch, so evicting the oldest insertions is safe.
    assert f"{bound + 24:040x}" in kept
    assert f"{0:040x}" not in kept


def test_repo_hygiene_separates_debris_from_vendored() -> None:
    big = b"x" * (6 * 1024 * 1024)
    tree, _ = consumer_sync_artifact_ingest.make_tree_responses(
        {
            "src/app.py": "print(1)",
            # A PYTHON manifest at the root must not vouch for a JavaScript node_modules.
            "pyproject.toml": "[project]",
            # Root node_modules with NO manifest: debris from `npm install --no-save`.
            "node_modules/@octokit/rest/index.js": "x" * 2048,
            "node_modules/toad-cache/index.js": "y",
            # Vendored: a sibling package.json declares it. stranske/Template really does this —
            # `"minimatch": "file:node_modules/minimatch"`, copied by agents-auto-pilot.yml and
            # allowlisted by agents-guard.yml. Untracking it breaks both.
            ".github/scripts/package.json": '{"dependencies":{"minimatch":"file:node_modules/minimatch"}}',
            ".github/scripts/node_modules/minimatch/index.js": "z" * 4096,
            "tests/__pycache__/t.pyc": "c" * 512,
            "src/ui/vendor/lib.js": "v",
            "data/image_cache/huge.jpg": big,
            "data/image_cache/small.jpg": "tiny",
        }
    )
    nodes = {n["path"]: n for n in tree["tree"]}
    report = consumer_sync_artifact_ingest.repo_hygiene("stranske/Repo", nodes)
    by_path = {row["path"]: row for row in report["findings"]}

    assert by_path["node_modules"]["disposition"] == "untrack"
    assert by_path["node_modules"]["manifest"] is None
    assert by_path[".github/scripts/node_modules"]["disposition"] == "review_vendored"
    assert (
        by_path[".github/scripts/node_modules"]["manifest"]
        == ".github/scripts/package.json"
    )
    assert by_path["tests/__pycache__"]["disposition"] == "untrack"
    # `vendor/` means "committed on purpose" by convention, and JS actions ship a committed
    # `dist/` — recommending removal of either would be reckless.
    assert by_path["src/ui/vendor"]["disposition"] == "review_vendored"
    assert by_path["data/image_cache"]["disposition"] == "review_owner"

    # Patterns are ANCHORED. A bare "node_modules/" matches at every depth and would swallow the
    # vendored copy — that recommendation would have broken CI.
    assert by_path["node_modules"]["gitignore"] == "/node_modules/"
    assert (
        by_path[".github/scripts/node_modules"]["gitignore"]
        == "/.github/scripts/node_modules/"
    )
    assert "node_modules/" not in report["remediation"]["gitignore_lines"]

    # Only debris gets untrack commands; the vendored and owner-decision items are review-only.
    untracked = " ".join(report["remediation"]["untrack_commands"])
    assert "git rm -r --cached node_modules" in untracked
    assert ".github/scripts/node_modules" not in untracked
    assert "data/image_cache" not in untracked
    reviewed = {row["path"] for row in report["remediation"]["review_only"]}
    assert reviewed == {".github/scripts/node_modules", "src/ui/vendor", "data/image_cache"}

    # A large-binary dir counts ALL its blobs, not just the ones over the threshold, or the
    # number understates the case it is making.
    assert by_path["data/image_cache"]["files"] == 2
    assert by_path["data/image_cache"]["bytes"] == len(big) + 4
    assert report["reclaimable_bytes"] == by_path["node_modules"]["bytes"] + by_path["tests/__pycache__"]["bytes"]

    # A clean repo produces findings-free output, not a false positive.
    clean_tree, _ = consumer_sync_artifact_ingest.make_tree_responses(
        {"src/app.py": "print(1)"}
    )
    clean = consumer_sync_artifact_ingest.repo_hygiene(
        "stranske/Clean", {n["path"]: n for n in clean_tree["tree"]}
    )
    assert clean["findings"] == []
    assert clean["remediation"] == {}
    assert clean["oversized"] is False


def _hygiene_report(**findings_by_repo):
    return {
        repo: {"hygiene": {"findings": findings}}
        for repo, findings in findings_by_repo.items()
    }


def test_hygiene_escalation_asks_only_about_judgment_calls() -> None:
    MB = 1024 * 1024
    report = _hygiene_report(
        **{
            "stranske/a": [
                # machine-decidable -> digest, never a question
                {"kind": "dependency_dir", "path": "node_modules", "bytes": 7 * MB,
                 "files": 356, "gitignore": "/node_modules/", "disposition": "untrack"},
                # expected and load-bearing -> neither
                {"kind": "dependency_dir", "path": ".github/scripts/node_modules",
                 "bytes": 604127, "files": 84,
                 "gitignore": "/.github/scripts/node_modules/",
                 "disposition": "review_vendored"},
                # below the digest floor -> not worth a line
                {"kind": "dependency_dir", "path": "tests/__pycache__", "bytes": 15088,
                 "files": 3, "gitignore": "/tests/__pycache__/", "disposition": "untrack"},
            ],
            "stranske/b": [
                # judgment, and material -> the ONE thing a human is asked
                {"kind": "large_binary_dir", "path": "data/image_cache", "bytes": 105 * MB,
                 "files": 225, "gitignore": "/data/image_cache/",
                 "disposition": "review_owner"},
                # judgment, but trivial -> the floor keeps it silent
                {"kind": "large_binary_dir", "path": "docs/img", "bytes": 47755, "files": 1,
                 "gitignore": "/docs/img/", "disposition": "review_owner"},
            ],
        }
    )
    esc = consumer_sync_artifact_ingest.hygiene_escalation(report)

    assert [row["path"] for row in esc["digest"]] == ["node_modules"]
    assert esc["untrackable_bytes"] == 7 * MB
    assert esc["digest"][0]["command"] == "git rm -r --cached node_modules"

    assert len(esc["questions"]) == 1, esc["questions"]
    q = esc["questions"][0]
    assert q["path"] == "data/image_cache"
    # Silence must be safe: the default changes nothing.
    assert "change nothing" in q["default_action"]

    # Standing findings must be ONE question, not a new one on every byte change — the question
    # text carries a coarse band, not exact bytes, because record_owner_question keys on the text.
    grown = _hygiene_report(**{"stranske/b": [
        {"kind": "large_binary_dir", "path": "data/image_cache", "bytes": 105 * MB + 5000,
         "files": 226, "gitignore": "/data/image_cache/", "disposition": "review_owner"}]})
    assert (
        consumer_sync_artifact_ingest.hygiene_escalation(grown)["questions"][0]["question"]
        == q["question"]
    )
    assert str(105 * MB) not in q["question"]

    # Nothing to say when nothing is wrong.
    empty = consumer_sync_artifact_ingest.hygiene_escalation(_hygiene_report(**{"c": []}))
    assert empty["digest"] == [] and empty["questions"] == []


def test_hygiene_escalation_recording_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reporting problem must never fail a read-only ingest.
    import feedback

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(feedback, "record_owner_question", boom)
    out = consumer_sync_artifact_ingest.record_hygiene_escalation(
        {"questions": [{"repository": "r", "path": "p", "question": "q",
                        "default_action": "d", "options": []}]}
    )
    assert out == {"recorded": [], "asked": 0}


def test_corrupt_state_no_write(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "consumer-sync-ingest-state.json"
    state_file.write_text("corrupt data", encoding="utf-8")

    with pytest.raises(ValueError, match="Corrupt state"):
        consumer_sync_artifact_ingest.main(
            [
                "ingest",
                "--state-dir",
                str(state_dir),
                "--mode",
                "shadow",
                "--now",
                "100",
            ]
        )


def test_preview_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    ledger_path = tmp_path / "capabilities.json"
    capabilities.save({}, ledger_path)

    plan = valid_plan()
    handoff = valid_handoff(plan)
    handoff["run_ref"] = "github-actions:stranske/Workflows:12345:1"
    handoff["handoff_id"] = stable_hash(
        "consumer-sync-shadow-handoff",
        {k: handoff[k] for k in handoff if k != "handoff_id"},
    )

    artifact_zip_bytes = make_zip(
        {
            "consumer-sync-plan.json": json.dumps(plan),
            "handoff.json": json.dumps(handoff),
        }
    )

    repo_tree, repo_blobs = consumer_sync_artifact_ingest.make_tree_responses(
        {".github/workflows/new.yml": "hello old"}
    )

    run_list = {
        "workflow_runs": [{"id": 12345, "run_attempt": 1, "conclusion": "success"}]
    }
    artifact_list = {
        "artifacts": [
            {
                "id": 9999,
                "name": "consumer-sync-shadow-evidence-12345-1",
                "expired": False,
            }
        ]
    }

    def mock_gh(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "health-69-consumer-sync-shadow-evidence.yml/runs?status=success" in cmd:
            return json.dumps(run_list)
        if "runs/12345/artifacts" in cmd:
            return json.dumps(artifact_list)
        if "artifacts/9999/zip" in cmd:
            return artifact_zip_bytes
        if "repos/stranske/Template/git/trees/" in cmd:
            return json.dumps(repo_tree)
        if "repos/stranske/Template/git/blobs/" in cmd:
            return json.dumps(repo_blobs[cmd.rsplit("/", 1)[-1]])
        if "repos/stranske/Template" in cmd:
            return json.dumps({"default_branch": "main"})
        raise ValueError(f"Unexpected mock gh command: {cmd}")

    monkeypatch.setattr(consumer_sync_artifact_ingest, "GH_COMMAND_MOCK", mock_gh)
    monkeypatch.setattr(
        consumer_sync_artifact_ingest, "TEST_REGISTRY", ["stranske/template"]
    )

    rc = consumer_sync_artifact_ingest.main(
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
            "100",
        ]
    )
    assert rc == 0
    assert not (state_dir / "consumer-sync-ingest-state.json").exists()
    assert not (state_dir / "consumer-sync-artifact-ingest-report.json").exists()


def test_end_to_end_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    ledger_path = tmp_path / "capabilities.json"
    capabilities.save({}, ledger_path)

    plan = valid_plan()
    handoff = valid_handoff(plan)
    handoff["run_ref"] = "github-actions:stranske/Workflows:12345:1"
    handoff["handoff_id"] = stable_hash(
        "consumer-sync-shadow-handoff",
        {k: handoff[k] for k in handoff if k != "handoff_id"},
    )

    # Built as the SUPERSET the producer really uploads, so the end-to-end path — not only the
    # unit block above — is the regression guard for the artifact contract.
    artifact_zip_bytes = make_zip(
        {
            "consumer-sync-plan.json": json.dumps(plan),
            "handoff.json": json.dumps(handoff),
            "evidence-ledger.json": json.dumps([]),
            "completion-evidence.json": json.dumps({"status": "accepted"}),
            "capabilities-state.json": json.dumps({}),
            "runtime-report.json": json.dumps(
                {"schema": "workflows.consumer-sync-runtime-report/v1"}
            ),
        }
    )

    repo_tree, repo_blobs = consumer_sync_artifact_ingest.make_tree_responses(
        {
            ".github/workflows/new.yml": "hello old",
            ".github/workflows/create-only.yml": "create-only content",
            ".github/workflows/skipped.yml": "skipped content",
            ".github/workflows/obsolete.yml": "obsolete content",
        }
    )

    run_list = {
        "workflow_runs": [{"id": 12345, "run_attempt": 1, "conclusion": "success"}]
    }
    artifact_list = {
        "artifacts": [
            {
                "id": 9999,
                "name": "consumer-sync-shadow-evidence-12345-1",
                "expired": False,
            }
        ]
    }

    def mock_gh(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "health-69-consumer-sync-shadow-evidence.yml/runs?status=success" in cmd:
            return json.dumps(run_list)
        if "runs/12345/artifacts" in cmd:
            return json.dumps(artifact_list)
        if "artifacts/9999/zip" in cmd:
            return artifact_zip_bytes
        if "repos/stranske/Template/git/trees/" in cmd:
            return json.dumps(repo_tree)
        if "repos/stranske/Template/git/blobs/" in cmd:
            return json.dumps(repo_blobs[cmd.rsplit("/", 1)[-1]])
        if "repos/stranske/Template" in cmd:
            return json.dumps({"default_branch": "main"})
        raise ValueError(f"Unexpected mock gh command: {cmd}")

    monkeypatch.setattr(consumer_sync_artifact_ingest, "GH_COMMAND_MOCK", mock_gh)
    monkeypatch.setattr(
        consumer_sync_artifact_ingest, "TEST_REGISTRY", ["stranske/template"]
    )

    # First Run
    rc1 = consumer_sync_artifact_ingest.main(
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
            "100",
        ]
    )
    assert rc1 == 0

    ledger_data = capabilities.load(ledger_path, create=False)
    cap = ledger_data[consumer_sync_shadow.CAPABILITY_ID]
    events_count_first = len(cap["event_history"])

    # Subject identity must survive the ingest, or the promotion gate's "3 distinct consumer
    # repos" can never be measured no matter how many artifacts land.
    # (the reference-workflow seeding that creates the capability also emits output events, and
    # those legitimately carry no subject — only the ingest's own effect does)
    outputs = [e for e in cap["event_history"] if e.get("type") == "output"]
    assert "stranske/template" in {
        (e.get("metadata") or {}).get("subject_id") for e in outputs
    }
    # Second Run
    rc2 = consumer_sync_artifact_ingest.main(
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
            "101",
        ]
    )
    assert rc2 == 0

    ledger_data2 = capabilities.load(ledger_path, create=False)
    cap2 = ledger_data2[consumer_sync_shadow.CAPABILITY_ID]
    events_count_second = len(cap2["event_history"])

    assert events_count_first == events_count_second

    # ...and the subject reaches the reader the promotion gate actually consults. The live ledger
    # carries matcher.kind == "compiled_workflow" for this rail (stamped when the compiled module
    # was adopted) while a freshly seeded fixture does not, so stamp it before reading.
    ledger_data2[consumer_sync_shadow.CAPABILITY_ID]["matcher"] = {
        "kind": "compiled_workflow",
        "name": "reference_sync_hygiene",
    }
    capabilities.save(ledger_data2, ledger_path)
    assert capability_outcome_bridge.compiled_workflow_subjects(path=ledger_path)[
        consumer_sync_shadow.CAPABILITY_ID
    ]["subjects"] == ["stranske/template"]


def test_module_selftests():
    consumer_sync_artifact_ingest.run_selftests()
