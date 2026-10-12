'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { verifyHostedCI, verifyDiscoveredCI } = require('../scripts/verify_seatbelt_receipts');
const { withStepLogs } = require('./seatbelt_ci_fixture');

for (const [file, verify] of [
  ['380c160-hosted-ci.json', verifyHostedCI],
  ['0f88580-ci-discovery.json', verifyDiscoveredCI],
]) {
  const snapshot = withStepLogs(JSON.parse(fs.readFileSync(path.join(__dirname,
    '../docs/verification/issue-532-seatbelt-regex', file), 'utf8')));
  const check = evidence => verify(evidence, snapshot.head_sha);
  const copy = () => structuredClone(snapshot);

  test(`${file}: matching summaries from other steps and jobs cannot be accepted`, () => {
    for (const change of [
      line => line.replace('\tVerify\t', '\tInstall dependencies\t'),
      line => line.replace('\tVerify\t', '\tUNKNOWN STEP\t'),
      line => line.replace('\tVerify\t', '\tComplete job\t'),
      line => `other job\tVerify\t${line.split('\t')[2]}`,
      line => line.split('\t')[2],
    ]) {
      const evidence = copy();
      evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
        .map(line => line.includes('HEAD is now at') ? line : change(line));
      assert.throws(() => check(evidence), /producer binding|unrelated producer step/);
    }
  });

  test(`${file}: a trusted step name inside unrelated stdout cannot forge provenance`, () => {
    const evidence = copy();
    evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
      .map(line => line.includes('HEAD is now at') ? line
        : `${line.split('\t')[0]}\tInstall dependencies\t${line.split('\t')[2].split(' ')[0]} ${line}`);
    assert.throws(() => check(evidence), /missing test measurement/);
  });

  test(`${file}: conflicting producer summaries are rejected in either order`, () => {
    for (const [before, after, error] of [
      ['2863 passed', '2862 passed', /conflicting test measurement/],
      ['97 of 104', '96 of 104', /conflicting selftest measurement/],
      ['3 of 5 gates green', '4 of 5 gates green', /conflicting verifier verdict measurement/],
    ]) {
      for (const first of [true, false]) {
        const evidence = copy();
        const lines = evidence.receipts[0].selected_authentic_lines;
        const conflict = lines.find(line => line.includes(before)).replace(before, after);
        if (first) lines.unshift(conflict);
        else lines.push(conflict);
        assert.throws(() => check(evidence), error);
      }
    }
  });

  test(`${file}: missing producer summaries fail and identical repeats remain valid`, () => {
    for (const category of ['pytest:', 'selftests:', 'VERIFIED —']) {
      const evidence = copy();
      evidence.receipts[0].selected_authentic_lines = evidence.receipts[0].selected_authentic_lines
        .filter(line => !line.includes(category));
      assert.throws(() => check(evidence), /missing .*measurement/);
    }
    const evidence = copy();
    evidence.receipts[0].selected_authentic_lines.push(...evidence.receipts[0].selected_authentic_lines);
    assert.equal(check(evidence).measurements[0].passed, 2863);
  });

  test(`${file}: valid summaries cannot hide malformed or failed producer summaries`, () => {
    for (const [before, after, error] of [
      ['0 failed', '1 failed', /invalid test measurement/],
      ['97 of 104 modules ran', 'incomplete', /invalid selftest measurement/],
      ['2863 tests actually executed and passed', 'incomplete', /invalid verifier verdict measurement/],
    ]) {
      const evidence = copy();
      const lines = evidence.receipts[0].selected_authentic_lines;
      lines.push(lines.find(line => line.includes(before)).replace(before, after));
      assert.throws(() => check(evidence), error);
    }
  });

  test(`${file}: summaries cannot borrow success from another producer step`, () => {
    for (const conclusion of ['failure', 'skipped']) {
      const evidence = copy();
      const entry = evidence.runs[0].jobs;
      const job = (entry.jobs || entry).find(job => job.id === evidence.receipts[0].job_id);
      (job.steps || job.product_steps)[0].conclusion = conclusion;
      assert.throws(() => check(evidence), /product step did not execute/);
    }
  });
}
