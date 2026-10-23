'use strict';

// Exercise the actual consumer export, not an event/helper-only surrogate.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { buildVerifierContext } = require('../.github/scripts/agents_verifier_context.js');

const head = 'b'.repeat(40), merge = 'c'.repeat(40), base = 'a'.repeat(40);
const structured = '## Tasks\n- [x] Retain authentic requirements.\n\n## Acceptance Criteria\n- [x] Emit revision-bound evidence.\n\nOriginal prose obligation outside headings: preserve café provenance.';
const patch = 'diff --git a/src/value.py b/src/value.py\n--- a/src/value.py\n+++ b/src/value.py\n@@ -1 +1 @@\n-old\n+new\n';
const subject = 'stranske/orchestrator';

async function isolated(work) {
  const cwd = process.cwd();
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-verifier-builder-'));
  const keys = ['VERIFICATION_EVENT','VERIFICATION_MATERIAL','VERIFICATION_PREDECESSOR',
    'VERIFIER_MODE','VERIFIER_MODEL','VERIFIER_MODEL2','VERIFIER_PROVIDER','VERIFIER_SEMANTIC_MODE',
    'GITHUB_RUN_ATTEMPT','GITHUB_WORKFLOW_REF'];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  for (const key of keys) delete process.env[key];
  process.env.VERIFIER_PROVIDER = 'anthropic';
  process.chdir(directory);
  try { return await work(directory); }
  finally {
    process.chdir(cwd);
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key]; else process.env[key] = saved[key];
    }
    fs.rmSync(directory, {recursive:true,force:true});
  }
}

function fixture({body=structured, issues=[], comments=[], diff=patch, skipped=true}={}) {
  const outputs = {}, calls = {files:0};
  const pull = {number:7,title:'Verify actual consumer builder',body,merged:true,commits:1,
    merged_at:'2026-10-23T00:00:00Z',html_url:`https://github.com/${subject}/pull/7`,
    merge_commit_sha:merge,base:{ref:'main',sha:base,repo:{full_name:subject}},
    head:{sha:head,repo:{full_name:subject}}};
  const priorRun = () => ({id:77,run_attempt:1,status:'completed',conclusion:'cancelled',
    head_sha:'d'.repeat(40),path:'.github/workflows/agents-verifier.yml',event:'workflow_dispatch',
    repository:{full_name:subject},head_repository:{full_name:subject}});
  const github = {graphql:async()=>({repository:{pullRequest:{closingIssuesReferences:{
    nodes:issues,totalCount:issues.length,pageInfo:{hasNextPage:false}}}}}),rest:{
    pulls:{get:async()=>({data:pull}),listCommits:async()=>({data:[{sha:head,parents:[{sha:base}]}]}),
      listReviewComments:async()=>({data:[],headers:{}}),listReviews:async()=>({data:[],headers:{}}),
      listFiles:async()=>{calls.files++;throw new Error('Consumer API diff fallback must remain disabled');}},
    issues:{listComments:async()=>({data:comments,headers:{}})},
    actions:{listWorkflowRunsForRepo:async()=>({data:{workflow_runs:[]},headers:{}}),
      listWorkflowRuns:async()=>({data:{workflow_runs:[{id:21,head_sha:merge,status:'completed',conclusion:'success',html_url:`https://github.com/${subject}/actions/runs/21`}]}}),
      listJobsForWorkflowRun:async()=>({data:{jobs:[],total_count:0},headers:{}}),
      getWorkflowRun:async()=>({data:priorRun()}),getWorkflowRunAttempt:async()=>({data:priorRun()}),
      listJobsForWorkflowRunAttempt:async()=>({data:{total_count:1,jobs:[{id:78,run_id:77,run_attempt:1,
        head_sha:'d'.repeat(40),name:'verifier / Run post-merge verifier',status:'completed',
        conclusion:'cancelled',steps:[{name:'Record verification event pending',number:5,status:'completed',conclusion:'success'},
          {name:'Run LLM evaluation',number:15,status:'completed',conclusion:skipped?'skipped':'cancelled'}]}]},headers:{}}),
    },
  }};
  const core = {setOutput:(key,value)=>{outputs[key]=value;},info:()=>{},warning:()=>{},notice:()=>{}};
  const context = {repo:{owner:'stranske',repo:'Orchestrator'},runId:999,sha:merge,
    eventName:'pull_request',payload:{repository:{default_branch:'main'},pull_request:pull}};
  return {outputs,calls,invoke: options=>buildVerifierContext({github,context,core,ciWorkflows:['ci.yml'],
    fetchLocalDiff:()=>diff,...options})};
}

function marker(payload,id=9) {
  return {id,body:`<!-- ${payload.schema} ${JSON.stringify(payload)} -->`,
    user:{login:'github-actions[bot]',type:'Bot'},html_url:`https://github.com/${subject}/pull/7#issuecomment-${id}`};
}
function terminal(manifest) {
  const event = manifest.verification_event;
  return {schema:'verifier-corpus-decision/v1',repo:subject,pr:'7',head_sha:head,evaluated_sha:merge,
    run_id:'77',run_attempt:'1',ci_failed:false,verdict:'NON_PASS',provider_verdicts:['CONCERNS'],
    verification_event:event,assessment_identity:manifest.assessment_identity};
}

test('actual exported initial builder forwards model mode and preserves full authentic request',()=>isolated(async()=>{
  const source=fixture();
  const result=await source.invoke({mode:'evaluate'});
  assert.equal(result.shouldRun,true);
  assert.equal(result.verificationManifest.assessment_identity.mode,'evaluate');
  assert.equal(result.verificationManifest.verification_event.head_sha,head);
  assert.equal(result.verificationManifest.verification_event.evaluated_sha,merge);
  assert.match(fs.readFileSync(result.contextPath,'utf8'),/Original prose obligation outside headings: preserve café provenance/);
  const coverage=result.sourceCoverage.acceptance_sources[0];
  assert.equal(coverage.original_body_sha256,createHash('sha256').update(structured).digest('hex'));
  assert.equal(coverage.original_body_bytes,Buffer.byteLength(structured));
  assert.equal(source.calls.files,0);
}));

test('freeform model requests are authentic while default checkbox admission remains structured',()=>isolated(async()=>{
  const body='Restore the original requirements and prove the fix without inventing a checklist.';
  for(const mode of ['evaluate','compare']) {
    const result=await fixture({body}).invoke({mode});
    assert.equal(result.shouldRun,true);
    assert.equal(result.verificationManifest.assessment_identity.mode,mode);
  }
  const checkbox=await fixture({body}).invoke({});
  assert.equal(checkbox.shouldRun,false);
  assert.match(checkbox.reason,/No tasks and acceptance criteria/);
  const structuredCheckbox=await fixture().invoke({});
  assert.equal(structuredCheckbox.shouldRun,true);
  assert.equal(structuredCheckbox.verificationManifest,null);
}));

test('actual repeat builder retains original verdict and admits fresh required material once',()=>isolated(async()=>{
  const first=await fixture().invoke({mode:'evaluate'});
  const old=terminal(first.verificationManifest), prior=marker(old);
  const duplicate=await fixture({comments:[prior]}).invoke({mode:'evaluate'});
  assert.equal(duplicate.shouldRun,false);
  assert.equal(duplicate.reason,'duplicate-verification-event');
  const proof={id:10,body:'Exact restoration and regression transcript for the original obligation.',
    user:{login:'reviewer'},html_url:`https://github.com/${subject}/pull/7#issuecomment-10`};
  process.env.VERIFICATION_PREDECESSOR=first.verificationManifest.verification_event.event_id;
  process.env.VERIFICATION_MATERIAL=JSON.stringify([{kind:'comment',url:proof.html_url}]);
  const repeat=await fixture({comments:[prior,proof]}).invoke({mode:'evaluate'});
  assert.equal(repeat.shouldRun,true);
  assert.notEqual(repeat.verificationManifest.verification_event.event_id,old.verification_event.event_id);
  assert.equal(repeat.verificationManifest.predecessor_event_id,old.verification_event.event_id);
  assert.equal(old.verdict,'NON_PASS');
}));

test('actual pending builder retains uncertain effects and reenters only with skipped-step proof',()=>isolated(async()=>{
  const first=await fixture().invoke({mode:'evaluate'});
  const state={...terminal(first.verificationManifest),schema:'verifier-event-state/v1',status:'pending',
    execution_binding:first.verificationManifest.execution_binding};
  delete state.verdict;delete state.provider_verdicts;delete state.ci_failed;
  const pending=marker(state);
  const blocked=await fixture({comments:[pending],skipped:false}).invoke({mode:'evaluate'});
  assert.equal(blocked.shouldRun,false);
  assert.equal(blocked.reason,'verification-existing-execution-retrieval-pending');
  const recovered=await fixture({comments:[pending]}).invoke({mode:'evaluate'});
  assert.equal(recovered.shouldRun,true);
  assert.equal(recovered.verificationManifest.pending_no_evaluation.run_attempt,'1');
  assert.equal(state.status,'pending');
}));

test('invalid or missing requests and missing lossless local diff fail without API fallback',()=>isolated(async()=>{
  for(const body of [{invalid:'body'},'', '<!-- metadata only -->']) {
    const result=await fixture({body}).invoke({mode:'evaluate'});
    assert.equal(result.shouldRun,false);
    assert.match(result.reason,/original_request_missing_or_invalid/);
  }
  const missing=fixture({diff:''});
  const result=await missing.invoke({mode:'evaluate'});
  assert.equal(result.shouldRun,false);
  assert.match(result.reason,/Authoritative pull request diff unavailable/);
  assert.equal(missing.calls.files,0);
}));

test('actual builder preserves linked original requirements and refuses malformed linked source',()=>isolated(async()=>{
  const issue={number:42,title:'Authentic original request',body:'Original issue obligation outside standardized headings.',
    state:'OPEN',url:`https://github.com/${subject}/issues/42`,repository:{nameWithOwner:subject},labels:{nodes:[]}};
  const body='Delivery implementation for the original linked request.';
  const result=await fixture({body,issues:[issue]}).invoke({mode:'evaluate'});
  assert.equal(result.shouldRun,true);
  assert.match(fs.readFileSync(result.contextPath,'utf8'),/Original issue obligation outside standardized headings/);
  assert.equal(result.sourceCoverage.acceptance_sources[1].original_body_sha256,createHash('sha256').update(issue.body).digest('hex'));
  process.env.VERIFICATION_MATERIAL='[]';
  await assert.rejects(()=>fixture({body,issues:[{...issue,body:{invalid:'source'}}]}).invoke({mode:'evaluate'}),
    /complete authoritative request and changed-code inputs/);
}));
