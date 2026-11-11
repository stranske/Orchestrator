# Evidence-backed review dispositions

Check date: 2026-11-11. Implementation/evidence commit: `742351188ff41b0f8b12c564282691dae724e0f3`.
The focused command on the PR #1341 continuation printed `33 passed in 6.30s`:
`.venv/bin/python -m pytest tests/test_issue1232_ranked_coverage.py tests/test_verify_sharding.py -q --no-cov`.
Black printed `1 file reformatted`; the subsequent check printed
`1 file would be left unchanged`.

The GitHub connector rejected attempts to reply to threads and resolve them:
`MCP tool call requires approval, but approval policy is never`. These are prepared
dispositions, not claims that GitHub threads were resolved. Current thread state must be
read from GitHub. All evidence links below point to the implementation commit and remain
valid if a review bot independently resolves a thread.

- [Prior-rank case, comment 4527620224](https://github.com/stranske/Orchestrator/pull/1334#discussion_r4527620224):
  addressed. Input/alphabetical order alpha,zebra opposes prior zebra,alpha. The prior-key
  mutation fails exactly the zebra,alpha assertion; alphabetical fallback remains independently
  discriminating. [Prior red](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/06-red.txt),
  [prior restored](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/06-restored.txt).
- [Eligible plan, comment 4527620246](https://github.com/stranske/Orchestrator/pull/1334#discussion_r4527620246):
  addressed. Assertions cover operator_inspect, True eligibility, empty blockers, report existence,
  and exactly the five requested command-suffix elements with DEFAULT_AC. Cases 08–10 and 12
  each exit 1 mutated / 0 restored. [Exact suffix red](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/12-red.txt),
  [restored](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/12-restored.txt).
- [Alphabetical mutation, comment 4529783619](https://github.com/stranske/Orchestrator/pull/1334#discussion_r4529783619):
  addressed. Case 07 now replaces only the agent-name sort key. The prior-order assertion passes,
  then the alphabetical assertion fails with zebra instead of alpha. [Red](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/07-red.txt),
  [restored](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/07-restored.txt).
- [Backend mutation, comment 4529783657](https://github.com/stranske/Orchestrator/pull/1334#discussion_r4529783657):
  addressed. Case 11 executes test_keepalive_supervisor_proposal_backend, which actually enables
  proposal_backend and asserts the exact backend suffix. [Red](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/11-red.txt),
  [restored](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/11-restored.txt).
- [Blend value, comment 4529783672](https://github.com/stranske/Orchestrator/pull/1334#discussion_r4529783672):
  addressed. Named ready_priors is inspected after blending; task1/agent2 must equal approximately
  0.675 and comparison count remains 10. Case 13 changes the BT coefficient and fails at 0.680
  versus 0.675. [Red](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/13-red.txt),
  [restored](https://github.com/stranske/Orchestrator/blob/01cdadb92393c09510040fe65f5a12ba91bc98b3/docs/verification/issue-1324-round9/13-restored.txt).
- [Exact-tuple assertions, thread PRRT_kwDOUAH6nM62CWMJ](https://github.com/stranske/Orchestrator/pull/1341#discussion_r4534266920):
  addressed. Mutation cases 01-04 verify exact tuple assertions for outcome recording, gap prefixes,
  citation names, and dispatch-only recording removal. [Outcome tuple red/restored](https://github.com/stranske/Orchestrator/blob/742351188ff41b0f8b12c564282691dae724e0f3/docs/verification/issue-1324-round9/01-red.txt),
  [Gap prefix red/restored](https://github.com/stranske/Orchestrator/blob/742351188ff41b0f8b12c564282691dae724e0f3/docs/verification/issue-1324-round9/02-red.txt),
  [Citation names red/restored](https://github.com/stranske/Orchestrator/blob/742351188ff41b0f8b12c564282691dae724e0f3/docs/verification/issue-1324-round9/03-red.txt),
  [Dispatch recording red/restored](https://github.com/stranske/Orchestrator/blob/742351188ff41b0f8b12c564282691dae724e0f3/docs/verification/issue-1324-round9/04-red.txt).
  The dispatch outcome list now must contain exactly
  `("record_outcome", "test-run-1232-adjudication", "PASS_RUNTIME_AC", "runtime_ac_panel: PASS_RUNTIME_AC - pass ratio 2/2 met threshold 0.67")`.
  Case 15 changes only the reason passed to recording from the dispatch branch of
  `runtime_ac_panel.main`. The named test fails at the dispatch tuple because the notes
  contain `wrong dispatch notes` (exit 1), then passes after byte-for-byte restoration
  (exit 0). See the [failure](15-red.txt) and [restored pass](15-restored.txt).
- [Forwarding assertion, thread PRRT_kwDOUAH6nM62D_KS](https://github.com/stranske/Orchestrator/pull/1341#pullrequestreview-PRRT_kwDOUAH6nM62D_KS):
  addressed. Case 14 verifies plan_target forwards the exact target to gather_signals; a wrong-target
  mutation fails the forwarding assertion. [Forwarding red](https://github.com/stranske/Orchestrator/blob/742351188ff41b0f8b12c564282691dae724e0f3/docs/verification/issue-1324-round9/14-red.txt),
  [Forwarding restored](https://github.com/stranske/Orchestrator/blob/742351188ff41b0f8b12c564282691dae724e0f3/docs/verification/issue-1324-round9/14-restored.txt).
