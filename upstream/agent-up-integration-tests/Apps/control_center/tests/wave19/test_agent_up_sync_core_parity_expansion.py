from __future__ import annotations

from functools import lru_cache
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    attach_transaction_authority_metadata_to_receipt,
    build_sync_core_transaction_candidate_request,
    invoke_sync_core_once,
)
from Apps.control_center.backend.convergence.sync_state_web import (
    attach_sync_state_web_snapshot,
    validate_sync_state_web_snapshot,
)


ROOT = Path(__file__).resolve().parents[4]
RAW_JJ_COMMAND_RE = re.compile(r"(^|[;&|]\s*)jj(\s|$)")


@lru_cache(maxsize=1)
def _rust_binary() -> Path:
    rust_env = dict(os.environ)
    real_home = ROOT.parent
    rust_env.setdefault("CARGO_HOME", str(real_home / ".cargo"))
    rust_env.setdefault("RUSTUP_HOME", str(real_home / ".rustup"))
    subprocess.run(
        ["cargo", "build", "-p", "agent-up-sync-core", "--all-features"],
        cwd=ROOT,
        env=rust_env,
        check=True,
    )
    binary = ROOT / "target/debug/agent-up-sync-core"
    assert binary.exists()
    return binary


def _init_jj_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["jj", "git", "init", str(repo)], cwd=ROOT, check=True, capture_output=True, text=True)
    return repo


def _jj(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jj", "--repository", str(repo), *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _jj_text(repo: Path, *args: str) -> str:
    return _jj(repo, *args).stdout.strip()


def _commit_id(repo: Path, rev: str) -> str:
    return _jj_text(repo, "log", "--no-graph", "-r", rev, "-T", "commit_id.short()")


def _seed_base(repo: Path, *, path: str = "file.txt") -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base\n", encoding="utf-8")
    _jj(repo, "describe", "-m", "base")
    base_rev = _commit_id(repo, "@")
    _jj(repo, "bookmark", "set", "--allow-backwards", "-r", "@", "rolling-control-center")
    return base_rev


def _request(
    repo: Path,
    journal: Path,
    *,
    live_rev: str,
    transaction_class: str = "dirty_publish",
    source_revset: str = "@-",
    affected_paths: list[str] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = affected_paths or ["file.txt"]
    source_rev = _commit_id(repo, source_revset)
    context: dict[str, Any] = {
        "selected_workspace": {
            "workspace_id": "workspace::repo-a::agent-up-worker.b",
            "lane_id": "agent-up-worker.b",
            "workspace_role": "worker",
            "workspace_lifecycle": "disposable",
            "workspace_sync_state": "fresh",
        },
        "sync_group": {
            "sync_group_id": "sync-repo-a",
            "peer_debt_state": "advisory",
            "group_head": live_rev,
            "basis_state": "fresh",
        },
        "live_target": {
            "repo_id": "repo-a",
            "live_rev": live_rev,
            "live_root_state": "unchanged",
            "basis_state": "fresh",
        },
        "source_state": {
            "workspace_rev": source_rev,
            "source_rev": source_rev,
            "prepared_rev": source_rev,
            "authored_state": "prepared",
            "source_provenance_state": "prepared",
            "no_local_commit_meaning": "prepared_revision_recovery",
            "prepared_revision_recovery": {"handle": "recover-prepared-publish"},
        },
        "runtime_context": {
            "runtime_cutover_required": False,
            "runtime_cutover_state": "already_current",
            "runtime_stage_content_current": True,
            "runtime_stage_hash": "runtime-stage-test",
        },
        "transaction_candidate": {
            "phases": ["prepare", "retry", "publish", "refresh", "fold"],
            "affected_paths": paths,
            "scenario_id": transaction_class,
            "operation_kind": transaction_class,
            "transaction_class": transaction_class,
            "source_revset": source_revset,
            "publish_bookmark": "rolling-control-center",
            "execute": True,
        },
    }
    if extra_context:
        _deep_update(context, extra_context)
    request = build_sync_core_transaction_candidate_request(
        workspace_id="workspace::repo-a::agent-up-worker.b",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-repo-a",
        python_context=context,
        requested_operation="prepare_publish",
        transaction_id="sync-core-pce75-parity-expansion",
        correlation_id="corr-sync-core-pce75-parity-expansion",
        idempotency_key=f"idem-sync-core-pce75-{transaction_class}",
        recovery_journal_path=str(journal),
    )
    request["feature_flags"]["rust_sync_core_transaction_executor"] = True
    return request


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _run(request: dict[str, Any]) -> dict[str, Any]:
    return invoke_sync_core_once(request, runner=RustSyncCoreRunner(_rust_binary()))


def _command_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for value_item in value for item in _command_strings(value_item)]
    if isinstance(value, dict):
        strings: list[str] = []
        for key, item in value.items():
            if key in {"command", "next_command", "next_exact_command", "continue_command", "after_resolving_files_command"}:
                strings.extend(_command_strings(item))
            elif isinstance(item, (dict, list)):
                strings.extend(_command_strings(item))
        return strings
    return []


def _assert_no_raw_jj_guidance(payload: dict[str, Any]) -> None:
    leaked = [command for command in _command_strings(payload) if RAW_JJ_COMMAND_RE.search(command)]
    assert leaked == []


def test_conflict_packet_authority_and_continuation_eligibility_are_rust_topology(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)
    (repo / "file.txt").write_text("from-a\n", encoding="utf-8")
    _jj(repo, "describe", "-m", "worker A")
    a_rev = _commit_id(repo, "@")
    _jj(repo, "bookmark", "set", "--allow-backwards", "-r", "@", "rolling-control-center")
    _jj(repo, "new", "-r", base_rev, "-m", "worker B")
    (repo / "file.txt").write_text("from-b\n", encoding="utf-8")

    response = _run(
        _request(
            repo,
            tmp_path / "journal/conflict.jsonl",
            live_rev=a_rev,
            transaction_class="publish_conflict_materialize",
            source_revset="@",
            extra_context={
                "conflict_context": {
                    "conflict_packet_id": "packet-ab-1",
                    "conflict_packet_version": "v1",
                    "base_rev": base_rev,
                    "conflicted_paths": ["file.txt"],
                    "semantic_paths": ["file.txt"],
                }
            },
        )
    )

    assert response["decision_class"] == "materialized_conflict"
    assert response["conflict_packet_authority"]["conflict_authority"] == "rolling_live_head"
    assert response["conflict_packet_authority"]["conflict_packet_id"] == "packet-ab-1"
    assert response["continuation_eligibility"]["state"] == "eligible_after_file_resolution"
    assert response["continuation_eligibility"]["after_resolving_files_command"] == (
        'agent-up sync -m "<resolution summary>" --brief --json'
    )
    assert response["python_post_rust_graph_recompute_required"] is False
    assert "conflict_packet_authority" in response["inspected_fact_classes"]
    assert "continuation_eligibility" in response["inspected_fact_classes"]
    _assert_no_raw_jj_guidance(response)


def test_no_local_commit_meaning_and_source_provenance_round_trip(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)

    response = _run(
        _request(
            repo,
            tmp_path / "journal/no-local.jsonl",
            live_rev=base_rev,
            source_revset="@",
            extra_context={
                "source_state": {
                    "authored_state": "clean",
                    "source_provenance_state": "published",
                    "no_local_commit_meaning": "already_published_hidden_marker",
                },
                "transaction_candidate": {"execute": False},
            },
        )
    )

    assert response["source_provenance_state"] == "published"
    assert response["no_local_commit_meaning"] == "already_published_hidden_marker"
    assert response["source_provenance"]["state"] == "published"
    assert response["source_provenance"]["no_local_commit_meaning"] == "already_published_hidden_marker"
    assert response["python_post_rust_graph_recompute_required"] is False
    _assert_no_raw_jj_guidance(response)


def test_stale_worker_stack_guard_blocks_broad_authority_surface_range(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)
    _jj(repo, "new", "-r", base_rev, "-m", "old broad worker stack")
    authority_path = "@planning/agent-up-v0.3/workpacks/CLAIMS-MAP.agent-up-v0.3.json"
    path = repo / authority_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"old":"worker"}\n', encoding="utf-8")

    response = _run(
        _request(
            repo,
            tmp_path / "journal/stale-stack.jsonl",
            live_rev=base_rev,
            source_revset="@",
            affected_paths=[authority_path],
            extra_context={
                "unpublished_range": {
                    "commit_count": 7,
                    "range_shape": "broad",
                    "age_class": "old",
                    "authority_surface_overlap": True,
                    "changed_paths": [authority_path],
                },
                "live_basis": {"state": "stale", "basis_valid": False, "reason": "worker_stack_old"},
            },
        )
    )

    assert response["decision_class"] == "blocked"
    assert response["mutation_plan"]["safe_to_apply"] is False
    assert response["unpublished_range_shape"] == "broad"
    assert response["range_age_class"] == "old"
    assert response["authority_surface_overlap"] is True
    assert response["stale_worker_stack_guard"]["state"] == "stop"
    assert response["stale_worker_stack_guard"]["next_agent_up_action"]["command"].startswith("agent-up sync --probe")
    assert response["unsupported_topology_reason"] == "stale_worker_stack_authority_surface_overlap"
    _assert_no_raw_jj_guidance(response)


def test_fresh_authority_surface_edit_is_advisory_not_stale_stack_stop(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)
    _jj(repo, "new", "-r", base_rev, "-m", "fresh authority edit")
    authority_path = "@planning/agent-up-v0.3/workpacks/example.md"
    path = repo / authority_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fresh planning edit\n", encoding="utf-8")

    response = _run(
        _request(
            repo,
            tmp_path / "journal/fresh-authority.jsonl",
            live_rev=base_rev,
            source_revset="@",
            affected_paths=[authority_path],
        )
    )

    assert response["decision_class"] == "clean_merge"
    assert response["mutation_plan"]["safe_to_apply"] is True
    assert response["stale_worker_stack_guard"]["state"] == "salvage"
    assert response["stale_worker_stack_guard"]["reasons"] == ["authority_surface_overlap"]
    assert response["unsupported_topology_reason"] is None
    assert response["topology_authority"]["unsupported_topology"]["state"] == "supported"
    _assert_no_raw_jj_guidance(response)


def test_live_and_sync_group_stale_basis_fail_closed_with_one_agent_up_action(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)
    _jj(repo, "new", "-r", base_rev, "-m", "worker edit")
    (repo / "file.txt").write_text("worker\n", encoding="utf-8")

    response = _run(
        _request(
            repo,
            tmp_path / "journal/stale-basis.jsonl",
            live_rev=base_rev,
            source_revset="@",
            extra_context={
                "live_basis": {"state": "stale", "basis_valid": False, "reason": "live_head_moved"},
                "sync_group": {"basis_state": "stale", "basis_valid": False, "stale_reason": "group_head_moved"},
            },
        )
    )

    assert response["decision_class"] == "blocked"
    assert response["live_basis_state"] == "stale"
    assert response["sync_group_basis_state"] == "stale"
    assert response["unsupported_topology_reason"] == "stale_live_or_sync_group_basis"
    assert response["next_agent_up_action"]["action"] == "python_fallback"
    assert response["next_agent_up_action"]["command"] == "agent-up sync --probe --brief --json"
    _assert_no_raw_jj_guidance(response)


def test_sync_state_web_projects_pce75_topology_and_cost_fields(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)
    _jj(repo, "new", "-r", base_rev, "-m", "worker dirty publish")
    (repo / "file.txt").write_text("worker\n", encoding="utf-8")

    response = _run(_request(repo, tmp_path / "journal/dirty.jsonl", live_rev=base_rev, source_revset="@"))
    receipt = attach_transaction_authority_metadata_to_receipt(
        {
            "workspace_id": "workspace::repo-a::agent-up-worker.b",
            "sync_group_id": "sync-repo-a",
            "repo_id": "repo-a",
            "live_rolling_head": base_rev,
        },
        response,
    )
    annotated = attach_sync_state_web_snapshot(
        receipt,
        trigger="pce75-test",
        workspace_id="workspace::repo-a::agent-up-worker.b",
        persist=False,
    )
    snapshot = annotated["sync_state_web_snapshot"]

    assert validate_sync_state_web_snapshot(snapshot) == []
    assert snapshot["conflict_packet_authority"]["state"] in {"not_applicable", "none"}
    assert snapshot["source_provenance"]["state"] == response["source_provenance_state"]
    assert snapshot["no_local_commit_meaning"] == response["no_local_commit_meaning"]
    assert snapshot["unpublished_range_shape"] == response["unpublished_range_shape"]
    assert snapshot["unpublished_range"]["shape"] == response["unpublished_range_shape"]
    assert snapshot["range_age_class"] == response["range_age_class"]
    assert snapshot["authority_surface_overlap"] is response["authority_surface_overlap"]
    assert snapshot["stale_worker_stack_guard"]["state"] == response["stale_worker_stack_guard"]["state"]
    assert snapshot["live_basis_state"] == response["live_basis_state"]
    assert snapshot["sync_group_basis_state"] == response["sync_group_basis_state"]
    assert snapshot["unsupported_topology_reason"] == response["unsupported_topology_reason"]
    assert snapshot["python_post_rust_graph_recompute_required"] is False
    assert "rust_adapter_jj_command_count" in snapshot["cost_split"]


def test_transaction_receipt_exposes_topology_handoff_without_python_graph_archaeology(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    base_rev = _seed_base(repo)
    _jj(repo, "new", "-r", base_rev, "-m", "worker dirty publish")
    (repo / "file.txt").write_text("worker\n", encoding="utf-8")

    response = _run(_request(repo, tmp_path / "journal/receipt.jsonl", live_rev=base_rev, source_revset="@"))
    receipt = attach_transaction_authority_metadata_to_receipt({"workspace_id": "workspace::repo-a::agent-up-worker.b"}, response)
    metadata = receipt["sync_core_transaction_authority"]

    assert metadata["topology_summary"]["schema_id"] == "control-center.agent-up.sync-core.topology-summary.v0.1"
    assert metadata["python_post_rust_graph_recompute_required"] is False
    assert receipt["sync_runtime_metrics"]["python_post_rust_graph_recompute_count"] == 0
    assert "sync_core_transaction_authority_adapter_jj_command_count" in receipt["sync_runtime_metrics"]
    assert "sync_core_transaction_authority_adapter_subprocess_count" in receipt["sync_runtime_metrics"]
    _assert_no_raw_jj_guidance(receipt)


def test_installed_agent_up_sync_ab_conflict_carries_pce75_topology(tmp_path: Path) -> None:
    from Apps.control_center.tests.agent_up_scenarios.harness import AgentUpScenarioHarness

    installed_agent_up = Path(os.environ.get("AGENT_UP_SCENARIO_AGENT_UP_BIN") or ROOT / "Apps/control_center/bin/agent-up")
    harness = AgentUpScenarioHarness(tmp_path / "installed-pce75-ab-conflict", agent_up_path=installed_agent_up)
    harness.bootstrap_topology(worker_count=2)
    extra_env = {
        "CONTROLCENTER_SYNC_CORE_RUST_BINARY": str(_rust_binary()),
        "AGENT_UP_CODE_INTELLIGENCE_SYNC_REFRESH": "0",
    }

    harness.edit_file("scenario.agent2", "file.txt", "from-b\n")
    harness.edit_file("scenario.agent1", "file.txt", "from-a\n")

    first = harness.run_sync(
        scenario_id="pce75_installed_a_publish",
        lane_id="scenario.agent1",
        message="pce75 installed A publish",
        engine_mode="python",
        extra_args=["--apply-runtime"],
        extra_env=extra_env,
    )
    assert first.returncode == 0
    first_tx = first.receipt["sync_core_transaction_authority"]
    assert first_tx["transaction_class"] == "dirty_publish"
    assert first_tx["mutation_performed"] is True
    assert first_tx["topology_authority"]["authority_owner"] == "rust_sync_core"
    assert first_tx["topology_authority"]["source_provenance"]["state"] in {"prepared", "published"}
    assert first_tx["python_post_rust_graph_recompute_count"] == 0
    assert first.receipt["agent_facing_command_budget"]["raw_jj_command_count"] == 0
    _assert_no_raw_jj_guidance(first.receipt)

    conflict = harness.run_sync(
        scenario_id="pce75_installed_b_conflict",
        lane_id="scenario.agent2",
        message="pce75 installed B conflict",
        engine_mode="python",
        extra_args=["--apply-runtime"],
        extra_env=extra_env,
    )
    assert conflict.returncode in {0, 11}
    assert conflict.receipt["caller_action_required"] is True
    assert conflict.receipt["source_publish_outcome"] == "publish_conflict"
    conflict_tx = conflict.receipt["sync_core_transaction_authority"]
    topology = conflict_tx["topology_authority"]
    packet_authority = topology["conflict_packet_authority"]
    assert conflict_tx["transaction_class"] == "publish_conflict_materialize"
    assert conflict_tx["conflict_materialized"] is True
    assert conflict_tx["after_resolving_files_command"] == 'agent-up sync -m "<resolution summary>" --brief --json'
    assert packet_authority["conflict_authority"] == "rolling_live_head"
    assert packet_authority["continuation_eligibility"]["state"] == "eligible_after_file_resolution"
    assert topology["worker_raw_jj_guidance"] is False
    assert conflict_tx["python_post_rust_graph_recompute_count"] == 0
    assert conflict.receipt["agent_facing_command_budget"]["raw_jj_command_count"] == 0
    _assert_no_raw_jj_guidance(conflict.receipt)
