Check date: 2026-11-11 America/Chicago.

Outcome: the effective installed generation, observed by the existing hourly collector, prints a continuous baseline and `provision: failed(branch-held|branch-exists) = 0 over 7d`.
Command: `node scripts/audit_provision_ledger.js /Users/teacher/.codex/orchestrator "$installed_generation"`.
Selected interpreter: `/opt/anaconda3/bin/python3` (no checkout virtual environment).
Stop rule: two attempts per failing acceptance step or 45 minutes.
Blocking 1, drainable 1: preserve ownership and all ledger segments while the existing hourly collector continues observing; read-only audits remain available.
The bare verifier runs the full local suite, which this keepalive explicitly forbids; do not substitute focused results for that acceptance.
