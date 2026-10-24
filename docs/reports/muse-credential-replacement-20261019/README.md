# Muse file-backend credential replacement probe

Observed 2026-10-19 with Muse Code 1.4.10 (1.4.10-R6427.1) in a disposable
`python:3.12-bookworm` container (Debian bookworm). No host credential directory,
keychain, socket, or real API key was mounted. `MUSE_LOGIN=0` prevented interactive
login during install; `TBH_CREDENTIAL_BACKEND=file` pinned the plain `auth.json`
store. The probe used an obviously fake key (`OBVIOUSLY_FAKE_KEY_NOT_VALID`).

`probe.sh` installs the real Linux CLI, briefly starts `muse login` (device-code URL
printed; approval not available in CI), seeds a synthetic OAuth-shaped file fixture,
then calls `auth set --api-key-stdin`. `transcript.txt` records only version, field
names, the non-secret `mechanism` value, and presence booleans.

**Result:** `muse auth set` **replaces** the login record. After the call,
`providers.meta` retains only `api_key` — no `mechanism`, no `subs_active`, no other
seeded OAuth fields. The fields do not coexist.

This does not prove a completed real device-code login (browser approval). It does
prove the file-backend replacement operation the seat gate must refuse. No owner
credentials should be modified to fill the completed-login gap.
