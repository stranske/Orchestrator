# Readable current evidence for root #518

This additive proof repairs the seven omitted binary inputs in native compare
38836533097. All historical gzip files and the transferable exact-source bundle
remain unchanged. This successor does not change toolchain pins, collection
floors, ceilings, production code, tests or managed workflows.

## Full command and collection bytes

`command-index.json` contains all 12 original argv/cwd/exit records and every
stdout/stderr stream. Text streams are exact UTF-8 files or ordered segments.
Binary archive streams retain their original byte-count/hash receipts; exact
source trees remain in the unchanged historical bundle. No command output is
summarized or discarded.

`collection-nodes.txt` is the complete ordered 3470-node listing. Its transparent
prefix-sharing grammar is in `collection-node-layout.json`: `F` sets a file,
`S` its shared name prefix, `N` emits a name, `P` sets a parameterized name and
`A` emits its exact argument suffix. Join file + `::` + prefix/name + argument;
restore LF after each node and the recorded trailing literal. Every test name,
parameter and position remains inspectable. Both original collection outputs
share these exact node bytes, followed by their separate complete tails.
`collection-index.json` binds the standalone invocation; command 11 binds the
strict collector's full coverage-startup output. Coverage percentages in this
collect-only tail are startup diagnostics, not execution coverage acceptance.

Run the read-only reconstruction check from this checkout:

```
/opt/anaconda3/bin/python3 docs/evidence/issue-518-successor607/readable-20261016/verify-readable.py.txt
```

It reconstructs the complete original JSON capture and collection stdout,
compares every byte against the retained gzip payloads, checks all hashes and
3470 ordered nodes, and validates source/code/config/test equivalence. The
`.txt` source snapshots prevent evidence from becoming additional pytest tests,
formatter targets or executable workflow configuration.

## Actual source and delivery bindings

Measurement ran at `63a80a3c13f5d73b68773a2191986eb3f46d5b86`; all 21 named
focused tests ran at `cf87bad89061684ce984eeed6429f0eb3f3843c4`.
`source-bindings.json` records actual Git objects and SHA-256 snapshots across
those heads, reviewed/gated `8fe5cb7ff7cfee616e5aa22c11719279fc52f740`, and
merged main `108dfdb9aa13f9d9760e3318856312d027e5a4eb`. Every production,
test, configuration and workflow scope is identical. Outside evidence, only
`docs/CI_LINT_BASELINE.md` changed; the exact docs patch is included. These
bindings establish code equivalence; they do not claim commands reran on final
head. Full source bytes remain in the historical bundle and exact Git objects.

`authenticated-gate-review-merge.json` records actual Gate 38834930061 and job
outcomes on final head, native review comment 6205929444 explicitly reviewing
that head, and authenticated merged-PR readback. The complete original API
responses remain hash-bound durable run receipts.
`guarded-merge-receipt.json` records the actual guarded merge, zero active
threads, successful checks and elapsed seven-minute review floor.

## Acceptance remains pending

The executable check establishes byte reconstruction, source equivalence and
prior delivery receipts. Current successor CI, review, guarded merge and actual
native compare are separate obligations; no provider PASS is asserted. Root
#518 and generated successor #607 remain open pending their real acceptance.
STANDARD capture must include the entire successor diff. If its actual receipt
reports truncation or omission, record that limitation and use only a supported
source-owned capture rollout; never trim proof, rotate providers, evade the
fingerprint or infer acceptance from this local check.
