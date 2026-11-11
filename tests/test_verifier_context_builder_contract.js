'use strict';

// Exercise the actual consumer export, not an event/helper-only surrogate.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
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

test('invalid requests and unavailable diff evidence cannot start evaluation',()=>isolated(async()=>{
  for(const body of [{invalid:'body'},'', '<!-- metadata only -->']) {
    const result=await fixture({body}).invoke({mode:'evaluate'});
    assert.equal(result.shouldRun,false);
    assert.match(result.reason,/original_request_missing_or_invalid/);
  }
  const missing=fixture({diff:''});
  const result=await missing.invoke({mode:'evaluate'});
  assert.equal(result.shouldRun,false);
  assert.match(result.reason,/Authoritative pull request diff unavailable/);
  // A newer shared collector may attempt bounded API capture. The stub refuses
  // it, so neither the legacy local-only path nor the shared fallback may
  // invent evidence, a manifest, or an evaluation after retrieval fails.
  assert.ok(missing.calls.files <= 1);
  assert.equal(result.verificationManifest,undefined);
  assert.equal(missing.outputs.should_run,'false');
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

test('CSV canary native verifier attempts require retained recovery, not another evaluation',()=>{
  const { validateEvent, completedEvent, guardExistingEvent } = require('../.github/scripts/verification_event.js');
  // Genuine native comments retained by the acceptance run, not generated decisions.
  const readback = require('../docs/verification/issue-1184-acceptance-20261111/native-readback.json');
  const corrective = readback.pull_requests.find(pr => pr.number === 1168);
  const comments = readback.verifier_events;
  const before = JSON.stringify(readback);
  const attempts = new Set();
  assert.equal(corrective.merged, true);
  for (const comment of comments) {
    const match = comment.body.match(/^<!-- verifier-event-state\/v1 (\{[^\n]+\}) -->$/);
    assert.ok(match, `missing retained native state: ${comment.url}`);
    const state = JSON.parse(match[1]);
    // Validate the native event digest before testing rejection, so a malformed
    // fixture cannot make the negative completion assertion pass vacuously.
    const event = validateEvent(state.verification_event);
    assert.equal(event.pr, '1168');
    assert.equal(event.head_sha, corrective.head_sha);
    assert.equal(event.evaluated_sha, corrective.merge_commit_sha);
    assert.equal(completedEvent(event, comments, state.assessment_identity), false);
    if (state.status !== 'incomplete') continue;
    const run = readback.workflow_runs.find(run => String(run.id) === state.run_id);
    assert.ok(run, `missing native workflow readback: ${state.run_id}`);
    assert.equal(run.status, 'completed');
    const attemptComments = comments.filter(row => {
      const attempt = JSON.parse(row.body.match(/(\{[^\n]+\})/)[1]);
      return attempt.run_id === state.run_id;
    });
    assert.throws(
      () => guardExistingEvent(event, attemptComments, state.assessment_identity),
      /incomplete; recover retained execution before another evaluation/,
      `workflow ${state.run_id} (${run.conclusion}) is not retry or completion authority`,
    );
    attempts.add(state.run_id);
  }
  assert.deepEqual([...attempts].sort(), ['42141057834', '42142328293', '42142641060']);
  assert.equal(JSON.stringify(readback), before, 'native evidence must remain unchanged');
});

test('CSV retained recovery command uses integer selectors bound to native source execution',()=>{
  const { parseRecoverySelector, validateEvent } = require('../.github/scripts/verification_event.js');
  const previous = require('../docs/verification/issue-1184-acceptance-20261111/round7/native-readback.json');
  const current = require('../docs/verification/issue-1184-acceptance-20261111/round8/native-readback.json');
  const before = JSON.stringify(current);
  const commandSelector = record => {
    const args = record.recovery_dispatch.argv;
    const argument = args.find(value => value.startsWith('verification_recovery='));
    assert.ok(argument, 'the actual dispatch must supply an explicit recovery selector');
    assert.equal(args[args.indexOf(argument) - 1], '-f');
    return argument.slice('verification_recovery='.length);
  };
  // The earlier command fails native admission even if transport is restored.
  assert.throws(()=>parseRecoverySelector(commandSelector(previous)),
    /Recovery accepts exact run_id\/run_attempt\/artifact_id selectors only/);
  const selector = parseRecoverySelector(commandSelector(current));
  const state = JSON.parse(current.recovery_state.body.match(/(\{[^\n]+\})/)[1]);
  const event = validateEvent(state.verification_event);
  const pr = current.pull_requests.find(pr => pr.number === 1168);
  const artifact = current.evaluation_artifact;
  assert.equal(current.recovery_state.user.login, 'github-actions[bot]');
  assert.equal(event.head_sha, pr.head_sha);
  assert.equal(event.evaluated_sha, pr.merge_commit_sha);
  assert.equal(pr.merged, true);
  assert.deepEqual(selector, {
    run_id:Number(state.retained_revalidation.source_run_id),
    run_attempt:Number(state.retained_revalidation.source_run_attempt),
    artifact_id:state.retained_revalidation.artifact_id,
  });
  assert.equal(selector.run_id, artifact.workflow_run.id);
  assert.equal(artifact.name, `evaluation-results-${selector.run_id}`);
  assert.equal(selector.artifact_id, artifact.id);
  assert.notEqual(selector.run_id, Number(state.run_id), 'recover the original evaluation');
  assert.equal(Object.isFrozen(selector), true);
  for (const key of Object.keys(selector)) {
    assert.throws(()=>parseRecoverySelector(JSON.stringify({...selector, [key]:String(selector[key])})),
      /Recovery accepts exact run_id\/run_attempt\/artifact_id selectors only/);
  }
  // Parser admission is no claim that dispatch or native recovery completed.
  assert.equal(current.recovery_dispatch.exit_status, 1);
  assert.deepEqual(current.trusted_corpus_markers, []);
  assert.equal(JSON.stringify(current), before);
});

test('CSV recovery follows the latest native comparison instead of the superseded evaluation',async()=>{
  const { guardRetainedRecoveryHistory } = require('../.github/scripts/verifier_pending_execution.js');
  const { parseRecoverySelector, validateEvent, digest } = require('../.github/scripts/verification_event.js');
  const readback = require('../docs/verification/issue-1184-acceptance-20261111/round9/native-readback.json');
  const before = JSON.stringify(readback);
  const states = readback.verifier_events.map(comment =>
    JSON.parse(comment.body.match(/(\{[^\n]+\})/)[1]));
  assert.equal(states.length, 6, 'exercise the complete retained native state history');
  const original = states.find(state => state.run_id === '42141057834');
  const latest = states.find(state => state.run_id === '42142641060');
  const event = validateEvent(latest.verification_event);
  assert.equal(original.assessment_identity.mode, 'evaluate');
  assert.equal(latest.assessment_identity.mode, 'compare');
  assert.equal(validateEvent(original.verification_event).event_id, event.event_id);
  const pr = readback.pull_requests.find(pr => pr.number === 1168);
  assert.equal(event.head_sha, pr.head_sha);
  assert.equal(event.evaluated_sha, pr.merge_commit_sha);
  const argv = readback.recovery_dispatch.argv;
  assert.ok(argv.includes('mode=compare'));
  assert.ok(argv.includes(`pr_head_sha=${pr.head_sha}`));
  const argument = argv.find(value => value.startsWith('verification_recovery='));
  assert.ok(argument);
  const selector = parseRecoverySelector(argument.slice('verification_recovery='.length));
  const artifact = readback.comparison_artifact;
  assert.equal(selector.run_id, Number(latest.run_id));
  assert.equal(selector.run_attempt, Number(latest.run_attempt));
  assert.equal(selector.artifact_id, artifact.id);
  assert.equal(artifact.workflow_run.id, selector.run_id);
  assert.equal(artifact.name, `comparison-results-${selector.run_id}`);
  assert.equal(artifact.expired, false);
  // Use unchanged native comments. A throwing client proves history admission
  // performs no remote mutation or acquisition; it does not claim recovery ran.
  const github = new Proxy({}, {get() {throw new Error('unexpected GitHub access');}});
  const options = (state, selected) => ({
    github, event, assessment:state.assessment_identity, comments:readback.verifier_events,
    sourceRun:selected.run_id, sourceAttempt:selected.run_attempt,
    currentRun:selector.run_id + 1, currentAttempt:1,
    recoveryKey:digest({schema:'retained-revalidation-key/v1',event_id:event.event_id,
      assessment:state.assessment_identity,selector:selected}),
  });
  const oldRecovery = options(original, {
    run_id:Number(original.run_id),run_attempt:Number(original.run_attempt),artifact_id:13468410186,
  });
  // Positive control: the old recovery is admissible before the comparison.
  // Omitting the later authentic comments would conceal the actual blocker.
  assert.equal(await guardRetainedRecoveryHistory({...oldRecovery,
    comments:readback.verifier_events.filter((comment, index) =>
      Number(states[index].run_id) < Number(latest.run_id)),
  }), 'ready');
  await assert.rejects(guardRetainedRecoveryHistory(oldRecovery),
    /Retained recovery is superseded by a newer authenticated assessment/);
  assert.equal(await guardRetainedRecoveryHistory(options(latest, selector)), 'ready');
  assert.deepEqual(readback.terminal_corpus_markers, []);
  assert.equal(JSON.stringify(readback), before, 'never rewrite producer history for admission');
});

test('native CSV comparison reconciles to existing outputs even when its evaluation step was skipped',async()=>{
  const { reconcilePendingExecution, executionBinding } = require('../.github/scripts/verifier_pending_execution.js');
  const { validateEvent } = require('../.github/scripts/verification_event.js');
  const readback = require('../docs/verification/issue-1184-acceptance-20261111/round10/native-readback.json');
  const before = JSON.stringify(readback);
  const states = readback.verifier_events.map(comment => JSON.parse(comment.body.match(/(\{[^\n]+\})/)[1]));
  const pending = states.find(state => state.run_id === String(readback.run.id) && state.status === 'pending');
  const incomplete = states.find(state => state.run_id === pending.run_id && state.status === 'incomplete');
  const event = validateEvent(pending.verification_event);
  assert.equal(event.pr, '1168');
  assert.equal(event.head_sha, '01e86ae7e4ef0e6a06193c916c8c780ca5cab052');
  assert.equal(event.evaluated_sha, 'a30b34ada04a30d04360867e14097a8c5b6d731c');
  assert.equal(pending.assessment_identity.mode, 'compare');
  assert.deepEqual(pending.execution_binding, executionBinding('compare'));
  assert.equal(readback.jobs.total_count, readback.jobs.jobs.length);
  const job = readback.jobs.jobs.find(row => row.name === 'verifier / Run post-merge verifier');
  assert.equal(job.conclusion, 'success');
  const step = name => job.steps.find(row => row.name === name);
  assert.equal(step('Run LLM evaluation').conclusion, 'skipped');
  assert.equal(step('Run multi-provider comparison').conclusion, 'success');
  assert.equal(step('Upload comparison artifacts').conclusion, 'success');
  assert.equal(incomplete.retry_kind, 'recover_existing_evaluation');
  const calls = [];
  // Replay unchanged native GET results through the actual reconciler. A skipped
  // evaluate-mode step must not authorize repeating the completed comparison.
  const check = (args, attempt = false) => {
    assert.equal(args.owner, 'stranske');
    assert.equal(args.repo, 'orchestrator');
    assert.equal(args.run_id, readback.run.id);
    if (attempt) assert.equal(args.attempt_number, readback.attempt.run_attempt);
  };
  const github = {rest:{actions:{
    getWorkflowRun:async args => {
      check(args); calls.push('run'); return {data:readback.run,headers:{}};
    },
    getWorkflowRunAttempt:async args => {
      check(args,true); calls.push('attempt'); return {data:readback.attempt,headers:{}};
    },
    listJobsForWorkflowRunAttempt:async args => {
      check(args,true); assert.equal(args.page,1); assert.equal(args.per_page,100);
      calls.push('jobs'); return {data:readback.jobs,headers:{}};
    },
  }}};
  const result = await reconcilePendingExecution({github,event,
    assessment:pending.assessment_identity,state:pending});
  assert.deepEqual(calls, ['run','attempt','jobs'], 'reach the evaluator inspection with complete native input');
  assert.equal(result.retry_kind, 'uncertain_execution');
  assert.equal(result.retryable, false);
  assert.equal(result.failure_cause, 'prior_evaluator_started_collect_existing_outputs');
  assert.deepEqual(result.existing_execution.output_names, ['comparison.json']);
  assert.equal(result.existing_execution.run_id, pending.run_id);
  assert.equal(result.existing_execution.run_attempt, pending.run_attempt);
  assert.equal(result.proof, undefined, 'never mint a skipped-evaluation proof for this comparison');
  assert.deepEqual(readback.terminal_corpus_markers, []);
  assert.equal(JSON.stringify(readback), before, 'native run and event records remain unchanged');
});

test('CSV recovery authenticates the comparison artifact before retaining any origin proof',()=>isolated(async directory=>{
  const { acquireRetainedExecution } = require('../.github/scripts/verifier_pending_execution.js');
  const { validateEvent } = require('../.github/scripts/verification_event.js');
  const native = require('../docs/verification/issue-1184-acceptance-20261111/round10/native-readback.json');
  const older = require('../docs/verification/issue-1184-acceptance-20261111/round8/native-readback.json');
  const before = JSON.stringify([native, older]);
  const pending = native.verifier_events.map(comment => JSON.parse(comment.body.match(/(\{[^\n]+\})/)[1]))
    .find(state => state.run_id === String(native.run.id) && state.status === 'pending');
  const event = validateEvent(pending.verification_event);
  const caller = native.source_origins.find(row => row.repository.toLowerCase() === event.repo);
  assert.equal(event.pr, '1168');
  assert.equal(caller.retained_sha, native.run.head_sha);
  assert.notEqual(older.evaluation_artifact.workflow_run.id, native.run.id);
  const selector = {run_id:native.run.id, run_attempt:native.run.run_attempt,
    artifact_id:native.comparison_artifact.id};
  const unavailable = new Error('Native checkout log transport unavailable in regression');
  const calls = [];
  let artifact = native.comparison_artifact;
  const response = data => ({data, headers:{}});
  const github = {rest:{repos:{
    get:async args => {
      assert.equal(`${args.owner}/${args.repo}`.toLowerCase(), event.repo);
      return response({full_name:caller.repository, default_branch:caller.default_branch});
    },
    getCommit:async args => {
      assert.equal(args.ref, caller.default_branch);
      return response({sha:caller.canonical_sha});
    },
    compareCommitsWithBasehead:async args => {
      assert.equal(args.basehead, `${caller.retained_sha}...${caller.canonical_sha}`);
      return response({status:caller.comparison_status, base_commit:{sha:caller.base_sha},
        merge_base_commit:{sha:caller.merge_base_sha}});
    },
  },actions:{
    getWorkflowRun:async () => response(native.run),
    getWorkflowRunAttempt:async () => response(native.attempt),
    listJobsForWorkflowRunAttempt:async () => response(native.jobs),
    getArtifact:async args => {
      assert.equal(args.artifact_id, selector.artifact_id);
      calls.push('artifact'); return response(artifact);
    },
    downloadJobLogsForWorkflowRun:async args => {
      const job = native.jobs.jobs.find(row => row.name === 'verifier / Run post-merge verifier');
      assert.equal(args.job_id, job.id);
      calls.push('checkout-log'); throw unavailable;
    },
  }}};
  const acquire = () => acquireRetainedExecution({github,event,selector,directory,
    assessment:pending.assessment_identity,currentRun:native.run.id + 1});
  // Positive control: complete native metadata reaches the next acquisition
  // boundary. Metadata admission does not establish archive or result custody.
  await assert.rejects(acquire(), error => error === unavailable);
  assert.deepEqual(calls, ['artifact','checkout-log']);
  assert.deepEqual(fs.readdirSync(directory), [], 'unavailable custody must not mint origin proof');
  calls.length = 0;
  // Replay a real earlier artifact as a mismatched API response, without
  // rewriting its identity or manufacturing a terminal decision.
  artifact = older.evaluation_artifact;
  await assert.rejects(acquire(), /Retained artifact attempt\/origin\/size unproven/);
  assert.deepEqual(calls, ['artifact'], 'reject the wrong artifact before further acquisition');
  assert.deepEqual(fs.readdirSync(directory), []);
  assert.equal(JSON.stringify([native, older]), before, 'native evidence remains unchanged');
}));

test('native CSV verifier states require both the corrective head and published merge',async()=>{
  const root = path.resolve(__dirname, '..');
  const python = fs.existsSync('/opt/anaconda3/bin/python3') && !process.env.VIRTUAL_ENV
    ? '/opt/anaconda3/bin/python3' : 'python3';
  // Query unchanged native comments with identities from the two real merges.
  // A reader that checks just one SHA must fail this regression.
  const program = `
import importlib.util
import json
from pathlib import Path

root = Path.cwd()
spec = importlib.util.spec_from_file_location(
    "csv_native_state_binding",
    root / "tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py",
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
readback = json.loads((root / "docs/verification/issue-1184-acceptance-20261111/round4/native-readback.json").read_text())
corrective = next(pr for pr in readback["pull_requests"] if pr["number"] == 1168)
other = next(pr for pr in readback["pull_requests"] if pr["number"] == 989)
comments = readback["verifier_events"]
before = json.dumps(comments, sort_keys=True)
def observed(head, merge):
    return [event for comment in comments
            for event in reader._events_from_comment(comment, 1168, head, merge)]
head, merge = corrective["head_sha"], corrective["merge_commit_sha"]
events = observed(head, merge)
print(json.dumps(dict(
    events=len(events), statuses=sorted({event["status"] for event in events}),
    heads=sorted({event["head_sha"] for event in events}),
    merges=sorted({event["evaluated_sha"] for event in events}),
    expected_head=head, expected_merge=merge,
    wrong_head=observed(other["head_sha"], merge),
    wrong_merge=observed(head, other["merge_commit_sha"]),
    head_as_merge=observed(head, head),
    unchanged=json.dumps(comments, sort_keys=True) == before,
)))
`;
  const result = await promisify(execFile)(python, ['-I', '-c', program], {
    cwd:root, encoding:'utf8', timeout:30000,
  });
  assert.equal(result.stderr, '');
  const measured = JSON.parse(result.stdout);
  assert.equal(measured.events, 6, 'prove native records were read before checking rejection');
  assert.deepEqual(measured.statuses, ['incomplete', 'pending']);
  assert.deepEqual(measured.heads, [measured.expected_head]);
  assert.deepEqual(measured.merges, [measured.expected_merge]);
  assert.deepEqual(measured.wrong_head, []);
  assert.deepEqual(measured.wrong_merge, []);
  assert.deepEqual(measured.head_as_merge, []);
  assert.equal(measured.unchanged, true);
});

test('native PR 984 corpus decision and successful workflows cannot complete PR 1168',async()=>{
  const root = path.resolve(__dirname, '..');
  const python = fs.existsSync('/opt/anaconda3/bin/python3') && !process.env.VIRTUAL_ENV
    ? '/opt/anaconda3/bin/python3' : 'python3';
  // Exercise the real reader with the retained native partial decision. Do not
  // manufacture a terminal decision by rewriting the corrective pending state.
  const program = `
import importlib.util
import json
from pathlib import Path

root = Path.cwd()
spec = importlib.util.spec_from_file_location(
    "csv_native_revision_regression",
    root / "tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py",
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
partial = json.loads((root / "tests/fixtures/verifier_native_bot_origin/canary984-selected-origin.json").read_text())
readback = json.loads((root / "docs/verification/issue-1184-acceptance-20261111/native-readback.json").read_text())
comments = [dict(body=c["body"], url=c["url"], author=c["author"]["login"])
            for c in partial["comments"]["nodes"]]
comments += readback["verifier_events"]
records = [dict(pr, kind="pr", name=str(pr["number"]), merge_sha=pr["merge_commit_sha"])
           for pr in readback["pull_requests"] if pr["number"] in (1168, 989)]
corrective = next(pr for pr in records if pr["number"] == 1168)
head, merge = corrective["head_sha"], corrective["merge_sha"]
partial_corpus = [d for c in comments for d in reader._partial_corpus_from_comment(c)]
corpus = [d for c in comments for d in reader._corpus_from_comment(c, head)]
events = [e for c in comments for number, h, m in (
    (984, partial["headRefOid"], partial["mergeCommit"]["oid"]), (1168, head, merge))
    for e in reader._events_from_comment(c, number, h, m)]
assessment = reader._assess(events, comments, records, head,
                            corpus=corpus, partial_corpus=partial_corpus)
report, status = reader._format_report(assessment, head, None,
                                      verifier_runs=readback["workflow_runs"])
print(json.dumps(dict(status=status, report=report, partial=partial_corpus,
    corrective=corpus, partial_events=len(assessment["partial"]),
    corrective_events=len(assessment["corrective"]), unlabeled=assessment["unlabeled"])))
`;
  const result = await promisify(execFile)(python, ['-I', '-c', program], {
    cwd:root, encoding:'utf8', timeout:30000,
  });
  assert.equal(result.stderr, '');
  const measured = JSON.parse(result.stdout);
  // Prove the real partial corpus was accepted for its own revision before
  // asserting exclusion from the corrective result; missing input cannot pass.
  assert.equal(measured.partial.length, 1);
  assert.equal(measured.partial[0].run_id, '40620861168');
  assert.equal(measured.partial[0].verdict, 'NON_PASS');
  assert.equal(measured.partial[0].head_sha, '0507d28acf68d6bf21c68138d18d56cd01968827');
  assert.equal(measured.partial[0].evaluated_sha, 'd1947b5b4775c2e6a07bb46b3f85eb2e28c1b001');
  assert.equal(measured.partial_events, 5);
  assert.equal(measured.corrective_events, 3);
  assert.deepEqual(measured.corrective, []);
  assert.deepEqual(measured.unlabeled, []);
  assert.equal(measured.status, 2);
  assert.match(measured.report, /partial_corpus_decision: verdict=NON_PASS run_id=40620861168/);
  assert.match(measured.report, /agents_verifier_run: id=42141057834 .*conclusion=success/);
  assert.match(measured.report, /agents_verifier_run: id=42142641060 .*conclusion=success/);
  assert.match(measured.report, /native_verify_for_corrective_head: MISSING/);
  assert.match(measured.report, /completion: evidence incomplete/);
  assert.doesNotMatch(measured.report, /native_verify_for_corrective_head: PRESENT/);
});

test('native unavailable CSV reports cannot turn provider concerns into a corpus decision',async()=>{
  const root = path.resolve(__dirname, '..');
  const python = fs.existsSync('/opt/anaconda3/bin/python3') && !process.env.VIRTUAL_ENV
    ? '/opt/anaconda3/bin/python3' : 'python3';
  // Preserve the actual native headline and provider table. Rewriting either
  // would hide the distinction between diagnostic output and a corpus decision.
  const program = `
import importlib.util
import json
from pathlib import Path

root = Path.cwd()
spec = importlib.util.spec_from_file_location(
    "csv_unavailable_reports",
    root / "tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py",
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
readback = json.loads((root / "docs/verification/issue-1184-acceptance-20261111/round7/native-readback.json").read_text())
head = "01e86ae7e4ef0e6a06193c916c8c780ca5cab052"
reports = readback["reports"]
before = json.dumps(reports, sort_keys=True)
print(json.dumps(dict(
    headlines=[reader._verdict_lines(report["body"]) for report in reports],
    corpus=[reader._corpus_from_comment(report, head) for report in reports],
    trusted=[reader._native_comment(report, 1168, reader._TRUSTED_VERIFIER_AUTHORS)
             for report in reports],
    unchanged=json.dumps(reports, sort_keys=True) == before,
)))
`;
  const readback = require('../docs/verification/issue-1184-acceptance-20261111/round7/native-readback.json');
  assert.deepEqual(readback.reports.map(report => report.url.split('#')[1]), [
    'issuecomment-6673820837', 'issuecomment-6674054349',
  ]);
  assert.match(readback.reports[0].body, /Provider execution is not established/);
  assert.match(readback.reports[1].body, /CONCERNS/);
  const result = await promisify(execFile)(python, ['-I', '-c', program], {
    cwd:root, encoding:'utf8', timeout:30000,
  });
  assert.equal(result.stderr, '');
  const measured = JSON.parse(result.stdout);
  assert.deepEqual(measured.trusted, [true, true], 'reject the contents, not the native authors');
  assert.deepEqual(measured.headlines, [['UNVERIFIABLE_INPUT'], ['UNVERIFIABLE_INPUT']]);
  assert.deepEqual(measured.corpus, [[], []]);
  assert.equal(measured.unchanged, true);
});

test('merged CSV canary acknowledgment does not substitute for stored corrective custody',async()=>{
  const root = path.resolve(__dirname, '..');
  const readback = require('../docs/verification/issue-1184-acceptance-20261111/round4/native-readback.json');
  const before = JSON.stringify(readback);
  const corrective = readback.pull_requests.find(pr => pr.number === 1168);
  const acknowledgment = readback.publication_acknowledgments[0];
  assert.equal(corrective.merged, true);
  assert.equal(acknowledgment.author, 'stranske');
  assert.equal(acknowledgment.url, 'https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6670373009');
  assert.ok(acknowledgment.body.startsWith(
    `<!-- closer-core/v1 action=merge target=stranske/Orchestrator#1168 head=${corrective.head_sha} `,
  ));
  // Run the real fixture reader against retained native records. No producer
  // record, terminal reference or decision is invented to fill the custody gap.
  const python = fs.existsSync('/opt/anaconda3/bin/python3') && !process.env.VIRTUAL_ENV
    ? '/opt/anaconda3/bin/python3' : 'python3';
  const program = `
import importlib.util
import json
from pathlib import Path

root = Path.cwd()
spec = importlib.util.spec_from_file_location(
    "csv_custody_regression",
    root / "tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py",
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
readback = json.loads((root / "docs/verification/issue-1184-acceptance-20261111/round4/native-readback.json").read_text())
records = [dict(pr, kind="pr", name=str(pr["number"]),
                merge_sha=pr["merge_commit_sha"])
           for pr in readback["pull_requests"] if pr["number"] in (1168, 989)]
corrective = next(pr for pr in records if pr["number"] == 1168)
comments = readback["verifier_events"] + readback["publication_acknowledgments"]
events = [event for comment in comments for event in reader._events_from_comment(
    comment, 1168, corrective["head_sha"], corrective["merge_sha"])]
assessment = reader._assess(events, comments, records, corrective["head_sha"])
report, status = reader._format_report(assessment, corrective["head_sha"], None)
print(json.dumps(dict(status=status, report=report, events=len(events),
    merged=assessment["corrective_pr"]["merged"],
    publication=assessment["publication"], owned_publication=assessment["owned_publication"],
    unlabeled=assessment["unlabeled"])))
`;
  const result = await promisify(execFile)(python, ['-I', '-c', program], {
    cwd:root, encoding:'utf8', timeout:30000,
  });
  assert.equal(result.stderr, '');
  const measured = JSON.parse(result.stdout);
  assert.equal(measured.events, 6, 'all exact-head native states must be admitted before rejection');
  assert.equal(measured.merged, true);
  assert.deepEqual(measured.unlabeled, [], 'unrelated labeling must not cause rejection');
  assert.equal(measured.status, 2);
  assert.equal(measured.publication, false);
  assert.equal(measured.owned_publication, false);
  assert.match(measured.report, /native_verify_for_corrective_head: MISSING/);
  assert.match(measured.report, /closer_publication_proof_for_corrective_head: MISSING/);
  assert.match(measured.report, /owned_delivery_publication: MISSING/);
  assert.match(measured.report, /corrective_pr_merged: yes/);
  assert.match(measured.report, /completion: evidence incomplete/);
  assert.doesNotMatch(measured.report, /PR 1168 is unmerged/);
  assert.equal(JSON.stringify(readback), before, 'native evidence must remain unchanged');
});
