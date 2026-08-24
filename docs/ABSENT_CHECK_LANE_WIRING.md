# An absent check must reach the lanes — the one wiring step this repo cannot land

`scripts/check_checks_reported.py` is in-repo, tested and callable. The lanes that actually **merge**
are not: `~/.codex/automations/{imi-merge-verify-closer,pd-workloop-resume}/automation.toml` and
`~/.codex/bin/handoff-prerun.sh` are the owner's files, outside any repository. So this doc is the
handoff, in the same shape as `docs/MIRROR_SYNC_PATCH.md`.

## Why the merge point is the right home, and CI is not

The defect is that **an absent check is indistinguishable from a passing one.** `gh pr checks` lists
what reported; a check that never started is simply not in the list, so a PR with no Gate reads
exactly like a PR whose Gate passed.

That cannot be fixed from inside GitHub Actions, for two reasons:

1. **A workflow can be held.** Six of this repo's workflows currently sit at `action_required` with
   zero jobs, `pr-00-gate.yml` among them. A detector implemented as a workflow can be held by the
   same mechanism it is watching.
2. **Ordering.** Any in-CI check runs *concurrently* with the Gate, so it cannot know whether the
   Gate is absent or merely slower.

The merge decision is the one place where "did everything report?" is both answerable and
actionable. In this fleet that decision is the closer lane's.

## Why not branch protection

Because it converts this defect into a worse one. A required status check that is **held** never
reports, so the PR can never merge — the clear path blocked by the very thing the gate measures,
which is this workspace's most-repeated defect. On a solo-maintained repo, "unverified but movable"
beats "permanently stuck". `main` is deliberately unprotected; this tool is what replaces the
protection, at the point where a human or a lane can still exercise judgement.

## The closer wiring (pre-merge assertion)

The closer already parses `action_required`, but frames it as *"may need human approval"*. That is
the wrong frame: the question is not whether someone should approve a workflow, it is whether **this
PR was verified at all.** Before merging any PR, run:

```bash
python3 ~/Library/CloudStorage/Dropbox/Learning/Code/Orchestrator/scripts/check_checks_reported.py --pr <N>
```

* **exit 0** — every check that normally reports also reported here. Proceed on the usual criteria.
* **exit 1** — one or more checks NEVER reported. Do not merge on the strength of a green list.
  Post one inbox item naming the absent checks, and either wait for them or record explicitly why
  the absence is acceptable (a workflow genuinely removed) before merging.

This adds no queue: it is evaluated per merge attempt from live state, so there is nothing to drain
and nothing to mark as read.

## The prerun surfacing (FYI, never blocking)

Add to `handoff-prerun.sh`, in the section that already prints lane state:

```bash
python3 "$ORCH/scripts/check_checks_reported.py" --sweep 2>/dev/null || true
```

`--sweep` reports every open PR with an absent check and every currently-held workflow. It is
report-only by construction — it takes no action, has no state, and cannot accumulate. `|| true`
because a lane round must never fail on the health reporter.

## Attention cost

Six workflows are held today; approving each in the Actions UI is a one-off of a couple of minutes
in total. Ongoing, an approval is needed each time an edit re-arms a workflow — a few times a week
at this repo's rate, so low single-digit minutes per week. The sweep makes those visible without
queuing them, and the closer assertion makes the dangerous case (merging unverified) impossible to
reach by accident rather than adding a step to remember.

## What this deliberately does not do

It does not model **why** a check is absent. A check can vanish to a held workflow, a cancellation,
a deleted or renamed workflow, a rate limit, a mistaken path filter, or a GitHub incident. Those
present identically to whoever is merging, and they are the same bug: something that was being
checked stopped being checked, quietly. The next cause will be one not listed here, which is exactly
why the tool asks only "did it report?".
