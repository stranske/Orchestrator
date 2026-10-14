# Current review regression recovery

Reviewed head: `262733c9a3f35e43137f38c1eb1c72936b8003b5`.

All three strengthened tests failed on that original code (3 failed); the corrected six-suite run passed 215 tests and 23 subtests. These are local isolated fixtures, not provider acceptance, production use or catalog availability.

- Linked source criteria accumulate from every issue, with each issue's own revision/source.
- Dry previews preserve the saved metadata abstention. Failed dispatched retries preserve the old packet and paid identity while retaining the failed attempt separately. Successful retry replaces the saved packet.
- `Commit changes to the branch` and `Please commit updates` are rejected; noun-led `Commit history needs inspection` remains accepted.

Commands: `python3 -m pytest tests/test_adjudicator_retro.py tests/test_adjudicator_retro_evidence.py tests/test_adjudicator_retro_cost.py tests/test_adjudicator_retro_collection.py tests/test_adjudicator_qualified_input.py tests/test_adjudicator_collected_input.py -q`; Black/Ruff on the four changed source/test files; focused mypy on `src/adjudicator_retro.py src/roles.py`.

The strengthened assertions extend existing tests without adding collected test nodes or changing ceilings. Hosted checks and a fresh seven-minute review floor are required on the pushed head.
