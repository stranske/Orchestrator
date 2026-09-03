
import contextlib, io, json, sys
from pathlib import Path
import feature_scan as m
p=Path(__file__).parent; root=p/'tree'; reg=p/'registry.json'; before=reg.read_bytes()
if sys.argv[1]=='break': (root/'unlogged.py').write_text('def no_doc(): pass\n')
b=io.StringIO()
with contextlib.redirect_stdout(b): rc=m.main(['--root',str(root),'--registry',str(reg),'--json'])
rep=json.loads(b.getvalue())
if sys.argv[1]=='break': (root/'unlogged.py').write_text('\"\"\"qualified\"\"\"\ndef _selftest(): pass\n')
names=[r['name'] for r in rep['unlogged']]
ok=rc==0 and rep['qualifying_modules']==2 and names==['unlogged'] and reg.read_bytes()==before
print(json.dumps({'status':'PASS' if ok else 'FAIL','report':rep,'registry_unchanged':reg.read_bytes()==before},indent=2))
