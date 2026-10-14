# Duplicate persisted triage link identities

Actual PR598 comparison reports duplicate persisted links silently overwrite each other. The four new controls cover explicit consumption IDs and uniquely mapped legacy timestamps, both identical and conflicting statuses. On merged03cd1566 the controls fail4/4 at the UNKNOWN assertion. With duplicate detection before dictionary insertion, all59triage tests pass. Link bytes stay unchanged and no influence edge is created on refusal. Existing unique-link and interrupted-retry controls remain passing.

This bounded follow-up addresses that concrete provider concern. Source583 remains OPEN for native consumption/outcome and dated acceptance; no original acceptance or provider PASS is claimed.

Commands: python3 -m pytest -q tests/test_triage_shadow.py -k duplicate_link_receipts (RED); python3 -m pytest -q tests/test_triage_shadow.py (GREEN). Initial adjacent filename typo ran no tests and is excluded from qualification.
