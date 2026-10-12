# Issue 558: explicit author exclusion and coverage

- [x] In `src/adversarial.py` function `review_at_head`, filter excluded authors from explicit as well as automatic reviewer sets; surface insufficient coverage rather than silently seating an author or relaxing the threshold.

Explicit argument and environment reviewer lists never dispatch excluded authors. Partial and
fully excluded panels remain INCONCLUSIVE. Their receipts now retain the original requested
population and threshold 2, actual returned-reviewer and veto counts, and blocker evidence.
A smaller panel's cached PASS cannot satisfy the larger explicit request. Automatic selection
continues to exclude the author and uses its owner-approved threshold 1.

Ten new regression cases exercise these paths, including PASS and BLOCKED provider replies.
The production selection, dispatch driver, aggregation, recorder and memo lookup run against
disposable Brain/state; provider responses, confinement setup and checkout state are faked.
These tests do not qualify native confinement or paid provider behavior.

`proof.json` retains argv and source/test hashes. Temporarily restoring `_shortfall` and
`review_at_head` from both the prior head and the original implementation at `5247ea1` causes
all nine explicit-exclusion cases to fail with assertions and zero errors. Corrected production
was restored byte-for-byte, then the same nine cases passed. Raw logs and JUnit are retained losslessly in `proof.tar.gz`; `proof.json` binds each member.
The complete four-file focused suite passes 88 cases, both module selftests pass, and the
required whole-repository Black check passes with 394 files unchanged.

Exact collection is 3,144: the previous 3,134 plus ten new executed cases. The floor retains
all skip/selftest/mypy ceilings. This is an executed-node union, not a full-suite execution.
Remaining PR tasks, fresh hosted checkout/mirror verification and final acceptance stay open.

Tooling was loaded from official release sources into `/tmp` because pytest and Black were
absent and PyPI DNS was unavailable. Black ran unchanged with one worker and a periodic
asyncio timer to wake thread completions in this sandbox. Versions and runtime are in proof.json.
