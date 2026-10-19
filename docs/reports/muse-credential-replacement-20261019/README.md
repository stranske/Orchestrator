# Muse file-backend credential replacement probe

Observed 2026-10-19 with Muse Code 1.4.10 (1.4.10-R6427.1), in the existing python:3.14-bookworm image (Debian bookworm, aarch64). The container had only the Muse launcher and probe script mounted read-only. No host credential directory, keychain, socket, or real API key was mounted. `MUSE_LOGIN=0` prevented interactive login during download; the probe used an obviously fake key.

`probe.sh` creates a synthetic OAuth-shaped file fixture and calls the real Linux CLI `auth set --api-key-stdin`. `transcript.txt` records only version, field names, the non-secret mechanism value, and presence booleans. The observed replacement removes all seeded OAuth fields, retaining only `api_key` in `providers.meta`. This is an actual CLI transition, not a mocked auth implementation.

This does not prove that a real device-code login was performed. The original issue's explicit real-login task remains unproven by this fixture experiment. Tests and experiment winner scores do not establish that task. No owner credentials should be modified to fill this gap.
