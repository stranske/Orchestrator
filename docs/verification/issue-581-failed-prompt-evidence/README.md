# Failed prompt evidence (issue 581)

The production prompt role now returns a rejected proposal separately from the accepted
proposal. The existing batch writer retains failed authored bodies and raw backend output
beside its manifest; only valid, successfully recorded proposals use `NNN.md`.

## Local proof

- Original production source with the new/extended regressions: **6 FAIL, 19 PASS**.
- Corrected prompt batch suite: **25 PASS**.
- Six related role suites: **65 PASS plus 6 subtests**, no skips.
- Roles module selftest and focused mypy pass. Ruff, Black and whitespace checks pass.
- Exact collection **3299 = merged base3295 +4 new nodes**. The executed-node union
  floor preserves all skip, selftest, gate and mypy ceilings; it is not a full-suite PASS.
- Full local `python3 src/verify.py`: **3296 PASS, 3 FAIL, 0 SKIP, 3299 collected**;
  104/104 module selftests, all five gates and the 122-module mypy ratchet pass.
  The three failures are Gemini catalog availability guards tracked by source578.
  All3299 nodes executed; the derived pass-floor diagnostic does not mean missing tests.
  This full run remains RED; no pin, model or effort setting was changed.
- Exact unchanged base `34bfcff4816cecd9d18bdc957e22c44ba7ab7740` independently
  reproduces those same three guard failures; both test files and the capacity/adapter
  source bytes match this branch (`581-base-catalog-bindings.json`). The configured
  Gemini Pro pin is absent from the live catalog; the existing source578 runtime owner
  retains qualification. This evidence does not waive hosted checks.

`581-mutations.json` binds both surgical mutations and the restored production bytes.
Removing only `rejected_proposal = proposal` makes the named mixed-batch regression fail.
Changing only the failed-body filename to `NNN.md` makes the CLI no-publication assertion
fail. Each mutation exited1; its byte-identical restoration exited0. Raw logs are adjacent.

The named mixed batch uses actual production role/batch/writer calls with a private Brain:
a valid replay, validator-rejected `Bad body` replay and unparseable backend output. The
other controls cover valid bodies withheld on backend or role-recording failure, exact
UTF-8/CRLF/trailing-whitespace retention and target-independent ordinal filenames.
No external model call is made by these tests.

## Replay

```sh
python3 -m pytest tests/test_role_prompt_batch.py -q
python3 -m pytest tests/test_role_prompt_batch.py tests/test_role_prompt_offering.py tests/test_role_prompt_offering_controls.py tests/test_role_compiler.py tests/test_roles_lineage.py tests/test_rejected_role_capability_edges.py -q
python3 src/roles.py --selftest
python3 -m mypy src/roles.py
python3 -m ruff check src/roles.py tests/test_role_prompt_batch.py
python3 -m black --check src/roles.py tests/test_role_prompt_batch.py
```

## Remaining delivery evidence

Hosted exact-head CI/review and the next real role-authored batch of at least five bodies
remain independent requirements. After normal merge and mirror sync, the existing program
owner checks that every failed first attempt has a retained body or raw output and the
per-half first-pass rate can be computed from its manifest alone (next audit/research batch,
or the source issue's2026-10-27 check). No production batch, mirror deployment, provider
PASS or future program acceptance is claimed here. Keep/reopen source581 until those
criteria have an actual disposition.
