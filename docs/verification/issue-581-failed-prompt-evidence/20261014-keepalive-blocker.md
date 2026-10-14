# Keepalive validation blocker — 2026-10-14

The source retention changes and named regression already exist at `fc0414a`.
This round extends existing tests for null evidence paths on valid CLI items,
zero-byte raw output retention, and exact withheld-body retention with no published
file after recording failure. No test nodes or collection/skip ceilings changed.

The roles selftest, Python compilation, whitespace checks, and independent direct
production checks pass. These do not replace the required pytest and Black gates.
Neither available Python has pytest, and Black is absent. Package installation
failed because package-index DNS/network access is unavailable; neither tool is
in the local pip cache. No commit or push was made because the pre-commit Black
gate cannot pass. No task checkbox was marked complete this round.

Attempts to post the blocker comment and add `needs-human` to PR #589 were denied:
`MCP tool call requires approval, but approval policy is never`.
The PR was read successfully and is open with `draft=false`.

Follow-up: provision pytest and Black, run the required focused suite and
repository-wide Black check, then commit the prepared test changes. The next real
role-authored batch of at least five bodies and its qualification query remain
pending; no production batch or 2026-10-27 acceptance is claimed.
