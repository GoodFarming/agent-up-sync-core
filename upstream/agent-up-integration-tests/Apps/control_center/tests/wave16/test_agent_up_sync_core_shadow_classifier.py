from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess
from typing import Any

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    attach_shadow_metadata_to_receipt,
    build_sync_core_shadow_request,
    compare_shadow_to_python,
    invoke_sync_core_once,
    invoke_sync_core_with_fallback,
    python_decision_from_sync_receipt,
)
from Apps.control_center.backend.convergence.agent_up_sync_core_schema import (
    build_contract_request_example,
    build_contract_response_example,
    validate_sync_core_response,
)


ROOT = Path(__file__).resolve().parents[4]
CORPUS_PATH = ROOT / "Apps/control_center/tests/sync_core_corpus/sync_core_python_expected_decisions.json"


@lru_cache(maxsize=1)
def _rust_binary() -> Path:
    subprocess.run(["cargo", "build", "-p", "agent-up-sync-core"], cwd=ROOT, check=True)
    binary = ROOT / "target/debug/agent-up-sync-core"
    assert binary.exists()
    return binary


def _init_jj_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["jj", "git", "init", str(repo)], cwd=ROOT, check=True, capture_output=True, text=True)
    return repo


def _request_for_repo(repo: Path, *, authored_state: str = "clean") -> dict[str, Any]:
    request = build_contract_request_example(
        transaction_id="sync-core-shadow-classifier-pytest",
        repo_path=str(repo),
        workspace_path=str(repo),
        live_root_path=str(repo),
        adapter_profile="cli-jj",
        correlation_id="corr-sync-core-shadow-classifier-pytest",
        idempotency_key="idem-sync-core-shadow-classifier-pytest",
    )
    request["python_context"]["source_state"]["authored_state"] = authored_state
    request["python_context"]["source_state"]["source_provenance_state"] = (
        "authored" if authored_state != "clean" else "none_or_clean"
    )
    request["python_context"]["runtime_context"]["runtime_cutover_state"] = "already_current"
    return request


def _clean_noop_receipt() -> dict[str, Any]:
    return {
        "outcome": "boundary_green",
        "exit_phase": "boundary_green",
        "local_outcome": "sync_noop",
        "publish_outcome": "not_attempted",
        "source_publish_outcome": "skipped",
        "workspace_final_state": "fresh",
        "workspace_sync_state": "fresh",
        "safe_to_continue": True,
        "live_head_moved": False,
        "runtime_cutover_required": False,
        "runtime_cutover_state": "already_current",
        "runtime_stage_content_current": True,
        "sync_engine_mode": "python",
    }


def test_shadow_classifier_uses_python_corpus_noop_decision() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    fixture = next(item for item in corpus["fixtures"] if item["fixture_id"] == "sync_clean_noop_no_local_commit")
    receipt = _clean_noop_receipt()

    python_decision = python_decision_from_sync_receipt(receipt)

    assert python_decision["authority_state"] == "python_authoritative"
    assert python_decision["decision_class"] == fixture["expected_decision"]["decision_class"]
    assert python_decision["selected_workspace_state"] == "clean"
    assert python_decision["source_provenance_state"] == "none_or_clean"


def test_rust_shadow_classifier_emits_schema_valid_noop_decision_for_repo(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(_rust_binary())

    response = invoke_sync_core_once(_request_for_repo(repo), runner=runner)
    python_decision = python_decision_from_sync_receipt(_clean_noop_receipt())
    parity = compare_shadow_to_python(response, python_decision)

    assert runner.call_count == 1
    assert response["engine_mode_actual"] == "rust_shadow"
    assert response["authority_state"] == "rust_shadow_observed"
    assert response["decision_class"] == "noop"
    assert response["runtime_relevance"] == "already_current"
    assert response["graph_metrics"]["kernel_call_count"] == 1
    assert parity["parity_state"] == "matched"


def test_parity_mismatch_records_diffs_and_preserves_python_outcome() -> None:
    receipt = _clean_noop_receipt()
    python_decision = python_decision_from_sync_receipt(receipt)
    request = build_contract_request_example()
    response = build_contract_response_example(
        request,
        decision_class="clean_merge",
        source_provenance_state="published",
        live_root_state="advanced",
        reason_codes=["test_forced_mismatch", "python_authority_preserved"],
        decision_drivers=["forced_test_mismatch"],
    )

    updated = attach_shadow_metadata_to_receipt(receipt, response, python_decision=python_decision)

    assert updated["outcome"] == "boundary_green"
    assert updated["safe_to_continue"] is True
    assert updated["sync_engine_mode"] == "python"
    assert updated["sync_engine_parity_state"] == "mismatch"
    assert updated["sync_core_shadow"]["parity_state"] == "mismatch"
    assert updated["sync_core_shadow"]["python_remains_authoritative"] is True
    assert updated["sync_core_shadow"]["parity"]["mismatch_count"] >= 1


def test_receipt_metadata_exposes_shadow_fields_without_raw_jj_guidance() -> None:
    receipt = _clean_noop_receipt()
    request = build_contract_request_example()
    response = build_contract_response_example(
        request,
        parity_state="not_compared",
        feedback_observation={"state": "pending_next_sync"},
    )

    updated = attach_shadow_metadata_to_receipt(
        receipt,
        response,
        python_decision=python_decision_from_sync_receipt(receipt),
    )
    shadow = updated["sync_core_shadow"]

    assert shadow["engine_mode_actual"] == "rust_shadow"
    assert shadow["authority_state"] == "rust_shadow_observed"
    assert shadow["decision_class"] == "noop"
    assert shadow["python_authoritative_decision"]["authority_state"] == "python_authoritative"
    assert shadow["fallback"]["python_fallback_available"] is True
    assert shadow["graph_metrics"]["kernel_call_count"] == 1
    assert shadow["worker_raw_jj_guidance"] is False
    assert "jj " not in str(shadow)


def test_fallback_when_rust_missing_is_visible_and_non_authoritative(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(tmp_path / "missing-agent-up-sync-core")
    response = invoke_sync_core_with_fallback(_request_for_repo(repo), runner=runner)
    receipt = _clean_noop_receipt()

    updated = attach_shadow_metadata_to_receipt(
        receipt,
        response,
        python_decision=python_decision_from_sync_receipt(receipt),
    )

    assert runner.call_count == 1
    assert updated["outcome"] == "boundary_green"
    assert updated["sync_engine_mode"] == "python"
    assert updated["sync_core_shadow"]["engine_mode_actual"] == "python_fallback"
    assert updated["sync_core_shadow"]["fallback"]["fallback_reason"].startswith("rust_runner_failed:")
    assert updated["sync_core_shadow"]["python_remains_authoritative"] is True


def test_heuristic_feedback_fields_are_required_and_surfaceable() -> None:
    request = build_contract_request_example()
    response = validate_sync_core_response(
        build_contract_response_example(
            request,
            decision_confidence=0.83,
            reason_codes=["exact_match", "python_authority_preserved"],
            inspected_fact_classes=["selected_workspace", "source_state", "live_target", "runtime_context"],
            decision_drivers=["source_provenance_state", "conflict_count"],
            feedback_observation={"state": "pending_next_sync", "expected_next_observation": "confirmed_or_mismatch"},
        )
    )
    metadata = attach_shadow_metadata_to_receipt(
        _clean_noop_receipt(),
        response,
        python_decision=python_decision_from_sync_receipt(_clean_noop_receipt()),
    )["sync_core_shadow"]

    assert metadata["decision_confidence"] == 0.83
    assert metadata["reason_codes"]
    assert metadata["inspected_fact_classes"]
    assert metadata["decision_drivers"]
    assert metadata["feedback_observation"]["state"] == "pending_next_sync"


def test_shadow_request_defaults_to_non_mutating_feature_flags(tmp_path: Path) -> None:
    request = build_sync_core_shadow_request(
        workspace_id="workspace::control-center::agent-up-worker.shadow-test",
        workspace_path=str(tmp_path),
        repo_path=str(tmp_path),
        live_root_path=str(tmp_path),
        sync_group_id="sync-control-center",
        python_context={
            "selected_workspace": {"workspace_id": "workspace::control-center::agent-up-worker.shadow-test"},
            "sync_group": {"sync_group_id": "sync-control-center"},
            "live_target": {"live_rev": "live-rev", "live_root_state": "unchanged"},
            "source_state": {"workspace_rev": "workspace-rev", "source_rev": "source-rev", "authored_state": "clean"},
            "runtime_context": {"runtime_cutover_required": False, "runtime_stage_content_current": True},
        },
    )

    assert request["engine_mode_requested"] == "rust_shadow"
    assert request["mutation_allowed"] is False
    assert request["feature_flags"]["rust_sync_core_enabled"] is False
    assert request["feature_flags"]["rust_sync_core_shadow"] is True
    assert request["feature_flags"]["rust_sync_core_mutation"] is False
