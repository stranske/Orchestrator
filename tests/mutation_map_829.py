"""Production mutation specs for #834 tests (one deliberate break per test name)."""

from __future__ import annotations

# Keys are pytest test function names (node id suffix after ::).
MUTATIONS_BY_TEST_NAME: dict[str, dict[str, str]] = {
    "test_get_maint_68_repos_parses_registry_and_fails_closed": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'if "REGISTERED_CONSUMER_REPOS:" in line:',
        "new": 'if "REGISTERED_CONSUMER_REPOS:" not in line:',
    },
    "test_run_gh_and_bytes_use_token_file_and_surface_failures": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '    r = subprocess.run(args, capture_output=True, text=True, env=env)\n    if r.returncode != 0:\n        raise Exception(f"gh command failed: {r.stderr}")\n    return r.stdout',
        "new": '    r = subprocess.run(args, capture_output=True, text=True, env=env)\n    if r.returncode != 0:\n        raise Exception(f"gh command failed: {r.stderr}")\n    return "not-ok-text"',
    },
    "test_discover_artifact_rejects_bad_payloads_and_lookup_errors": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'raise ValueError("workflow_runs_response_missing_array")',
        "new": "return None  # break missing-array guard",
    },
    "test_blob_sha256_encodings_and_guards": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'raise ValueError(f"invalid_blob_sha:{repo}:{sha!r}")',
        "new": "return sha  # break invalid sha guard",
    },
    "test_fetch_tree_and_observed_budget_edges": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'raise ValueError(f"repo_tree_missing_array:{repo}")',
        "new": "return {}  # break tree guard",
    },
    "test_safe_extract_size_and_absolute_path_guards": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'f"Archive zip_bytes size ({len(zip_bytes)}) exceeds max_total_bytes limit ({max_total_bytes})"',
        "new": '"zip size ok"  # break zip_bytes guard',
    },
    "test_safe_extract_rejects_resolve_escape_via_dest_symlink": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'raise ValueError(f"Unsafe zip entry: {filename}")',
        "new": "pass  # break symlink escape guard",
    },
    "test_load_state_schema_guards": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'data.get("schema_version") != 1',
        "new": 'data.get("schema_version") != 99',
    },
    "test_hygiene_skips_nested_large_dir_already_covered": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'if any(parent == f["path"] or parent.startswith(f["path"] + "/") for f in findings):',
        "new": 'if False and any(parent == f["path"] or parent.startswith(f["path"] + "/") for f in findings):',
    },
    "test_record_hygiene_escalation_records_success": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '"asked": len(recorded)',
        "new": '"asked": 0',
    },
    "test_gh_throttle_swallows_import_errors": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": "    except Exception:\n        pass",
        "new": "    except Exception:\n        raise",
    },
    "test_main_kill_switch_selftest_and_validation_edges": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'os.environ.get("ORCH_REFERENCE_WORKFLOW_DISABLED") == "1"',
        "new": 'os.environ.get("ORCH_REFERENCE_WORKFLOW_DISABLED") != "1"',
    },
    "test_main_lock_download_extract_and_parse_failures": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '"Lock acquired by another ingest process. Exiting."',
        "new": '"Lock ok."',
    },
    "test_main_repo_metadata_and_tree_failures_are_reported": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '"Failed to fetch metadata/default branch for {repo}: {e}"',
        "new": '"metadata ok for {repo}"',
    },
    "test_main_defaults_argv_none_to_sys_argv": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": "    if argv is None:\n        argv = sys.argv[1:]",
        "new": "    if argv is None:\n        argv = []",
    },
    "test_invalid_repository_format_raises": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'raise ValueError(f"Invalid repository format: {repo}")',
        "new": "pass  # break repo format guard",
    },
    "test_zip_extraction_failure_returns_one": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '"Zip extraction failed/unsafe: {e}"',
        "new": '"Zip ok: {e}"',
    },
    "test_token_file_read_errors_are_swallowed": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": "            except Exception:\n                pass\n    r = subprocess.run(args, capture_output=True, text=True, env=env)",
        "new": '            except Exception:\n                env["GH_TOKEN"] = "from-failed-read"\n    r = subprocess.run(args, capture_output=True, text=True, env=env)',
    },
    "test_blob_sha256_rejects_oversized_decoded_content": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": "    if len(content) > REPO_READ_MAX_BLOB_BYTES:",
        "new": "    if len(content) > REPO_READ_MAX_BLOB_BYTES * 100:",
    },
    "test_get_maint_68_repos_stops_at_next_env_key": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '                    and ":" in line',
        "new": '                    and ":" not in line',
    },
    "test_main_defaults_missing_command_to_ingest": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'print("Kill switch active: ORCH_REFERENCE_WORKFLOW_DISABLED=1. Exiting.")',
        "new": 'print("Kill switch inactive.")',
    },
    "test_main_preview_rejects_classification_side_effects": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": 'raise Exception("classification_returned_side_effects")',
        "new": "pass  # allow side effects",
    },
    "test_main_loads_cohort_from_registry_when_repositories_omitted": {
        "target": "src/consumer_sync_artifact_ingest.py",
        "old": '"Error downloading artifact zip: {e}"',
        "new": '"Download ok: {e}"',
    },
    "test_text_and_json_helpers_cover_none_empty_and_path_reads": {
        "target": "src/redirect_shadow.py",
        "old": '    if text is None:\n        return None\n    return hashlib.sha256(text.encode("utf-8")).hexdigest()',
        "new": '    if text is None:\n        return None\n    return "broken-sha"',
    },
    "test_read_json_from_stdin": {
        "target": "src/redirect_shadow.py",
        "old": "        return json.load(sys.stdin)",
        "new": "        return {}",
    },
    "test_iter_events_skips_blank_and_invalid_json": {
        "target": "src/redirect_shadow.py",
        "old": "        if isinstance(obj, dict):\n            rows.append(obj)",
        "new": "        if isinstance(obj, dict):\n            pass",
    },
    "test_outcome_for_run_handles_empty_id_db_error_and_row": {
        "target": "src/redirect_shadow.py",
        "old": "    if not run_id:\n        return None",
        "new": "    if not run_id:\n        return {}",
    },
    "test_historical_candidates_skips_non_dict_rows": {
        "target": "src/redirect_shadow.py",
        "old": "        if not isinstance(row, dict):\n            continue",
        "new": "        if not isinstance(row, dict):\n            pass",
    },
    "test_main_falls_through_when_cmd_is_unhandled": {
        "target": "src/redirect_shadow.py",
        "old": '    return 0\n\n\nif __name__ == "__main__":',
        "new": '    return 1\n\n\nif __name__ == "__main__":',
    },
    "test_historical_candidates_counts_missing_target": {
        "target": "src/redirect_shadow.py",
        "old": '"missing_target_rows": missing_target,',
        "new": '"missing_target_rows": 0,',
    },
    "test_historical_report_state_and_outcome_branches": {
        "target": "src/redirect_shadow.py",
        "old": '    if state == "stalled":\n        recommended = "inspect"',
        "new": '    if state == "stalled":\n        recommended = "wait"',
    },
    "test_latest_keepalive_outcome_for_target_edges": {
        "target": "src/redirect_shadow.py",
        "old": "    if not target:\n        return None",
        "new": "    if not target:\n        return {}",
    },
    "test_main_record_and_link_outcome_cli_paths": {
        "target": "src/redirect_shadow.py",
        "old": "f\"recorded {entry['entry_id']} backend={entry.get('backend') or '-'} \"",
        "new": "f\"broken {entry['entry_id']} backend={entry.get('backend') or '-'} \"",
    },
    "test_historical_candidates_rank_failures_and_keep_calibration_opt_in": {
        "target": "src/redirect_shadow.py",
        "old": "        if not reasons:\n            continue",
        "new": "        if reasons:\n            continue",
    },
    "test_collect_historical_dispatch_is_blinded_and_counterfactual": {
        "target": "src/redirect_shadow.py",
        "old": '"counterfactual": True,',
        "new": '"counterfactual": False,',
    },
    "test_historical_report_outcomes_and_cli_are_offline": {
        "target": "src/redirect_shadow.py",
        "old": '        state = "running"',
        "new": '        state = "progress"',
    },
}
