# Issue 428: first local evidence collection batch

The collection-only path reads complete regular Git blobs from a saved case’s evaluated commit and writes a separate output. It never dispatches a role, updates feedback, or upgrades a retrospective verdict.

`receipt.json` records an actual collection of the two source paths named in the linked finding. Both blobs were read completely, with their object IDs, byte lengths and SHA-256 digests recorded. The original retrospective report remained byte-identical. The source inventory is deliberately partial: no acceptance inventory was supplied, so overall completeness remains **false**. This is collection plumbing, not acceptance or provider-trial completion.

The output transport is local Git only. Artifact/log retrieval, authoritative acceptance inventories, provider-native measured usage, and later-truth grading remain separate follow-up work.
