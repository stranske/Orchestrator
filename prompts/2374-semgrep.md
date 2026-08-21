# Task: Add Semgrep CE security scan — stranske/Workflows#2374

## Context
You are working in a local git worktree of `stranske/Workflows`. Your job is to implement the
changes below, then commit, push the branch, and open a PR that references the issue. Do NOT
modify any existing workflow files — the only allowed changes are:
1. A new file `.github/workflows/health-52-semgrep.yml`
2. One new line in `.github/workflows/README.md`

---

## Issue summary (stranske/Workflows#2374)

**Why:** Add Semgrep Community Edition as defence-in-depth SAST alongside CodeQL.
The `p/github-actions` ruleset catches workflow-injection in YAML (CodeQL does not scan Actions).

**Non-Goals:** Do NOT touch `health-50-security-scan.yml` or any other existing file except README.md.

---

## File 1 — `.github/workflows/health-52-semgrep.yml`

Create this file. Model its structure on the sibling `health-50-security-scan.yml` (reproduced
below for reference), adjusting for Semgrep instead of CodeQL.

### Sibling for reference (DO NOT MODIFY):
```yaml
name: Health 50 Security Scan

on:
  push:
    branches: [ "main" ]
  pull_request:
    branches: [ "main" ]
  schedule:
    - cron: '30 1 * * 0'

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  classify:
    name: classify changed paths
    runs-on: ubuntu-latest
    outputs:
      is_security_relevant: ${{ steps.classify.outputs.is-security-relevant || 'true' }}
      classification_rationale: >-
        ${{ steps.classify.outputs.classification-rationale || 'path classifier unavailable' }}
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - name: Classify changed paths
        id: classify
        uses: ./.github/actions/path-classifier
        with:
          force-full: ${{ github.event_name == 'schedule' }}

  codeql:
    name: CodeQL
    needs: classify
    if: ${{ needs.classify.outputs.is_security_relevant == 'true' }}
    runs-on: ubuntu-latest
    permissions:
      actions: read
      contents: read
      security-events: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
      - name: Initialize CodeQL
        uses: github/codeql-action/init@v4
        with:
          languages: python
      - name: Autobuild
        uses: github/codeql-action/autobuild@v4
      - name: Perform CodeQL Analysis
        id: codeql_analyze
        uses: github/codeql-action/analyze@v4
        continue-on-error: true
        with:
          category: "/language:python"
```

### Spec for the new file:

- **name:** `Health 52 Semgrep Scan`
- **triggers:** same as health-50 — push + pull_request on `main`, plus weekly schedule
  (use cron `'30 1 * * 0'` or similar weekly schedule)
- **permissions (top-level):** `contents: read`
- **concurrency:** same pattern as health-50 (group by workflow+ref, cancel-in-progress)
- **jobs:**

  **`classify` job** — copy exactly from health-50 (same action, same outputs, same steps)

  **`semgrep` job:**
  - `name: Semgrep`
  - `needs: classify`
  - `if: ${{ needs.classify.outputs.is_security_relevant == 'true' }}`
  - `runs-on: ubuntu-latest`
  - `permissions:` at job level: `contents: read` + `security-events: write`
  - Steps:
    1. `actions/checkout@v6`
    2. `actions/setup-python@v5` with `python-version: '3.12'`
    3. `pip install semgrep`
    4. Run semgrep scan:
       ```
       semgrep scan --config p/default --config p/python --config p/github-actions --config p/secrets --sarif --output semgrep.sarif
       ```
       with `continue-on-error: true`
    5. Upload SARIF: `github/codeql-action/upload-sarif@v4`
       - `sarif_file: semgrep.sarif`
       - `category: semgrep`
       - `continue-on-error: true`

The workflow must be **report-only** — both the semgrep scan step and the upload-sarif step use
`continue-on-error: true`. This must NOT gate or block PRs.

---

## File 2 — `.github/workflows/README.md`

Find the "Governance & Health" bullet line (around line 19), which currently reads:
```
- Governance & Health: `health-40-repo-selfcheck.yml`, `health-41-repo-health.yml`, `health-42-actionlint.yml`, `health-43-ci-signature-guard.yml`, `health-44-gate-branch-protection.yml`, labelers, dependency review, CodeQL.
```

Add a **new separate bullet** immediately after that line:
```
- Semgrep CE scan (`health-52-semgrep.yml`): report-only SAST using Semgrep Community Edition with `p/default`, `p/python`, `p/github-actions`, and `p/secrets` rulesets; uploads SARIF to GitHub Security for defence-in-depth alongside CodeQL.
```

Do NOT edit the existing Governance & Health bullet — add a new line after it.

---

## Acceptance criteria (from the issue)
- `.github/workflows/health-52-semgrep.yml` exists and is valid YAML (actionlint clean)
- Both semgrep scan and upload-sarif steps have `continue-on-error: true`
- `.github/workflows/README.md` has the new bullet; no other files changed
- PR opened referencing #2374

---

## Workflow: implement → validate → commit → push → open PR

1. Write both files as described above.
2. If `actionlint` is available, run it on the new workflow to verify YAML is valid:
   `actionlint .github/workflows/health-52-semgrep.yml`
   Fix any issues before committing.
3. Commit with a clear message, e.g.:
   `feat: add Semgrep CE security scan (health-52-semgrep.yml) — closes #2374`
4. Push the branch to origin.
5. Open a PR with `gh pr create`:
   - Title: `feat: add Semgrep CE security scan (health-52-semgrep.yml)`
   - Body should reference `Closes #2374` and briefly describe the change.
   - Do NOT target a draft PR — open it normally.

Complete all steps through the opened PR. Do not stop and ask for confirmation.
