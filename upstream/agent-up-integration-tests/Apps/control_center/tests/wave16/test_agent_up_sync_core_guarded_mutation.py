from __future__ import annotations

from functools import lru_cache
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    build_sync_core_guarded_mutation_request,
    invoke_sync_core_once,
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


def _base_context() -> dict[str, Any]:
    return {
        "selected_workspace": {
            "workspace_id": "workspace::control-center::agent-up-worker.sync",
            "lane_id": "agent-up-worker.sync",
            "workspace_role": "worker",
            "workspace_lifecycle": "disposable",
        },
        "sync_group": {"sync_group_id": "sync-control-center"},
        "live_target": {"repo_id": "control-center", "live_rev": "live-1001", "live_root_state": "advanced"},
        "source_state": {
            "workspace_rev": "worker-1010x",
            "source_rev": "worker-1010x",
            "prepared_rev": "prepared-1010x",
            "authored_state": "prepared",
            "source_provenance_state": "prepared",
            "prepared_revision_recovery": {"handle": "recover-prepared-1010x"},
        },
        "runtime_context": {
            "runtime_cutover_required": False,
            "runtime_cutover_state": "already_current",
            "runtime_stage_content_current": True,
        },
    }


def _generated_context(*, worker_intent: bool = False) -> dict[str, Any]:
    path = "frontend/cockpit/dist/assets/index.js"
    context = _base_context()
    context["guarded_mutation"] = {
        "requested_mutation": "generated_artifact_cleanup",
        "affected_paths": [path],
        "recovery_handle": "recover-generated-cleanup",
    }
    context["conflict_context"] = {
        "conflict_packet_id": "packet-generated-cleanup",
        "generated_artifact_paths": [path],
        "conflicted_paths": [path],
        "worker_intent_paths": [path] if worker_intent else [],
    }
    return context


def _semantic_context(*, stale: bool = False, unrelated: bool = False) -> dict[str, Any]:
    context = _base_context()
    changed_paths = ["src/router.py", "src/unrelated.py"] if unrelated else ["src/router.py"]
    context["guarded_mutation"] = {
        "requested_mutation": "semantic_conflict_continuation_fold",
        "affected_paths": ["src/router.py"],
        "changed_paths": changed_paths,
        "stale_packet": stale,
        "recovery_handle": "recover-semantic-fold",
    }
    context["conflict_context"] = {
        "conflict_packet_id": "packet-semantic-fold",
        "conflict_kind": "publish",
        "base_rev": "head-1",
        "materialized_conflict_paths": ["src/router.py"],
        "conflicted_paths": ["src/router.py"],
        "semantic_paths": ["src/router.py"],
        "side_context": {
            "base": {"revision": "head-1"},
            "live": {"revision": "head-1001"},
            "worker": {"revision": "head-1010x"},
        },
    }
    return context


def _request(repo: Path, context: dict[str, Any], *, journal: Path) -> dict[str, Any]:
    return build_sync_core_guarded_mutation_request(
        workspace_id="workspace::control-center::agent-up-worker.sync",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=context,
        transaction_id="sync-core-guarded-mutation-test",
        correlation_id="corr-sync-core-guarded-mutation-test",
        idempotency_key="idem-sync-core-guarded-mutation-test",
        recovery_journal_path=str(journal),
    )


def _run(repo: Path, context: dict[str, Any], *, journal: Path) -> dict[str, Any]:
    return invoke_sync_core_once(_request(repo, context, journal=journal), runner=RustSyncCoreRunner(_rust_binary()))


def _journal_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_generated_artifact_cleanup_writes_journal_and_is_idempotent(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    artifact = repo / "frontend/cockpit/dist/assets/index.js"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("generated", encoding="utf-8")
    journal = tmp_path / "journal/generated.jsonl"

    response = _run(repo, _generated_context(), journal=journal)
    second = _run(repo, _generated_context(), journal=journal)

    assert response["engine_mode_actual"] == "rust_mutation_authoritative"
    assert response["authority_state"] == "rust_mutation_authoritative"
    assert response["decision_class"] == "generated_policy_applied"
    assert response["mutation_plan"]["mutation_class"] == "generated_artifact_cleanup"
    assert response["mutation_plan"]["safe_to_apply"] is True
    assert response["mutation_plan"]["journal_required"] is True
    assert response["journal_record"]["state"] == "applied"
    assert response["journal_record"]["recovery_handle"] == "recover-generated-cleanup"
    assert response["telemetry"]["mutation_performed"] is True
    assert not artifact.exists()
    assert second["mutation_plan"]["safe_to_apply"] is True
    assert len(_journal_lines(journal)) == 2


def test_generated_cleanup_blocks_worker_authored_generated_surface(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    artifact = repo / "frontend/cockpit/dist/assets/index.js"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("worker-authored", encoding="utf-8")
    journal = tmp_path / "journal/generated-blocked.jsonl"

    response = _run(repo, _generated_context(worker_intent=True), journal=journal)

    assert response["decision_class"] == "blocked"
    assert response["mutation_plan"]["safe_to_apply"] is False
    assert response["mutation_plan"]["blocked_reason"] == "worker_authored_generated_surface"
    assert response["journal_record"]["state"] == "blocked"
    assert artifact.exists()
    assert _journal_lines(journal)[0]["blocked_reason"] == "worker_authored_generated_surface"


def test_semantic_fold_eligibility_is_journaled_without_applying_fold(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    journal = tmp_path / "journal/semantic-fold.jsonl"

    response = _run(repo, _semantic_context(), journal=journal)

    assert response["decision_class"] == "clean_merge"
    assert response["conflict_authority"] == "semantic_resolution_required"
    assert response["mutation_plan"]["mutation_class"] == "semantic_conflict_continuation_fold"
    assert response["mutation_plan"]["execution_owner"] == "python_fold_executor_after_rust_guard"
    assert response["mutation_plan"]["safe_to_apply"] is True
    assert response["mutation_plan"]["mutation_performed"] is False
    assert response["journal_record"]["state"] == "journaled"
    assert response["journal_record"]["affected_paths"] == ["src/router.py"]
    assert response["telemetry"]["mutation_performed"] is False
    assert _journal_lines(journal)[0]["operation_kind"] == "semantic_conflict_continuation_fold"


def test_semantic_fold_blocks_stale_packet_and_unrelated_edits(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    stale = _run(repo, _semantic_context(stale=True), journal=tmp_path / "journal/stale.jsonl")
    unrelated = _run(repo, _semantic_context(unrelated=True), journal=tmp_path / "journal/unrelated.jsonl")

    assert stale["decision_class"] == "blocked"
    assert stale["mutation_plan"]["blocked_reason"] == "stale_conflict_packet_revision_anchor"
    assert stale["journal_record"]["state"] == "blocked"
    assert unrelated["decision_class"] == "blocked"
    assert unrelated["mutation_plan"]["blocked_reason"] == "changed_paths_outside_materialized_conflict_paths"
    assert unrelated["journal_record"]["state"] == "blocked"


def test_mutation_authority_request_requires_mutation_allowed(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    request = _request(repo, _generated_context(), journal=tmp_path / "journal/schema.jsonl")
    assert request["engine_mode_requested"] == "rust_mutation_authoritative"
    assert request["mutation_allowed"] is True

    bad_request = dict(request)
    bad_request["mutation_allowed"] = False
    with pytest.raises(SyncCoreSchemaError):
        validate_sync_core_request(bad_request)
