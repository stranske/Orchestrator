"""Read native closer-core CSV delivery evidence; record its real postmerge check.

The native worker and guarded publication already happened. The original bytes
are frozen when the current closer's evidence step runs; refreshing observations
never rewrites those source artifacts. This module grants no effect authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

REPO = "stranske/Orchestrator"
ISSUE = 988
PR = 989
TARGET = f"{REPO}#{ISSUE}"
KEY = f"{TARGET}@{PR}"
SCHEMA = "closer-core-completion/v1"
STATE = Path(os.environ.get("CLOSER_CORE_STATE", Path.home() / ".codex/orchestrator/closer-core"))
FIXTURE = "tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("native evidence object is malformed")
    return value


def _bytes(path, limit=16 * 1024 * 1024):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("native evidence exceeds read bound")
    return raw


def _gh(args):
    from switch_review import _gh_call

    ok, output, reason = _gh_call(args, timeout_s=60)
    if not ok:
        raise RuntimeError(reason)
    return output


def remote(gh=None, *, cache_path=None):
    gh = gh or _gh
    prefix = 'query { repository(owner:"stranske",name:"Orchestrator") { pullRequest(number:989) { '
    fields = "state headRefOid headRefName updatedAt mergedAt mergeCommit { oid }"

    def fetch(query):
        response = _object(json.loads(gh(["api", "graphql", "-f", "query=" + query])))
        if response.get("errors"):
            raise ValueError("native delivery remote read is incomplete")
        pr = _object(_object(response["data"])["repository"])["pullRequest"]
        if not isinstance(pr, dict):
            raise ValueError("native delivery remote state is unavailable")
        return pr

    if cache_path and Path(cache_path).exists():
        try:
            cached = json.loads(_bytes(cache_path))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict):
            latest = fetch(prefix + fields + " } } }")
            if all(
                cached.get(key) == latest.get(key)
                for key in ("state", "headRefOid", "headRefName", "updatedAt", "mergeCommit")
            ):
                return _publication(cached)
    pr = fetch(
        prefix + fields + " comments(first:100) { nodes { body url author { login } } "
        "pageInfo { hasNextPage } } } } }"
    )
    result = _publication(pr)
    if cache_path:
        _write(Path(cache_path), (json.dumps(pr, sort_keys=True) + "\n").encode())
    return result


def _publication(pr):
    comments = _object(pr["comments"])
    if _object(comments["pageInfo"])["hasNextPage"]:
        raise ValueError("native delivery comments exceed bounded read")
    if (
        pr["state"] != "MERGED"
        or not re.fullmatch(r"[0-9a-f]{40}", pr["headRefOid"])
        or not re.fullmatch(r"[0-9a-f]{40}", _object(pr["mergeCommit"])["oid"])
    ):
        raise ValueError("native delivery is not an exact pinned merge")
    marker = re.compile(
        r"^<!-- closer-core/v1 action=merge target=stranske/Orchestrator#989 head="
        + re.escape(pr["headRefOid"])
        + r" round=([0-9]{8}T[0-9]{6}Z) -->$"
    )
    nodes = comments["nodes"]
    if not isinstance(nodes, list):
        raise ValueError("native publication comments are malformed")
    for comment in nodes:
        _object(comment)
        if comment.get("author") is not None:
            _object(comment["author"])
    matches = [
        c
        for c in nodes
        if (c.get("author") or {}).get("login") == "stranske"
        and isinstance(c.get("body"), str)
        and marker.fullmatch(c["body"].strip())
    ]
    if len(matches) != 1:
        raise ValueError(
            "native exact-head guarded publication acknowledgment missing or ambiguous"
        )
    return pr, matches[0]


def worker_proof(raw, state, head, branch):
    """Require the native dispatch origin, successful push and terminal worker.

    A worker may create a new head. Its successful commit/push plus a subsequent
    exact-head read in that same terminal run proves publication of that head;
    demanding that it started on its newly created head would be impossible.
    """
    if _object(state).get("key") != KEY:
        raise ValueError("worker origin belongs to another native target")
    origin = _object(state.get("pr_repair"))
    if not origin.get("pid") or not origin.get("agents") or not origin.get("log"):
        raise ValueError("native corrective worker origin is incomplete")
    lines = raw.decode().splitlines()
    if not lines or not re.search(r"dispatch [^ ]+ -> stranske/Orchestrator#989 ", lines[0]):
        raise ValueError("native dispatch header belongs to another target")
    records = [json.loads(line) for line in lines if line.startswith("{")]
    typed = [r for r in records if "type" in r]
    if (
        not typed
        or typed[-1].get("type") != "turn.completed"
        or records[-1] != {"released": True, "target": f"{REPO}#{PR}"}
    ):
        raise ValueError("native worker terminal result is missing")
    commands = [
        r["item"]
        for r in records
        if r.get("type") == "item.completed"
        and _object(r.get("item")).get("type") == "command_execution"
        and r["item"].get("exit_code") == 0
    ]
    pushes = [
        index
        for index, command in enumerate(commands)
        if "git push" in command.get("command", "")
        and any(
            head.startswith(match.group(1))
            for match in re.finditer(
                r"\b([0-9a-f]{4,40})\s+HEAD -> " + re.escape(branch) + r"(?:\s|$)",
                command.get("aggregated_output", ""),
            )
        )
    ]
    observed = bool(pushes) and any(
        "git push" not in command.get("command", "")
        and head in command.get("aggregated_output", "")
        for command in commands[pushes[0] + 1 :]
    )
    if not observed:
        raise ValueError("native worker did not publish and read back the pinned delivery head")
    return {"pid": origin["pid"], "agents": origin["agents"], "origin_log": origin["log"]}


def _path(root):
    return Path(root) / "completions" / "stranske_orchestrator_988_989.json"


def read_completion(*, root=STATE, gh=None):
    path = _path(root)
    record = _object(json.loads(_bytes(path)))
    if record.get("schema") != SCHEMA or record.get("target") != KEY:
        raise ValueError("native completion receipt identity differs")
    pr, marker = remote(gh, cache_path=path.with_suffix(".observation.json"))
    if (
        record["head"] != pr["headRefOid"]
        or record["merge"] != pr["mergeCommit"]["oid"]
        or record["publication_url"] != marker["url"]
    ):
        raise ValueError("native completion receipt differs from current publication")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", record.get(key, ""))
        for key in ("worker_sha256", "origin_sha256")
    ):
        raise ValueError("native source digest is invalid")
    evidence = path.parent / "evidence"
    raw = _bytes(evidence / record["worker_sha256"])
    state_raw = _bytes(evidence / record["origin_sha256"])
    if digest(raw) != record["worker_sha256"] or digest(state_raw) != record["origin_sha256"]:
        raise ValueError("frozen native source evidence changed")
    worker_proof(raw, json.loads(state_raw), record["head"], pr["headRefName"])
    verification = _object(record["verification"])
    transcript = _object(json.loads(verification.get("transcript", "{}")))
    gate = _object(transcript.get("gate"))
    checks = transcript.get("check_results")
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        raise ValueError("native postmerge checks are malformed")
    if (
        verification.get("result") != "PASS"
        or record.get("verification_engine") != "runtime_ac_gate.recheck_merged"
        or record.get("verified_merge") != record["merge"]
        or transcript.get("target") != f"{REPO}#{PR}"
        or transcript.get("confirmed_run") is not True
        or gate.get("verdict") != "PASS"
        or not any(
            c.get("id") == "CSV-CONTRACT"
            and c.get("status") == "PASS"
            and c.get("type") == "command"
            and c.get("returncode") == 0
            and c.get("stdout_tail") == "contract ok\n"
            and isinstance(c.get("command"), str)
            and c.get("command", "").endswith(f" -I {FIXTURE}")
            for c in checks
        )
    ):
        raise ValueError("native postmerge verification is incomplete")
    return record


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_completion(*, state_root=STATE, output_root=STATE, gh=None):
    """Called by native closer-core's evidence step, never by the measuring query."""
    if _path(output_root).exists():
        return read_completion(root=output_root, gh=gh)
    pr, marker = remote(gh)
    state_raw = _bytes(Path(state_root) / "targets/stranske_orchestrator_988_989.json")
    state = _object(json.loads(state_raw))
    log = Path(_object(state["pr_repair"])["log"])
    if log.resolve().parent != (Path.home() / ".codex/handoff/dispatch-logs").resolve():
        raise ValueError("worker log is outside the native dispatch source")
    raw = _bytes(log)
    worker = worker_proof(raw, state, pr["headRefOid"], pr["headRefName"])
    import provision
    import runtime_ac
    import runtime_ac_gate

    spec = {
        "verification": {
            "id": "native-closer-csv-988",
            "title": "Native CSV contract",
            "target": f"{REPO}#{PR}",
            "repo": REPO,
            "goal": "Independently execute the delivered CSV contract",
            "risk_level": "low",
        },
        "runtime_context": {"worktree": "."},
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": False,
            "fail_on_missing_checks": True,
            "required_check_ids": ["CSV-CONTRACT"],
            "min_pass_ratio": 1.0,
        },
        "acceptance_criteria": [
            {
                "id": "CSV",
                "statement": "The delivered CSV contract passes",
                "evidence_required": ["command_output"],
                "checks": [
                    {
                        "id": "CSV-CONTRACT",
                        "type": "command",
                        "name": "Explicit standard-library CSV contract",
                        "command": f"python3 -I {FIXTURE}",
                        "expected": "exit_0",
                    }
                ],
            }
        ],
    }
    errors = runtime_ac.validate_spec(spec)
    if errors:
        raise ValueError("native acceptance specification: " + "; ".join(errors))
    with provision.temporary_exact_head_worktree(
        f"{REPO}#{PR}", pr["mergeCommit"]["oid"]
    ) as worktree:
        verification = runtime_ac_gate.recheck_merged(spec, worktree, allow_command_checks=True)
    record = {
        "schema": SCHEMA,
        "target": KEY,
        "head": pr["headRefOid"],
        "merge": pr["mergeCommit"]["oid"],
        "publication_url": marker["url"],
        "worker_sha256": digest(raw),
        "origin_sha256": digest(state_raw),
        "worker": worker,
        "verification_engine": "runtime_ac_gate.recheck_merged",
        "verification": verification,
        "verified_merge": pr["mergeCommit"]["oid"],
        "completed_at": int(time.time()),
        "acceptance_authority": False,
    }
    if verification.get("result") != "PASS":
        raise ValueError("native runtime acceptance stopped: " + json.dumps(verification)[:1200])
    evidence = _path(output_root).parent / "evidence"
    _write(evidence / record["worker_sha256"], raw)
    _write(evidence / record["origin_sha256"], state_raw)
    _write(_path(output_root), (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return read_completion(root=output_root, gh=gh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        record = read_completion()
        print(json.dumps({"completion": "verified", **record}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"completion": "incomplete", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
