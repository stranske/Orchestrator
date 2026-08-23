#!/usr/bin/env python3
"""Fetch LangSmith fleet artifacts from GitHub Actions and optionally ingest them.

The durable join lives in langsmith_pull.py. This module supplies the missing source
and cadence piece: read the Workflows fleet registry, download each repo's latest
`langsmith-fleet.ndjson` artifact, concatenate all NDJSON records into local disk,
then optionally ingest that combined file into the feedback DB.

Usage:
  python3 langsmith_fetch.py --dry-run --json
  python3 langsmith_fetch.py --ingest --json
  python3 langsmith_fetch.py --selftest
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import langsmith_pull

ORCH_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = ORCH_DIR.parent / "Workflows" / "config" / "langsmith_fleet_registry.json"
DEFAULT_OUTPUT_DIR = Path.home() / ".codex" / "orchestrator" / "langsmith-artifacts"
DEFAULT_ARTIFACT_NAME = "langsmith-fleet.ndjson"
DEFAULT_REGISTRY_REPO = "stranske/Workflows"
DEFAULT_REGISTRY_CONTENT_PATH = "config/langsmith_fleet_registry.json"
DEFAULT_ROLLUP_REPO = "stranske/Workflows"
DEFAULT_ROLLUP_PREFIX = "langsmith-fleet-rollup-"
ARTIFACT_DISTRIBUTION_SCHEMA_VERSION = "langsmith-artifact-distribution/v1"
DEFAULT_PRODUCER_WORKFLOW_PATHS = {".github/workflows/ci.yml"}
DEFAULT_PRODUCER_WORKFLOW_NAMES = {"CI"}
NON_ARTIFACT_ROLLOUT_STATUSES = {
    "covered-via-langsmith-direct",
    "paused",
    "contract-owner",
    "not-applicable",
}


@dataclass
class GhResult:
    returncode: int
    stdout: str | bytes
    stderr: str | bytes = ""


Runner = Callable[[list[str], bool], GhResult]


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


DEFAULT_ARTIFACT_LOOKUP_PAGES = _env_int("ORCH_LANGSMITH_ARTIFACT_LOOKUP_PAGES", 50)
DEFAULT_MISSING_RUN_LOOKUP_LIMIT = _env_int("ORCH_LANGSMITH_MISSING_RUN_LOOKUP_LIMIT", 20)
DEFAULT_MISSING_RUN_DETAIL_LIMIT = _env_int("ORCH_LANGSMITH_MISSING_RUN_DETAIL_LIMIT", 5)


def _runner(cmd: list[str], text: bool) -> GhResult:
    proc = subprocess.run(cmd, capture_output=True, text=text)
    return GhResult(proc.returncode, proc.stdout, proc.stderr)


def _repo_slug(repo: str) -> str:
    return repo.replace("/", "__").replace(":", "_")


def _load_registry(path: Path, runner: Runner = _runner) -> list[dict[str, Any]]:
    if path.exists():
        data = json.loads(path.read_text())
    elif path == DEFAULT_REGISTRY:
        api = f"repos/{DEFAULT_REGISTRY_REPO}/contents/{quote(DEFAULT_REGISTRY_CONTENT_PATH, safe='/')}"
        payload = _gh_json(api, runner=runner)
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise FileNotFoundError(
                f"{path} is missing and GitHub registry content was unavailable"
            )
        data = json.loads(base64.b64decode(encoded).decode("utf-8"))
    else:
        raise FileNotFoundError(path)
    repos = data.get("repos")
    if not isinstance(repos, list):
        raise ValueError(f"{path} has no repos[] list")
    out = []
    for row in repos:
        if not isinstance(row, dict) or not row.get("repo"):
            continue
        out.append(row)
    return out


def _gh_throttle(resource: str) -> None:
    """Pace/defer against the shared GitHub rate budget (gh_capacity) when ORCH_GH_THROTTLE=1;
    no-op + fail-open otherwise so the fetch never breaks on a missing/erroring module.
    """
    try:
        import gh_capacity

        gh_capacity.throttle_if_enabled(resource)
    except Exception:
        pass


def _gh_json(api_path: str, runner: Runner = _runner) -> dict[str, Any]:
    _gh_throttle("core")  # gh api = CORE (5000/hr)
    res = runner(["gh", "api", api_path], True)
    if res.returncode != 0:
        err = res.stderr.decode() if isinstance(res.stderr, bytes) else str(res.stderr)
        raise RuntimeError(err.strip() or f"gh api failed: {api_path}")
    stdout = res.stdout.decode() if isinstance(res.stdout, bytes) else res.stdout
    return json.loads(stdout or "{}")


def _gh_bytes(api_path_or_url: str, runner: Runner = _runner) -> bytes:
    _gh_throttle("core")  # gh api artifact download = CORE (5000/hr)
    res = runner(["gh", "api", api_path_or_url], False)
    if res.returncode != 0:
        err = res.stderr.decode() if isinstance(res.stderr, bytes) else str(res.stderr)
        raise RuntimeError(err.strip() or f"gh api download failed: {api_path_or_url}")
    return res.stdout if isinstance(res.stdout, bytes) else res.stdout.encode()


def _artifact_name_candidates(artifact_name: str) -> list[str]:
    """Return artifact names accepted for the current producer transition.

    The registry contract is `langsmith-fleet.ndjson`. Workflows PR #2473 shipped
    a producer that uploads the file under the reusable CI artifact prefix
    (`gate-langsmith-fleet` historically, `gate-langsmith-fleet.ndjson` after the
    fallback writer landed), so the fetcher accepts those transitional artifact
    names while the producer/template contract is corrected.
    """
    stem = Path(artifact_name).stem
    candidates = [artifact_name]
    if artifact_name:
        candidates.append(f"gate-{artifact_name}")
    if stem:
        candidates.extend([stem, f"gate-{stem}"])

    out: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _artifact_expectation(row: dict[str, Any]) -> tuple[bool, str]:
    status = str(row.get("rollout_status") or "").strip().lower()
    if status in NON_ARTIFACT_ROLLOUT_STATUSES:
        return False, f"rollout_status={status}"
    return True, "per-repo artifact expected"


def _latest_artifact(
    repo: str, artifact_name: str, runner: Runner = _runner
) -> dict[str, Any] | None:
    lookup_names = _artifact_name_candidates(artifact_name)
    candidates: list[dict[str, Any]] = []
    for lookup_name in lookup_names:
        api = f"repos/{repo}/actions/artifacts?name={quote(lookup_name)}&per_page=20"
        payload = _gh_json(api, runner=runner)
        artifacts = payload.get("artifacts") or []
        candidates.extend(
            a
            for a in artifacts
            if isinstance(a, dict) and a.get("name") == lookup_name and not a.get("expired")
        )
    if not candidates:
        return None
    latest = sorted(
        candidates,
        key=lambda a: a.get("updated_at") or a.get("created_at") or "",
        reverse=True,
    )[0]
    latest = dict(latest)
    latest["_orch_requested_artifact_name"] = artifact_name
    latest["_orch_matched_artifact_name"] = latest.get("name")
    latest["_orch_candidate_names"] = lookup_names
    return latest


def _latest_artifact_by_prefix(
    repo: str,
    artifact_prefix: str,
    runner: Runner = _runner,
    max_pages: int = DEFAULT_ARTIFACT_LOOKUP_PAGES,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        api = f"repos/{repo}/actions/artifacts?per_page=100&page={page}"
        payload = _gh_json(api, runner=runner)
        artifacts = payload.get("artifacts") or []
        if not artifacts:
            break
        candidates.extend(
            a
            for a in artifacts
            if isinstance(a, dict)
            and isinstance(a.get("name"), str)
            and a["name"].startswith(artifact_prefix)
            and not a.get("expired")
        )
        if len(artifacts) < 100:
            break
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda a: a.get("updated_at") or a.get("created_at") or "",
        reverse=True,
    )[0]


def _workflow_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "path": run.get("path"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "html_url": run.get("html_url"),
    }


def _recent_actions_runs(
    repo: str,
    *,
    runner: Runner = _runner,
    limit: int = DEFAULT_MISSING_RUN_LOOKUP_LIMIT,
) -> list[dict[str, Any]]:
    payload = _gh_json(f"repos/{repo}/actions/runs?per_page={limit}", runner=runner)
    runs = payload.get("workflow_runs") or []
    return [_workflow_run_summary(run) for run in runs[:limit] if isinstance(run, dict)]


def _latest_producer_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for run in runs:
        path = str(run.get("path") or "")
        name = str(run.get("name") or "")
        if path in DEFAULT_PRODUCER_WORKFLOW_PATHS or name in DEFAULT_PRODUCER_WORKFLOW_NAMES:
            return run
    return None


def _producer_missing_reason(producer_run: dict[str, Any] | None) -> str:
    if not producer_run:
        return "artifact_lookup_empty_no_recent_producer_run"
    status = str(producer_run.get("status") or "").lower()
    if status != "completed":
        return "artifact_lookup_empty_producer_run_not_completed"
    conclusion = str(producer_run.get("conclusion") or "unknown").lower()
    if conclusion == "success":
        return "artifact_lookup_empty_after_successful_producer_run"
    return f"artifact_lookup_empty_producer_run_{conclusion}"


def _recent_run_details(
    runs: list[dict[str, Any]],
    producer_run: dict[str, Any] | None,
    *,
    limit: int = DEFAULT_MISSING_RUN_DETAIL_LIMIT,
) -> list[dict[str, Any]]:
    details = list(runs[:limit])
    if not producer_run:
        return details
    producer_id = producer_run.get("id")
    if all(row.get("id") != producer_id for row in details):
        details.append(producer_run)
    return details


def _missing_artifact_diagnostics(
    repo: str,
    *,
    runner: Runner = _runner,
) -> dict[str, Any]:
    try:
        recent_runs = _recent_actions_runs(repo, runner=runner)
    except Exception as exc:
        return {
            "missing_reason": "artifact_lookup_empty_actions_run_lookup_failed",
            "actions_run_lookup_error": str(exc),
            "recent_actions_run_count": None,
            "latest_actions_run": None,
            "latest_producer_run": None,
            "producer_missing_reason": None,
            "recent_actions_runs": [],
        }
    if not recent_runs:
        return {
            "missing_reason": "artifact_lookup_empty_no_recent_actions_runs",
            "actions_run_lookup_error": None,
            "recent_actions_run_count": 0,
            "latest_actions_run": None,
            "latest_producer_run": None,
            "producer_missing_reason": "artifact_lookup_empty_no_recent_producer_run",
            "recent_actions_runs": [],
        }
    producer_run = _latest_producer_run(recent_runs)
    return {
        "missing_reason": "artifact_lookup_empty_recent_actions_runs_visible",
        "actions_run_lookup_error": None,
        "recent_actions_run_count": len(recent_runs),
        "latest_actions_run": recent_runs[0],
        "latest_producer_run": producer_run,
        "producer_missing_reason": _producer_missing_reason(producer_run),
        "recent_actions_runs": _recent_run_details(recent_runs, producer_run),
    }


def _safe_extract_zip(zip_path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = Path(info.filename)
            if name.is_absolute() or ".." in name.parts:
                continue
            out = dest / name
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(out)
    return written


def _combine_ndjson(paths: list[Path], output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = 0
    with output_path.open("w", encoding="utf-8") as out:
        for path in paths:
            with path.open("r", encoding="utf-8") as src:
                for line in src:
                    text = line.strip()
                    if not text:
                        continue
                    out.write(text + "\n")
                    lines += 1
    return lines


def _artifact_distribution_health(summary: dict[str, Any]) -> dict[str, Any]:
    repos_checked = int(summary.get("repos_checked") or 0)
    artifacts_found = int(summary.get("artifacts_found") or 0)
    expected_repos = int(summary.get("repos_expected") or 0)
    exempted_repos = int(summary.get("repos_exempted") or 0)
    missing = summary.get("missing") or []
    artifacts = summary.get("artifacts") or []
    errors = summary.get("errors") or []
    rollup = summary.get("rollup") or {}
    rollup_found = bool(rollup.get("artifact_found"))
    expected_found = sum(
        1 for row in artifacts if isinstance(row, dict) and row.get("artifact_expected")
    )
    expected_missing = [
        row for row in missing if isinstance(row, dict) and row.get("artifact_expected")
    ]
    exempted_missing = [
        row for row in missing if isinstance(row, dict) and not row.get("artifact_expected")
    ]
    coverage = round(expected_found / expected_repos, 3) if expected_repos else None

    if repos_checked <= 0:
        status = "unknown"
        recommendation = (
            "Verify the LangSmith fleet registry is readable, then rerun "
            "langsmith_fetch.py --dry-run --json."
        )
    elif expected_repos <= 0:
        status = "healthy"
        recommendation = (
            "No registered repo currently expects a per-repo GitHub LangSmith artifact."
        )
    elif expected_found == expected_repos and not errors:
        status = "healthy"
        recommendation = "Per-repo LangSmith artifact distribution is healthy."
    elif expected_found == 0 and rollup_found:
        status = "rollup_only"
        recommendation = (
            "Per-repo LangSmith artifacts are still missing; rollup fallback is visible. "
            "Verify the consumer reusable CI upload step and artifact name after the next repo runs."
        )
    elif expected_found == 0:
        status = "dry"
        recommendation = (
            "No per-repo or rollup LangSmith artifacts are visible. Verify the producer upload step, "
            "artifact name/candidate aliases, and workflow retention before relying on GitHub artifact ingestion."
        )
    else:
        status = "partial"
        recommendation = (
            "Some repos publish LangSmith artifacts and some do not. Inspect missing_repos and align "
            "consumer workflow artifact names before treating artifact ingestion as complete."
        )

    rollup_artifact = rollup.get("artifact") if isinstance(rollup.get("artifact"), dict) else {}
    return {
        "schema_version": ARTIFACT_DISTRIBUTION_SCHEMA_VERSION,
        "status": status,
        "registry": summary.get("registry"),
        "registered_repos": repos_checked,
        "expected_repos": expected_repos,
        "exempted_repos": exempted_repos,
        "visible_artifacts_found": artifacts_found,
        "per_repo_artifacts_found": expected_found,
        "per_repo_artifacts_missing": len(expected_missing),
        "per_repo_coverage": coverage,
        "missing_expected_with_recent_runs": sum(
            1 for row in expected_missing if row.get("latest_actions_run")
        ),
        "missing_expected_with_recent_producer_runs": sum(
            1 for row in expected_missing if row.get("latest_producer_run")
        ),
        "missing_expected_without_recent_runs": sum(
            1
            for row in expected_missing
            if row.get("missing_reason") == "artifact_lookup_empty_no_recent_actions_runs"
        ),
        "missing_expected_diagnostic_errors": sum(
            1 for row in expected_missing if row.get("actions_run_lookup_error")
        ),
        "missing_repos": [
            {
                "repo": row.get("repo"),
                "artifact_name": row.get("artifact_name"),
                "candidate_names": row.get("candidate_names"),
                "rollout_status": row.get("rollout_status"),
                "artifact_expected": row.get("artifact_expected"),
                "artifact_expectation_reason": row.get("artifact_expectation_reason"),
                "missing_reason": row.get("missing_reason"),
                "recent_actions_run_count": row.get("recent_actions_run_count"),
                "latest_actions_run": row.get("latest_actions_run"),
                "latest_producer_run": row.get("latest_producer_run"),
                "producer_missing_reason": row.get("producer_missing_reason"),
                "actions_run_lookup_error": row.get("actions_run_lookup_error"),
            }
            for row in expected_missing
            if isinstance(row, dict)
        ],
        "exempted_missing_repos": [
            {
                "repo": row.get("repo"),
                "artifact_name": row.get("artifact_name"),
                "rollout_status": row.get("rollout_status"),
                "artifact_expected": row.get("artifact_expected"),
                "artifact_expectation_reason": row.get("artifact_expectation_reason"),
            }
            for row in exempted_missing
            if isinstance(row, dict)
        ],
        "rollup_artifact_found": rollup_found,
        "rollup_repo": rollup.get("repo"),
        "rollup_prefix": rollup.get("artifact_prefix"),
        "rollup_artifact": (
            {
                "name": rollup_artifact.get("name"),
                "id": rollup_artifact.get("id"),
                "updated_at": rollup_artifact.get("updated_at"),
            }
            if rollup_artifact
            else None
        ),
        "error_count": len(errors),
        "error_samples": [str(error) for error in errors[:3]],
        "recommendation": recommendation,
    }


def diagnose_artifact_distribution(
    registry: Path = DEFAULT_REGISTRY,
    *,
    repos: set[str] | None = None,
    rollup_repo: str | None = DEFAULT_ROLLUP_REPO,
    rollup_prefix: str | None = DEFAULT_ROLLUP_PREFIX,
    runner: Runner = _runner,
) -> dict[str, Any]:
    """Read-only health probe for per-repo LangSmith artifact distribution."""
    summary = fetch_registry(
        registry,
        DEFAULT_OUTPUT_DIR,
        repos=repos,
        dry_run=True,
        ingest=False,
        ingest_dry_run=True,
        rollup_repo=rollup_repo,
        rollup_prefix=rollup_prefix,
        runner=runner,
    )
    return summary.get("artifact_distribution") or _artifact_distribution_health(summary)


def fetch_registry(
    registry: Path = DEFAULT_REGISTRY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    repos: set[str] | None = None,
    dry_run: bool = False,
    ingest: bool = False,
    ingest_dry_run: bool = False,
    strict: bool = False,
    rollup_repo: str | None = DEFAULT_ROLLUP_REPO,
    rollup_prefix: str | None = DEFAULT_ROLLUP_PREFIX,
    runner: Runner = _runner,
) -> dict[str, Any]:
    rows = _load_registry(registry, runner=runner)
    if repos:
        rows = [row for row in rows if row["repo"] in repos]

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "registry": str(registry),
        "output_dir": str(output_dir),
        "repos_checked": 0,
        "repos_expected": 0,
        "repos_exempted": 0,
        "artifacts_found": 0,
        "downloaded": 0,
        "ndjson_files": [],
        "combined": None,
        "combined_lines": 0,
        "artifacts": [],
        "rollup": {
            "repo": rollup_repo,
            "artifact_prefix": rollup_prefix,
            "artifact_found": False,
            "used": False,
        },
        "missing": [],
        "errors": [],
    }

    if not rows:
        summary["errors"].append("registry selection matched no repos")
        summary["artifact_distribution"] = _artifact_distribution_health(summary)
        return summary

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    work_dir = output_dir / stamp
    ndjson_paths: list[Path] = []

    for row in rows:
        repo = str(row["repo"])
        artifact_name = str(row.get("artifact_name") or DEFAULT_ARTIFACT_NAME)
        artifact_expected, artifact_expectation_reason = _artifact_expectation(row)
        rollout_status = row.get("rollout_status")
        summary["repos_checked"] += 1
        if artifact_expected:
            summary["repos_expected"] += 1
        else:
            summary["repos_exempted"] += 1
        try:
            artifact = _latest_artifact(repo, artifact_name, runner=runner)
        except Exception as exc:
            summary["errors"].append(f"{repo}: artifact lookup failed: {exc}")
            continue
        if not artifact:
            missing_row = {
                "repo": repo,
                "artifact_name": artifact_name,
                "candidate_names": _artifact_name_candidates(artifact_name),
                "rollout_status": rollout_status,
                "artifact_expected": artifact_expected,
                "artifact_expectation_reason": artifact_expectation_reason,
            }
            if artifact_expected:
                missing_row.update(_missing_artifact_diagnostics(repo, runner=runner))
            summary["missing"].append(missing_row)
            continue
        summary["artifacts_found"] += 1
        art_id = artifact.get("id")
        archive_url = artifact.get("archive_download_url")
        summary["artifacts"].append(
            {
                "repo": repo,
                "artifact_name": artifact_name,
                "matched_artifact_name": artifact.get("_orch_matched_artifact_name")
                or artifact.get("name"),
                "candidate_names": artifact.get("_orch_candidate_names")
                or _artifact_name_candidates(artifact_name),
                "rollout_status": rollout_status,
                "artifact_expected": artifact_expected,
                "artifact_expectation_reason": artifact_expectation_reason,
                "id": art_id,
                "updated_at": artifact.get("updated_at") or artifact.get("created_at"),
            }
        )
        if dry_run:
            continue
        if not archive_url:
            summary["errors"].append(f"{repo}: artifact {art_id} has no archive_download_url")
            continue
        try:
            zip_bytes = _gh_bytes(str(archive_url), runner=runner)
            repo_dir = work_dir / _repo_slug(repo) / str(art_id or "latest")
            zip_path = repo_dir / "artifact.zip"
            repo_dir.mkdir(parents=True, exist_ok=True)
            zip_path.write_bytes(zip_bytes)
            extracted = _safe_extract_zip(zip_path, repo_dir / "extracted")
        except Exception as exc:
            summary["errors"].append(f"{repo}: artifact download/extract failed: {exc}")
            continue
        summary["downloaded"] += 1
        found = sorted(p for p in extracted if p.suffix == ".ndjson")
        ndjson_paths.extend(found)
        summary["ndjson_files"].extend(str(p) for p in found)

    try_rollup = bool(rollup_repo and rollup_prefix) and (
        summary["artifacts_found"] == 0 if dry_run else not ndjson_paths
    )
    if try_rollup:
        try:
            rollup = _latest_artifact_by_prefix(str(rollup_repo), str(rollup_prefix), runner=runner)
        except Exception as exc:
            summary["errors"].append(f"{rollup_repo}: rollup artifact lookup failed: {exc}")
            rollup = None
        if rollup:
            summary["rollup"]["artifact_found"] = True
            summary["rollup"]["artifact"] = {
                "name": rollup.get("name"),
                "id": rollup.get("id"),
                "updated_at": rollup.get("updated_at") or rollup.get("created_at"),
            }
            if not dry_run:
                archive_url = rollup.get("archive_download_url")
                if not archive_url:
                    summary["errors"].append(
                        f"{rollup_repo}: rollup artifact {rollup.get('id')} has no archive_download_url"
                    )
                else:
                    try:
                        zip_bytes = _gh_bytes(str(archive_url), runner=runner)
                        repo_dir = (
                            work_dir
                            / _repo_slug(str(rollup_repo))
                            / f"rollup-{rollup.get('id') or 'latest'}"
                        )
                        zip_path = repo_dir / "artifact.zip"
                        repo_dir.mkdir(parents=True, exist_ok=True)
                        zip_path.write_bytes(zip_bytes)
                        extracted = _safe_extract_zip(zip_path, repo_dir / "extracted")
                    except Exception as exc:
                        summary["errors"].append(
                            f"{rollup_repo}: rollup artifact download/extract failed: {exc}"
                        )
                    else:
                        summary["downloaded"] += 1
                        summary["rollup"]["used"] = True
                        found = sorted(p for p in extracted if p.suffix == ".ndjson")
                        ndjson_paths.extend(found)
                        summary["ndjson_files"].extend(str(p) for p in found)

    if ndjson_paths:
        combined = output_dir / "combined-fleet.ndjson"
        lines = _combine_ndjson(ndjson_paths, combined)
        summary["combined"] = str(combined)
        summary["combined_lines"] = lines
        if ingest:
            summary["ingest"] = langsmith_pull.ingest_files(
                [combined],
                dry_run=ingest_dry_run,
                strict=strict,
                source="langsmith",
            )
    elif ingest and not dry_run:
        summary["ingest"] = {
            "dry_run": ingest_dry_run,
            "files": [],
            "records_read": 0,
            "matched_records": 0,
            "errors": ["no NDJSON files downloaded"],
        }

    summary["artifact_distribution"] = _artifact_distribution_health(summary)
    return summary


def _print_summary(summary: dict[str, Any], *, as_json: bool):
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    action = "would download" if summary["dry_run"] else "downloaded"
    print(
        f"langsmith_fetch: checked {summary['repos_checked']} repo(s), found "
        f"{summary['artifacts_found']} artifact(s), {action} {summary['downloaded']} "
        f"archive(s), combined {summary['combined_lines']} line(s)"
    )
    if summary["combined"]:
        print(f"combined: {summary['combined']}")
    if summary["missing"]:
        print(f"missing: {len(summary['missing'])}")
    health = summary.get("artifact_distribution") or {}
    if health:
        print(
            "artifact_distribution: "
            f"status={health.get('status')} "
            f"per_repo={health.get('per_repo_artifacts_found')}/{health.get('expected_repos')} "
            f"registered={health.get('registered_repos')} "
            f"rollup={'yes' if health.get('rollup_artifact_found') else 'no'}"
        )
    if summary["errors"]:
        print("errors:")
        for error in summary["errors"]:
            print(f"- {error}")


def _fixture_zip(path: Path, files: dict[str, str]) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path.read_bytes()


def _selftest():
    tmp = Path(tempfile.mkdtemp(prefix="langsmith-fetch-selftest-"))
    try:
        registry = tmp / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "schema_version": "langsmith-fleet-registry/v1",
                    "repos": [
                        {
                            "repo": "stranske/One",
                            "artifact_name": "langsmith-fleet.ndjson",
                        },
                        {
                            "repo": "stranske/Two",
                            "artifact_name": "langsmith-fleet.ndjson",
                        },
                    ],
                }
            )
        )
        zip_one = _fixture_zip(
            tmp / "one.zip",
            {
                "langsmith-fleet.ndjson": json.dumps(
                    {"schema_version": "langsmith-fleet/v1", "run_id": "r1"}
                )
                + "\n",
                "notes.txt": "ignored\n",
            },
        )
        zip_two = _fixture_zip(
            tmp / "two.zip",
            {
                "nested/langsmith-fleet.ndjson": json.dumps(
                    {"schema_version": "langsmith-fleet/v1", "run_id": "r2"}
                )
                + "\n",
            },
        )
        zip_rollup = _fixture_zip(
            tmp / "rollup.zip",
            {
                "combined-fleet.ndjson": json.dumps(
                    {"schema_version": "langsmith-fleet/v1", "run_id": "rollup-1"}
                )
                + "\n",
            },
        )

        def fake_runner(cmd: list[str], text: bool) -> GhResult:
            key = cmd[2]
            if key.startswith("repos/stranske/One/actions/artifacts"):
                return GhResult(
                    0,
                    json.dumps(
                        {
                            "artifacts": [
                                {
                                    "id": 0,
                                    "name": "langsmith-fleet.ndjson",
                                    "expired": True,
                                    "updated_at": "2026-01-01T00:00:00Z",
                                    "archive_download_url": "https://api.fake/expired.zip",
                                },
                                {
                                    "id": 1,
                                    "name": "langsmith-fleet.ndjson",
                                    "expired": False,
                                    "updated_at": "2026-06-16T00:00:00Z",
                                    "archive_download_url": "https://api.fake/one.zip",
                                },
                            ]
                        }
                    ),
                )
            if key == (
                "repos/stranske/Two/actions/artifacts?" "name=langsmith-fleet.ndjson&per_page=20"
            ):
                return GhResult(0, json.dumps({"artifacts": []}))
            if key == (
                "repos/stranske/Two/actions/artifacts?"
                "name=gate-langsmith-fleet.ndjson&per_page=20"
            ):
                return GhResult(
                    0,
                    json.dumps(
                        {
                            "artifacts": [
                                {
                                    "id": 2,
                                    "name": "gate-langsmith-fleet.ndjson",
                                    "expired": False,
                                    "updated_at": "2026-06-15T00:00:00Z",
                                    "archive_download_url": "https://api.fake/two.zip",
                                },
                            ]
                        }
                    ),
                )
            if key == ("repos/stranske/Two/actions/artifacts?" "name=langsmith-fleet&per_page=20"):
                return GhResult(0, json.dumps({"artifacts": []}))
            if key == (
                "repos/stranske/Two/actions/artifacts?" "name=gate-langsmith-fleet&per_page=20"
            ):
                return GhResult(0, json.dumps({"artifacts": []}))
            if key == "https://api.fake/one.zip":
                return GhResult(0, zip_one)
            if key == "https://api.fake/two.zip":
                return GhResult(0, zip_two)
            return GhResult(1, "" if text else b"", f"unexpected gh call: {cmd}")

        dry = fetch_registry(registry, tmp / "out-dry", dry_run=True, runner=fake_runner)
        assert (
            dry["artifacts_found"] == 2 and dry["downloaded"] == 0 and dry["combined"] is None
        ), dry
        assert dry["artifacts"][1]["artifact_name"] == "langsmith-fleet.ndjson", dry
        assert dry["artifacts"][1]["matched_artifact_name"] == "gate-langsmith-fleet.ndjson", dry
        assert dry["artifacts"][1]["candidate_names"] == [
            "langsmith-fleet.ndjson",
            "gate-langsmith-fleet.ndjson",
            "langsmith-fleet",
            "gate-langsmith-fleet",
        ], dry
        assert dry["artifact_distribution"]["status"] == "healthy", dry["artifact_distribution"]
        assert dry["artifact_distribution"]["per_repo_coverage"] == 1.0, dry[
            "artifact_distribution"
        ]
        diagnosed = diagnose_artifact_distribution(registry, runner=fake_runner)
        assert diagnosed["status"] == "healthy" and diagnosed["registered_repos"] == 2, diagnosed

        summary = fetch_registry(registry, tmp / "out", runner=fake_runner)
        assert summary["repos_checked"] == 2 and summary["artifacts_found"] == 2, summary
        assert summary["downloaded"] == 2 and summary["combined_lines"] == 2, summary
        assert len(summary["ndjson_files"]) == 2, summary
        assert summary["rollup"]["used"] is False, summary
        lines = Path(summary["combined"]).read_text().splitlines()
        assert [json.loads(line)["run_id"] for line in lines] == ["r1", "r2"], lines

        old_default = DEFAULT_REGISTRY
        try:
            globals()["DEFAULT_REGISTRY"] = tmp / "missing-default-registry.json"

            def fallback_runner(cmd: list[str], text: bool) -> GhResult:
                key = cmd[2]
                if key.startswith(
                    "repos/stranske/Workflows/contents/config/langsmith_fleet_registry.json"
                ):
                    encoded = base64.b64encode(registry.read_bytes()).decode("ascii")
                    return GhResult(0, json.dumps({"content": encoded}))
                return fake_runner(cmd, text)

            fallback = fetch_registry(
                DEFAULT_REGISTRY,
                tmp / "out-fallback",
                dry_run=True,
                runner=fallback_runner,
            )
            assert fallback["repos_checked"] == 2 and fallback["artifacts_found"] == 2, fallback
        finally:
            globals()["DEFAULT_REGISTRY"] = old_default

        missing_registry = tmp / "missing-registry.json"
        missing_registry.write_text(
            json.dumps(
                {
                    "schema_version": "langsmith-fleet-registry/v1",
                    "repos": [
                        {
                            "repo": "stranske/Missing",
                            "artifact_name": "langsmith-fleet.ndjson",
                            "rollout_status": "implemented",
                        },
                        {
                            "repo": "stranske/DirectOnly",
                            "artifact_name": "langsmith-fleet.ndjson",
                            "rollout_status": "covered-via-langsmith-direct",
                        },
                    ],
                }
            )
        )

        def rollup_runner(cmd: list[str], text: bool) -> GhResult:
            key = cmd[2]
            if key.startswith("repos/stranske/Missing/actions/artifacts"):
                return GhResult(0, json.dumps({"artifacts": []}))
            if key == "repos/stranske/Missing/actions/runs?per_page=20":
                return GhResult(
                    0,
                    json.dumps(
                        {
                            "workflow_runs": [
                                {
                                    "id": 41,
                                    "name": "Auto-Label Issues",
                                    "path": ".github/workflows/agents-auto-label.yml",
                                    "event": "issues",
                                    "status": "completed",
                                    "conclusion": "success",
                                    "head_branch": "main",
                                    "head_sha": "abc123",
                                    "created_at": "2026-06-18T00:02:00Z",
                                    "updated_at": "2026-06-18T00:03:00Z",
                                    "html_url": "https://github.com/stranske/Missing/actions/runs/41",
                                },
                                {
                                    "id": 42,
                                    "name": "CI",
                                    "path": ".github/workflows/ci.yml",
                                    "event": "push",
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "head_branch": "main",
                                    "head_sha": "def456",
                                    "created_at": "2026-06-18T00:00:00Z",
                                    "updated_at": "2026-06-18T00:01:00Z",
                                    "html_url": "https://github.com/stranske/Missing/actions/runs/42",
                                },
                            ]
                        }
                    ),
                )
            if key.startswith("repos/stranske/DirectOnly/actions/artifacts"):
                return GhResult(0, json.dumps({"artifacts": []}))
            if key.startswith("repos/stranske/Workflows/actions/artifacts") and key.endswith(
                "page=1"
            ):
                return GhResult(
                    0,
                    json.dumps(
                        {
                            "artifacts": [
                                {
                                    "id": 99,
                                    "name": "unrelated-telemetry",
                                    "expired": False,
                                    "updated_at": "2026-06-17T00:00:00Z",
                                    "archive_download_url": "https://api.fake/unrelated.zip",
                                },
                            ]
                            * 100
                        }
                    ),
                )
            if key.startswith("repos/stranske/Workflows/actions/artifacts") and key.endswith(
                "page=2"
            ):
                return GhResult(
                    0,
                    json.dumps(
                        {
                            "artifacts": [
                                {
                                    "id": 3,
                                    "name": "langsmith-fleet-rollup-123",
                                    "expired": False,
                                    "updated_at": "2026-06-16T00:00:00Z",
                                    "archive_download_url": "https://api.fake/rollup.zip",
                                },
                            ]
                        }
                    ),
                )
            if key == "https://api.fake/rollup.zip":
                return GhResult(0, zip_rollup)
            return GhResult(1, "" if text else b"", f"unexpected gh call: {cmd}")

        rollup_summary = fetch_registry(missing_registry, tmp / "out-rollup", runner=rollup_runner)
        assert (
            rollup_summary["artifacts_found"] == 0 and rollup_summary["rollup"]["used"]
        ), rollup_summary
        assert rollup_summary["repos_checked"] == 2, rollup_summary
        assert rollup_summary["artifact_distribution"]["expected_repos"] == 1, rollup_summary
        assert rollup_summary["artifact_distribution"]["exempted_repos"] == 1, rollup_summary
        assert rollup_summary["rollup"]["artifact"]["id"] == 3, rollup_summary
        assert rollup_summary["artifact_distribution"]["status"] == "rollup_only", rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["rollup_artifact_found"] is True
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_repos"][0]["repo"]
            == "stranske/Missing"
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_repos"][0]["missing_reason"]
            == "artifact_lookup_empty_recent_actions_runs_visible"
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_repos"][0]["latest_actions_run"][
                "name"
            ]
            == "Auto-Label Issues"
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_repos"][0]["latest_producer_run"][
                "name"
            ]
            == "CI"
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_repos"][0]["producer_missing_reason"]
            == "artifact_lookup_empty_producer_run_failure"
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_expected_with_recent_runs"] == 1
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["missing_expected_with_recent_producer_runs"]
            == 1
        ), rollup_summary
        assert len(rollup_summary["artifact_distribution"]["missing_repos"]) == 1, rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["exempted_missing_repos"][0]["repo"]
            == "stranske/DirectOnly"
        ), rollup_summary
        assert (
            rollup_summary["artifact_distribution"]["exempted_missing_repos"][0][
                "artifact_expectation_reason"
            ]
            == "rollout_status=covered-via-langsmith-direct"
        ), rollup_summary
        assert rollup_summary["artifact_distribution"]["missing_repos"][0]["candidate_names"] == [
            "langsmith-fleet.ndjson",
            "gate-langsmith-fleet.ndjson",
            "langsmith-fleet",
            "gate-langsmith-fleet",
        ], rollup_summary
        rollup_lines = Path(rollup_summary["combined"]).read_text().splitlines()
        assert [json.loads(line)["run_id"] for line in rollup_lines] == ["rollup-1"], rollup_lines
        print(
            "langsmith_fetch.py selftest: OK (registry lookup, latest artifact selection, "
            "artifact-name aliases, GitHub registry fallback, expected/exempted repo classification, "
            "missing-repo Actions/producer diagnostics, rollup fallback, safe zip extract, NDJSON combine, dry-run)"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="limit fetch to a registry repo, repeatable",
    )
    parser.add_argument(
        "--no-rollup-fallback",
        action="store_true",
        help="disable fallback to the Workflows fleet rollup artifact",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="query latest artifacts without downloading or ingesting",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="ingest the combined NDJSON into feedback after download",
    )
    parser.add_argument(
        "--ingest-dry-run",
        action="store_true",
        help="parse and join the combined artifact without writing feedback",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero on fetch errors or strict ingest failures",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    summary = fetch_registry(
        args.registry,
        args.output_dir,
        repos=set(args.repo) if args.repo else None,
        dry_run=args.dry_run,
        ingest=args.ingest or args.ingest_dry_run,
        ingest_dry_run=args.ingest_dry_run or args.dry_run,
        strict=args.strict,
        rollup_repo=None if args.no_rollup_fallback else DEFAULT_ROLLUP_REPO,
        rollup_prefix=None if args.no_rollup_fallback else DEFAULT_ROLLUP_PREFIX,
    )
    _print_summary(summary, as_json=args.json)
    failed = bool(summary["errors"]) or bool(summary.get("ingest", {}).get("strict_failed"))
    return 2 if args.strict and failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
