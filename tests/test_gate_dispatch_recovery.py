"""Execute the Gate resolver with the native sealed-delivery dispatch contract."""

import json
import subprocess
from pathlib import Path

import yaml


def test_gate_dispatch_is_bound_to_current_open_same_repository_head():
    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/pr-00-gate.yml").read_text()
    )
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["pr_number"]["type"] == "string"
    step = workflow["jobs"]["resolve-pr"]["steps"][0]
    assert step["env"]["PR_NUMBER"] == "${{ inputs.pr_number || '' }}"
    script = step["with"]["script"]
    harness = r"""
const script = JSON.parse(process.env.SCRIPT);
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const execute = new AsyncFunction('github','context','core',script);
const good={number:7,state:'open',head:{sha:'abc',repo:{full_name:'stranske/Orchestrator'}}};
async function run(pr) {
  let output=null;
  try {
    await execute({rest:{pulls:{get:async()=>({data:pr})}}},
      {eventName:'workflow_dispatch',sha:'abc',repo:{owner:'stranske',repo:'Orchestrator'},payload:{}},
      {setOutput:(_k,v)=>{output=JSON.parse(v);}});
    return {ok:true,output};
  } catch(error) {return {ok:false,error:error.message};}
}
(async()=>console.log(JSON.stringify(await Promise.all([
  run(good),run({...good,state:'closed'}),run({...good,head:{...good.head,sha:'stale'}}),
  run({...good,head:{...good.head,repo:{full_name:'other/fork'}}})
]))))();
"""
    import os

    result = subprocess.run(
        ["node", "-e", harness],
        env={
            **os.environ,
            "SCRIPT": json.dumps(script),
            "PR_NUMBER": "7",
            "GITHUB_REPOSITORY": "stranske/Orchestrator",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rows = json.loads(result.stdout)
    assert rows[0]["ok"] and rows[0]["output"]["number"] == 7
    assert all(not row["ok"] and "current open same-repository" in row["error"] for row in rows[1:])
