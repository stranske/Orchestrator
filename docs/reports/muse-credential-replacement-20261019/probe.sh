#!/usr/bin/env bash
set -euo pipefail
mkdir -p /tmp/muse-bin
cp /probe/muse /tmp/muse-bin/muse
chmod +x /tmp/muse-bin/muse
export MUSE_LOGIN=0 XDG_CONFIG_HOME=/tmp/muse-config
/tmp/muse-bin/muse --version
export MUSE_NO_AUTO_UPDATE=1
python3 - <<'PYSEED'
import json,pathlib
p=pathlib.Path('/tmp/muse-config/muse/auth.json');p.parent.mkdir(parents=True,exist_ok=True)
d={'schema_version':1,'providers':{'meta':{'mechanism':'oauth','storage':'file','obtained_via':'device_code','subs_active':True,'user_email':'synthetic@example.invalid'}}}
p.write_text(json.dumps(d));r=d['providers']['meta'];print(json.dumps({'phase':'before','seed':'synthetic OAuth-shaped fixture; no actual login','root_fields':sorted(d),'provider_fields':sorted(r),'mechanism':r.get('mechanism')}))
PYSEED
printf '%s\n' 'OBVIOUSLY_FAKE_KEY_NOT_VALID' | /tmp/muse-bin/muse auth set --api-key-stdin
python3 - <<'PYRESULT'
import json,pathlib
p=pathlib.Path('/tmp/muse-config/muse/auth.json');d=json.loads(p.read_text());r=d['providers']['meta'];print(json.dumps({'phase':'after','root_fields':sorted(d),'provider_fields':sorted(r),'mechanism':r.get('mechanism'),'oauth_marker_retained':'mechanism'in r,'subs_active_retained':'subs_active'in r,'fake_key_field_present':'api_key'in r}))
PYRESULT
