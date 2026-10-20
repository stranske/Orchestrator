"""Hermetic coverage for remaining consumer_sync_artifact_ingest branches.

CI's combined measure still shows ~126 uncovered statements in this module after the
redirect_shadow chunk; these tests target the ranked second file without live gh/network.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_consumer_sync_artifact_ingest import make_zip
from test_consumer_sync_shadow import stable_hash, valid_handoff, valid_plan

import capabilities
import consumer_sync_artifact_ingest as ingest


@pytest.fixture(autouse=True)
def private_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("ORCH_REFERENCE_WORKFLOW_DISABLED", raising=False)
    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", None)
    monkeypatch.setattr(ingest, "TEST_REGISTRY", None)
    started: list[list[str]] = []

    def _block_run(args, **kwargs):  # noqa: ANN001
        started.append(list(args))
        raise AssertionError(f"live subprocess must not start: {args}")

    monkeypatch.setattr(ingest.subprocess, "run", _block_run)
    yield
    assert started == [], started


def _b64_workflow(body: str) -> str:
    return json.dumps({"content": base64.b64encode(body.encode("utf-8")).decode("ascii")})


def test_get_maint_68_repos_parses_registry_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    yaml = (
        "env:\n"
        "  REGISTERED_CONSUMER_REPOS:\n"
        '    - "stranske/Ready"\n'
        "    - stranske/Template\n"
        "  OTHER: value\n"
    )
    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", lambda args: _b64_workflow(yaml))
    assert ingest.get_maint_68_repos() == ["stranske/Ready", "stranske/Template"]

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", lambda args: _b64_workflow("env:\n  X: 1\n"))
    with pytest.raises(Exception, match="Failed to load registry"):
        ingest.get_maint_68_repos(fallback_allowed=False)

    fallback = ingest.get_maint_68_repos(fallback_allowed=True)
    assert "stranske/Template" in fallback
    assert "failed to fetch maint-68" in capsys.readouterr().err


def test_run_gh_and_bytes_use_token_file_and_surface_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "gh_cli_token"
    token.write_text("tok-from-file\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex/credentials").mkdir(parents=True)
    token.rename(tmp_path / ".codex/credentials/gh_cli_token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    calls: list[dict] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append({"args": list(args), "env": kwargs.get("env")})
        text = bool(kwargs.get("text"))
        if args[-1] == "fail":
            return SimpleNamespace(
                returncode=1,
                stdout="" if text else b"",
                stderr="boom" if text else b"boom",
            )
        if text:
            return SimpleNamespace(returncode=0, stdout="ok-text", stderr="")
        return SimpleNamespace(returncode=0, stdout=b"ok-bytes", stderr=b"")

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", None)

    assert ingest.run_gh(["gh", "api", "ok"]) == "ok-text"
    assert calls[0]["env"]["GH_TOKEN"] == "tok-from-file"
    with pytest.raises(Exception, match="gh command failed: boom"):
        ingest.run_gh(["gh", "api", "fail"])

    assert ingest.run_gh_bytes(["gh", "api", "ok"]) == b"ok-bytes"
    with pytest.raises(Exception, match="gh command failed"):
        ingest.run_gh_bytes(["gh", "api", "fail"])

    # Mock path returns str/bytes for both helpers.
    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", lambda args: b"raw")
    assert ingest.run_gh(["x"]) == "raw"
    assert ingest.run_gh_bytes(["x"]) == b"raw"
    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", lambda args: "plain")
    assert ingest.run_gh_bytes(["x"]) == b"plain"


def test_discover_artifact_rejects_bad_payloads_and_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ingest, "GH_COMMAND_MOCK", lambda args: json.dumps({"workflow_runs": "nope"})
    )
    with pytest.raises(ValueError, match="workflow_runs_response_missing_array"):
        ingest.discover_artifact()

    def mock_gh(args: list[str]) -> str:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps(
                {
                    "workflow_runs": [
                        "skip-me",
                        {"id": "not-int"},
                        {"id": 11, "run_attempt": 1},
                        {"id": 12, "run_attempt": 1},
                    ]
                }
            )
        if "runs/11/artifacts" in cmd:
            raise RuntimeError("rate limited")
        if "runs/12/artifacts" in cmd:
            raise RuntimeError("gone")
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_gh)
    with pytest.raises(RuntimeError, match="artifact_lookup_failed"):
        ingest.discover_artifact()


def test_blob_sha256_encodings_and_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = ingest.BlobReader()
    with pytest.raises(ValueError, match="invalid_blob_sha"):
        ingest.blob_sha256("r/a", {"sha": "short"}, reader)

    with pytest.raises(ValueError, match="blob_exceeds_max_bytes"):
        ingest.blob_sha256(
            "r/a",
            {"sha": "a" * 40, "size": ingest.REPO_READ_MAX_BLOB_BYTES + 1, "path": "big.bin"},
            ingest.BlobReader(),
        )

    body = b"hello-utf8"
    sha = __import__("hashlib").sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    monkeypatch.setattr(
        ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps({"encoding": "utf-8", "content": body.decode(), "sha": sha}),
    )
    digest = ingest.blob_sha256("r/a", {"sha": sha, "size": len(body)}, ingest.BlobReader())
    assert digest == __import__("hashlib").sha256(body).hexdigest()

    monkeypatch.setattr(
        ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps({"encoding": "hex", "content": "00"}),
    )
    with pytest.raises(ValueError, match="unsupported_blob_encoding"):
        ingest.blob_sha256("r/a", {"sha": "b" * 40, "size": 1}, ingest.BlobReader())


def test_fetch_tree_and_observed_budget_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", lambda args: json.dumps({"truncated": False}))
    with pytest.raises(ValueError, match="repo_tree_missing_array"):
        ingest.fetch_repo_tree("r/a", "main")

    monkeypatch.setattr(
        ingest,
        "GH_COMMAND_MOCK",
        lambda args: json.dumps({"truncated": False, "tree": ["bad"]}),
    )
    with pytest.raises(ValueError, match="repo_tree_invalid_node"):
        ingest.fetch_repo_tree("r/a", "main")

    monkeypatch.setattr(ingest, "REPO_READ_MAX_TOTAL_BYTES", 10)
    nodes = {
        "a.txt": {"path": "a.txt", "mode": "100644", "type": "blob", "sha": "c" * 40, "size": 8},
        "b.txt": {"path": "b.txt", "mode": "100644", "type": "blob", "sha": "d" * 40, "size": 8},
        "dir": {"path": "dir", "mode": "040000", "type": "tree", "sha": "e" * 40},
        "dir/x.txt": {
            "path": "dir/x.txt",
            "mode": "100644",
            "type": "blob",
            "sha": "f" * 40,
            "size": 8,
        },
        "dir/y.txt": {
            "path": "dir/y.txt",
            "mode": "100644",
            "type": "blob",
            "sha": "a" * 40,
            "size": 8,
        },
    }

    class BoomReader(ingest.BlobReader):
        def sha256(self, repo: str, node: dict) -> str:  # noqa: ANN001
            return "0" * 64

    with pytest.raises(ValueError, match="repo_read_exceeds_max_total_bytes"):
        ingest.observed_targets_from_tree(
            "r/a",
            nodes,
            {
                "entries": [
                    {"target": "a.txt", "is_directory": False},
                    {"target": "b.txt", "is_directory": False},
                ],
                "removals": [],
            },
            reader=BoomReader(),
        )
    with pytest.raises(ValueError, match="repo_read_exceeds_max_total_bytes"):
        ingest.observed_targets_from_tree(
            "r/a",
            nodes,
            {"entries": [{"target": "dir", "is_directory": True}], "removals": []},
            reader=BoomReader(),
        )


def test_safe_extract_size_and_absolute_path_guards(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    dest.mkdir()
    with pytest.raises(ValueError, match="zip_bytes size"):
        ingest.safe_extract_zip(b"x" * 20, dest, max_total_bytes=5)

    # Deflated zeros stay tiny on the wire but expand past the uncompressed budget.
    compressible = io.BytesIO()
    with zipfile.ZipFile(compressible, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.txt", "Z" * 10_000)
    packed = compressible.getvalue()
    assert len(packed) < 500
    with pytest.raises(ValueError, match="uncompressed size"):
        ingest.safe_extract_zip(packed, dest, max_total_bytes=1_000)

    with pytest.raises(ValueError, match="max_entry_bytes"):
        ingest.safe_extract_zip(packed, dest, max_total_bytes=50_000, max_entry_bytes=10)

    abs_buf = io.BytesIO()
    with zipfile.ZipFile(abs_buf, "w") as zf:
        zf.writestr("/abs.txt", "nope")
    with pytest.raises(ValueError, match="Unsafe zip entry"):
        ingest.safe_extract_zip(abs_buf.getvalue(), dest)


def test_load_state_schema_guards(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": 2, "records": {}, "exceptions": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Unsupported or missing state schema"):
        ingest.load_state(path)

    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing records or exceptions"):
        ingest.load_state(path)


def test_hygiene_skips_nested_large_dir_already_covered() -> None:
    # A dependency-dir finding under node_modules must suppress a nested large-binary finding.
    tree, _ = ingest.make_tree_responses(
        {
            "package.json": "{}",
            "node_modules/pkg/index.js": "x" * 2048,
            "node_modules/pkg/big.bin": b"y" * (6 * 1024 * 1024),
        }
    )
    report = ingest.repo_hygiene("r/a", {n["path"]: n for n in tree["tree"]})
    paths = [f["path"] for f in report["findings"]]
    assert "node_modules" in paths
    assert not any(p.startswith("node_modules/") for p in paths if p != "node_modules")


def test_record_hygiene_escalation_records_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import feedback

    monkeypatch.setattr(
        feedback,
        "record_owner_question",
        lambda *a, **k: {"id": "q1", "status": "asked"},
    )
    out = ingest.record_hygiene_escalation(
        {
            "questions": [
                {
                    "repository": "stranske/Ready",
                    "path": "data/cache",
                    "question": "keep?",
                    "default_action": "change nothing",
                    "options": ["keep", "drop"],
                }
            ]
        }
    )
    assert out["asked"] == 1
    assert out["recorded"][0]["repository"] == "stranske/Ready"
    assert out["recorded"][0]["path"] == "data/cache"


def test_gh_throttle_swallows_import_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "gh_capacity":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ingest._gh_throttle("core")  # must not raise


def test_main_kill_switch_selftest_and_validation_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ORCH_REFERENCE_WORKFLOW_DISABLED", "1")
    assert ingest.main(["ingest", "--state-dir", str(tmp_path / "s")]) == 0
    assert "Kill switch active" in capsys.readouterr().out
    monkeypatch.delenv("ORCH_REFERENCE_WORKFLOW_DISABLED")

    monkeypatch.setattr(
        ingest, "run_selftests", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert ingest.main(["--selftest"]) == 1
    assert "Selftest failed" in capsys.readouterr().err

    with pytest.raises(ValueError, match="Expiry date must be explicitly specified"):
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "s2"),
                "--mode",
                "human-on-exception",
                "--now",
                "100",
            ]
        )
    with pytest.raises(ValueError, match="Invalid ISO date"):
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "s3"),
                "--mode",
                "human-on-exception",
                "--expiry",
                "not-a-date",
                "--now",
                "100",
            ]
        )
    with pytest.raises(ValueError, match="phase-id must be a short"):
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "s4"),
                "--phase-id",
                "bad id!",
                "--now",
                "100",
            ]
        )
    with pytest.raises(ValueError, match="must start with importer-"):
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "s5"),
                "--mode",
                "human-on-exception",
                "--expiry",
                "2099-01-01",
                "--phase-id",
                "not-importer",
                "--now",
                "100",
            ]
        )


def test_main_lock_download_extract_and_parse_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_path = state_dir / "consumer-sync-ingest.lock"
    lock_f = open(lock_path, "w")
    import fcntl

    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setattr(
        ingest,
        "GH_COMMAND_MOCK",
        lambda args: (
            json.dumps(
                {
                    "workflow_runs": [{"id": 1, "run_attempt": 1}],
                }
            )
            if "runs?status=success" in " ".join(args)
            else json.dumps(
                {
                    "artifacts": [
                        {"id": 9, "name": "consumer-sync-shadow-evidence-1-1", "expired": False}
                    ]
                }
            )
        ),
    )
    # Lock held -> exit 0 before discover continues meaningfully; still needs discover to succeed
    # after lock. Hold the lock so ingest returns early.
    assert (
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(state_dir),
                "--repository",
                "stranske/Template",
                "--now",
                "100",
            ]
        )
        == 0
    )
    assert "Lock acquired by another ingest process" in capsys.readouterr().err
    fcntl.flock(lock_f, fcntl.LOCK_UN)
    lock_f.close()

    state_dir2 = tmp_path / "state2"
    ledger = tmp_path / "ledger.json"
    capabilities.save({}, ledger)
    plan = valid_plan()
    handoff = valid_handoff(plan)
    handoff["run_ref"] = "github-actions:stranske/Workflows:7:1"
    handoff["handoff_id"] = stable_hash(
        "consumer-sync-shadow-handoff",
        {k: handoff[k] for k in handoff if k != "handoff_id"},
    )

    def mock_download_fail(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps({"workflow_runs": [{"id": 7, "run_attempt": 1}]})
        if "runs/7/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 70,
                            "name": "consumer-sync-shadow-evidence-7-1",
                            "expired": False,
                        }
                    ]
                }
            )
        if "artifacts/70/zip" in cmd:
            raise RuntimeError("zip gone")
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_download_fail)
    monkeypatch.setattr(ingest, "TEST_REGISTRY", ["stranske/template"])
    assert (
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(state_dir2),
                "--ledger",
                str(ledger),
                "--repository",
                "stranske/Template",
                "--now",
                "100",
            ]
        )
        == 1
    )
    assert "Error downloading artifact zip" in capsys.readouterr().err

    bad_zip = make_zip({"consumer-sync-plan.json": "{", "handoff.json": "{}"})

    def mock_bad_plan(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps({"workflow_runs": [{"id": 8, "run_attempt": 1}]})
        if "runs/8/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 80,
                            "name": "consumer-sync-shadow-evidence-8-1",
                            "expired": False,
                        }
                    ]
                }
            )
        if "artifacts/80/zip" in cmd:
            return bad_zip
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_bad_plan)
    assert (
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "state3"),
                "--ledger",
                str(ledger),
                "--repository",
                "stranske/Template",
                "--now",
                "100",
            ]
        )
        == 1
    )
    assert "Error parsing consumer-sync-plan.json" in capsys.readouterr().err

    bad_handoff_zip = make_zip({"consumer-sync-plan.json": json.dumps(plan), "handoff.json": "{"})

    def mock_bad_handoff(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps({"workflow_runs": [{"id": 9, "run_attempt": 1}]})
        if "runs/9/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 90,
                            "name": "consumer-sync-shadow-evidence-9-1",
                            "expired": False,
                        }
                    ]
                }
            )
        if "artifacts/90/zip" in cmd:
            return bad_handoff_zip
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_bad_handoff)
    assert (
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "state4"),
                "--ledger",
                str(ledger),
                "--repository",
                "stranske/Template",
                "--now",
                "100",
            ]
        )
        == 1
    )
    assert "Error parsing handoff.json" in capsys.readouterr().err


def test_main_repo_metadata_and_tree_failures_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    ledger = tmp_path / "ledger.json"
    capabilities.save({}, ledger)
    plan = valid_plan()
    handoff = valid_handoff(plan)
    handoff["run_ref"] = "github-actions:stranske/Workflows:5:1"
    handoff["handoff_id"] = stable_hash(
        "consumer-sync-shadow-handoff",
        {k: handoff[k] for k in handoff if k != "handoff_id"},
    )
    zbytes = make_zip(
        {"consumer-sync-plan.json": json.dumps(plan), "handoff.json": json.dumps(handoff)}
    )

    def mock_meta_fail(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps({"workflow_runs": [{"id": 5, "run_attempt": 1}]})
        if "runs/5/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 50,
                            "name": "consumer-sync-shadow-evidence-5-1",
                            "expired": False,
                        }
                    ]
                }
            )
        if "artifacts/50/zip" in cmd:
            return zbytes
        if cmd.endswith("repos/stranske/Template"):
            raise RuntimeError("meta down")
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_meta_fail)
    monkeypatch.setattr(ingest, "TEST_REGISTRY", ["stranske/template"])
    rc = ingest.main(
        [
            "ingest",
            "--state-dir",
            str(state_dir),
            "--ledger",
            str(ledger),
            "--repository",
            "stranske/Template",
            "--now",
            "100",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Failed to fetch metadata/default branch" in err

    def mock_tree_fail(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps({"workflow_runs": [{"id": 5, "run_attempt": 1}]})
        if "runs/5/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 50,
                            "name": "consumer-sync-shadow-evidence-5-1",
                            "expired": False,
                        }
                    ]
                }
            )
        if "artifacts/50/zip" in cmd:
            return zbytes
        if cmd.endswith("repos/stranske/Template"):
            return json.dumps({"default_branch": "main"})
        if "git/trees/" in cmd:
            raise RuntimeError("tree down")
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_tree_fail)
    rc = ingest.main(
        [
            "ingest",
            "--state-dir",
            str(tmp_path / "state-b"),
            "--ledger",
            str(ledger),
            "--repository",
            "stranske/Template",
            "--now",
            "100",
        ]
    )
    assert rc == 1
    assert "Failed to read tree" in capsys.readouterr().err


def test_main_defaults_argv_none_to_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest.sys, "argv", ["consumer_sync_artifact_ingest.py", "--selftest"])
    monkeypatch.setattr(ingest, "run_selftests", lambda: None)
    assert ingest.main(None) == 0


def test_invalid_repository_format_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingest,
        "GH_COMMAND_MOCK",
        lambda args: (
            json.dumps({"workflow_runs": [{"id": 1, "run_attempt": 1}]})
            if "runs?status=success" in " ".join(args)
            else json.dumps(
                {
                    "artifacts": [
                        {"id": 2, "name": "consumer-sync-shadow-evidence-1-1", "expired": False}
                    ]
                }
            )
        ),
    )
    monkeypatch.setattr(ingest, "TEST_REGISTRY", ["not a repo"])
    with pytest.raises(ValueError, match="Invalid repository format"):
        ingest.main(
            [
                "preview",
                "--state-dir",
                str(tmp_path / "s"),
                "--repository",
                "not a repo",
                "--now",
                "100",
            ]
        )


def test_zip_extraction_failure_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.json"
    capabilities.save({}, ledger)
    # Required members present but extraction will be forced to fail.
    zbytes = make_zip(
        {
            "consumer-sync-plan.json": "{}",
            "handoff.json": "{}",
        }
    )

    def mock_gh(args: list[str]) -> str | bytes:
        cmd = " ".join(args)
        if "runs?status=success" in cmd:
            return json.dumps({"workflow_runs": [{"id": 3, "run_attempt": 1}]})
        if "runs/3/artifacts" in cmd:
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": 30,
                            "name": "consumer-sync-shadow-evidence-3-1",
                            "expired": False,
                        }
                    ]
                }
            )
        if "artifacts/30/zip" in cmd:
            return zbytes
        raise AssertionError(cmd)

    monkeypatch.setattr(ingest, "GH_COMMAND_MOCK", mock_gh)
    monkeypatch.setattr(ingest, "TEST_REGISTRY", ["stranske/template"])
    monkeypatch.setattr(
        ingest, "safe_extract_zip", lambda *a, **k: (_ for _ in ()).throw(ValueError("unsafe"))
    )
    assert (
        ingest.main(
            [
                "ingest",
                "--state-dir",
                str(tmp_path / "s"),
                "--ledger",
                str(ledger),
                "--repository",
                "stranske/Template",
                "--now",
                "100",
            ]
        )
        == 1
    )
    assert "Zip extraction failed/unsafe" in capsys.readouterr().err
