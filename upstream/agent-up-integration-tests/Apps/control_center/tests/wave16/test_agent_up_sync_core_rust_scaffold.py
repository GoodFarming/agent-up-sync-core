from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess
from typing import Any

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    invoke_sync_core_once,
    invoke_sync_core_with_fallback,
)
from Apps.control_center.backend.convergence.agent_up_sync_core_schema import (
    build_contract_request_example,
    validate_sync_core_response,
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


def _request_for_repo(repo: Path, **overrides: Any) -> dict[str, Any]:
    request = build_contract_request_example(
        transaction_id="sync-core-rust-scaffold-pytest",
        repo_path=str(repo),
        workspace_path=str(repo),
        live_root_path=str(repo),
        adapter_profile="cli-jj",
        correlation_id="corr-sync-core-rust-scaffold-pytest",
        idempotency_key="idem-sync-core-rust-scaffold-pytest",
    )
    request.update(overrides)
    return request


def test_rust_cli_returns_schema_valid_read_only_repo_facts(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    request = _request_for_repo(repo)
    completed = subprocess.run(
        [_rust_binary()],
        input=json.dumps(request),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    response = validate_sync_core_response(json.loads(completed.stdout))

    assert response["engine_mode_actual"] == "rust_shadow"
    assert response["authority_state"] == "rust_shadow_observed"
    assert response["decision_class"] == "noop"
    assert response["mutation_plan"] == {}
    assert response["journal_record"] == {}
    assert response["fallback"]["python_fallback_available"] is True
    assert response["repo_facts"]["repo_path"] == str(repo)
    assert response["repo_facts"]["mutation_performed"] is False
    assert response["repo_facts"]["conflict_count"] == 0
    assert response["repo_facts"]["current"]["commit_id"]
    assert response["repo_facts"]["operation_id"]


def test_python_bridge_invokes_rust_binary_once_and_preserves_schema(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(_rust_binary())
    response = invoke_sync_core_once(_request_for_repo(repo), runner=runner)

    assert runner.call_count == 1
    assert response["graph_metrics"]["kernel_call_count"] == 1
    assert response["engine_mode_actual"] == "rust_shadow"
    assert response["authority_state"] == "rust_shadow_observed"
    assert response["repo_facts"]["root_path"] == str(repo)
    assert response["next_agent_up_action"]["action"] == "continue"
    assert "jj " not in str(response["next_agent_up_action"])


def test_rust_structured_adapter_error_is_fallback_safe(tmp_path: Path) -> None:
    missing_repo = tmp_path / "missing"
    runner = RustSyncCoreRunner(_rust_binary())
    response = invoke_sync_core_once(_request_for_repo(missing_repo), runner=runner)

    assert runner.call_count == 1
    assert response["decision_class"] == "degraded"
    assert response["degraded_reason"] == "adapter_failure"
    assert response["fallback"]["python_fallback_available"] is True
    assert response["python_fallback_reason"]
    assert response["errors"]
    assert response["errors"][0]["mutation_safe"] is True
    assert response["errors"][0]["raw_jj_guidance"] is False
    assert response["mutation_plan"] == {}
    assert response["journal_record"] == {}
    assert "jj " not in str(response["next_agent_up_action"])


def test_python_fallback_remains_available_when_rust_binary_is_missing(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner = RustSyncCoreRunner(tmp_path / "missing-agent-up-sync-core")
    response = invoke_sync_core_with_fallback(_request_for_repo(repo), runner=runner)

    assert runner.call_count == 1
    assert response["engine_mode_actual"] == "python_fallback"
    assert response["authority_state"] == "python_fallback"
    assert response["fallback"]["python_fallback_available"] is True
    assert response["fallback"]["fallback_reason"].startswith("rust_runner_failed:")
    assert response["graph_metrics"]["kernel_call_count"] == 1
    assert response["mutation_plan"] == {}
    assert response["journal_record"] == {}
