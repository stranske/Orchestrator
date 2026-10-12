'use strict';

// Synthetic gh-rendered provenance for checker/recovery tests. Never upgrade the
// retained historical JSON files: their whole-job excerpts lack step bindings.
function withStepLogs(snapshot) {
  const evidence = structuredClone(snapshot);
  const jobs = evidence.runs.flatMap(run => run.jobs.jobs || run.jobs);
  for (const receipt of evidence.receipts) {
    const job = jobs.find(job => job.id === receipt.job_id);
    const steps = job.steps || job.product_steps;
    const step = steps.find(step => step.name === 'Verify'
      || step.name === "Build the flat copy with the sync's copy contract and verify it there"
      || step.name === 'Pytest (unit tests with coverage)');
    receipt.selected_authentic_lines = receipt.selected_authentic_lines
      .filter(line => /HEAD is now at|\s+pytest:|\s+selftests:|\s+gates:|VERIFIED —|== verify-before-sync: VERIFIED/.test(line))
      .map(line => `${job.name}\t${line.includes('HEAD is now at')
        ? 'Run actions/checkout@v7' : step.name}\t${line}`);
  }
  return evidence;
}

module.exports = { withStepLogs };
