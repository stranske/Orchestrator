# Strict verification under small pipe capacity

The local strict release of merged #859 plus its reconciled floor collected all
7882 tests. It recorded 7859 passes, two failures and 21 skips at the existing
ceiling. All 115 module selftests, five capability gates and the 153-module mypy
ratchet passed. Installation remained blocked.

Both failures reproduced together outside the full suite. Fresh nonblocking
pipes on that macOS host accepted 512 bytes. Homebrew Bash 5.3.15 heredoc controls
of 1000 and 10000 bytes timed out; the same controls passed under system Bash
3.2. The release continues to use its existing shell selection.

`test_mirror_generation_imports.sh` supplied its Python program through a
heredoc. Bash writes that program before its reader starts. Move the unchanged
Python witness to a file and execute it with the same repository argument. The
old and moved Python ASTs match, including every publication/import assertion.
The shell deadline and the witness's internal alarm remain unchanged.

The watchdog end-to-end test captures its tick log in a pipe and waits for the
durable drain before reading that log. `watch()` previously printed/flushed an
alert before saving its observed drain. Under log backpressure it could block
with the initial journal still present. Save the already computed record before
writing each alert. A failed or blocked log consumer then cannot hide the drain
that has already been observed. This does not promise nonblocking log output or
change the report-only policy, process identity checks or signal authority.

Three added regressions fail on the preceding code: two unavailable-log cases
(write and flush), and a structural file-backed witness guard. With the repair,
39 watchdog, mirror-witness and shell-transport neighbor tests pass under the
same approved process-observation profile as strict verification, including both
original timeouts. No timing, collection floor, skip ceiling or capability gate
was relaxed. A sandboxed diagnostic matrix that denied `ps` was excluded from
validation evidence.

The failed strict journal/log, focused reproduction XML and input provenance
remain retained outside synthetic pytest fixture trees. No whole-suite retry is
justified until this repair is reviewed and the normal 27 GiB space admission
passes again. Native publication is separate from this repair.

The first PR CI run exposed a separate flat-distribution fixture amplification:
`test_closer_native_observer.host` copied the parent of `closer_campaign.py`.
In a checkout that is `src/`; in the shipped flat distribution it also contains
tests and docs. Repeating that copy for native qualification cases exhausted
the CI job's disk quota. The retained local failed run had 724248 synthetic
entries and about 19.5 GB of logical fixture data.

The fixture now copies `campaign.module_closure(source, campaign.PINNED_MODULES)`,
the existing production runtime closure. Manifest creation still seals that
closure. A regression builds a flat source containing unrelated test/doc payloads,
executes the real fixture, and verifies both exact closure membership and the
sealed manifest. This reduces repeated fixture payload without dropping runtime
modules, changing qualification assertions, or reducing the release space guard.
