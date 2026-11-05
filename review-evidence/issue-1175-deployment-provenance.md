# Issue 1175: deployment provenance work

The exporter now marks output produced by the hosted challenger filter with
`exploration.challenger_filter = "hosted-runnable-alternatives/v1"`.

To retain deployment evidence, supply a JSON object containing `published_commit`
(full SHA), `deployed_at` (UTC timestamp with whole seconds), and
`deployment_evidence` (reference to the successful publication's audit record):

```sh
python3 src/route_weights_export.py --record-challenger-deployment /path/to/provenance.json --state-dir /path/to/state
```

The recorder fetches the export branch, requires complete history, and verifies
that the supplied commit is its first explicitly marked filter artifact. It
validates the challenger map and artifact timestamp, rejects missing or malformed
provenance, and preserves the first receipt. Deployment time must come from the
external audit record; artifact creation and Git commit times are not substitutes.
The recorder verifies Git history, not the external audit record's authenticity.

Local verification: 15 regression tests passed using standard-library unittest,
including real `publish_document` pushes and unchanged publications against
temporary bare remotes. The exporter's existing `--selftest` also passed.

No production acceptance checkbox is complete. This runner cannot reach GitHub,
has no production feedback database, and cannot establish the required fourteen
days of observation. The production query must run no earlier than 2026-11-08
and fourteen days after the verified deployment instant, whichever is later.

The required Anaconda interpreter, pytest, and Black are unavailable. Package
installation failed because DNS is unavailable. The mandatory Black formatting
gate therefore could not pass, so these Python changes must remain uncommitted
until Black and the required pytest checks can run successfully.
