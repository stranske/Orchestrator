# Bounded follow-up check

Check date: 2026-11-10. Starting commit: `7a18de0715e37dd29c392b0e9691332fccb562a1`.

Outcome: validate the preserved statement measurement against its complete raw job
log and configuration, and preserve new attempts to locate a matching baseline or
download the original report artifacts. Unknown baseline, branch measurements, and
exact test populations must remain unknown. This does not complete a controlled
comparison or the five historical mutation executions.

Printing command: `node scripts/verify_issue_771_coverage.js`.

Stop rule: each subprocess is limited to 120 seconds. Query one page of at most
100 exact-baseline runs, then inspect its returned CI candidates. Attempt each
identified historical coverage artifact once. If an artifact is unavailable or
pytest cannot import, retain the failure and leave the dependent item unchecked.
Do not start a whole-repository combined coverage run.
