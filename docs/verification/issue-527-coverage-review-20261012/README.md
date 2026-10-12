# PR545 cost coverage review recovery

Prior unchanged production at36c0910 fails three intended assertions: no estimate must not be flagged imputed; zero-dollar/no-pool report must not be flagged imputed; coverage must have one exclusive population including a retained legacy dollar receipt without backend identity. Thirty-six other cases pass, including six new Gemini terminal fixtures. Candidate source passes95 focused Python tests and54 CLI tests. This is an original-production RED then candidate GREEN, not a claim of a separate mutation of the repaired source.

The estimate flag is false unless an eligible own-agent/global pool produced an estimate. Summary coverage classifies each attempted backend/legacy measured receipt exactly once. Historical dollar aggregate semantics are retained; new role-dollar admission still refuses zero-dollar costs. The CLI expectations now forbid impossible counts and duplicate classification, and Gemini stream-json usage is covered.

Existing main e626002 was integrated; only verification-floor content conflicted. All main skip/type ceilings retained; exact collection3000 includes main2961 plus32 existingPR545 nodes and7 new nodes. Full isolated checkout executes2992PASS/8 named missing-state skips,100selftests/4skips,3gates/2skips; separate existing-state snapshot verification follows. No paid native call, live Brain write or manual mirror publication. Fresh hosted topology, all active reviews, seven-minute new-head floor and actual post-merge comparison remain required.

Final existing-state private-snapshot verification:3000PASS/0FAIL/0SKIP,104of104selftests,5of5gates,122mypy modules checked, no exemptions. Pytest7m17s, selftests27s, gates37s. Full raw summary in full-checkout-verifier.txt.
