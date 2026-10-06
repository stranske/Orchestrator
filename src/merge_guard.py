#!/usr/bin/env python3
"""Guard terminal PR merges: exact head, review floor, runtime-AC gate, advisory panel.

Default mode is dry-run. Active mode requires --confirm-merge, and required
runtime-AC gates still require ORCH_RUN_RUNTIME_AC=1 before any checks run.

THE CLOSER-PR REVIEW HOOKS LIVE HERE (2026-10-05). The tick ran the runtime-AC gate and the
adversarial panel before a remote delegation that no closer item can reach, so nothing they said
could change any outcome. The decision a closer PR actually faces is this one, its terminal merge.
The runtime-AC gate blocks it as before. A high-stakes PR also gets the adversarial panel, judged
once per exact head (`adversarial.review_at_head`) and ADVISORY, as the owner set it: its verdict
and blockers are reported beside the merge decision for the merger to verify against ground truth,
and it never blocks. It runs only when ORCH_RUN_ADVERSARIAL_REVIEW=1, the flag the tick read; with
the flag off, a verdict already recorded for this head is still reported.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import exact_head_merge_gate
import feedback
import outcomes
import provision
import runtime_ac_gate

MERGE_METHOD_FLAGS = {
    "squash": "--squash",
    "merge": "--merge",
    "rebase": "--rebase",
}


def build_merge_cmd(
    target: str,
    *,
    method: str = "squash",
    delete_branch: bool = False,
    expected_head: str | None = None,
) -> list[str]:
    repo, num = provision.parse_target(target)
    if num is None:
        raise ValueError(f"merge target must be a PR ref owner/repo#N: {target!r}")
    if method not in MERGE_METHOD_FLAGS:
        raise ValueError(f"unknown merge method {method!r}")
    cmd = ["gh", "pr", "merge", str(num), "-R", repo, MERGE_METHOD_FLAGS[method]]
    if delete_branch:
        cmd.append("--delete-branch")
    if expected_head:
        cmd.extend(["--match-head-commit", expected_head])
    return cmd


def _label_names(labels: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(labels, list):
        return out
    for label in labels:
        if isinstance(label, dict) and label.get("name"):
            out.append(str(label["name"]))
        elif isinstance(label, str):
            out.append(label)
    return out


def pr_metadata(target: str, *, run_fn=subprocess.run) -> dict[str, Any]:
    repo, num = provision.parse_target(target)
    if num is None:
        return {"target": target, "error": "target does not contain a PR number"}
    cmd = [
        "gh",
        "pr",
        "view",
        str(num),
        "-R",
        repo,
        "--json",
        "title,labels,state,isDraft,mergeStateStatus,closingIssuesReferences",
    ]
    res = run_fn(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"target": target, "error": (res.stderr or "gh pr view failed")[-500:]}
    try:
        doc = json.loads(res.stdout or "{}")
    except Exception as exc:
        return {"target": target, "error": f"could not parse gh pr view JSON: {exc}"}
    closing = []
    for ref in doc.get("closingIssuesReferences") or []:
        if not isinstance(ref, dict) or not ref.get("number"):
            continue
        owner = ((ref.get("repository") or {}).get("owner") or {}).get("login")
        name = (ref.get("repository") or {}).get("name")
        closing.append(
            f"{owner}/{name}#{ref['number']}" if owner and name else f"{repo}#{ref['number']}"
        )
    return {
        "target": target,
        "title": doc.get("title") or "",
        "labels": _label_names(doc.get("labels")),
        "state": doc.get("state"),
        "is_draft": bool(doc.get("isDraft")),
        "merge_state_status": doc.get("mergeStateStatus"),
        "closing_issues": closing,
    }


def source_issue_labels(closing: list[str], *, run_fn=subprocess.run) -> dict:
    """The labels of the issues a PR closes. Risk metadata lives on ISSUES: no PR in the fleet
    carries a `risk:*` label (backlog.build_backlog says why), so the high-stakes rule reads these.
    `{"labels": [...]}` when every issue answered, `{"labels": None, "error": ...}` otherwise: a
    label set some issue did not answer for is unknown, never empty."""
    labels: list[str] = []
    for ref in closing:
        repo, num = provision.parse_target(ref)
        res = run_fn(["gh", "api", f"repos/{repo}/issues/{num}"], capture_output=True, text=True)
        try:
            doc = json.loads(res.stdout or "{}") if res.returncode == 0 else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            return {"labels": None, "error": f"{ref}: {(res.stderr or 'unreadable')[-200:]}"}
        labels.extend(_label_names(doc.get("labels")))
    return {"labels": sorted(set(labels))}


def evaluate_merge_gate(
    target: str,
    *,
    dry_run: bool,
    env: dict | None = None,
    spec_dir: str | Path | None = None,
    require_runtime_ac: bool = False,
    metadata_fn=pr_metadata,
    gate_fn=runtime_ac_gate.gate_status,
) -> dict[str, Any]:
    meta = metadata_fn(target)
    base = {
        "target": target,
        "dry_run": dry_run,
        "metadata": meta,
        "gate": None,
        "blocked": False,
        "reason": None,
    }
    if meta.get("error"):
        return {**base, "blocked": True, "reason": f"could not read PR metadata: {meta['error']}"}
    if str(meta.get("state") or "").upper() != "OPEN":
        return {**base, "blocked": True, "reason": f"PR state is {meta.get('state')!r}, not OPEN"}
    if meta.get("is_draft"):
        return {**base, "blocked": True, "reason": "PR is draft"}

    labels = list(meta.get("labels") or [])
    if require_runtime_ac and "runtime-ac" not in {label.lower() for label in labels}:
        labels.append("runtime-ac")
    item = {
        "target": target,
        "task_type": "implement",
        "lane": "closer",
        "labels": labels,
        "title": meta.get("title") or "",
    }
    gate = gate_fn(item, dry_run=dry_run, env=env, spec_dir=spec_dir)
    result = {**base, "gate": gate}
    if gate is None:
        return result
    if gate.get("blocks"):
        return {**result, "blocked": True, "reason": f"runtime AC gate {gate.get('status')}"}
    if dry_run and gate.get("status") == "missing_spec":
        return {
            **result,
            "blocked": True,
            "reason": "runtime AC gate would need a spec before active merge",
        }
    return result


def adversarial_review_status(
    target: str,
    meta: Mapping[str, Any],
    *,
    head: str | None,
    env: Mapping[str, str] | None = None,
    run_fn=subprocess.run,
    **review_kwargs: Any,
) -> dict[str, Any]:
    """The advisory panel at this merge: `routine`, `unknown` (the source-issue labels the rule
    needs did not answer), `no_head`, `required_but_not_run` (flag off and nothing recorded for this
    head), or a verdict, `reused` for this exact head or `executed` now. Never blocks.
    `review_kwargs` reach `adversarial.review_at_head` (its worktree and test seams)."""
    import adversarial

    source = source_issue_labels(list(meta.get("closing_issues") or []), run_fn=run_fn)
    pr_labels: list[str] = list(meta.get("labels") or [])
    source_labels: list[str] = list(source.get("labels") or [])
    title = str(meta.get("title") or "")
    item = {
        "target": target,
        "lane": "closer",
        "labels": pr_labels,
        "source_labels": source_labels,
        "title": title,
    }
    reason = adversarial.high_stakes_reason(item)
    if not reason:
        if source.get("labels") is None:
            return {
                "status": "unknown",
                "detail": f"source-issue labels unread ({source.get('error')}); high stakes "
                "undetermined, so no panel was considered",
            }
        return {"status": "routine"}
    if not head:
        return {
            "status": "no_head",
            "reason": reason,
            "detail": "pass --expected-head: a panel verdict is recorded per exact head",
        }
    env = os.environ if env is None else env
    reviewers = adversarial.reviewers_from_env(env)
    if not adversarial.review_enabled(env):
        recorded = adversarial.recorded_verdict(target, head, reviewers)
        if recorded.get("state") == "found":
            return {"reason": reason, "status": "reused", **recorded}
        return {
            "status": "required_but_not_run",
            "reason": reason,
            "memo": recorded.get("state"),
            "detail": "high-stakes PR with no panel verdict at this head; set "
            "ORCH_RUN_ADVERSARIAL_REVIEW=1, or run: python3 src/adversarial.py review "
            f"--target {target} --head {head}",
        }
    labels = ", ".join(pr_labels + source_labels) or "(none)"
    context = f"High-stakes PR {target}: {title}. Reason: {reason}. Labels: {labels}."
    out = adversarial.review_at_head(
        target, head, reviewers=reviewers, context=context, env=env, **review_kwargs
    )
    return {"reason": reason, **out}


def adjudicate_disagreement(
    target: str,
    gate: Mapping[str, Any] | None,
    panel: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None,
    dry_run: bool,
    activate_fn=None,
) -> dict[str, Any] | None:
    """The adjudicator role on a genuine disagreement between the two verdicts this merge holds: an
    executed runtime-AC gate and a CONCLUSIVE panel verdict. None when either is missing or they
    agree, so a routine merge records nothing. An inconclusive panel is a shortfall to re-run, not
    a disagreement. Shadow, as everywhere: its advice blocks nothing."""
    import adversarial
    import roles

    verdict = str(panel.get("verdict") or "").upper()
    if verdict not in adversarial.CONCLUSIVE_VERDICTS:
        return None
    item = {"target": target, "lane": "closer"}
    review = {"result": {**(panel.get("result") or {}), "verdict": verdict}}
    if roles.adjudication_case_for_disagreement(item, dict(gate or {}), review) is None:
        return None
    import router

    activate = activate_fn or roles.activate_adjudicator_disagreement
    try:
        cap = router.load_capacity()
    except Exception:
        cap = {}
    out = activate(item, dict(gate or {}), review, cap, env=env, dry_run=dry_run)
    return {
        "selector": out.get("selector"),
        "case": out.get("case"),
        "role_run_id": (out.get("result") or {}).get("role_run_id"),
    }


def record_merge_outcome(
    target: str,
    *,
    remote_runs_fn=feedback.runs_for_target,
    record_outcome_fn=feedback.record_outcome,
) -> dict[str, Any]:
    """Credit the merge to the latest remote run on `target` that the merge itself can credit.

    A keepalive run IS its PR, so the PR merging is its PASS: outcome ingest applies the same rule.
    A remote DELEGATION is credited only with its own PR and a completed round of the delegated
    agent's runner since the label, which only ingest reads (`outcomes._delegated_pr_state`). So a
    delegation is never credited here. Ingest decides only a run with NO outcome row: it never
    re-decides a recorded one, whether merged and pending durability or terminal. So the report
    names two lists, each empty when there is none and None when the runs could not be read:
    `deferred_to_ingest`, the delegations ingest will decide, and `delegations_already_recorded`,
    the ones neither side touches (a PR that closed, so ingest ended its delegation, then reopened
    and merged). Until 2026-10-04 the latest remote run was credited whatever its source."""
    # Nothing after a merge may raise: the caller must still see that the merge ran.
    try:
        runs = remote_runs_fn(target, mode="remote")
        delegations = [run for run in runs if outcomes.needs_delegation_guard(run.get("source"))]
        report = {
            "deferred_to_ingest": [
                run["run_id"] for run in delegations if not run.get("has_outcome")
            ],
            "delegations_already_recorded": [
                run["run_id"] for run in delegations if run.get("has_outcome")
            ],
        }
        creditable = [
            run["run_id"] for run in runs if not outcomes.needs_delegation_guard(run.get("source"))
        ]
    except Exception as exc:
        return {
            "recorded": False,
            "error": str(exc),
            "deferred_to_ingest": None,
            "delegations_already_recorded": None,
        }
    if not creditable:
        reason = (
            "no remote run_id found for target that the merge can credit; it credits no remote "
            "delegation, which only outcome ingest's attribution guard decides"
            if delegations
            else "no remote run_id found for target"
        )
        return {"recorded": False, "reason": reason, **report}
    run_id = creditable[0]
    try:
        record_outcome_fn(
            run_id,
            adjudicated_verdict="PASS",
            merged=True,
            durability="pending",
            notes="merge_guard: gh pr merge succeeded; durability pending sweep",
        )
    except Exception as exc:
        return {"recorded": False, "run_id": run_id, "error": str(exc), **report}
    return {"recorded": True, "run_id": run_id, **report}


def _preflight_block_reason(preflight: Any) -> str | None:
    if not isinstance(preflight, dict) or not isinstance(preflight.get("blocked"), bool):
        return "exact-head preflight returned a malformed result"
    if preflight["blocked"]:
        reason = preflight.get("reason")
        return reason if isinstance(reason, str) and reason else "exact-head preflight blocked"
    return None


def guarded_merge(
    target: str,
    *,
    method: str = "squash",
    delete_branch: bool = False,
    expected_head: str | None = None,
    confirm_merge: bool = False,
    env: dict | None = None,
    spec_dir: str | Path | None = None,
    require_runtime_ac: bool = False,
    metadata_fn=pr_metadata,
    gate_fn=runtime_ac_gate.gate_status,
    merge_fn=subprocess.run,
    remote_runs_fn=feedback.runs_for_target,
    record_outcome_fn=feedback.record_outcome,
    preflight_fn=exact_head_merge_gate.snapshot,
    panel_fn=None,
    adjudicate_fn=None,
) -> dict[str, Any]:
    dry_run = not confirm_merge
    if confirm_merge and not expected_head:
        return {
            "target": target,
            "dry_run": dry_run,
            "blocked": True,
            "reason": "--expected-head is required for an active merge",
            "merge_executed": False,
        }
    preflight = None
    if expected_head:
        preflight = preflight_fn(target, expected_head=expected_head)
        preflight_reason = _preflight_block_reason(preflight)
        if preflight_reason is not None:
            return {
                "target": target,
                "dry_run": dry_run,
                "blocked": True,
                "reason": preflight_reason,
                "preflight": preflight,
                "merge_executed": False,
            }
    cmd = build_merge_cmd(
        target, method=method, delete_branch=delete_branch, expected_head=expected_head
    )
    gate_result = evaluate_merge_gate(
        target,
        dry_run=dry_run,
        env=env,
        spec_dir=spec_dir,
        require_runtime_ac=require_runtime_ac,
        metadata_fn=metadata_fn,
        gate_fn=gate_fn,
    )
    result = {
        **gate_result,
        "merge_cmd": cmd,
        "merge_executed": False,
        "merge_returncode": None,
    }
    if gate_result["blocked"]:
        return result
    # Advisory, after every deterministic gate has passed, so no panel is paid for a PR that
    # cannot merge. Nothing it returns, and nothing it raises, may block or stop this merge.
    try:
        panel = (panel_fn or adversarial_review_status)(
            target, gate_result["metadata"], head=expected_head, env=env
        )
    except Exception as exc:
        panel = {"status": "error", "error": str(exc)[:500]}
    result["adversarial_review"] = panel
    try:
        adjudication = (adjudicate_fn or adjudicate_disagreement)(
            target, gate_result.get("gate"), panel, env=env, dry_run=dry_run
        )
    except Exception as exc:
        adjudication = {"error": str(exc)[:500]}
    if adjudication is not None:
        result["adjudication"] = adjudication
    if dry_run:
        return result

    final_preflight = preflight_fn(target, expected_head=expected_head)
    result["final_preflight"] = final_preflight
    final_preflight_reason = _preflight_block_reason(final_preflight)
    if final_preflight_reason is not None:
        result["blocked"] = True
        result["reason"] = final_preflight_reason
        return result

    merge = merge_fn(cmd, capture_output=True, text=True)
    result["merge_executed"] = True
    result["merge_returncode"] = merge.returncode
    result["stdout_tail"] = (merge.stdout or "")[-1000:]
    result["stderr_tail"] = (merge.stderr or "")[-1000:]
    if merge.returncode != 0:
        result["blocked"] = True
        result["reason"] = "gh pr merge failed"
        return result
    result["outcome"] = record_merge_outcome(
        target,
        remote_runs_fn=remote_runs_fn,
        record_outcome_fn=record_outcome_fn,
    )
    return result


def _selftest() -> None:
    """Every case runs against a disposable Brain, state dir and spec dir.

    Until 2026-10-05 these cases ran the real runtime-AC gate against the LIVE Brain: 735 runs from
    2026-08-21 wrote 3,702 gate events for the fixtures `o/r#5` and `o/r#6`, and the runtime-AC flow
    monitor reported them as live firing (51 PASS, 204 required in one 72-hour window) and asked
    for specs at `o/r#5` and `o/r#6`, while the fleet's real executions were zero."""
    import tempfile

    saved = {key: os.environ.get(key) for key in ("ORCH_STATE_DIR", "ORCH_RUNTIME_AC_SPEC_DIR")}
    saved_db = feedback.DB_PATH
    with tempfile.TemporaryDirectory(prefix="merge-guard-sandbox-") as sandbox:
        feedback.DB_PATH = Path(sandbox) / "brain.db"
        os.environ["ORCH_STATE_DIR"] = sandbox
        os.environ["ORCH_RUNTIME_AC_SPEC_DIR"] = str(Path(sandbox) / "specs")
        try:
            _selftest_cases()
            _selftest_panel()
            leaked = feedback.runtime_ac_gate_events(cutoff_ts=0)
            assert leaked and all(e["target"].startswith("o/r#") for e in leaked), leaked
        finally:
            feedback.DB_PATH = saved_db
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print(
        "merge_guard.py selftest: OK (metadata, runtime AC gate, dry-run, guarded gh merge, "
        "outcome patch, delegation deferred to ingest, advisory panel once per head, "
        "conclusive-disagreement adjudication, disposable Brain)"
    )


def _selftest_cases() -> None:
    import tempfile

    assert build_merge_cmd("o/r#5") == ["gh", "pr", "merge", "5", "-R", "o/r", "--squash"]
    assert build_merge_cmd(
        "o/r#5", method="rebase", delete_branch=True, expected_head="abc123"
    ) == [
        "gh",
        "pr",
        "merge",
        "5",
        "-R",
        "o/r",
        "--rebase",
        "--delete-branch",
        "--match-head-commit",
        "abc123",
    ]

    def open_meta(target):
        return {
            "target": target,
            "labels": [],
            "title": "ready",
            "state": "OPEN",
            "is_draft": False,
        }

    def clean_preflight(target, *, expected_head):
        return {"target": target, "head": expected_head, "blocked": False, "reason": None}

    dry = guarded_merge("o/r#5", metadata_fn=open_meta)
    assert dry["merge_cmd"] and dry["merge_executed"] is False and dry["blocked"] is False, dry
    meta_fail = guarded_merge(
        "o/r#5", metadata_fn=lambda target: {"target": target, "error": "no auth"}
    )
    assert meta_fail["blocked"] is True and "metadata" in meta_fail["reason"], meta_fail
    draft = guarded_merge(
        "o/r#5", metadata_fn=lambda target: {**open_meta(target), "is_draft": True}
    )
    assert draft["blocked"] is True and draft["reason"] == "PR is draft", draft
    closed = guarded_merge(
        "o/r#5", metadata_fn=lambda target: {**open_meta(target), "state": "CLOSED"}
    )
    assert closed["blocked"] is True and "not OPEN" in closed["reason"], closed
    missing_state = guarded_merge(
        "o/r#5", metadata_fn=lambda target: {"target": target, "labels": []}
    )
    assert missing_state["blocked"] is True and "not OPEN" in missing_state["reason"], missing_state

    with tempfile.TemporaryDirectory(prefix="merge-guard-") as tmp:

        def runtime_meta(target):
            return {
                "target": target,
                "labels": ["runtime-ac"],
                "title": "runtime",
                "state": "OPEN",
                "is_draft": False,
            }

        missing = guarded_merge(
            "o/r#5",
            expected_head="abc123",
            confirm_merge=True,
            spec_dir=tmp,
            metadata_fn=runtime_meta,
            preflight_fn=clean_preflight,
            merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")),
        )
        assert missing["blocked"] is True and missing["gate"]["status"] == "missing_spec", missing

        path = runtime_ac_gate.spec_path("o/r#5", spec_dir=tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        # `_command_spec` is runtime_ac_gate's own exercise fixture: its verification.target is
        # pinned to stranske/Workflows#303, the target exercise_gate() uses. execute_gate() later
        # gained a spec-target/closer-target match guard, which made this fixture fail closed here
        # with `spec target ... does not match closer target 'o/r#5'` -- so the happy-path merge
        # assertion below could never be reached. Restate the target the way runtime_ac_gate's own
        # selftest does for its materialized spec. (2026-08-21)
        gate_spec = runtime_ac_gate._command_spec(
            str(Path(__file__).resolve().parent),
            f"{sys.executable} -c 'print(\"ok\")'",
        )
        gate_spec["verification"]["target"] = "o/r#5"
        path.write_text(json.dumps(gate_spec), encoding="utf-8")
        disabled = guarded_merge(
            "o/r#5",
            expected_head="abc123",
            confirm_merge=True,
            spec_dir=tmp,
            env={},
            metadata_fn=runtime_meta,
            preflight_fn=clean_preflight,
            merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")),
        )
        assert (
            disabled["blocked"] is True and disabled["gate"]["status"] == "required_but_not_run"
        ), disabled
        forced = guarded_merge(
            "o/r#6",
            expected_head="abc123",
            confirm_merge=True,
            spec_dir=tmp,
            env={},
            require_runtime_ac=True,
            metadata_fn=open_meta,
            preflight_fn=clean_preflight,
            merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")),
        )
        assert forced["blocked"] is True and forced["gate"]["status"] == "missing_spec", forced

        calls = []

        def fake_merge(cmd, capture_output=True, text=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")

        recorded = []
        passed = guarded_merge(
            "o/r#5",
            expected_head="abc123",
            confirm_merge=True,
            spec_dir=tmp,
            env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1"},
            metadata_fn=runtime_meta,
            preflight_fn=clean_preflight,
            merge_fn=fake_merge,
            remote_runs_fn=lambda target, mode=None: [
                {"run_id": "keepalive:o/r#5:codex", "source": "keepalive", "ts": 1}
            ],
            record_outcome_fn=lambda run_id, **kwargs: recorded.append((run_id, kwargs)),
        )
        assert passed["blocked"] is False and passed["merge_executed"] is True, passed
        assert calls and recorded and recorded[0][1]["durability"] == "pending", (calls, recorded)
        no_run = record_merge_outcome("o/r#5", remote_runs_fn=lambda target, mode=None: [])
        assert no_run["recorded"] is False and "no remote run_id" in no_run["reason"], no_run
        assert no_run["deferred_to_ingest"] == [], no_run

        # A remote delegation is never credited by the merge (outcome ingest's guard decides it),
        # even when it is the latest remote run; an older keepalive run on the PR is its PR's run.
        mixed = [
            {"run_id": "remote:o/r#5:gemini", "source": "orchestrator_remote", "ts": 2},
            {"run_id": "keepalive:o/r#5:codex", "source": "keepalive", "ts": 1},
        ]
        credited: list = []
        keep = record_merge_outcome(
            "o/r#5",
            remote_runs_fn=lambda target, mode=None: mixed,
            record_outcome_fn=lambda run_id, **kwargs: credited.append(run_id),
        )
        assert credited == ["keepalive:o/r#5:codex"], (keep, credited)
        assert keep["deferred_to_ingest"] == ["remote:o/r#5:gemini"], keep
        only = record_merge_outcome(
            "o/r#5",
            remote_runs_fn=lambda target, mode=None: mixed[:1],
            record_outcome_fn=lambda run_id, **kwargs: credited.append(run_id),
        )
        assert only["recorded"] is False and credited == ["keepalive:o/r#5:codex"], only
        assert only["deferred_to_ingest"] == ["remote:o/r#5:gemini"], only

        blocked = guarded_merge(
            "o/r#5",
            expected_head="abc123",
            confirm_merge=True,
            metadata_fn=runtime_meta,
            preflight_fn=clean_preflight,
            gate_fn=lambda item, **kwargs: {
                "target": item["target"],
                "status": "executed",
                "verdict": "FAIL",
                "blocks": True,
            },
            merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")),
        )
        assert (
            blocked["blocked"] is True and blocked["reason"] == "runtime AC gate executed"
        ), blocked

    parsed_meta = pr_metadata(
        "o/r#5",
        run_fn=lambda cmd, capture_output=True, text=True: subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "title": "T",
                    "state": "OPEN",
                    "isDraft": False,
                    "mergeStateStatus": "CLEAN",
                    "labels": [{"name": "runtime-ac"}],
                    "closingIssuesReferences": [
                        {"number": 4, "repository": {"name": "r", "owner": {"login": "o"}}},
                        {"number": 9},
                    ],
                }
            ),
            stderr="",
        ),
    )
    assert parsed_meta["labels"] == ["runtime-ac"] and parsed_meta["title"] == "T", parsed_meta
    assert parsed_meta["closing_issues"] == ["o/r#4", "o/r#9"], parsed_meta


def _selftest_panel() -> None:
    """The advisory panel at the terminal merge: high stakes read from the SOURCE issue, judged
    once per exact head, never blocking, and adjudicated only on a conclusive disagreement."""
    head = "a" * 40
    reviews: list[str] = []

    def issue_labels(*names):
        doc = {"labels": [{"name": name} for name in names]}
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(doc), "")

    def stub_panel(verdict):
        def run(worktree, reviewers, context):
            reviews.append(context)
            return {"verdict": verdict, "blockers": [{"severity": "high", "finding": "fails open"}]}

        return run

    def meta(target, closing=("o/r#40",), title="routine copy edit"):
        return {
            "target": target,
            "labels": ["agent:codex"],
            "title": title,
            "state": "OPEN",
            "is_draft": False,
            "closing_issues": list(closing),
        }

    def clean(target, *, expected_head):
        return {"target": target, "head": expected_head, "blocked": False, "reason": None}

    merged: list[list[str]] = []

    def fake_merge(cmd, capture_output=True, text=True):
        merged.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")

    on = {"ORCH_RUN_ADVERSARIAL_REVIEW": "1", "ORCH_ADVERSARIAL_REVIEWERS": "vibe,gemini"}

    def merge(verdict="BLOCKED", labels=("risk:major",), env=None, expected=head, **kw):
        def panel(target, metadata, *, head, env):
            return adversarial_review_status(
                target,
                metadata,
                head=head,
                env=env,
                run_fn=issue_labels(*labels),
                worktree="/wt/at-head",
                head_fn=lambda wt: head,
                review_fn=stub_panel(verdict),
            )

        return guarded_merge(
            "o/r#41",
            expected_head=expected,
            confirm_merge=expected is not None,
            env=on if env is None else env,
            metadata_fn=meta,
            gate_fn=lambda item, **kwargs: None,
            preflight_fn=clean,
            merge_fn=fake_merge,
            remote_runs_fn=lambda target, mode=None: [],
            panel_fn=panel,
            **kw,
        )

    # Risk lives on the SOURCE issue; the PR carries only its agent label. Advisory: it merges.
    first = merge()
    panel = first["adversarial_review"]
    assert panel["status"] == "executed" and panel["verdict"] == "BLOCKED", first
    assert panel["reason"] == "high-stakes label: risk:major", panel
    assert first["merge_executed"] is True and first["blocked"] is False, first
    assert "fails open" in panel["result"]["blockers"][0]["finding"], panel
    # The same head is judged once; the verdict and its findings come back from the record.
    again = merge(verdict="PASS")
    assert again["adversarial_review"]["status"] == "reused", again
    assert again["adversarial_review"]["verdict"] == "BLOCKED" and len(reviews) == 1, again
    # Flag off: a verdict recorded for this head is still reported, and nothing runs.
    quiet = merge(env={"ORCH_ADVERSARIAL_REVIEWERS": "vibe,gemini"})
    assert quiet["adversarial_review"]["status"] == "reused" and len(reviews) == 1, quiet
    # Flag off at a head with no verdict: required, named, with the one command that judges it.
    other = "b" * 40
    unjudged = merge(env={"ORCH_ADVERSARIAL_REVIEWERS": "vibe,gemini"}, expected=other)
    status = unjudged["adversarial_review"]
    assert status["status"] == "required_but_not_run" and status["memo"] == "none", status
    assert f"--head {other}" in status["detail"] and unjudged["merge_executed"], status
    # A dry run with no head cannot key a verdict, so it does not judge one.
    assert merge(expected=None)["adversarial_review"]["status"] == "no_head"
    # Routine work is named routine; unread source labels are unknown, never routine.
    assert merge(labels=("bug",))["adversarial_review"] == {"status": "routine"}
    unread = guarded_merge(
        "o/r#42",
        metadata_fn=meta,
        gate_fn=lambda item, **kwargs: None,
        panel_fn=lambda target, metadata, *, head, env: adversarial_review_status(
            target,
            metadata,
            head=head,
            env=env,
            run_fn=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "HTTP 502"),
        ),
    )
    assert unread["adversarial_review"]["status"] == "unknown", unread

    # A panel that raises is reported and the merge goes ahead: it is advisory.
    def boom(*a, **k):
        raise RuntimeError("provision failed")

    crashed = guarded_merge(
        "o/r#43",
        expected_head=head,
        confirm_merge=True,
        metadata_fn=meta,
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=clean,
        merge_fn=fake_merge,
        remote_runs_fn=lambda target, mode=None: [],
        panel_fn=boom,
    )
    assert crashed["adversarial_review"]["status"] == "error" and crashed["merge_executed"], crashed

    # Adjudication only on a CONCLUSIVE disagreement with an executed gate.
    called: list[tuple] = []

    def activate(item, gate, review, cap, **kwargs):
        called.append((gate.get("verdict"), review["result"]["verdict"]))
        return {"selector": {"selector_status": "matched_not_invoked"}, "case": {}, "result": None}

    passed_gate = {"status": "executed", "verdict": "PASS", "blocks": False}
    blocked_panel = {"status": "executed", "verdict": "BLOCKED", "result": {"blockers": []}}
    adjudicated = adjudicate_disagreement(
        "o/r#41", passed_gate, blocked_panel, env={}, dry_run=False, activate_fn=activate
    )
    assert adjudicated is not None, "a PASS gate against a BLOCKED panel is a disagreement"
    assert adjudicated["selector"]["selector_status"] == "matched_not_invoked", adjudicated
    assert called == [("PASS", "BLOCKED")], called
    for gate, panel in (
        (passed_gate, {"verdict": "PASS"}),
        (passed_gate, {"verdict": "INCONCLUSIVE"}),
        (None, blocked_panel),
        (passed_gate, {"status": "routine"}),
    ):
        assert (
            adjudicate_disagreement(
                "o/r#41", gate, panel, env={}, dry_run=False, activate_fn=activate
            )
            is None
        ), (gate, panel)
    assert len(called) == 1, called


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Guard gh pr merge with Orchestrator runtime AC gates."
    )
    parser.add_argument("target", nargs="?", help="PR target owner/repo#N")
    parser.add_argument("--method", choices=sorted(MERGE_METHOD_FLAGS), default="squash")
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument(
        "--expected-head",
        help="exact PR head SHA; required with --confirm-merge",
    )
    parser.add_argument("--confirm-merge", action="store_true", help="actually run gh pr merge")
    parser.add_argument(
        "--require-runtime-ac",
        action="store_true",
        help="force a runtime AC gate even without a runtime-ac label or spec",
    )
    parser.add_argument(
        "--json", action="store_true", help="accepted for consistency; output is always JSON"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if not args.target:
        parser.error("target is required unless --selftest is used")
    result = guarded_merge(
        args.target,
        method=args.method,
        delete_branch=args.delete_branch,
        expected_head=args.expected_head,
        confirm_merge=args.confirm_merge,
        require_runtime_ac=args.require_runtime_ac,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if not result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
