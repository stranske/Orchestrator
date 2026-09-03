
import json, os, sys
from pathlib import Path
import range_lane_rollout as m
p=Path(__file__).parent; fx=json.load((p/'fixture.json').open())
if sys.argv[1]=='break': fx['backlog']['items'][0]['lane']='closer'
# Prevent preview subprocesses while exercising real filtering, router planning, sanitizing, and rollout assembly.
m._dispatch_preview=lambda d:[{'target':a['target'],'agent':a['agent'],'lane':a['lane'],'task_type':a['task_type']} for a in d.get('assignments',[])]
r=m.build_rollout(task_types={'testgen'},max_dispatches=1,backlog_payload=fx['backlog'],capacity_payload=fx['capacity'],dry_run=True)
ground=m.router.plan(r['selected_backlog'],fx['capacity'],max_concurrent=1,dry_run=True,learned=m.router.learned_ranks())
ok=r['read_only'] and not r['active_dispatch'] and r['counts']['dispatched']==0 and r['decision']['assignments']==ground['assignments'] and r['counts']['selected']==1
print(json.dumps({'status':'PASS' if ok else 'FAIL','rollout':r,'ground_assignments':ground['assignments'],'env_set':bool(os.environ.get('ORCH_RANGE_LANE_ROLLOUT'))},indent=2))
