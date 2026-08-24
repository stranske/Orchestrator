# Improvement backlog — POINTER. The log itself is machine-local.

**Do not append to this file.** It is a tracked pointer, a few lines long, and `test_improvement_log.py`
fails if it grows into a log. The real improvement log — the numbered items and their status notes —
is this instance's EVIDENCE, not the tool, so it lives outside the tree with the ledger and the Brain
(`$ORCH_LOCAL_RUNTIME/IMPROVEMENT_BACKLOG.md`, default `~/.codex/orchestrator/`). Reach it through the
accessor, which resolves the path for you:

```bash
python3 src/improvement_log.py search <term>        # CLAUDE.md §0 step 3 — is this already DONE?
python3 src/improvement_log.py append <item-ref> "<note>"   # CLAUDE.md §5 — record a status note
python3 src/improvement_log.py path                 # where it resolved to, and whether it is here
```

`search` prints each hit under the item heading that owns it, plus the number of lines and sections it
read, so "no matching items" is a statement about a file that was actually read. When the log is not on
this machine at all — a fresh clone, a CI runner, a second instance — every command says what is
missing and where it would be, and exits 2 rather than returning empty.

## Why this pointer exists

The rules above are mandatory and, until this file, unfollowable by the workers they bind. The log was
gitignored, and **a gitignored file does not exist in a git worktree** — agents work in worktrees, so
the project's own countermeasure against its #1 defect (building something that already exists) was
invisible to every worker required to consult it. Nothing in a worktree even hinted the log existed.
Moving it to machine-local state is what `CLAUDE.md` §1 already prescribes for runtime state and
instance evidence; this pointer is what makes it findable from anywhere.
