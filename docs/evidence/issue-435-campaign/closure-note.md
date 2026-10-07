# Parent delivery disposition note

Historical runner limitation: the GitHub write tools rejected both posting this note and closing the parent issue:
`MCP tool call requires approval, but approval policy is never`.
Issue #435 remains open. PR #514 uses the non-closing reference `Refs #435`; its eventual merge
must follow the repository's merge guard. No merge is authorized by these receipts.

Current bounded delivery disposition:

Rails [PR #502](https://github.com/stranske/Orchestrator/pull/502) and all six delivery
PRs are merged, and every filed target issue is closed. The per-repository receipts
in this directory pin the exact merged commits and reconstruct the delivered ignore
files from the original GitHub blobs plus appended text. All five ignore probe paths
pass Git verification for every repository. The learning-management-system delivery
also includes a hygiene regression test in `tests/test_repo_hygiene.py`. That is an
explicit exception to the campaign's "Only .gitignore changes" constraint. The changed-file
record remains accurate, and .gitignore-only compliance is not claimed. The test protects
ignore behavior; it does not establish a waiver or complete campaign acceptance.

Historical acceptance was reported by closure commit
`49fe4ccd03b52b4e3aa0183b07affd96fea785a0`:
`python3 -m pytest tests/test_codemod_campaign.py -q` — 23 passed.
The earlier runner lacked pytest and did not rerun it. Independent opener replay on
2026-10-07 used `/opt/anaconda3/bin/python3 -m pytest tests/test_codemod_campaign.py -q -o addopts=`
and observed 23 passed. The existing combined CLI/receipt Node suite passed 13 tests.
The new receipt validation passes eight tests and rejects four evidence-corruption
controls. The existing combined CLI/receipt Node suite passes 13 tests.

Delivery completion does not establish a live range-dispatch trial, whole-delivery
cost or time-based durability. Brain attribution/cost/durability follow-up remains
on [PR #513](https://github.com/stranske/Orchestrator/pull/513); all six total-cost and
durability measurements remain UNKNOWN/null. The original measurement tasks must
not be relabeled measured on the strength of the merged child PRs.
