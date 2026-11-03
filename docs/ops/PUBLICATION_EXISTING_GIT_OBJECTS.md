# Existing Git objects for bounded publication

The prospective private `closer-owned-pr-publication-policy/v2` uses the existing
owned repository as its Git object anchor. It retains H, the complete tree and
path/mode/blob-OID/size/SHA256 metadata, plus the existing bounded changed-file
payload. It does not retain unchanged file contents, seed another object store,
copy a worktree, clone, or create an alternates file. Public correction request
v1 is unchanged.

A new original coding admission explicitly selects `publication_v2`. Its policy
must remain byte-identical for coding, later publication and recovery. Mixed
v1/v2 keys, migration, addition and removal after admission are refused. Existing
v1 records retain their original meaning and are not upgraded.

## Separate baseline inventory bounds

The closed `baseline_limits` object has schema
`closer-publication-baseline-limits/v1` and these maximum values:

| Field | Maximum |
| --- | ---: |
| `max_files` | 4096 |
| `max_file_bytes` | 83886080 |
| `max_total_bytes` | 134217728 |
| `max_inventory_bytes` | 1048576 |

These limits bound the original complete repository inventory, including its
single physical workspace verification pass. They do not increase changed-file,
patch, ACK, model, request-time, generic inspection or 1 MiB custody limits. The
complete source-baseline and candidate custody envelopes must still fit 1 MiB.
The original request deadline and per-native-command bounds still apply.
Read-only inventory uses the existing original writer's `check_context` on each
entry, full source/native checks at entry and exit (including exceptional exit),
and the original full checks before each native phase. No read-loop check issues
an effect permit.

Existing tracked `.gitattributes` can be inventoried and retained by OID only.
It cannot be added, edited or deleted as a candidate. Other protected control
paths remain refused. Original config/filter/hook restrictions are unchanged.

## Actual source and native boundaries

Only the actual HostGit source derives the ODB from authenticated worktree/common
Git topology. Native fixed `cat-file --batch-check` verifies the exact finite H,
derived trees and blob OIDs/types/sizes. This is native identity and availability,
not an independent cryptographic rehash of every stored blob body. H/tree and
changed bytes retain their existing hash checks; the clean physical workspace
inventory verifies its bytes against H before coding.

Held ODB and pack directories, pack/idx and selected loose-file identity
fingerprints are checked before each phase. Loose objects do not consume one
permanent file descriptor each. Missing objects, GC/pack replacement, symlinks,
changed metadata or foreign object sources hold. The source-owned environment
sets `GIT_NO_LAZY_FETCH=1` and `GIT_NO_REPLACE_OBJECTS=1`; promisor/configuration
and alternates sources remain unsupported. Nothing fetches or rebuilds a missing
anchor.

Private staging contains only control/config/hooks, an empty worktree, a private
index/HEAD/ref, and a private native temporary directory. The source sets
`GIT_OBJECT_DIRECTORY` to the actual held original ODB. Fixed `read-tree`, changed
blob hashing, `update-index`, `write-tree` and commit plumbing add normal new Git
objects there. Background Git GC/maintenance is disabled in the private config.
The original working files, real index, local branch HEAD and refs stay unchanged.
The exact candidate tree and single H parent remain required before the original
one-push WAL, force-with-lease, authenticated owner/head readback and verification.

After a known remote ACK, local original H/index may still be at H. A later
ordinary provision/reconciliation step must observe the new remote C; this path
never silently resets or cleans the original worktree. UNKNOWN or partial command
history remains non-repeatable even if objects still exist. A fresh root may
reopen known protected staging only against current source admission and the same
complete anchor; lost objects hold rather than authorizing recovery writes.

## Observed data proof and limits of that evidence

The actual d194 repository proof used 3387 files, 125281416 raw bytes and a
73640875-byte largest file. Its metadata was 804409 bytes; one 443-byte canary
change produced the exact new tree/commit through the existing ODB in 3.094 s.
Private disk was 463821 bytes, with zero unchanged-body, seed or worktree copies.
Original HEAD/refs/index, canary and `.gitattributes` were unchanged. A peer
independently joined that commit/tree/parent and the unchanged originals.

This is real local Git DATA evidence, not a fabricated controller admission,
provider execution, publication or activation. The receipt is
`/private/tmp/closer-d194-existing-odb-positive-20261027/RESULT.json`; the separate
raw-copy snapshot experiment remains diagnostic history and is excluded from this
source path. Runtime admission, native account, actual coding and publication
must still use their genuine source-controlled issuers.

## Timestamp-only Git freshening

Native Git can refresh an existing loose object or pack timestamp while writing
an already present object. An unchanged timestamp remains a metadata-only check.
A changed timestamp requires bounded content verification before the new times
are cached: loose payloads must match the original SHA256 and native object ID;
packs must match their native Git container ID and trailer; index and sidecar
bytes must match the original SHA256. Device, inode, owner, mode, link count and
size remain fixed. Same-inode content changes remain held.

The protected anchor retains the original fingerprints, encoded as exact decimal
strings for custody compatibility. Cold reentry supplies those original values;
it cannot adopt the current timestamps as a new baseline. Source-owned streaming
reads use the existing writer lifetime/deadline context and fixed byte bounds.
No unchanged object body is copied or retained, and unchanged barriers do not
rehash packs. Partial/promisor object stores and unknown pack inventory remain
unsupported; this fix does not turn missing objects into fetch permission.
