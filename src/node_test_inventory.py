"""Repository-owned Node suites and their verification entry points.

Paths are relative to the repository root, including in the flat exec mirror.
The package manifests under .github/scripts define no test scripts. The Gate's
two Node jobs and the pytest bridges below are the existing automated callers.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NodeSuite:
    """Canonical Node file and any existing automated callers."""

    path: str
    pytest_nodeid: str | None = None
    gate_job: str | None = None


# Each entry here runs directly with `node --test <path>`.
DIRECT_SUITES = (
    "adjudicator_acceptance_runner",
    "adjudicator_case_gaps",
    "adjudicator_closer_shadow",
    "adjudicator_completion",
    "adjudicator_evidence_ledger",
    "adjudicator_inventory",
    "adjudicator_receipts",
    "adjudicator_retro_cli",
    "adjudicator_retrospective_batch",
    "adjudicator_summary",
    "agent_gh_guard_bash",
    "audit_pacing_ledger",
    "audit_provision_ledger",
    "capture_review_source_evidence",
    "ci_toolchain_mirror",
    "closer_pipeline_capacity_clock",
    "codemod_campaign_cli",
    "codemod_campaign_receipts",
    "codemod_campaign_recipe",
    "discover_seatbelt_ci",
    "exec_mirror_contract_witness",
    "install_verified_snapshot_reader",
    "issue_1138_artifacts",
    "issue_1138_mutation",
    "mirror_reader_reentry",
    "muse_offload_witness",
    "observe_redirect_runtime",
    "observer_verdict_cli",
    "profile_trial_cli",
    "provision_native_witnesses",
    "provision_verify",
    "record_fleet_delivery_checks",
    "recover_seatbelt_artifact",
    "recover_seatbelt_ci",
    "redirect_completion_tracking",
    "redirect_recovered_receipt",
    "redirect_routing_mutations",
    "redirect_stall_fixtures",
    "redirect_stall_provenance",
    "redirect_sweep_shell_backend",
    "relay_acceptance_cli",
    "review_contract_inputs",
    "role_prompt_offering_cli",
    "seatbelt_discovered_ci",
    "seatbelt_hosted_ci",
    "seatbelt_measurement_producer",
    "seatbelt_original_ci",
    "seatbelt_receipts",
    "seatbelt_regex_profile_contract",
    "switch_review_stuck_targets",
    "value_chain_preflight_cli",
    "verify_assess_sandbox",
    "verify_fleet_delivery_live",
    "verify_fleet_delivery_mutation",
    "verify_redirect_merged_revision",
    "verify_triage_shadow_mutation",
)

SCRIPT_TESTS = ".github/workflows/pr-00-gate.yml::github-scripts-tests"
ORCHESTRATOR_TESTS = ".github/workflows/pr-00-gate.yml::orchestrator-node-tests"

SUPPORTED_NODE_SUITES = (
    *(NodeSuite(f"tests/test_{name}.js") for name in DIRECT_SUITES),
    NodeSuite(".github/scripts/__tests__/gate_delivery_seal.test.js", gate_job=SCRIPT_TESTS),
    NodeSuite("tests/test_capability_invocation_deadlines.js", gate_job=SCRIPT_TESTS),
    NodeSuite("tests/test_route_weights_export_cli.js", gate_job=ORCHESTRATOR_TESTS),
    NodeSuite("tests/test_switch_review_exploration.js", gate_job=ORCHESTRATOR_TESTS),
    NodeSuite(
        "tests/test_capture_mirror_deployment_evidence.js",
        pytest_nodeid=(
            "tests/test_mirror_product_witnesses.py::"
            "test_deployment_evidence_collector_acceptance"
        ),
    ),
    NodeSuite(
        "tests/test_orchestrator_loop_svg.js",
        pytest_nodeid=(
            "tests/test_orchestrator_loop_svg_wiring.py::test_orchestrator_loop_svg_contract"
        ),
    ),
    NodeSuite(
        "tests/test_verifier_context_builder_contract.js",
        pytest_nodeid=(
            "tests/test_verifier_context_builder_wiring.py::"
            "test_full_verifier_context_builder_contract"
        ),
    ),
)

EXCLUDED_NODE_PATHS = {
    ".github/scripts/__tests__/capability_invocation_deadlines.test.js": (
        f"{SCRIPT_TESTS} collects this forwarding entry. Its only action is "
        "requiring tests/test_capability_invocation_deadlines.js, which owns the suite identity."
    ),
    "tests/seatbelt_ci_fixture.js": (
        "Provides withStepLogs to tests/test_recover_seatbelt_ci.js, "
        "tests/test_seatbelt_discovered_ci.js, tests/test_seatbelt_hosted_ci.js and "
        "tests/test_seatbelt_measurement_producer.js. It defines no tests."
    ),
}


def check_tracked_inventory(root: Path) -> None:
    """Require exactly one disposition per tracked JS file in the test trees."""
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "tests", ".github/scripts/__tests__"], cwd=root
    )
    tracked_paths = {
        os.fsdecode(path)
        for path in tracked.split(b"\0")
        if path.endswith((b".js", b".cjs", b".mjs"))
    }
    paths = [suite.path for suite in SUPPORTED_NODE_SUITES] + list(EXCLUDED_NODE_PATHS)
    classified = set(paths)
    if len(paths) != len(classified):
        raise ValueError("duplicate paths in the Node test inventory")
    if tracked_paths != classified:
        raise ValueError(
            "Node test inventory differs from git ls-files: "
            f"unclassified {sorted(tracked_paths - classified)}, "
            f"untracked {sorted(classified - tracked_paths)}"
        )
