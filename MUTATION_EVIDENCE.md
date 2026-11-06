# Mutation evidence for `tests/test_issue1169_ranked_coverage.py` (PR #1170)

**Checked:** 2026-11-05, 21:34:47 to 21:36:12 UTC, on branch head `d453b03` (production code unchanged by this PR).
**Outcome:** all 34 collected tests have observed mutation-fail/restored-pass pairs: **34/34**.

## Method

- Interpreter: `python3` (CPython 3.12.2, pytest 9.1.1). The worktree has no `.venv`; `python3` is what the repo's own commands invoke. CI runs 3.14.
- Enumeration: `python3 -m pytest tests/test_issue1169_ranked_coverage.py --collect-only -q` printed `34 tests collected`; the 34 sections below are those node ids in collection order.
- Isolation: the worktree was copied (without `.git`) to a disposable `/tmp` directory. Every mutation was applied there, never in this checkout; `git status` here stayed clean, and after the run both copied source files were byte-identical to this branch.
- Per test, a script (kept outside the repo) did exactly this:
  1. replace the quoted production fragment with the mutant, refusing to run unless the fragment occurs **exactly once** in the file;
  2. run only that node: `python3 -m pytest tests/test_issue1169_ranked_coverage.py::<test> -q -rf --tb=short -p no:cacheprovider`, which must exit 1;
  3. write the original bytes back and check the sha256 matches;
  4. run the same node again, which must exit 0.
- To reproduce one pair by hand: make the shown one-fragment edit in a scratch copy, run the node command, revert, and run it again. `⏎` marks a newline inside a fragment.
- Failure text is the first `E` line that pytest printed, and "at" is the test line that raised. Temporary paths are shortened to `<tmp>/` and object addresses are removed.

## Correction to the previous version of this file

The earlier file is replaced, not amended. Its test-1 transcript was not the output of a real run: it showed `AssertionError: assert 'goal' in prompt` for removing the `goal must be non-empty` raise, and the real failure of that mutation is `Failed: DID NOT RAISE ValueError` (test 1 below). Tests 5–19 had only one-line claims with no captured output, tests 20–34 were missing, and it reported "all 19" for a file that collects 34. Every result below comes from the 2026-11-05 run.

## Matrix

### 1. `test_build_authoring_prompt_rejects_empty_goal_and_reads_context_file`

- Mutation, fragment starting at `src/codemod_lane.py:83`: `raise ValueError("goal must be non-empty")` → `pass`
- Mutated: exit 1, `1 failed in 0.38s`, failing at test line 83: `E   Failed: DID NOT RAISE ValueError`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.26s`

### 2. `test_validate_campaign_reports_structural_errors`

- Mutation, fragment starting at `src/codemod_lane.py:194`: `errors.append("campaign.id must be a lowercase slug")` → `pass`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 97: `E   assert False`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 3. `test_gitignore_add_only_contract_errors`

- Mutation, fragment starting at `src/codemod_lane.py:280`: `if campaign.get("ignore_entries") != list(IGNORE_ENTRIES):` → `if False:`
- Mutated: exit 1, `1 failed in 0.44s`, failing at test line 120: `E   assert False`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.26s`

### 4. `test_parse_campaign_json_and_delegation_warnings`

- Mutation, fragment starting at `src/codemod_lane.py:999`: `lines.extend(["", "Plan warnings:"])` → `lines.extend([""])`
- Mutated: exit 1, `1 failed in 0.29s`, failing at test line 137: `E   assert ('Plan warnings:' in 'You are in the Orchestrator codemod/refactor-campaign lane.\n\nCampaign: Rename LegacyHandler to ModernHandler (renam...ope.max_files or narrow include_globs)"}\'`\n- [validation] `pytest`\n- [validation] `ruff check`\n\n- preview omitted')`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 5. `test_recipe_dry_run_branches`

- Mutation, fragment starting at `src/codemod_lane.py:826`: `"rewrite:dryRun",` → `"rewrite:run",`
- Mutated: exit 1, `1 failed in 0.32s`, failing at test line 163: `E   AssertionError: assert ('./mvnw -Drewrite.activeRecipes=com.example.Fix rewrite:run' and 'rewrite:dryRun' in './mvnw -Drewrite.activeRecipes=com.example.Fix rewrite:run')`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 6. `test_campaign_measure_target_mismatch_and_sqlite_error`

- Mutation, fragment starting at `src/codemod_lane.py:623`: `"UNKNOWN: linked PR target/number mismatch"` → `"UNKNOWN: linked PR"`
- Mutated: exit 1, `1 failed in 0.33s`, failing at test line 188: `E   AssertionError: assert 'target/number mismatch' in 'UNKNOWN: linked PR'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.26s`

### 7. `test_campaign_backlog_validates_program_and_builds_items`

- Mutation, fragment starting at `src/codemod_lane.py:511`: `scoped = backlog.load_scoped_blockers()⏎    errors = validate_campaign(campaign)` → `scoped = backlog.load_scoped_blockers()⏎    errors = []`
- Mutated: exit 1, `1 failed in 0.32s`, failing at test line 238: `E   AssertionError: Regex pattern did not match. Expected regex: 'invalid campaign' Actual message: 'campaign receipt identity mismatch'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.25s`

### 8. `test_codemod_main_cli_paths`

- Mutation, fragment starting at `src/codemod_lane.py:1231`: `print(f"Could not read or parse campaign JSON: {exc}", file=sys.stderr)⏎            return 2` → `print(f"Could not read or parse campaign JSON: {exc}", file=sys.stderr)⏎            return 1`
- Mutated: exit 1, `1 failed in 0.30s`, failing at test line 256: `E   AssertionError: assert 1 == 2`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 9. `test_validate_state_rejects_corrupt_promotions`

- Mutation, fragment starting at `src/synthesis_promotion.py:202`: `raise ValueError("synthesis phase history is not exactly-once")` → `pass`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 285: `E   Failed: DID NOT RAISE ValueError`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.28s`

### 10. `test_ensure_evaluated_state_is_idempotent`

- Mutation, fragment starting at `src/synthesis_promotion.py:320`: `        if existing is not None:⏎            return existing⏎        meta = dict(` → `        if False:⏎            return existing⏎        meta = dict(`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 296: `E   assert 999 == 50`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.26s`

### 11. `test_accepted_source_influences_reads_brain`

- Mutation, fragment starting at `src/synthesis_promotion.py:241`: `                    ("capability_ids", "capability"),⏎` → `(line deleted)`
- Mutated: exit 1, `1 failed in 0.33s`, failing at test line 325: `E   AssertionError: assert False`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.29s`

### 12. `test_completion_probe_pending_pid_and_unreadable_head`

- Mutation, fragment starting at `src/synthesis_promotion.py:496`: `            return {"status": "pending"}⏎        except (OSError, ValueError):` → `            return {"status": "running"}⏎        except (OSError, ValueError):`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 342: `E   AssertionError: assert 'running' == 'pending'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 13. `test_safe_command_and_run_gate_timeout`

- Mutation, fragment starting at `src/synthesis_promotion.py:559`: `"reason": "timeout",⏎            "transient": True,` → `"reason": "timeout",⏎            "transient": False,`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 380: `E   AssertionError: assert (False is False and False is True)`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.26s`

### 14. `test_verify_synthesis_local_shlex_runtime_and_prefix_gates`

- Mutation, fragment starting at `src/synthesis_promotion.py:692`: `local_hollow = [str(node) for node in _hollow] if isinstance(_hollow, list) else []` → `local_hollow = _hollow`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 430: `E   AssertionError: assert 'not-a-list' == []`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 15. `test_default_delivery_discovery_statuses`

- Mutation, fragment starting at `src/synthesis_promotion.py:1283`: `return {"status": "no_pr"}` → `return {"status": "none"}`
- Mutated: exit 1, `1 failed in 0.30s`, failing at test line 450: `E   AssertionError: assert 'none' == 'no_pr'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.29s`

### 16. `test_shadow_reconcile_and_dry_run_reconcile`

- Mutation, fragment starting at `src/synthesis_promotion.py:1465`: `    if now >= int(state.get("expires_ts") or 0):⏎        return [` → `    if False:⏎        return [`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 489: `E   AssertionError: assert 'synth_running' == 'discarded'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.29s`

### 17. `test_mirror_outcome_discarded_and_mirror_once`

- Mutation, fragment starting at `src/synthesis_promotion.py:1442`: `    if event_key in state.get("mirrored_events", []):⏎        return state` → `    if False:⏎        return state`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 518: `E   AssertionError: assert ['evt', 'evt'] == ['evt']`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 18. `test_apply_delivery_link_and_compile_candidate_conflict`

- Mutation, fragment starting at `src/synthesis_promotion.py:1112`: `if prior.get("candidate_id") != candidate_id or prior.get("body_hash") != body_hash:` → `if False:`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 550: `E   Failed: DID NOT RAISE ValueError`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.28s`

### 19. `test_break_caveat_and_main_cli`

- Mutation, fragment starting at `src/synthesis_promotion.py:752`: `if node_verdict is not None and node_verdict != "PASS":` → `if False:`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 557: `E   AssertionError: assert '' != ''`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.53s`

### 20. `test_atomic_text_removes_temp_file_when_replace_fails`

- Mutation, fragment starting at `src/synthesis_promotion.py:154`: `            handle.write(content)⏎            handle.flush()⏎            os.fsync(handle.fileno())⏎            temporary = Path(handle.name)⏎        os.replace(temporary, path)⏎        temporary = None⏎    finally:⏎        if temporary is not None:⏎            temporary.unlink(missing_ok=True)` → `            handle.write(content)⏎            handle.flush()⏎            os.fsync(handle.fileno())⏎            temporary = Path(handle.name)⏎        os.replace(temporary, path)⏎        temporary = None⏎    finally:⏎        if temporary is not None:⏎            pass`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 689: `E   AssertionError: assert [PosixPath('/...huo8ozq.tmp')] == []`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.28s`

### 21. `test_promotion_guards_reject_illegal_states`

- Mutation, fragment starting at `src/synthesis_promotion.py:196`: `raise ValueError("synthesis maps outside canonical capability states")` → `pass`
- Mutated: exit 1, `1 failed in 0.33s`, failing at test line 727: `E   Failed: DID NOT RAISE ValueError`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.26s`

### 22. `test_corrupt_completion_payload_is_skipped`

- Mutation, fragment starting at `src/synthesis_promotion.py:238`: `except (TypeError, json.JSONDecodeError):⏎                    continue` → `except (TypeError,):⏎                    continue`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 751: `E   json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.29s`

### 23. `test_completion_probe_dead_pid_is_interrupted`

- Mutation, fragment starting at `src/synthesis_promotion.py:497`: `        except (OSError, ValueError):⏎            pass⏎    return {"status": "interrupted"` → `        except (OSError, ValueError):⏎            return {"status": "pending"}⏎    return {"status": "interrupted"`
- Mutated: exit 1, `1 failed in 0.32s`, failing at test line 757: `E   AssertionError: assert 'pending' == 'interrupted'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 24. `test_verification_config_shlex_and_runtime_pass`

- Mutation, fragment starting at `src/synthesis_promotion.py:569`: `raise ValueError("promotion-verification.json must be an object")` → `raise RuntimeError("promotion-verification.json must be an object")`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 765: `E   RuntimeError: promotion-verification.json must be an object`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.29s`

### 25. `test_producer_candidate_reuse_and_discovery_errors`

- Mutation, fragment starting at `src/synthesis_promotion.py:882`: `delivery_fn=lambda state: default_delivery_discovery(state, gh_fn=read_gh),` → `delivery_fn=lambda state: {"status": "unavailable"},`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 817: `E   AssertionError: assert 'delivery_discovery:no_pr' in ['awaiting_external_delivery_link', 'delivery_discovery:unavailable']`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.28s`

### 26. `test_deliver_producer_winner_stops_on_each_fail_closed_edge`

- Mutation, fragment starting at `src/synthesis_promotion.py:1004`: `except (ValueError, KeyError, TypeError) as exc:` → `except (RuntimeError,) as exc:`
- Mutated: exit 1, `1 failed in 0.43s`, failing at test line 910: `E   json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.34s`

### 27. `test_compile_link_and_outcome_edges`

- Mutation, fragment starting at `src/synthesis_promotion.py:1149`: `raise ValueError("conflicting delivery link")` → `pass`
- Mutated: exit 1, `1 failed in 0.35s`, failing at test line 942: `E   Failed: DID NOT RAISE ValueError`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 28. `test_shadow_plan_and_reconcile_failure_paths`

- Mutation, fragment starting at `src/synthesis_promotion.py:1484`: `if next_retry and now < int(next_retry):` → `if False:`
- Mutated: exit 1, `1 failed in 0.32s`, failing at test line 1025: `E   AssertionError: assert 'synthesis_process_launched' == 'retry_backoff'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.31s`

### 29. `test_promotion_cli_live_gate_link_and_heartbeat`

- Mutation, fragment starting at `src/synthesis_promotion.py:2078`: `    except Exception:⏎        pass⏎⏎⏎def main(` → `    except ImportError:⏎        pass⏎⏎⏎def main(`
- Mutated: exit 1, `1 failed in 0.40s`, failing at test line 1170: `E   RuntimeError: heartbeat down`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.29s`

### 30. `test_codemod_validation_recipe_and_plan_edges`

- Mutation, fragment starting at `src/codemod_lane.py:857`: `return None, f"no dry-run command could be derived for tool {tool!r}"` → `return None, f"unsupported tool {tool!r}"`
- Mutated: exit 1, `1 failed in 0.32s`, failing at test line 1287: `E   assert (None is None and 'no dry-run command' in "unsupported tool 'ast-grep'")`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 31. `test_codemod_filing_backlog_and_cli`

- Mutation, fragment starting at `src/codemod_lane.py:1221`: `print(json.dumps(file_targets(campaign), indent=2))⏎            return 0` → `print(json.dumps(file_targets(campaign), indent=2))⏎            return 7`
- Mutated: exit 1, `1 failed in 0.33s`, failing at test line 1434: `E   AssertionError: assert 7 == 0`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.30s`

### 32. `test_codemod_selftest_truncation_does_not_spawn`

- Mutation, fragment starting at `src/codemod_lane.py:712`: `print "# TRUNCATED: " NR` → `print "# CAPPED: " NR`
- Mutated: exit 1, `1 failed in 0.34s`, failing at test line 1457: `E   AssertionError: ('scope_inventory cap is silent', {'purpose': 'scope_inventory', 'command': 'rg --files --hidden -g \'!.git/**\' -g \'**/*.py\' -g \'!**/migrations/**\' -g \'!**/vendor/**\' . | awk -v cap=200 \'NR<=cap {print} END {if (NR>cap) print "# CAPPED: " NR " files in scope, listed " cap …`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

### 33. `test_candidate_ready_predecessor_holds_when_the_table_would_allow_it`

- Mutation, fragment starting at `src/synthesis_promotion.py:403`: `raise ValueError("candidate_ready requires synth_verified predecessor")` → `pass`
- Mutated: exit 1, `1 failed in 0.32s`, failing at test line 1467: `E   Failed: DID NOT RAISE ValueError`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.25s`

### 34. `test_selftest_restores_preset_ship_gate_without_starting_git`

- Mutation, fragment starting at `src/synthesis_promotion.py:1957`: `                os.environ[FOLLOWUP_SHIP_GATE_ENV] = old_ship_gate` → `                os.environ.pop(FOLLOWUP_SHIP_GATE_ENV, None)`
- Mutated: exit 1, `1 failed in 0.31s`, failing at test line 1493: `E   KeyError: 'ORCH_FOLLOWUP_SHIP_GATE'`
- Restored (sha256 identical: True): exit 0, `1 passed in 0.27s`

## Notes on individual pairs

- **24:** the first mutant tried for this test (`raise ValueError(...)` → `payload = {}`) also failed, but only because the suite's autouse guard caught the git call the code then reached, not on the test's own `pytest.raises`. It was replaced by the exception-type swap above, which fails on the test's own assertion. Both runs are from 2026-11-05.
- **7:** with validation disabled, the invalid document reaches the receipt check, so `pytest.raises(match="invalid campaign")` fails on the wrong message. That is the assertion the mutant targets.
- **26, 31, 33, 34** repeat, as real node runs, the four break-and-revert cases the PR body described for this head. Each failed with the message the body quoted.
- **32** fails inside `codemod_lane._selftest`, which the test drives, on its `scope_inventory cap is silent` assertion.

## What this does and does not show

One production mutant per collected test, each failing that exact node and passing once restored: 34/34. Several tests make many assertions (for example 26, 28, 30 and 31), and only the assertion each mutant reaches is proven here. Their other assertions are covered by executing the code, not by a mutant of their own.

## Restored suite

In this checkout, after all mutants were reverted, `python3 -m pytest tests/test_issue1169_ranked_coverage.py -m 'not slow' -q -p no:cacheprovider` printed `34 passed in 0.58s`. This PR changes no production file: `git diff origin/main...HEAD -- src/` is empty.
