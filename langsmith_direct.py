#!/usr/bin/env python3
"""Direct LangSmith API -> Orchestrator feedback store.

This is the cost/trace source that actually works today. The GitHub-artifact
producer chain (langsmith_fetch.py + Workflows reusable CI upload) is starved:
no consumer CI step writes `artifacts/langsmith/langsmith-fleet.ndjson`, and the
consumer *runtime* fleet records carry no cost and no orchestrator-joinable ref.

LangSmith itself, however, holds the real agent-automation telemetry: the
`workflows-agents` project records carry `total_cost`, `total_tokens`, and
metadata `repo` + `pr_number`/`issue_number`. We pull those, shape them into the
`langsmith-fleet/v1` records that langsmith_pull already knows how to join (via
the github_pr/github_issue bridge), and reuse langsmith_pull.ingest_files for the
durable join + write. Writes are idempotent (costs PK=run_id, traces PK=trace_key).

Usage:
  python3 langsmith_direct.py --dry-run --json
  python3 langsmith_direct.py --ingest --json
  python3 langsmith_direct.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import langsmith_pull
import feedback

ORCH_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = Path.home() / ".codex" / "orchestrator" / "langsmith-artifacts"
DEFAULT_PROJECTS = ["workflows-agents"]
# workflows-agents runs ~42/day (measured 2026-06-19, 376 runs / 9 days). A 30-day window with a
# generous cap covers that volume with headroom AND spans typical multi-day PR lifetimes, so per-PR
# cost sums stay complete under idempotent replace (costs PK=run_id). A shorter window would
# undercount long-lived PRs as their older LLM calls age out of the pulled set.
DEFAULT_LIMIT = 3000
DEFAULT_SINCE_HOURS = 24 * 30
CRED_PATH = Path.home() / ".codex" / "credentials" / "langsmith_api_key"
SCHEMA_VERSION = "langsmith-fleet/v1"
API_MAX_PER_REQUEST = 100
DEFAULT_API_URL = "https://api.smith.langchain.com"
RUN_SELECT_FIELDS = [
    "app_path",
    "completion_tokens",
    "end_time",
    "extra",
    "id",
    "prompt_tokens",
    "run_type",
    "start_time",
    "status",
    "total_cost",
    "total_tokens",
    "trace_id",
]


def load_api_key() -> str | None:
    key = (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY") or "").strip()
    if key:
        return key
    if CRED_PATH.exists():
        return CRED_PATH.read_text().strip() or None
    return None


def _make_client():
    key = load_api_key()
    if not key:
        raise RuntimeError(
            f"no LANGSMITH_API_KEY in env or {CRED_PATH} " "(extract from Code/Numbers/values.txt)"
        )
    os.environ.setdefault("LANGCHAIN_API_KEY", key)
    try:
        from langsmith import Client

        return Client(api_key=key)
    except ModuleNotFoundError as exc:
        if exc.name != "langsmith":
            raise
        return _StdlibLangSmithClient(api_key=key)


def _api_url() -> str:
    return (
        (
            os.environ.get("LANGSMITH_ENDPOINT")
            or os.environ.get("LANGCHAIN_ENDPOINT")
            or DEFAULT_API_URL
        )
        .strip()
        .strip('"')
        .strip("'")
        .rstrip("/")
    )


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _host_url(api_url: str) -> str:
    parsed = urlparse.urlparse(api_url)
    if parsed.netloc == "api.smith.langchain.com":
        return "https://smith.langchain.com"
    if parsed.netloc == "beta.api.smith.langchain.com":
        return "https://beta.smith.langchain.com"
    if parsed.netloc.startswith("api."):
        return f"{parsed.scheme}://{parsed.netloc[4:]}"
    return api_url


class _StdlibRun:
    def __init__(self, raw: dict[str, Any], *, host_url: str):
        self._host_url = host_url.rstrip("/")
        for key, value in raw.items():
            if key in {"start_time", "end_time", "first_token_time"}:
                value = _parse_dt(value)
            setattr(self, key, value)
        if not getattr(self, "trace_id", None):
            self.trace_id = getattr(self, "id", None)

    @property
    def url(self) -> str | None:
        app_path = getattr(self, "app_path", None)
        if isinstance(app_path, str) and app_path:
            return f"{self._host_url}{app_path}"
        return None


class _StdlibLangSmithClient:
    """Small read-only LangSmith client used when the SDK is not installed."""

    def __init__(self, *, api_key: str, api_url: str | None = None):
        self.api_key = api_key
        self.api_url = (api_url or _api_url()).rstrip("/")
        self.host_url = _host_url(self.api_url)
        self._project_ids: dict[str, str] = {}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        query = f"?{urlparse.urlencode(params)}" if params else ""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlrequest.Request(
            f"{self.api_url}{path}{query}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"LangSmith HTTP {exc.code} for {method} {path}: {detail}") from exc
        if not payload:
            return None
        return json.loads(payload)

    def _project_id(self, project_name: str) -> str:
        if project_name not in self._project_ids:
            result = self._request_json(
                "GET",
                "/sessions",
                params={"limit": 1, "name": project_name, "include_stats": "false"},
            )
            if not isinstance(result, list) or not result:
                raise RuntimeError(f"Project {project_name} not found")
            self._project_ids[project_name] = str(result[0]["id"])
        return self._project_ids[project_name]

    def list_runs(
        self,
        *,
        project_name: str | Iterable[str] | None = None,
        start_time: datetime | None = None,
        **kwargs: Any,
    ):
        project_ids: list[str] = []
        if project_name is not None:
            names = [project_name] if isinstance(project_name, str) else list(project_name)
            project_ids = [self._project_id(str(name)) for name in names]
        body: dict[str, Any] = {
            "session": project_ids or None,
            "start_time": start_time.isoformat() if start_time else None,
            "select": kwargs.get("select") or RUN_SELECT_FIELDS,
        }
        body = {k: v for k, v in body.items() if v is not None}
        while True:
            response = self._request_json("POST", "/runs/query", body=body)
            if not isinstance(response, dict):
                return
            runs = response.get("runs") or []
            for run in runs:
                if isinstance(run, dict):
                    yield _StdlibRun(run, host_url=self.host_url)
            next_cursor = (response.get("cursors") or {}).get("next")
            if not next_cursor:
                return
            body["cursor"] = next_cursor


def _ref_num(value: Any) -> str | None:
    if value in (None, "", "null", "unknown"):
        return None
    s = str(value).strip()
    return s if s.isdigit() else None


def run_to_record(run: Any) -> dict[str, Any] | None:
    """Shape one LangSmith run into a langsmith-fleet/v1 record, or None if unusable."""
    extra = getattr(run, "extra", None) or {}
    meta = extra.get("metadata") or {} if isinstance(extra, dict) else {}
    repo = meta.get("repo")
    if not repo:
        return None
    operation = str(meta.get("operation") or getattr(run, "run_type", "llm"))
    try:
        operation_role = feedback.derive_operation_role(operation, meta.get("operation_role"))
    except ValueError:
        return None
    rec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo),
        "surface": str(meta.get("surface") or "agent-automation"),
        "operation": operation,
        "operation_role": operation_role,
        "run_id": str(meta.get("run_id") or getattr(run, "id", "") or ""),
        "status": str(meta.get("status") or "success"),
        "domain": {},
        "trace_id": str(getattr(run, "trace_id", None) or getattr(run, "id", "")) or None,
    }
    pr = _ref_num(meta.get("pr_number"))
    iss = _ref_num(meta.get("issue_number") or meta.get("issue_or_pr_number"))
    if pr:
        rec["github_pr"] = f"{repo}#{pr}"
    elif iss:
        rec["github_issue"] = f"{repo}#{iss}"
    else:
        return None  # no orchestrator-joinable bridge -> drop (would never join)
    if meta.get("agent"):
        rec["agent"] = str(meta["agent"]).lower()
    cost = getattr(run, "total_cost", None)
    if cost:
        rec["cost_usd"] = float(cost)
    pt, ct, tt = (
        getattr(run, "prompt_tokens", None),
        getattr(run, "completion_tokens", None),
        getattr(run, "total_tokens", None),
    )
    if pt is not None:
        rec["tokens_in"] = int(pt or 0)
    if ct is not None:
        rec["tokens_out"] = int(ct or 0)
    if tt and "tokens_in" not in rec:
        rec["total_tokens"] = int(tt)
    st, en = getattr(run, "start_time", None), getattr(run, "end_time", None)
    if st and en:
        rec["latency_ms"] = (en - st).total_seconds() * 1000.0
    if meta.get("model"):
        rec["model"] = str(meta["model"])
        if operation_role != "worker" or meta.get("resolved_model"):
            rec["resolved_model"] = str(meta.get("resolved_model") or meta["model"])
    if meta.get("ls_provider"):
        rec["provider"] = str(meta["ls_provider"])
        rec["resolved_provider"] = str(meta["ls_provider"])
    for key in (
        "profile_id",
        "requested_provider",
        "requested_model",
        "resolved_provider",
        "resolved_model",
        "fallback_reason",
    ):
        if meta.get(key) is not None:
            rec[key] = str(meta[key])
    if meta.get("attempt") is not None:
        rec["attempt_ordinal"] = meta["attempt"]
    trace_url = getattr(run, "url", None)
    if trace_url:
        rec["trace_url"] = str(trace_url)
    return rec


def fetch_records(
    *,
    client: Any = None,
    projects: Iterable[str] = DEFAULT_PROJECTS,
    limit: int = DEFAULT_LIMIT,
    since_hours: int | None = DEFAULT_SINCE_HOURS,
) -> tuple[list[dict[str, Any]], list[str]]:
    client = client or _make_client()
    start_time = None
    if since_hours:
        start_time = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for project in projects:
        try:
            kwargs: dict[str, Any] = {"project_name": project}
            if start_time is not None:
                kwargs["start_time"] = start_time
            runs = islice(client.list_runs(**kwargs), limit)
            for run in runs:
                rec = run_to_record(run)
                if rec is not None:
                    records.append(rec)
        except Exception as exc:  # network/auth/project-missing: record, continue
            errors.append(f"{project}: {exc}")
    return records, errors


def fetch_and_ingest(
    *,
    client: Any = None,
    projects: Iterable[str] = DEFAULT_PROJECTS,
    limit: int = DEFAULT_LIMIT,
    since_hours: int | None = DEFAULT_SINCE_HOURS,
    dry_run: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    records, errors = fetch_records(
        client=client, projects=list(projects), limit=limit, since_hours=since_hours
    )
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "projects": list(projects),
        "records_built": len(records),
        "with_cost": sum(1 for r in records if "cost_usd" in r),
        "with_github_ref": sum(1 for r in records if r.get("github_pr") or r.get("github_issue")),
        "errors": errors,
        "combined": None,
    }
    if records:
        output_dir.mkdir(parents=True, exist_ok=True)
        combined = output_dir / "langsmith-direct.ndjson"
        combined.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n", encoding="utf-8"
        )
        summary["combined"] = str(combined)
        summary["ingest"] = langsmith_pull.ingest_files(
            [combined], dry_run=dry_run, source="langsmith"
        )
    else:
        summary["ingest"] = {
            "records_read": 0,
            "matched_records": 0,
            "written_cost_records": 0,
            "written_trace_records": 0,
        }
    return summary


# ---- offline selftest --------------------------------------------------------


class _FakeRun:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeClient:
    def __init__(self, runs):
        self._runs = runs

    def list_runs(self, project_name=None, **kw):
        return iter(self._runs)


def _selftest():
    import shutil
    import tempfile
    import feedback

    tmp = Path(tempfile.mkdtemp(prefix="langsmith-direct-selftest-"))
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp / "orchestrator.db"
    try:
        feedback.record_run(
            "keepalive:stranske/Workflows#2479:codex",
            "stranske/Workflows#2479",
            "implement",
            "codex",
            mode="remote",
            pr_number=2479,
        )
        t0 = datetime(2026, 6, 19, tzinfo=timezone.utc)
        runs = [
            _FakeRun(
                id="ls-1",
                run_type="llm",
                total_cost="0.0337",
                prompt_tokens=4000,
                completion_tokens=719,
                total_tokens=4719,
                start_time=t0,
                end_time=t0 + timedelta(seconds=2),
                url="https://smith.langchain.com/r/ls-1",
                extra={
                    "metadata": {
                        "repo": "stranske/Workflows",
                        "pr_number": "2479",
                        "operation": "verifier",
                        "model": "gpt-x",
                        "ls_provider": "openai",
                        "run_id": "27852902373",
                    }
                },
            ),
            _FakeRun(
                id="ls-2",
                run_type="llm",
                total_cost="0.011",
                prompt_tokens=2000,
                completion_tokens=300,
                total_tokens=2300,
                start_time=t0,
                end_time=t0 + timedelta(seconds=1),
                extra={
                    "metadata": {
                        "repo": "stranske/Workflows",
                        "pr_number": "2479",
                        "operation": "verifier",
                    }
                },
            ),
            _FakeRun(
                id="ls-3",
                run_type="llm",
                total_cost="0.5",
                extra={"metadata": {"repo": "stranske/Workflows", "pr_number": "999999"}},
            ),  # unmatched PR
            _FakeRun(
                id="ls-4", run_type="chain", extra={"metadata": {"operation": "noop"}}
            ),  # no repo -> dropped
        ]
        client = _FakeClient(runs)

        recs, errs = fetch_records(client=client, since_hours=None, limit=10)
        assert errs == [], errs
        assert len(recs) == 3, recs  # ls-4 dropped (no repo)
        assert sum(1 for r in recs if "cost_usd" in r) == 3, recs
        assert {r["operation_role"] for r in recs} == {"verifier", "unknown"}, recs

        dry = fetch_and_ingest(
            client=client, since_hours=None, dry_run=True, output_dir=tmp / "out"
        )
        assert dry["records_built"] == 3, dry
        assert dry["ingest"]["matched_records"] == 2, dry["ingest"]  # both #2479 records
        assert dry["ingest"]["cost_records"] == 1, dry["ingest"]  # aggregated to 1 run
        with feedback._conn() as c:
            assert (
                c.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0
            )  # dry-run wrote nothing

        live = fetch_and_ingest(
            client=client, since_hours=None, dry_run=False, output_dir=tmp / "out"
        )
        assert live["ingest"]["written_cost_records"] == 1, live["ingest"]
        with feedback._conn() as c:
            row = c.execute(
                "SELECT tokens_in, tokens_out, cost_usd, source FROM costs "
                "WHERE run_id='keepalive:stranske/Workflows#2479:codex'"
            ).fetchone()
            attempt_roles = dict(
                c.execute(
                    "SELECT operation_role,COUNT(*) FROM execution_attempts "
                    "GROUP BY operation_role"
                ).fetchall()
            )
        # 4000+2000 in, 719+300 out, 0.0337+0.011 cost, joined by github_pr bridge
        assert (
            row[:2] == (6000, 1019) and abs(row[2] - 0.0447) < 1e-9 and row[3] == "langsmith"
        ), row
        assert attempt_roles == {"verifier": 2}, attempt_roles
        assert (
            feedback.resolved_worker_model_for_run("keepalive:stranske/Workflows#2479:codex")
            is None
        )
        # idempotency: re-ingest replaces (does not inflate)
        fetch_and_ingest(client=client, since_hours=None, dry_run=False, output_dir=tmp / "out")
        with feedback._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 1
            again = c.execute(
                "SELECT cost_usd FROM costs "
                "WHERE run_id='keepalive:stranske/Workflows#2479:codex'"
            ).fetchone()
        assert abs(again[0] - 0.0447) < 1e-9, again
        print(
            "langsmith_direct.py selftest: OK (run->record shape, github_pr bridge join, "
            "validated operation roles, evaluator-safe attribution, cost aggregation, dry-run, "
            "idempotent re-ingest)"
        )
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def _print(summary: dict[str, Any], *, as_json: bool):
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return
    ing = summary.get("ingest", {})
    verb = "would write" if summary["dry_run"] else "wrote"
    print(
        f"langsmith_direct: built {summary['records_built']} record(s) "
        f"({summary['with_cost']} with cost), matched {ing.get('matched_records', 0)}, "
        f"{verb} {ing.get('written_cost_records', ing.get('cost_records', 0))} cost row(s), "
        f"{ing.get('written_trace_records', ing.get('trace_records', 0))} trace row(s)"
    )
    for e in summary["errors"]:
        print(f"- error: {e}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", action="append", default=[], help="LangSmith project (repeatable)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--since-hours", type=int, default=DEFAULT_SINCE_HOURS)
    p.add_argument("--all-time", action="store_true", help="ignore --since-hours")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--ingest", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if not (args.dry_run or args.ingest):
        p.error("pass --dry-run or --ingest")

    summary = fetch_and_ingest(
        projects=args.project or DEFAULT_PROJECTS,
        limit=args.limit,
        since_hours=None if args.all_time else args.since_hours,
        dry_run=args.dry_run,
    )
    _print(summary, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
