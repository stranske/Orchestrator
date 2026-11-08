"""Local keepalive loop: give a LOCAL agent's PR the CI-feedback rounds hosted keepalive gives a hosted one.

Owner decision 2026-11-08: route local first and mirror the hosted loop locally (Workflows
docs/keepalive/GoalsAndPlumbing.md). Hosted keepalive re-runs the agent on every CI result with the task
list and progress injected, runs acceptance commands, measures productivity and rotates agents. Before
this module a local start was ONE pass. This rail adds the same four features for local agents:

1. CI feedback rounds: when CI reports on the pushed head, re-run the agent on the same branch with the
   failing checks and their error lines, up to ROUND_CAP rounds.
2. Task injection: Scope/Tasks/Acceptance of the source issue and "Progress: k/n" in every round's prompt;
   checkboxes in the source issue (the task of record) are ticked from evidence — the agent's
   `LOOP-TASKS-DONE:` claim counts only for a round that pushed a head whose CI is green, acceptance boxes
   when the acceptance commands pass.
3. Acceptance gate: on a green head the issue's acceptance commands run on that exact head
   (keepalive_supervisor._closure_gate) before the loop calls the PR done.
4. Productivity + rotation: a round is productive if it pushed, ticked a box, or turned CI red -> green;
   STALL_ROUNDS unproductive rounds in a row rotate to an untried agent (router.select_agent).

ONE owner per PR: the label `agents:local-loop` marks a PR this loop iterates; such a PR never carries
`agents:keepalive` (hosted keepalive needs `agent:*` AND `agents:keepalive`). Every way out of the loop is
motion, not silence: done (closer-core merges the green PR), or HAND-OFF to hosted keepalive (label swap)
when the round cap is reached, no untried agent is left, or no local slot frees within CAPACITY_WAIT.

GitHub cost: one GraphQL search per tick for every looped PR (head, check rollup, source issue). A PR
whose head and rollup were already acted on costs nothing more. REST is used only for failing-job logs
on a new failing head (max MAX_FAIL_JOBS) and for label/issue writes, and never while the shared REST
reset (~/.codex/orchestrator/github-rest-reset.json) is in the future.

Usage: local_loop.py tick [--apply] | status [--json]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

LABEL = "agents:local-loop"
HOSTED_LABELS = ("agents:keepalive", "autofix")
MARKER = "<!-- local-loop/v1 -->"
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator"))
STATE_DIR = Path(os.environ.get("ORCH_LOCAL_LOOP_STATE", LOCAL_RUNTIME / "local-loop"))
HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
DISPATCH_LOG_DIR = HANDOFF / "dispatch-logs"
REST_RESET = Path(os.environ.get("LANE_REST_RESET", str(LOCAL_RUNTIME / "github-rest-reset.json")))
OWNER = os.environ.get("LOCAL_LOOP_OWNER", "stranske")

ROUND_CAP = 12  # hosted keepalive's default max_iterations (GoalsAndPlumbing §3)
STALL_ROUNDS = 2  # unproductive rounds in a row before rotating the agent
MAX_AGENTS = 3  # agents tried on one PR before the loop hands it to hosted keepalive
CAPACITY_WAIT = 2 * 3600  # a due round waits this long for a local slot, then hands off
NO_CI_WAIT = 3600  # no check reported on a head this long after its round: hand off
FRESH = 2 * 3600  # closer-core honours the loop's ownership only while its state is this fresh
MAX_FAIL_JOBS = 3
LOG_LINES = 40
START_ERRORS = 3  # consecutive failed round starts before hand-off
PENDING = {None, "PENDING", "EXPECTED"}
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED"}
PIN_RULE = (
    "Never edit dependency pins: `.github/workflows/autofix-versions.env`, `requirements.lock`, "
    "`*.lock`, or version pins in `pyproject.toml`. They have ONE writer "
    "(maint-auto-update-pypi-versions.yml); if a pin blocks you, say so in your final message."
)

SEARCH = """query($q:String!,$after:String){search(query:$q,type:ISSUE,first:40,after:$after){
pageInfo{hasNextPage endCursor} nodes{... on PullRequest{number state isDraft headRefName headRefOid
body repository{nameWithOwner} labels(first:40){nodes{name}}
closingIssuesReferences(first:3){nodes{number title state body}}
commits(last:1){nodes{commit{oid committedDate statusCheckRollup{state contexts(first:80){nodes{__typename
... on CheckRun{name status conclusion detailsUrl}
... on StatusContext{context state targetUrl}}}}}}}}}}}"""


# --------------------------------------------------------------------------- GitHub I/O


def rest_blocked(now: float | None = None) -> int | None:
    """The shared REST reset epoch if it is still in the future (another lane saw REST exhausted)."""
    try:
        reset = int(json.loads(REST_RESET.read_text())["reset"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return reset if reset > (now or time.time()) else None


def _gh(args: list[str], *, rest: bool, timeout: int = 60, stdin: str | None = None) -> str:
    if rest and rest_blocked():
        raise RuntimeError("REST allowance exhausted (shared reset in the future); GraphQL only")
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    done = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout, input=stdin, env=env
    )
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


class GitHub:
    """The loop's GitHub surface (tests replace it)."""

    def looped_prs(self) -> list[dict]:
        q = f"is:pr is:open label:{LABEL} user:{OWNER}"
        out, after = [], None
        while True:
            args = ["api", "graphql", "-f", f"query={SEARCH}", "-f", f"q={q}"]
            if after:
                args += ["-f", f"after={after}"]
            data = json.loads(_gh(args, rest=False))["data"]["search"]
            out += [n for n in data["nodes"] if n]
            if not data["pageInfo"]["hasNextPage"]:
                return out
            after = data["pageInfo"]["endCursor"]

    def merged_prs(self) -> list[dict]:
        q = f"is:pr is:merged label:{LABEL} user:{OWNER}"
        query = (
            "query($q:String!){search(query:$q,type:ISSUE,first:100){nodes{... on PullRequest{"
            "number mergedAt repository{nameWithOwner}}}}}"
        )
        data = json.loads(
            _gh(["api", "graphql", "-f", f"query={query}", "-f", f"q={q}"], rest=False)
        )
        return [n for n in data["data"]["search"]["nodes"] if n]

    def job_log(self, repo: str, job_id: str) -> str:
        return _gh(["api", f"repos/{repo}/actions/jobs/{job_id}/logs"], rest=True, timeout=60)

    def set_issue_body(self, repo: str, number: int, body: str) -> None:
        _gh(
            ["api", "-X", "PATCH", f"repos/{repo}/issues/{number}", "--input", "-"],
            rest=True,
            stdin=json.dumps({"body": body}),
        )

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if labels:
            _gh(
                ["api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "--input", "-"],
                rest=True,
                stdin=json.dumps({"labels": labels}),
            )

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            _gh(["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{label}"], rest=True)
        except RuntimeError as exc:
            if "Label does not exist" not in str(exc) and "404" not in str(exc):
                raise

    def ready(self, repo: str, number: int) -> None:
        _gh(["pr", "ready", str(number), "-R", repo], rest=False)

    def comment(self, repo: str, number: int, body: str) -> None:
        _gh(
            ["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
            rest=True,
            stdin=json.dumps({"body": body}),
        )


# --------------------------------------------------------------------------- pure helpers


def section(body: str, name: str) -> str:
    m = re.search(rf"(?ims)^#{{1,6}}\s+{name}\b[^\n]*\n(.*?)(?=^#{{1,6}}\s|\Z)", body or "")
    return m[1] if m else ""


BOX = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(.+)$", re.M)


def checklist(body: str) -> list[dict]:
    """Checkboxes of the Tasks (T1..) and Acceptance Criteria (A1..) sections, in order."""
    items = []
    for prefix, name in (("T", "Tasks"), ("A", "Acceptance Criteria")):
        for i, m in enumerate(BOX.finditer(section(body, name)), 1):
            items.append({"id": f"{prefix}{i}", "done": m[2] != " ", "text": m[4].strip()})
    return items


def tick(body: str, ids: set[str]) -> tuple[str, list[str]]:
    """Check the boxes with these ids inside their own sections; returns (new body, ids ticked)."""
    ticked: list[str] = []
    for prefix, name in (("T", "Tasks"), ("A", "Acceptance Criteria")):
        m = re.search(rf"(?ims)^#{{1,6}}\s+{name}\b[^\n]*\n(.*?)(?=^#{{1,6}}\s|\Z)", body or "")
        if not m:
            continue
        counter = iter(range(1, 10_000))

        def sub(box: re.Match) -> str:
            ident = f"{prefix}{next(counter)}"
            if box[2] == " " and ident in ids:
                ticked.append(ident)
                return f"{box[1]}x{box[3]}{box[4]}"
            return box[0]

        new = BOX.sub(sub, m[1])
        body = body[: m.start(1)] + new + body[m.end(1) :]
    return body, ticked


CLAIM = re.compile(r"LOOP-TASKS-DONE:\s*([^\n`]*)")


def claimed_ids(log_text: str) -> set[str]:
    """Ids from the LAST `LOOP-TASKS-DONE:` line the agent printed (its final message)."""
    found = CLAIM.findall(log_text or "")
    if not found:
        return set()
    return {x.upper() for x in re.findall(r"\b([TtAa]\d{1,3})\b", found[-1])}


def rollup(pr: dict) -> dict:
    """{state, head, failing: [{name, url}]} for the PR's last commit."""
    nodes = (pr.get("commits") or {}).get("nodes") or []
    commit = (nodes[-1] or {}).get("commit") if nodes else {}
    roll = (commit or {}).get("statusCheckRollup") or {}
    failing = []
    for c in ((roll.get("contexts") or {}).get("nodes")) or []:
        if c.get("__typename") == "CheckRun" and (c.get("conclusion") or "") in FAILED_CONCLUSIONS:
            failing.append({"name": c.get("name"), "url": c.get("detailsUrl") or ""})
        elif c.get("__typename") == "StatusContext" and c.get("state") in {"FAILURE", "ERROR"}:
            failing.append({"name": c.get("context"), "url": c.get("targetUrl") or ""})
    return {"state": roll.get("state"), "head": pr.get("headRefOid"), "failing": failing}


ERROR_LINE = re.compile(
    r"(error|fail|traceback|assert|exception|^E\s|would reformat|not found|denied|exit code [1-9])",
    re.I,
)


def error_lines(log: str, limit: int = LOG_LINES) -> list[str]:
    lines = [re.sub(r"^\S+Z\s", "", ln).rstrip() for ln in (log or "").splitlines()]
    hits = [ln for ln in lines if ERROR_LINE.search(ln) and "##[group]" not in ln]
    return (hits or lines)[-limit:]


def job_id(url: str) -> str | None:
    m = re.search(r"/job/(\d+)", url or "")
    return m[1] if m else None


def state_path(target: str) -> Path:
    return STATE_DIR / (re.sub(r"[^A-Za-z0-9_.-]+", "__", target) + ".json")


def load_state(target: str) -> dict | None:
    try:
        return json.loads(state_path(target).read_text())
    except (OSError, ValueError):
        return None


def owns(target: str, now: float | None = None) -> dict | None:
    """closer-core's question: does the local loop own this PR right now? The state if it is active and
    fresh (the loop ticked it within FRESH), else None so the normal closer path applies."""
    st = load_state(target)
    if not st or st.get("status") != "active":
        return None
    return st if (now or time.time()) - float(st.get("updated") or 0) < FRESH else None


# --------------------------------------------------------------------------- the round prompt


def build_prompt(
    st: dict,
    pr: dict,
    issue: dict | None,
    cause: str,
    failures: list[dict],
    acceptance: dict | None,
) -> str:
    body = (issue or {}).get("body") or pr.get("body") or ""
    items = checklist(body)
    done = sum(i["done"] for i in items)
    k = len(st["rounds"]) + 1
    lines = [
        f"LOCAL KEEPALIVE ROUND {k}/{ROUND_CAP} for {st['target']}"
        + (f" (source issue {st['issue']})" if st.get("issue") else "")
        + f". You are in a local worktree on the PR's head branch `{pr.get('headRefName')}`.",
        "Continue the work on THIS branch: commit and push so the PR updates. Do NOT open a new pull request.",
        "",
        f"Why this round: {cause}",
    ]
    if failures:
        lines += ["", "## Failing checks on the current head"]
        for f in failures:
            lines.append(f"### {f['name']}" + (f" ({f['url']})" if f.get("url") else ""))
            if f.get("lines"):
                lines += ["```", *f["lines"], "```"]
    if acceptance and acceptance.get("result") == "FAIL":
        lines += [
            "",
            "## Acceptance commands failed on the current head",
            "```",
            str(acceptance.get("detail") or "")[-3000:],
            "```",
        ]
    lines += [
        "",
        "---",
        "## PR Tasks and Acceptance Criteria",
        "",
        f"**Progress:** {done}/{len(items)} tasks complete, {len(items) - done} remaining",
    ]
    scope = section(body, "Scope").strip()
    if scope:
        lines += ["", "### Scope", scope[:3000]]
    for prefix, title, lead in (
        (
            "T",
            "Tasks",
            "Complete these in order. A task is done ONLY after its implementation is verified:",
        ),
        ("A", "Acceptance Criteria", "The PR is complete when ALL of these are satisfied:"),
    ):
        own = [i for i in items if i["id"].startswith(prefix)]
        if own:
            lines += ["", f"### {title}", lead, ""]
            lines += [f"- [{'x' if i['done'] else ' '}] {i['id']}: {i['text']}" for i in own]
    lines += [
        "---",
        "",
        "Rules:",
        "- Change only what the remaining tasks and the failures above need. No cleanup or refactor.",
        f"- {PIN_RULE}",
        "- Run only the focused tests for the files you change and the commands the acceptance criteria "
        "name. Never run the full test suite locally: CI runs it on your push.",
        "- End your final message with ONE line `LOOP-TASKS-DONE: <ids>` naming the task/criterion ids "
        "(e.g. T1, T3, A2) that are done AND verified on the head you pushed, or `LOOP-TASKS-DONE: none`.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the tick


def _rails():
    """Orchestrator modules, imported lazily so the pure helpers stay importable in isolation."""
    import claims  # noqa: PLC0415
    import router  # noqa: PLC0415

    return claims, router


def default_running(targets: list[str]) -> bool:
    claims, _ = _rails()
    return any(claims.holder(t) for t in targets if t)


def default_capacity() -> dict:
    _, router = _rails()
    return router.local_capacity()


def default_rotate(task_type: str, tried: set[str]) -> str | None:
    _, router = _rails()
    known = {e["agent"] for spec in router._ROUTE_TABLE_TYPED.values() for e in spec["agents"]}
    pick = router.select_agent(
        task_type,
        router.load_capacity(),
        learned=(router.learned_ranks() or {}).get(task_type),
        only=(known - tried) or {"__none__"},
    )
    return pick["agent"] if pick and pick.get("agent") not in tried else None


def default_hosted_agent(task_type: str, current: str) -> str | None:
    _, router = _rails()
    if current in router.KEEPALIVE_AGENTS:
        return current
    pick = router.select_remote_agent(task_type, router.load_capacity())
    return (pick or {}).get("agent")


def default_acceptance(pr_target: str, issue_target: str | None, body: str, head: str) -> dict:
    import keepalive_supervisor  # noqa: PLC0415

    try:
        gate = keepalive_supervisor._closure_gate(
            pr_target, {"target": issue_target or pr_target, "body": body}, head
        )
    except Exception as exc:  # noqa: BLE001 — an unrunnable gate is reported, never a pass
        return {"result": "UNRUNNABLE", "detail": f"gate error: {str(exc)[:300]}"}
    result = str(gate.get("result") or "UNRUNNABLE").upper()
    return {
        "result": result if result in {"PASS", "FAIL"} else "UNRUNNABLE",
        "detail": str(gate.get("reason") or gate.get("summary") or gate)[:3000],
    }


def default_launch(agent: str, pr_target: str, prompt: str, rationale: str) -> dict:
    """One round through the Orchestrator's own delegation rail: claim + the PR's head-branch worktree
    (lane closer, reused across rounds) + detached agent, recorded with mode=local and this rationale.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    brief = (
        STATE_DIR
        / "briefs"
        / f"{re.sub(r'[^a-z0-9]+', '_', pr_target.lower())}-{time.time_ns()}.md"
    )
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(prompt)
    here = Path(__file__).resolve().parent
    argv = [
        sys.executable,
        str(here / "dispatcher.py"),
        "delegate",
        "--agent",
        agent,
        "--target",
        pr_target,
        "--lane",
        "closer",
        "--task-type",
        "implement",
        "--prompt-file",
        str(brief),
        "--rationale",
        rationale,
    ]
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env["PATH"] = (
        f"/opt/homebrew/bin:{Path.home()}/.local/bin:{Path.home()}/.cursor/bin:"
        + env.get("PATH", "")
    )
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=300, cwd=str(here), env=env, check=False
    )
    found = re.findall(r"\{.*\}", done.stdout, re.S)
    try:
        return json.loads(found[-1]) if found else {"error": (done.stderr or done.stdout)[-300:]}
    except ValueError:
        return {"error": done.stdout[-300:]}


def default_record_handoff(pr_target: str, agent: str, reason: str) -> None:
    """A hand-off is a start in hosted mode: record it like dispatcher.delegate_remote does."""
    try:
        import feedback  # noqa: PLC0415

        repo, num = pr_target.split("#")
        feedback.record_run(
            f"remote:{pr_target}:{agent}",
            pr_target,
            "implement",
            agent,
            mode="remote",
            rationale=f"local-loop hand-off: {reason}",
            pr_number=int(num),
        )
    except Exception:  # noqa: BLE001 — the label swap already happened; the ledger is best-effort
        pass


def read_log(path: str | None) -> str:
    try:
        return Path(path).read_text(errors="replace")[-200_000:] if path else ""
    except OSError:
        return ""


class Tick:
    def __init__(
        self,
        gh: Any,
        *,
        apply: bool,
        now: Callable[[], float] = time.time,
        running: Callable[[list[str]], bool] = default_running,
        capacity: Callable[[], dict] = default_capacity,
        rotate: Callable[[str, set[str]], str | None] = default_rotate,
        hosted_agent: Callable[[str, str], str | None] = default_hosted_agent,
        acceptance: Callable[[str, str | None, str, str], dict] = default_acceptance,
        launch: Callable[[str, str, str, str], dict] = default_launch,
        record_handoff: Callable[[str, str, str], None] = default_record_handoff,
        log_reader: Callable[[str | None], str] = read_log,
    ):
        self.gh, self.apply, self.now = gh, apply, now
        self.running, self.capacity, self.rotate = running, capacity, rotate
        self.hosted_agent, self.acceptance, self.launch = hosted_agent, acceptance, launch
        self.record_handoff, self.log_reader = record_handoff, log_reader
        self.counts: dict[str, int] = {}
        self.items: list[dict] = []
        self._cap: dict | None = None

    def bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def cap(self) -> dict:
        if self._cap is None:
            self._cap = self.capacity()
        return self._cap

    def save(self, st: dict) -> None:
        if not self.apply:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = state_path(st["target"])
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
        os.replace(tmp, path)

    # ---- one PR
    def new_state(self, pr: dict, target: str, issue: dict | None, repo: str) -> dict:
        labels = {n["name"] for n in pr["labels"]["nodes"]}
        agent = next(
            (
                lab.split(":", 1)[1]
                for lab in sorted(labels)
                if lab.startswith("agent:")
                and lab not in {"agent:auto", "agent:retry", "agent:rate-limited"}
            ),
            None,
        )
        if (
            agent is None
        ):  # the branch says who wrote it (codex/issue-12, claude-code/issue-12, ...)
            m = re.match(r"^([a-z]+)", pr.get("headRefName") or "")
            agent = m[1] if m else "codex"
        issue_t = f"{repo}#{issue['number']}" if issue else None
        log = str(
            DISPATCH_LOG_DIR
            / f"{(issue_t or target).replace('/', '__').replace('#', '_')}.{agent}.log"
        )
        return {
            "target": target,
            "issue": issue_t,
            "agent": agent,
            "tried": [agent],
            "status": "active",
            "created": self.now(),
            "updated": self.now(),
            "unproductive": 0,
            "start_errors": 0,
            "rounds": [
                {
                    "n": 1,
                    "agent": agent,
                    "cause": "start",
                    "head_before": None,
                    "log": log,
                    "started": None,
                }
            ],
        }

    def step(self, pr: dict) -> dict:
        repo = pr["repository"]["nameWithOwner"]
        target = f"{repo}#{pr['number']}"
        issues = [i for i in (pr.get("closingIssuesReferences") or {}).get("nodes") or [] if i]
        issue = issues[0] if issues else None
        st = load_state(target) or self.new_state(pr, target, issue, repo)
        st["updated"] = self.now()
        item = {"target": target, "round": len(st["rounds"]), "agent": st["agent"]}
        try:
            item.update(self.decide(st, pr, issue, repo))
        except Exception as exc:  # noqa: BLE001 — one PR's failure never costs the tick
            item.update(action="error", reason=str(exc)[:300])
        self.bump(item["action"])
        st["last"] = {k: item.get(k) for k in ("action", "reason")}
        self.save(st)
        self.items.append(item)
        return item

    def decide(self, st: dict, pr: dict, issue: dict | None, repo: str) -> dict:
        target = st["target"]
        labels = {n["name"] for n in pr["labels"]["nodes"]}
        if st.get("status") != "active":
            return {"action": st.get("status", "inactive")}
        hosted = sorted(labels & set(HOSTED_LABELS))
        if hosted and self.apply:  # one owner: a stray hosted label (e.g. a sweep race) comes off
            for lab in hosted:
                self.gh.remove_label(repo, pr["number"], lab)
        if self.running([t for t in (target, st.get("issue")) if t]):
            return {"action": "agent-running"}
        ci = rollup(pr)
        last = st["rounds"][-1]
        if not last.get("closed"):
            if ci["state"] in PENDING:
                since = float(last.get("started") or st.get("created") or self.now())
                if ci["state"] is None and self.now() - since > NO_CI_WAIT:
                    return self.handoff(
                        st,
                        pr,
                        repo,
                        labels,
                        f"no check reported on head {str(ci['head'])[:8]} for {NO_CI_WAIT // 60} min",
                    )
                return {
                    "action": "wait-ci",
                    "reason": f"CI {ci['state'] or 'not started'} on {str(ci['head'])[:8]}",
                }
            self.close_round(st, last, pr, issue, repo, ci)
        if (
            ci["state"] in PENDING
        ):  # a push from outside the loop (autofix, a person): wait for its CI
            return {
                "action": "wait-ci",
                "reason": f"CI {ci['state'] or 'not started'} on {str(ci['head'])[:8]}",
            }
        body = (issue or {}).get("body") or pr.get("body") or ""
        failures: list[dict] = []
        acceptance = None
        if ci["state"] == "SUCCESS":
            acceptance = self.evidence(st, target, body, ci["head"])
            if acceptance["result"] == "PASS" and issue:
                ids = {
                    i["id"] for i in checklist(body) if i["id"].startswith("A") and not i["done"]
                }
                body = self.tick_boxes(st, repo, issue, body, ids)
            remaining = [i for i in checklist(body) if not i["done"]]
            no_commands = (
                acceptance["result"] == "UNRUNNABLE" and "no gate commands" in acceptance["detail"]
            )
            if (acceptance["result"] == "PASS" or no_commands) and not remaining:
                return self.done(st, pr, repo, acceptance)
            if acceptance["result"] == "FAIL":
                cause = "CI is green but the issue's acceptance commands fail on this head (output below)."
            elif acceptance["result"] == "UNRUNNABLE" and not no_commands:
                cause = f"CI is green; the acceptance commands could not run ({acceptance['detail'][:200]})."
            else:
                cause = (
                    f"CI is green but {len(remaining)} task/criterion box(es) are unchecked: "
                    + ", ".join(i["id"] for i in remaining[:12])
                    + ". Finish them, or verify and report them."
                )
        else:
            failures = self.failures(st, repo, ci)
            cause = f"CI {ci['state']} on head {str(ci['head'])[:8]}: " + (
                ", ".join(f["name"] for f in ci["failing"][:8]) or "no failing check named"
            )
        return self.next_round(st, pr, issue, repo, labels, cause, failures, acceptance)

    def close_round(
        self, st: dict, last: dict, pr: dict, issue: dict | None, repo: str, ci: dict
    ) -> None:
        """The last round's agent has exited and CI reported on what it left: measure it once."""
        pushed = last.get("head_before") != ci["head"]
        ticked: list[str] = []
        if ci["state"] == "SUCCESS" and issue:  # a claim counts only against a green head
            claims = claimed_ids(self.log_reader(last.get("log")))
            open_ids = {i["id"] for i in checklist(issue.get("body") or "") if not i["done"]}
            if claims & open_ids:
                self.tick_boxes(st, repo, issue, issue.get("body") or "", claims & open_ids)
                ticked = sorted(claims & open_ids)
        previous = st.get("last_ci")
        healed = previous in {"FAILURE", "ERROR"} and ci["state"] == "SUCCESS"
        productive = bool(pushed or ticked or healed)
        last.update(
            closed=True,
            head_after=ci["head"],
            ci=ci["state"],
            pushed=pushed,
            ticked=ticked,
            productive=productive,
        )
        st["last_ci"] = ci["state"]
        st["unproductive"] = 0 if productive else int(st.get("unproductive", 0)) + 1

    def tick_boxes(self, st: dict, repo: str, issue: dict, body: str, ids: set[str]) -> str:
        new, ticked = tick(body, ids)
        if not ticked:
            return body
        if self.apply:
            self.gh.set_issue_body(repo, int(issue["number"]), new)
        issue["body"] = new
        st["ticked"] = sorted(set(st.get("ticked", [])) | set(ticked))
        return new

    def evidence(self, st: dict, target: str, body: str, head: str) -> dict:
        ev = st.get("evidence") or {}
        if ev.get("head") == head:
            return ev
        ev = {"head": head, **self.acceptance(target, st.get("issue"), body, head)}
        st["evidence"] = ev
        return ev

    def failures(self, st: dict, repo: str, ci: dict) -> list[dict]:
        cached = st.get("failures") or {}
        if cached.get("head") == ci["head"]:
            return cached["items"]
        items: list[dict] = []
        for f in ci["failing"][:8]:
            entry = {"name": f["name"], "url": f["url"]}
            jid = job_id(f["url"])
            if jid and len([i for i in items if i.get("lines")]) < MAX_FAIL_JOBS:
                try:
                    entry["lines"] = error_lines(self.gh.job_log(repo, jid))
                except (
                    Exception
                ) as exc:  # noqa: BLE001 — the check name and URL still go to the agent
                    entry["lines"] = [f"(log unavailable: {str(exc)[:150]}; open the URL above)"]
            items.append(entry)
        st["failures"] = {"head": ci["head"], "items": items}
        return items

    def next_round(
        self,
        st: dict,
        pr: dict,
        issue: dict | None,
        repo: str,
        labels: set[str],
        cause: str,
        failures: list[dict],
        acceptance: dict | None,
    ) -> dict:
        if len(st["rounds"]) >= ROUND_CAP:
            return self.handoff(st, pr, repo, labels, f"round cap {ROUND_CAP} reached")
        if int(st.get("unproductive", 0)) >= STALL_ROUNDS:
            tried = set(st["tried"])
            nxt = self.rotate("implement", tried) if len(tried) < MAX_AGENTS else None
            if not nxt:
                return self.handoff(
                    st,
                    pr,
                    repo,
                    labels,
                    f"{STALL_ROUNDS} unproductive rounds and no untried local agent ({', '.join(sorted(tried))})",
                )
            st.update(agent=nxt, tried=sorted(tried | {nxt}), unproductive=0)
            cause = f"agent rotated after {STALL_ROUNDS} unproductive rounds. " + cause
        cap = self.cap()
        if not cap["ok"]:
            since = st.get("waiting_since") or self.now()
            st["waiting_since"] = since
            if self.now() - since >= CAPACITY_WAIT:
                return self.handoff(
                    st,
                    pr,
                    repo,
                    labels,
                    f"no local slot for {int((self.now() - since) / 60)} min ({cap['reason']})",
                )
            return {
                "action": "wait-capacity",
                "reason": f"{cap['reason']}; hands off to hosted after {CAPACITY_WAIT // 60} min",
            }
        n = len(st["rounds"]) + 1
        prompt = build_prompt(st, pr, issue, cause, failures, acceptance)
        rationale = f"local-loop round {n}/{ROUND_CAP}: {cause[:160]}; {cap['reason']}"
        if not self.apply:
            return {"action": "would-round", "reason": cause[:200]}
        res = self.launch(st["agent"], st["target"], prompt, rationale)
        if res.get("error"):
            st["start_errors"] = int(st.get("start_errors", 0)) + 1
            if st["start_errors"] >= START_ERRORS:
                return self.handoff(
                    st,
                    pr,
                    repo,
                    labels,
                    f"{START_ERRORS} round starts failed: {res['error'][:150]}",
                )
            return {"action": "start-error", "reason": str(res["error"])[:200]}
        self._cap = None  # one more local agent now runs
        st["start_errors"] = 0
        st["waiting_since"] = None
        st["rounds"].append(
            {
                "n": n,
                "agent": st["agent"],
                "cause": cause[:300],
                "head_before": pr["headRefOid"],
                "started": self.now(),
                "log": res.get("log"),
                "run_id": res.get("run_id"),
            }
        )
        return {"action": "round", "round": n, "reason": cause[:200]}

    def done(self, st: dict, pr: dict, repo: str, acceptance: dict) -> dict:
        st["status"] = "done"
        rounds = len(st["rounds"])
        feedback_rounds = rounds - 1
        msg = (
            f"{MARKER}\nLocal loop: done after {rounds} round(s) ({feedback_rounds} CI-feedback round(s)); "
            f"CI green on {str(pr['headRefOid'])[:8]}, acceptance commands {acceptance['result']}"
            f"{' (none named)' if acceptance['result'] == 'UNRUNNABLE' else ''}, every task box checked. "
            "closer-core merges it from here."
        )
        if self.apply:
            if pr.get("isDraft"):  # closer-core never merges a draft: done work is ready for review
                self.gh.ready(repo, pr["number"])
            self.gh.comment(repo, pr["number"], msg)
        return {"action": "done", "reason": f"{rounds} rounds"}

    def handoff(self, st: dict, pr: dict, repo: str, labels: set[str], reason: str) -> dict:
        """Hosted keepalive takes the PR: one owner, so agents:local-loop comes off first."""
        agent = self.hosted_agent("implement", st["agent"])
        if self.apply:
            self.gh.remove_label(repo, pr["number"], LABEL)
            want = list(HOSTED_LABELS)
            if agent and not any(lab.startswith("agent:") for lab in labels):
                want.append(f"agent:{agent}")
            self.gh.add_labels(repo, pr["number"], [lab for lab in want if lab not in labels])
            self.gh.comment(
                repo,
                pr["number"],
                f"{MARKER}\nLocal loop hands this PR to hosted keepalive "
                f"after {len(st['rounds'])} round(s): {reason}.",
            )
            self.record_handoff(st["target"], agent or st["agent"], reason)
        st.update(status="handed-off", handoff_reason=reason)
        return {"action": "handed-off", "reason": reason}

    # ---- the whole tick
    def run(self) -> str:
        prs = self.gh.looped_prs()
        for pr in prs:  # drafts too: agents often open one, and CI still reports on it
            if pr.get("state") == "OPEN":
                self.step(pr)
        cap = self._cap or {}
        waiting = self.counts.get("wait-capacity", 0)
        return (
            f"local-loop tick apply={'yes' if self.apply else 'no'}: prs {len(prs)}; "
            + (", ".join(f"{k} {v}" for k, v in sorted(self.counts.items())) or "nothing to do")
            + f"; waiting for a local slot {waiting} (blocking {waiting}, drainable "
            f"{cap.get('drainable', '?') if waiting else 0})"
        )


def status(gh: Any) -> dict:
    """Pass numbers: (a) looped PRs with >= 2 CI-feedback rounds that merged."""
    rows = []
    for p in sorted(STATE_DIR.glob("*.json")):
        try:
            rows.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    merged = {f"{m['repository']['nameWithOwner']}#{m['number']}" for m in gh.merged_prs()}
    out = []
    for st in rows:
        fb = sum(1 for r in st.get("rounds", [])[1:] if r.get("started"))
        out.append(
            {
                "target": st["target"],
                "status": st.get("status"),
                "rounds": len(st.get("rounds", [])),
                "feedback_rounds": fb,
                "agents": st.get("tried"),
                "merged": st["target"] in merged,
                "last": st.get("last"),
            }
        )
    passing = [r for r in out if r["merged"] and r["feedback_rounds"] >= 2]
    return {
        "prs": out,
        "looped": len(out),
        "merged": sum(r["merged"] for r in out),
        "pass_a": len(passing),
        "pass_a_line": (
            f"pass (a): looped PRs merged with >= 2 CI-feedback rounds = {len(passing)} "
            f"({'PASS' if passing else 'not yet'}); looped {len(out)}, merged {sum(r['merged'] for r in out)}"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_loop.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--apply", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        res = status(GitHub())
        print(json.dumps(res, indent=1) if args.json else res["pass_a_line"])
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "tick.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("local-loop: previous tick still running; blocking 0, drainable 0")
            return 0
        tick_ = Tick(GitHub(), apply=args.apply)
        line = tick_.run()
    if args.apply:
        with (STATE_DIR / "ticks.jsonl").open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "summary": line,
                        "items": tick_.items,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
