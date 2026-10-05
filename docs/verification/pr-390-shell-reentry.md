# Shell reentry through a publication-parent alias

On 2026-10-05, the generation reader could select the old physical generation,
release its lock, and then open a new shell script through an aliased command
argument. `mirror_reader.run()` canonicalizes the mirror's parent, but the shell
passed its original `ORCH_REPO` spelling as the command path. The helper's command
rewrite therefore missed an absolute parent alias or a relative path.

`orchestrate.sh` now canonicalizes the parent before reader selection, leaving
the mirror publication link unresolved. The helper can rewrite the shell command
to the selected generation before unlocking.

The witness in `tests/test_mirror_reader_reentry.js` pauses the production helper
immediately before `os.execvpe`, after generation selection and lock release.
It then publishes a new generation with the production installer and resumes the
reader. Absolute `ORCH_DIR` aliases, relative aliases, and script self-location
through a parent alias all reproduced `SHELL:new` with old Python and child
module values before the repair. With the repair, all three return `SHELL:old`,
old Python and child values, and the original physical generation root.

The six retained cases use the same observer and publisher. Three negative
controls remove only the new parent-anchoring assignment from the fixture's
production shell prologue; three run the current production prologue unchanged.
Temporary mirrors are isolated from installed wrappers and live runtime state.

Validation passed:

```bash
node --test --test-isolation=none tests/test_mirror_reader_reentry.js tests/test_install_verified_snapshot_reader.js
PYTHONPATH=src:. python3 -m unittest -q tests.test_mirror_generations
bash tests/test_mirror_generation_imports.sh
bash -n scripts/verify_before_sync.sh orchestrate.sh
git diff --check
```

The Node command passes ten cases, including six new reentry cases. The unittest
command passes thirteen cases. The shell import witness detects mixed generations
with the incumbent in-place publisher and retains one generation with the
production publisher.

The required six-file pytest baseline remains pending: this runner has no pytest,
and installing it failed because `pypi.org` could not resolve. Black is also absent;
this change edits no `.py` files. No task checkbox is completed on this evidence
alone. GitHub PR state could not be read because `api.github.com` was unreachable;
this run has made no remote PR changes. Post-merge/pull wrapper installation and
live guarded-publication evidence remain pending.

The workspace mounts `.git` read-only, so `git add`/`git commit` cannot update the
shared branch. The repair is committed in an isolated checkout under `/tmp`, with
a format-patch artifact for applying it to the branch from a writable checkout.
