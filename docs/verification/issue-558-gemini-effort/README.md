# Gemini effort recovery for source #558

Merged #560's native boundary proof remains intact. Its full local verification exposed model-catalog drift. This bounded follow-up moves Flash pins and profiles together from 3.7 to the installed 3.8 high/low identities and fixes rename selection so low/medium requests do not silently become high when the replacement family advertises the requested effort. An explicit Pro3.1 profile is retained.

Production mutation: restore the old highest-effort-first resolver. The low and medium named cases fail by assertion, high remains a control. Restore the exact candidate bytes: all three pass. Raw console, JUnit, argv, cwd, exits and production hashes are in receipt.json and red/restored-green files. Original candidate restoration is byte-identical.

Focused model/capacity suites: 87 PASS, 3 FAIL. The three failures are unchanged real live guards: Pro3.1 is absent from the installed catalog, so default-pin, all-tier-pin and registered-profile availability guards remain red. No skip, relaxed assertion, fabricated Pro identity, paid trial or model setting change was made. Literal-pin argv wiring is isolated from the external catalog; separate live guards remain active.

All three affected module selftests pass. Dispatcher now gives its historical 3.6 reported-label fixture a matching historical catalog, rather than asking the current host catalog to certify a retired fixture label. Production identity resolution remains strict.

Exact collection: 3190 = base 3187 + 3 new cases, all existing ceilings preserved. Full hosted verification and fresh exact-head review gates remain required. This is bounded recovery, not complete source558 acceptance, provider PASS or mirror deployment.

Original fixed snapshot acd91af full private verification:3187 PASS,3 live Pro FAIL,0 SKIP,3190 collected,104/104 selftests and all5 gates PASS. Natural exit1 remains explicit. Rebased after concurrent566 merge68c79c9: expected integrated union3211 = current main3208 +3; fresh hosted checkout/flat mirror validates the final head.
