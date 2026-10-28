# Existing-PR publication Git context

This source leaf adds an explicit optional `git_environment_v1` only to the
existing v2 publication policy. It pins a private Git HOME and exact global
configuration bytes. Git reads and the existing backend commit/push use the same
child environment. Root HOME, model-account routes, native API context and user
Git settings remain unchanged. `GH_CONFIG_DIR` explicitly selects the original
stored GitHub CLI configuration; its content is never copied or read by this
preparer. Existing native helper/API-principal production checks still run.

The original source environment is rechecked before every observed Git child.
Unknown inherited Git/runtime overrides still refuse. Private configuration,
canonical configuration and hooks retain the same name parser, physical-file
and directory checks. The private-home directory/config inventory is closed;
extra paths, changed content, symlinks, executable configuration or active hooks
hold. This does not allowlist the user's executable LFS configuration.

`inspect_git_context_data` reuses that same canonical physical collector to
compute the necessary context digest before admission. It accepts a policy
without the still-unknown digest, not a placeholder. It runs bounded local
read-only Git commands and returns DATA only, with no native principal or runtime
authority. Actual `_observe_git_context` retains original source-native transport,
operation/writer/lifetime checks, compares the exact approved digest, and obtains
the original native helper/API principal before constructing HostGit.

`provision_absent_owned_head` uses a genuine configured canonical clone and
managed worktree namespace. The exact owned public PR head must already exist
locally. It checks safe configuration and hooks before checkout, requires both
the local branch and worktree to be absent, and uses Git's create-only `-b`
without `-B`, force, holder reclamation, prune, reset, fetch, or push. Unknown or
partial outcomes remain for inspection; there is no automatic creation retry.
The result is a local DATA receipt and is not native ownership or admission.
Existing worktree_git metadata checks still authenticate real topology later.

Actual local proof used the complete existing Orchestrator ODB and public PR989
head `05a7ce20246df00b09daf091d16e5f25c13e96a7`, in a fresh local namespace. The
original source repository's refs/index/main were preserved. A single local
clone and create-only named worktree were prepared, with no remote request or
push and no provider operation. Root must review and compose these source bytes
before enrolling the new policy. The genuine helper/API principal is obtained
only by the actual later Root callback; preparatory DATA cannot mint it.

For the actual canary, the initial process PATH is frozen to physical known
Cellar Git/GH binaries, existing user-local model launchers and system bins,
before admission. The generic Homebrew bin directory is group-writable on this
host and is omitted; it is not chmodded or silently accepted. Tool-injected
GIT_PAGER is removed only at that initial process boundary. This is per-process
preparation, not a global settings change or an environment reset mid-operation.

Publication/provision modules are outside the existing twenty-source model-role
descriptor. Root's full source/runtime closure and ordinary before-effect guards
still bind them. No additional model-quality callback or qualification claim is
made by this host-only correction. Existing first-PR, no-copy, queue/WAL, UNKNOWN,
lease, selected-proof, action, and required-test guards remain unchanged.

Private HOME alone isolates global configuration but does not isolate Git's
compiled system configuration. The source-owned Git child therefore sets both
`GIT_CONFIG_SYSTEM` and `GIT_CONFIG_GLOBAL` to the same existing regular owned
configuration file whose exact hash is already pinned. Both paths are observed
by the canonical inventory. No `/dev/null`, caller-selected system override or
ambient Git environment is accepted. Root and user system/global files remain
unchanged; only Git children receive these internal environment fields.

The Git-reported native POSIX shell may have a protected lexical alias route
(on merged-/usr Linux, for example). Source observation now pins each parent
symlink and the direct sh-to-dash/bash alias through original directory file
descriptors, lstat identities and readlink values, plus the canonical shell
file and hash. Only supported POSIX basename/direct same-directory shell routes
are added; arbitrary file symlinks/foreign targets still hold. The alias route
is part of the approved context and is checked before effects. Regular physical
Mac shell paths retain their original representation. Missing/unreadable private
Git paths return a fixed PublicationHeld after cleanup, without raw path export.
