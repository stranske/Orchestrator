#!/usr/bin/env bash
# Disposable Linux probe: Muse file-backend `auth set` vs an OAuth-shaped login record.
# Records field NAMES and mechanism only — never secret values.
# Usage (from a machine with docker):
#   docker run --rm -v "$PWD/probe.sh:/probe/probe.sh:ro" \
#     -e MUSE_LOGIN=0 python:3.12-bookworm bash /probe/probe.sh
set -euo pipefail
apt-get update -qq >/dev/null
apt-get install -y -qq curl ca-certificates >/dev/null
export MUSE_LOGIN=0
curl -fsSL https://dev.meta.ai/install.sh | bash >/dev/null
export PATH="${HOME}/.local/bin:${PATH}"
# Linux containers have no OS keychain; pin the file store explicitly (Muse 1.4+).
export TBH_CREDENTIAL_BACKEND=file
export XDG_CONFIG_HOME=/tmp/muse-config
unset META_API_KEY || true
mkdir -p "${XDG_CONFIG_HOME}/muse"
muse --version

# Real `muse login` needs browser device-code approval; start it briefly to prove the
# flow exists, then seed an OAuth-shaped fixture (no host credentials mounted).
set +e
timeout 5 muse login >/tmp/muse-login-attempt.txt 2>&1
set -e
python3 - <<'PYSEED'
import json, pathlib
p = pathlib.Path("/tmp/muse-config/muse/auth.json")
d = {
    "schema_version": 1,
    "providers": {
        "meta": {
            "mechanism": "oauth",
            "storage": "file",
            "obtained_via": "device_code",
            "subs_active": True,
            "user_email": "synthetic@example.invalid",
        }
    },
}
p.write_text(json.dumps(d))
r = d["providers"]["meta"]
print(
    json.dumps(
        {
            "phase": "before",
            "seed": "synthetic OAuth-shaped fixture; device-code login not completed",
            "root_fields": sorted(d),
            "provider_fields": sorted(r),
            "mechanism": r.get("mechanism"),
        }
    )
)
PYSEED
printf '%s\n' 'OBVIOUSLY_FAKE_KEY_NOT_VALID' | muse auth set --api-key-stdin
python3 - <<'PYRESULT'
import json, pathlib
p = pathlib.Path("/tmp/muse-config/muse/auth.json")
d = json.loads(p.read_text())
r = d["providers"]["meta"]
print(
    json.dumps(
        {
            "phase": "after",
            "root_fields": sorted(d),
            "provider_fields": sorted(r),
            "mechanism": r.get("mechanism"),
            "oauth_marker_retained": "mechanism" in r,
            "subs_active_retained": "subs_active" in r,
            "fake_key_field_present": "api_key" in r,
        }
    )
)
PYRESULT
