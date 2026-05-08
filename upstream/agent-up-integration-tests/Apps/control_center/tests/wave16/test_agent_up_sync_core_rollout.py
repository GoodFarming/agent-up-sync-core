from __future__ import annotations

from pathlib import Path

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    REQUIRED_DEPLOYMENT_CANARY_CLASSES,
    SYNC_CORE_ROLLOUT_MODE_ENV,
    build_sync_core_rollout_lock_receipt,
    summarize_sync_core_deployment_canary_matrix,
    sync_core_managed_repair_command,
)
from Apps.control_center.backend.convergence.agent_up_sync_engine import _sync_core_transaction_executor_enabled
from Apps.control_center.backend.convergence.blocker_registry import format_default_next_exact_command


ROOT = Path(__file__).resolve().parents[4]
CRATE_ROOT = ROOT / "Apps/control_center/rust/agent-up-sync-core"


def _canary_receipt(**overrides: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "outcome": "boundary_green",
        "workspace_id": "workspace::control-center::agent-up-worker.sync",
        "workspace_final_state": "fresh",
        "safe_to_continue": True,
        "runtime_cutover_required": False,
        "runtime_cutover_state": "already_current",
        "agent_facing_command_budget": {"raw_jj_command_count": 0},
        "sync_core_read_authority": {
            "engine_mode_actual": "rust_read_authoritative",
            "fallback": {
                "python_fallback_available": True,
                "fallback_command": "agent-up sync --brief --json",
            },
            "performance_budget": {
                "algorithmic_budget_class": "clean_noop",
                "latency_budget_state": "pass",
                "one_kernel_call": True,
            },
            "degraded_reason": "not_applicable",
            "worker_raw_jj_guidance": False,
        },
    }
    receipt.update(overrides)
    return receipt


def _deployment_canary_matrix() -> dict[str, object]:
    return {
        "classes": {
            class_name: {
                "result": "pass",
                "receipt_source": "installed_agent_up_sync",
                "receipt_artifact": f"Apps/control_center/test_artifacts/sync-core-canary/{class_name}.json",
            }
            for class_name in REQUIRED_DEPLOYMENT_CANARY_CLASSES
        }
    }


def test_clean_read_authority_canary_is_not_deployment_readiness() -> None:
    rollout = build_sync_core_rollout_lock_receipt(
        authority_mode="rust_read_authoritative",
        canary_receipt=_canary_receipt(),
        runtime_verify={"status": "current", "runtime_cutover_required": False},
        feature_flags={"mode": "rust_read_authoritative"},
    )

    assert rollout["rollout_state"] == "canary_ready"
    assert rollout["completion_tier"] == "canary_ready"
    assert rollout["deployment_ready"] is False
    assert set(rollout["deployment_blockers"]) == {
        "transaction_authority_not_proven",
        "deployment_canary_matrix_incomplete",
    }
    assert rollout["public_command"] == "agent-up sync"
    assert rollout["feature_flags"]["defaults_safe"] is True
    assert rollout["fallback"]["python_fallback_available"] is True
    assert rollout["rollback"]["available"] is True
    assert rollout["rollback"]["rollback_flag"] == f"{SYNC_CORE_ROLLOUT_MODE_ENV}=python"
    assert "jj " not in str(rollout)


def test_full_installed_canary_matrix_and_transaction_authority_can_reach_deployment_ready() -> None:
    receipt = _canary_receipt(
        sync_core_read_authority={
            "engine_mode_actual": "rust_transaction_candidate",
            "fallback": {
                "python_fallback_available": True,
                "fallback_command": "agent-up sync --brief --json",
            },
            "performance_budget": {
                "algorithmic_budget_class": "semantic_materialized_conflict_continuation",
                "latency_budget_state": "pass",
                "one_kernel_call": True,
            },
            "degraded_reason": "not_applicable",
            "worker_raw_jj_guidance": False,
        }
    )
    rollout = build_sync_core_rollout_lock_receipt(
        authority_mode="rust_transaction_candidate",
        canary_receipt=receipt,
        runtime_verify={"status": "current", "runtime_cutover_required": False},
        feature_flags={"mode": "rust_transaction_candidate"},
        canary_matrix=_deployment_canary_matrix(),
    )

    assert rollout["rollout_state"] == "deployment_ready"
    assert rollout["completion_tier"] == "deployment_ready"
    assert rollout["deployment_ready"] is True
    assert rollout["deployment_blockers"] == []
    assert rollout["deployment_canary_matrix"]["complete"] is True


def test_transaction_executor_defaults_on_with_explicit_rollback_flags(monkeypatch) -> None:
    monkeypatch.delenv("CONTROLCENTER_SYNC_CORE_TRANSACTION_EXECUTOR", raising=False)
    monkeypatch.delenv(SYNC_CORE_ROLLOUT_MODE_ENV, raising=False)
    assert _sync_core_transaction_executor_enabled() is True

    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_TRANSACTION_EXECUTOR", "0")
    assert _sync_core_transaction_executor_enabled() is False

    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_TRANSACTION_EXECUTOR", "1")
    assert _sync_core_transaction_executor_enabled() is True

    monkeypatch.setenv(SYNC_CORE_ROLLOUT_MODE_ENV, "python")
    assert _sync_core_transaction_executor_enabled() is False


def test_deployment_canary_matrix_rejects_constructed_or_synthetic_receipts() -> None:
    matrix = _deployment_canary_matrix()
    classes = matrix["classes"]
    assert isinstance(classes, dict)
    classes["dirty_publish"] = {
        "result": "pass",
        "receipt_source": "synthetic",
        "receipt_artifact": "Apps/control_center/test_artifacts/sync-core-canary/dirty_publish.json",
    }

    summary = summarize_sync_core_deployment_canary_matrix(matrix)

    assert summary["complete"] is False
    assert "dirty_publish" in summary["synthetic_classes"]
    assert "dirty_publish" in summary["failed_classes"]


def test_rollout_blocks_hidden_fallback_or_raw_jj_guidance() -> None:
    receipt = _canary_receipt(
        next_exact_command="jj workspace update-stale",
        agent_facing_command_budget={"raw_jj_command_count": 1},
        sync_core_read_authority={
            "engine_mode_actual": "rust_read_authoritative",
            "fallback": {"python_fallback_available": False},
            "performance_budget": {"latency_budget_state": "pass"},
            "degraded_reason": "not_applicable",
        },
    )
    rollout = build_sync_core_rollout_lock_receipt(
        authority_mode="rust_read_authoritative",
        canary_receipt=receipt,
        runtime_verify={"status": "current", "runtime_cutover_required": False},
    )

    assert rollout["rollout_state"] == "blocked"
    assert set(rollout["blockers"]) >= {"raw_jj_guidance", "python_fallback_unavailable"}


def test_op_drift_repair_is_agent_up_owned_not_raw_jj() -> None:
    command = sync_core_managed_repair_command(
        "jj_working_copy_stale",
        workspace_id="workspace::control-center::agent-up-worker.sync",
    )
    registry_command = format_default_next_exact_command(
        "jj_working_copy_stale",
        workspace_id="workspace::control-center::agent-up-worker.sync",
    )

    assert command == registry_command
    assert command.startswith("agent-up doctor --repair-workspace-stale")
    assert "jj " not in command
    assert "workspace update-stale" not in command


def test_guidance_sources_keep_raw_jj_out_of_routine_sync_paths() -> None:
    use_sync = (ROOT / "@agents/.shared/skills/use-agent-up-sync/SKILL.md").read_text()
    resolve_conflicts = (ROOT / "@agents/.shared/skills/resolve-conflicts/SKILL.md").read_text()
    agent_up_help = (ROOT / "Apps/control_center/bin/agent-up").read_text()

    assert "agent-up doctor --repair-workspace-stale" in use_sync
    assert "agent-up doctor --repair-workspace-stale" in resolve_conflicts
    assert "--repair-workspace-stale" in agent_up_help
    assert "Do not ask ordinary workers to run `jj workspace update-stale`" in use_sync
    assert "jj workspace update-stale" not in agent_up_help
    assert "agent-up sync --continue" not in use_sync
    assert "agent-up sync --continue" not in resolve_conflicts


def test_open_source_readiness_packet_is_complete() -> None:
    required = {
        "LICENSE",
        "README.md",
        "ARCHITECTURE.md",
        "SCHEMAS.md",
        "SAFETY.md",
        "EXAMPLES.md",
        "BENCHMARKS.md",
        "MSRV.md",
        "RELEASE.md",
    }
    missing = [name for name in sorted(required) if not (CRATE_ROOT / name).exists()]
    assert not missing

    cargo = (CRATE_ROOT / "Cargo.toml").read_text()
    readme = (CRATE_ROOT / "README.md").read_text()
    assert 'version = "0.1.0"' in cargo
    assert 'rust-version = "1.75"' in cargo
    assert 'license = "Apache-2.0"' in cargo
    assert "library-first Rust convergence kernel" in readme
