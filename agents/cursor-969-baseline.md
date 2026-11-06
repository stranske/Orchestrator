# Issue #969 Historical Baseline Evidence

## Recorded Gate run

- Repository: `stranske/Orchestrator`
- Workflow: Gate (`pr-00-gate.yml`)
- Run: [40269428960](https://github.com/stranske/Orchestrator/actions/runs/40269428960)
- Head SHA: `0f923500d64e6e5808d8542d511fe79597dc12ef`
- Recorded conclusion: `success`
- Job: [`python ci / python 3.12`, ID 121402729292](https://github.com/stranske/Orchestrator/actions/runs/40269428960/job/121402729292)
- Recorded job start: `2026-10-26T04:50:00Z`
- Recorded job completion: `2026-10-26T05:18:24Z`
- Job elapsed time from those timestamps: **28 minutes 24 seconds**.

### Recorded pytest step

GitHub Actions job metadata for job `121402729292` records step 19,
**Pytest (unit tests with coverage)**, with these timestamps:

- `startedAt`: `2026-10-26T04:50:19Z`
- `completedAt`: `2026-10-26T05:18:15Z`
- Recorded conclusion: `success`
- Step elapsed time from those timestamps: **27 minutes 56 seconds**.

These are GitHub's recorded step timestamps, not an independent measurement of
pytest process runtime. The step duration is distinct from the enclosing job's
duration and does not establish the full Gate critical path.

Corroboration command (checked 2026-11-06):

```bash
gh run view 40269428960 --repo stranske/Orchestrator --json url,headSha,conclusion,jobs
```

## Before/after comparison limitations

This historical reference supplies no measured post-change comparison.
Any future comparison must account for:

- Python version: this job used Python 3.12; a run on another version is a
  different execution environment.
- Cache state: dependency and test caches may differ between runs.
- Runners: runner availability, load, and instance characteristics may differ.
- Code and tests: differing heads may contain different workloads and test suites.
- Workflow configuration: versions, shards, and reusable-workflow revisions must
  be recorded before comparing job, pytest-step, or Gate durations.

Optimization, successful post-change evidence, and the authoritative Health 85
calculation with seven consecutive qualifying dated observations remain follow-up
work under [issue #969](https://github.com/stranske/Orchestrator/issues/969).
