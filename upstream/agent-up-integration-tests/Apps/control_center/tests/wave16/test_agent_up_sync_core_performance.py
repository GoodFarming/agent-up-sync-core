from __future__ import annotations

from functools import lru_cache
import subprocess
from pathlib import Path
from typing import Any

from Apps.control_center.backend.convergence import agent_up_sync_engine as sync_engine
from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    attach_read_authority_metadata_to_receipt,
    build_sync_core_read_authority_request,
    invoke_sync_core_once,
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


def _context(*, conflict: bool = False) -> dict[str, Any]:
    context: dict[str, Any] = {
        "selected_workspace": {
            "workspace_id": "workspace::control-center::agent-up-worker.sync",
            "lane_id": "agent-up-worker.sync",
            "workspace_role": "worker",
            "workspace_lifecycle": "disposable",
            "workspace_sync_state": "fresh",
        },
        "sync_group": {"sync_group_id": "sync-control-center", "peer_debt_state": "advisory"},
        "live_target": {"repo_id": "control-center", "live_rev": "live-head", "live_root_state": "unchanged"},
        "source_state": {
            "workspace_rev": "worker-head",
            "source_rev": "worker-head",
            "authored_state": "clean",
            "source_provenance_state": "none_or_clean",
        },
        "runtime_context": {
            "runtime_cutover_required": False,
            "runtime_cutover_state": "already_current",
            "runtime_stage_content_current": True,
        },
    }
    if conflict:
        context["selected_workspace"]["workspace_sync_state"] = "publish_conflict"
        context["live_target"]["live_root_state"] = "advanced"
        context["source_state"].update(
            {
                "authored_state": "prepared",
                "source_provenance_state": "prepared",
                "prepared_revision_recovery": {"handle": "prepared-rev-worker"},
            }
        )
        context["conflict_context"] = {
            "conflict_packet_id": "conflict-performance-ab",
            "conflict_kind": "publish",
            "base_rev": "head-1",
            "conflicted_paths": [
                "Apps/control_center/backend/convergence/agent_up_sync_engine.py",
                "frontend/cockpit/dist/assets/index.js",
            ],
            "semantic_paths": ["Apps/control_center/backend/convergence/agent_up_sync_engine.py"],
            "generated_artifact_paths": ["frontend/cockpit/dist/assets/index.js"],
            "side_context": {
                "base": {"revision": "head-1"},
                "live": {"revision": "head-1001"},
                "worker": {"revision": "head-1010x"},
            },
        }
    return context


def _request(repo: Path, *, conflict: bool = False) -> dict[str, Any]:
    return build_sync_core_read_authority_request(
        workspace_id="workspace::control-center::agent-up-worker.sync",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=_context(conflict=conflict),
        transaction_id="sync-core-performance",
        correlation_id="corr-sync-core-performance",
        idempotency_key="idem-sync-core-performance",
    )


def _performance_budget(response: dict[str, Any]) -> dict[str, Any]:
    return response["telemetry"]["performance_budget"]


def test_clean_noop_uses_one_rust_call_and_reports_budget_class(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(_rust_binary())

    response = invoke_sync_core_once(_request(repo), runner=runner)
    budget = _performance_budget(response)

    assert runner.call_count == 1
    assert response["graph_metrics"]["kernel_call_count"] == 1
    assert budget["one_kernel_call"] is True
    assert budget["algorithmic_budget_class"] == "clean_noop"
    assert budget["latency_budget_ms"] == 250.0
    assert budget["latency_budget_state"] == "pass"
    assert budget["memory_budget_state"] == "pass"
    assert budget["output_budget_state"] == "pass"
    assert budget["repo_lock_budget_state"] == "pass"
    assert budget["inspected_fact_count"] >= 1
    assert budget["decision_driver_count"] >= 1


def test_conflict_packet_reports_latency_memory_output_and_conflict_budget(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    response = invoke_sync_core_once(_request(repo, conflict=True), runner=RustSyncCoreRunner(_rust_binary()))
    budget = _performance_budget(response)
    packet = response["conflict_packet_candidate"]

    assert response["decision_class"] == "materialized_conflict"
    assert budget["algorithmic_budget_class"] == "conflict_packet"
    assert budget["latency_budget_ms"] == 2000.0
    assert budget["latency_budget_state"] == "pass"
    assert budget["conflict_count"] == 2
    assert budget["output_bytes_estimate"] > 0
    assert budget["memory_bytes_estimate"] > 0
    assert packet["usefulness"]["raw_jj_required"] is False


def test_performance_budget_is_projected_to_sync_receipt_metadata(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    response = invoke_sync_core_once(_request(repo, conflict=True), runner=RustSyncCoreRunner(_rust_binary()))
    receipt = {
        "outcome": "publish_conflict",
        "exit_phase": "publish_conflict",
        "workspace_final_state": "conflict_materialized",
        "workspace_sync_state": "publish_conflict",
        "conflict_authority": "semantic_resolution_required",
        "blocking": True,
        "live_head_moved": True,
    }

    updated = attach_read_authority_metadata_to_receipt(receipt, response)
    budget = updated["sync_core_read_authority"]["performance_budget"]
    metrics = updated["sync_runtime_metrics"]

    assert budget["algorithmic_budget_class"] == "conflict_packet"
    assert budget["one_kernel_call"] is True
    assert metrics["sync_core_read_authority_budget_class"] == "conflict_packet"
    assert metrics["sync_core_read_authority_latency_budget_state"] == "pass"
    assert metrics["sync_core_read_authority_one_kernel_call"] is True


def test_code_intelligence_refresh_is_queued_not_run_on_sync_hot_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_UP_CODE_INTELLIGENCE_SYNC_REFRESH_RUN_PROVIDER_PROCESSES", raising=False)
    observed: dict[str, Any] = {}
    background_observed: dict[str, Any] = {}

    def scheduler(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "outcome": "queued",
            "mode": "warm",
            "queue": {
                "job_state": "queued",
                "queued_heavy_jobs": 1,
                "runs_provider_processes": kwargs["run_provider_processes"],
                "provider_process_execution_requested": kwargs["run_provider_processes"],
            },
            "jobs": [{"provider": "codebase-memory", "job_state": "queued", "root_head": kwargs["root_head"]}],
        }

    def background_launcher(**kwargs: Any) -> dict[str, Any]:
        background_observed.update(kwargs)
        return {"state": "started", "pid": 12345, "root_head": kwargs["root_head"]}

    payload = sync_engine._with_code_intelligence_sync_refresh(
        {"source_publish_outcome": "green", "published_rev": "root-head-2"},
        sync_engine.SyncEngineRequest(workspace_id="workspace::control-center::agent-up-worker.sync"),
        store=object(),  # type: ignore[arg-type]
        workspace={"path": str(tmp_path), "project_key": str(tmp_path)},
        warm_scheduler=scheduler,
        background_launcher=background_launcher,
    )

    refresh = payload["code_intelligence_sync_refresh"]
    assert observed["run_provider_processes"] is False
    assert refresh["outcome"] == "queued"
    assert refresh["queue"]["runs_provider_processes"] is False
    assert refresh["queue"]["provider_process_execution_requested"] is False
    assert refresh["queue"]["queued_heavy_jobs"] == 1
    assert background_observed["root_head"] == "root-head-2"
    assert refresh["background_refresh"]["state"] == "started"
