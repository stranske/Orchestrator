# Root #518 successor #607 measurement

This is a repository-local preparation for the existing generated dev-tool PR
#607, not a replacement pin bundle or an unrelated intake item. The prior
accepted #519 history and evidence remain intact.

`report.json` records the actual strict collect operation against source
3da6bb956cecbb4ff1eca70a4e40c88f8573a341. `commands.json.gz` (losslessly compressed; hashes/byte counts in
`commands-integrity.json`) retains captured
argv, cwd, exit codes, stdout and stderr, including pytest collection startup.
`provenance.json` binds all nine installed versions, Python, source and
measurement script/pin hashes. `proposed-pins.env` is authenticated #607 input.
The run used a task venv and the unchanged measurement script, overriding only
its PIN_FILE to this retained input. It did not relax strict version checks.

`original-baseline-red.log` proves the old document rejects the proposed pins.
`successor-baseline-green.log` records the same check after this measured
record is added. Unknown/incomplete/hybrid controls remain active. Normal
repository pins are restored after each overlay in a finally block.

The zero coverage startup count is not a full-suite or provider verdict.
Hosted exact-head CI, review, guarded merge, native verification and source-owned
Maint52/71 regeneration remain required.

Native review4289533170 exposed the prior four-pin matching gap. The final
checker requires all nine tools; each tool is independently exercised by the
unknown/incomplete/hybrid controls without adding collected nodes. The actual
current bundle was freshly measured too (current-pins/), rather than padding
historical markers with unobserved versions. Native4289533177 also corrected
the current collection floor to3,463. The floor file itself is unchanged.

Native4289572205 corrected strict collection itself: all nine distribution versions are checked against exact pins before any measurement, in addition to existing actual CLI version checks. A one-node regression loops all nine mismatches and passes only when every mismatch refuses measurement. Original RED and21focusedGREEN retained. Floor advances only one executed regression node,3463 to3464, with every ceiling unchanged; hosted full verification remains mandatory.
