from __future__ import annotations

from functools import lru_cache
import subprocess
from pathlib import Path
from typing import Any

import pytest

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    attach_read_authority_metadata_to_receipt,
    build_sync_core_read_authority_request,
    invoke_sync_core_once,
    invoke_sync_core_with_fallback,
    python_decision_from_sync_receipt,
)
from Apps.control_center.backend.convergence.agent_up_sync_core_schema import (
    SyncCoreSchemaError,
    validate_sync_core_request,
)


ROOT = Path(__file__).resolve().parents[4]


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


def _read_authority_context(*, include_generated: bool = True) -> dict[str, Any]:
    conflicted_paths = [
        "Apps/control_center/backend/convergence/agent_up_sync_engine.py",
        "@planning/agent-up-v0.3/workpacks/martin-flow/WORKPACK.example.md",
    ]
    generated_paths = ["frontend/cockpit/dist/assets/index.js"] if include_generated else []
    return {
        "selected_workspace": {
            "workspace_id": "workspace::control-center::agent-up-worker.martin",
            "lane_id": "agent-up-worker.martin",
            "workspace_role": "worker",
            "workspace_lifecycle": "disposable",
            "workspace_sync_state": "publish_conflict",
        },
        "sync_group": {"sync_group_id": "sync-control-center", "peer_debt_state": "advisory"},
        "live_target": {"repo_id": "control-center", "live_rev": "live-1001", "live_root_state": "advanced"},
        "source_state": {
            "workspace_rev": "worker-1010x",
            "source_rev": "worker-1010x",
            "authored_state": "prepared",
            "source_provenance_state": "prepared",
            "no_local_commit_classification": "recoverable",
            "prepared_revision_recovery": {"handle": "prepared-rev-worker-1010x"},
        },
        "runtime_context": {
            "runtime_cutover_required": False,
            "runtime_cutover_state": "already_current",
            "runtime_stage_content_current": True,
        },
        "conflict_context": {
            "conflict_packet_id": "conflict-martin-ab",
            "conflict_kind": "publish",
            "base_rev": "head-1",
            "conflicted_paths": conflicted_paths + generated_paths,
            "semantic_paths": conflicted_paths,
            "generated_artifact_paths": generated_paths,
            "side_context": {
                "base": {"revision": "head-1"},
                "live": {"revision": "head-1001"},
                "worker": {"revision": "head-1010x"},
            },
        },
    }


def _request(repo: Path, *, include_generated: bool = True) -> dict[str, Any]:
    return build_sync_core_read_authority_request(
        workspace_id="workspace::control-center::agent-up-worker.martin",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=_read_authority_context(include_generated=include_generated),
        transaction_id="sync-core-read-authority-martin-packet",
        correlation_id="corr-sync-core-read-authority-martin-packet",
        idempotency_key="idem-sync-core-read-authority-martin-packet",
    )


def _conflict_receipt() -> dict[str, Any]:
    return {
        "outcome": "publish_conflict",
        "exit_phase": "publish_conflict",
        "local_outcome": "sync_boundary_saved_local",
        "publish_outcome": "failed",
        "source_publish_outcome": "blocked",
        "workspace_final_state": "conflict_materialized",
        "workspace_sync_state": "publish_conflict",
        "conflict_authority": "semantic_resolution_required",
        "blocking": True,
        "safe_to_continue": False,
        "live_head_moved": True,
        "runtime_cutover_required": False,
        "runtime_cutover_state": "already_current",
        "runtime_stage_content_current": True,
        "sync_engine_mode": "python",
    }


def test_martin_packet_read_authority_contains_side_context_without_raw_jj(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(_rust_binary())

    response = invoke_sync_core_once(_request(repo), runner=runner)
    packet = response["conflict_packet_candidate"]

    assert runner.call_count == 1
    assert response["engine_mode_actual"] == "rust_read_authoritative"
    assert response["authority_state"] == "rust_read_authoritative"
    assert response["decision_class"] == "materialized_conflict"
    assert response["mutation_plan"] == {}
    assert response["journal_record"] == {}
    assert packet["schema_id"] == "control-center.agent-up.sync-core.conflict-packet-candidate.v0.1"
    assert packet["side_context"]["base"]["revision"] == "head-1"
    assert packet["side_context"]["live"]["revision"] == "head-1001"
    assert packet["side_context"]["worker"]["revision"] == "head-1010x"
    assert packet["usefulness"]["raw_jj_required"] is False
    assert "conflict_packet_side_context" in response["decision_drivers"]
    assert "jj " not in str(packet)


def test_generated_surface_classification_keeps_artifacts_out_of_semantic_policy(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    response = invoke_sync_core_once(_request(repo), runner=RustSyncCoreRunner(_rust_binary()))
    packet = response["conflict_packet_candidate"]

    assert response["conflict_authority"] == "mixed_policy"
    assert "frontend/cockpit/dist/assets/index.js" in packet["generated_artifact_paths"]
    semantic_classes = {
        item["path"]: item["surface_class"]
        for item in packet["path_classifications"]
        if item["path"].endswith("agent_up_sync_engine.py")
    }
    generated_classes = {
        item["path"]: item["surface_class"]
        for item in packet["path_classifications"]
        if "/dist/" in item["path"]
    }
    assert set(semantic_classes.values()) == {"semantic"}
    assert set(generated_classes.values()) == {"generated"}
    assert packet["policy_context"]["semantic_auto_merge"] is False
    assert packet["policy_context"]["mutation_performed"] is False


def test_read_authority_fallback_is_explicit_and_does_not_leak_raw_jj(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(tmp_path / "missing-agent-up-sync-core")
    response = invoke_sync_core_with_fallback(_request(repo), runner=runner)
    receipt = _conflict_receipt()

    updated = attach_read_authority_metadata_to_receipt(
        receipt,
        response,
        python_decision=python_decision_from_sync_receipt(receipt),
    )
    metadata = updated["sync_core_read_authority"]

    assert runner.call_count == 1
    assert updated["sync_engine_mode"] == "python"
    assert metadata["engine_mode_actual"] == "python_fallback"
    assert metadata["fallback"]["fallback_reason"].startswith("rust_runner_failed:")
    assert metadata["worker_raw_jj_guidance"] is False
    assert "jj " not in str(metadata)


def test_packet_usefulness_fields_prevent_operator_ambiguity(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    response = invoke_sync_core_once(_request(repo, include_generated=False), runner=RustSyncCoreRunner(_rust_binary()))
    updated = attach_read_authority_metadata_to_receipt(
        _conflict_receipt(),
        response,
        python_decision=python_decision_from_sync_receipt(_conflict_receipt()),
    )
    metadata = updated["sync_core_read_authority"]
    usefulness = metadata["conflict_packet_candidate"]["usefulness"]

    assert metadata["engine_mode_actual"] == "rust_read_authoritative"
    assert metadata["python_policy_authority_state"] == "python_policy_authoritative"
    assert usefulness["raw_jj_required"] is False
    assert usefulness["routine_current_required"] is False
    assert usefulness["routine_diagnose_required"] is False
    assert usefulness["operator_ambiguity_expected"] is False


def test_read_authority_request_is_read_only_and_rejects_mutation(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    request = _request(repo)

    assert request["engine_mode_requested"] == "rust_read_authoritative"
    assert request["mutation_allowed"] is False
    assert request["feature_flags"]["rust_sync_core_mutation"] is False

    bad_request = dict(request)
    bad_request["mutation_allowed"] = True
    with pytest.raises(SyncCoreSchemaError):
        validate_sync_core_request(bad_request)
