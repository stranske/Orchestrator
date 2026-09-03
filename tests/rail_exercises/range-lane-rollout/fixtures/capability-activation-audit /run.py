
import json, sys
from pathlib import Path
import capability_activation_audit as m
p=Path(__file__).parent; ledger=json.load((p/'ledger.json').open()); m.HERE=p/'tree'; m.sibling_checkouts=lambda: []
cap=dict(ledger['fixture-activation'])
if sys.argv[1]=='break': cap['entrypoint']='present_fixture_module.py:main'; (m.HERE/'present_fixture_module.py').write_text('def main(): pass\n')
rep=m.entrypoint_presence(cap)
ok=rep['state']=='absent' and rep['missing']==['missing_fixture_module.py'] and 'missing_fixture_module.py is not in this tree' in rep['detail']
print(json.dumps({'status':'PASS' if ok else 'FAIL','entrypoint_presence':rep,'fixture_ledger':str(p/'ledger.json')},indent=2))
