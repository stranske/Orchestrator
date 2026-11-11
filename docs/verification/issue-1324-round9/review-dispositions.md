# Evidence-backed review dispositions

Check date: 2026-11-10. Implementation/evidence commit: `01cdadb92393c09510040fe65f5a12ba91bc98b3`.
The focused command passed 33 tests in 6.24s when rerun on that committed head.

The GitHub connector rejected attempts to reply to comment 4527620224 and resolve its
thread: `MCP tool call requires approval, but approval policy is never`. These are prepared
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
