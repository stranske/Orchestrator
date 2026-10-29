# Closer verified-release preflight

`scripts/closer_release.py` makes the host assumptions required by the existing
`ReleaseRecord.verify_and_install` path explicit. It is not a launcher and it
does not authorize native observation, provider work, scheduled work, or an
acceptance decision.

The default interpreter is `/opt/anaconda3/bin/python3`; no Apple Python
fallback is attempted. The command checks clean, exact source and Core Git
revisions, confirms that Core contains `scripts/closer_runtime.py`, and runs a
fake-HOME subprocess check proving that `python3` resolves to the selected
interpreter. It supplies a process-local PATH, Core binding, and four xdist
workers to the already-existing verifier and CAS installer.

The wrapper removes ambient `CLOSER_CORE_PACKAGE` and plugin-autoload disabling
settings before it calls the verifier. This prevents an older Core binding and
ensures pytest-xdist's required `-n` option is available. Check the wrapper
itself without touching a source, Core checkout, journal, or mirror:

```bash
/opt/anaconda3/bin/python3 scripts/closer_release.py --selftest
```

Run the non-mutating check first:

```bash
/opt/anaconda3/bin/python3 scripts/closer_release.py preflight \
  --source /path/to/clean/orchestrator-source \
  --expected-source-sha SOURCE_SHA \
  --mirror /path/to/orchestrator-mirror \
  --core /path/to/clean/core \
  --expected-core-sha CORE_SHA \
  --python /opt/anaconda3/bin/python3 \
  --journal /path/to/release.json \
  --owner closer
```

After a green preflight, use the same arguments with `install`. Only that mode
calls `ReleaseRecord.verify_and_install`, preserving its verifier receipt,
generation-token compare-and-swap, and installed-digest readback. Both modes
write one machine-readable JSON object to stdout. A preflight error returns
exit status 2 and does not create or alter the release journal or mirror.
