# Issue428: complete Gemini prompt transport

The qualified original Orchestrator445 case (immutable merge74fca179ede178f6d655dfd662ef3ce6081cc765) produced a complete1,211,243-byte prompt. Fresh main7aed5cf launched it through bash argv and failed before provider startup with errno7 / exit126. The original failed backend and role IDs are retained in native-start-failure.json; this is no native judgment.

The existing Gemini offload rail now uses private0600 UTF-8 NDJSON stdin above64KiB, retaining its selected model, workspace/session isolation, timeout, and final-text response contract. Compact prompts retain their prior print argv. There is no truncation, activation, mirror installation, outcome rewrite, or acceptance grant.

## Falsifiable validation

The real shell/executable-provider-stub regression sends over1MB of Unicode and shell metacharacters, compares the decoded prompt SHA256 and rejects oversized provider argv. Original production from7aed5cf causes the exact E2BIG assertion failure (red log/JUnit). Candidate bytes are restored identically (hashes in proof.json), and all4 transport controls pass (green log/JUnit), including private-file permissions and cleanup on timeout/startup failure and compact-mode compatibility.

Focused related suites:234PASS and23subtests. Actual whole-tree collection2667; floor2667 is the previous2663 plus4 newly executed tests, preserving every skip/mypy ceiling. This is a union floor, not a new full local-suite claim. Both touched-module selftests, Black, Ruff, mypy and diff checks pass. Fresh hosted verify.py on the final PR head remains the PR verdict.

Reproduce: `python3 -m pytest -q tests/test_gemini_large_prompt_transport.py`; related command: `python3 -m pytest -q tests/test_{offload_log_per_run,offload_failure_evidence,offload_incomplete_reads_the_agent,gemini_workspace_one_spelling,adjudicator_retro,adjudicator_retro_evidence,adjudicator_qualified_input,adjudicator_collected_input,adjudicator_retro_collection,gemini_large_prompt_transport}.py`.

## Acceptance boundary

This bounded follow-up relates to source428 and merged515. It does not close428. Fresh-main inspection plus executable original named controls corrected older provider claims about absent packet builder, retrospective run, contested-verdict binding and weekly line (515 comment6101857157). Actual provider NON_PASS remains unchanged. Native cost, independent later truth, historical full-head evidence, manual389 publication and installed-runtime evidence remain separately UNKNOWN until observed.

The source-level repaired retry completed with decision_source=adjudicator_agent, no errors, native role/backend IDs and observed model in native-repaired-receipt.json. Its reject_blocker advice is retained verbatim and reviewed as insufficient for source closure: named historical acceptance gaps remain. This is source-path execution, not installed-runtime publication.
