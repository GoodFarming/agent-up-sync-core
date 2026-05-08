from __future__ import annotations

from functools import lru_cache
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from Apps.control_center.backend.convergence import agent_up_sync_engine
from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    RustSyncCoreRunner,
    build_sync_core_transaction_candidate_request,
    invoke_sync_core_once,
    invoke_sync_core_with_fallback,
)
from Apps.control_center.backend.convergence.agent_up_sync_core_schema import (
    SyncCoreSchemaError,
    validate_sync_core_request,
)
from Apps.control_center.backend.convergence.agent_up_sync_engine import SyncEngineRequest, execute_sync_transaction
from Apps.control_center.backend.convergence.conflict_router import ConflictRouter
from Apps.control_center.backend.convergence.sync_manager import ConvergenceStore
from Apps.control_center.backend.convergence.workspace_controller import WorkspaceController
from Apps.control_center.tests.wave7._convergence_fixtures import bootstrap_git_repo


ROOT = Path(__file__).resolve().parents[4]
RAW_JJ_COMMAND_RE = re.compile(r"(^|[;&|]\s*)jj(\s|$)")


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


def _jj(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jj", "--repository", str(repo), *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _jj_text(repo: Path, *args: str) -> str:
    return _jj(repo, *args).stdout.strip()


def _operation_id(repo: Path) -> str:
    return _jj_text(repo, "op", "log", "--no-graph", "-n", "1").split()[0]


def _commit_id(repo: Path, rev: str) -> str:
    return _jj_text(repo, "log", "--no-graph", "-r", rev, "-T", "commit_id.short()")


def _seed_resolved_child(repo: Path, *, path: str = "src/router.py") -> tuple[str, str]:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base\n", encoding="utf-8")
    _jj(repo, "describe", "-m", "base")
    base_rev = _commit_id(repo, "@")
    _jj(repo, "new", "-m", "resolved child")
    target.write_text("resolved\n", encoding="utf-8")
    child_rev = _commit_id(repo, "@")
    return base_rev, child_rev


def _seed_real_ab_conflict_resolution(repo: Path, *, path: str = "src/router.py") -> dict[str, str]:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base\n", encoding="utf-8")
    _jj(repo, "describe", "-m", "base")
    base_rev = _commit_id(repo, "@")

    _jj(repo, "new", "-m", "worker A publishes")
    target.write_text("worker-a\n", encoding="utf-8")
    _jj(repo, "describe", "-m", "worker A publishes")
    a_rev = _commit_id(repo, "@")
    _jj(repo, "bookmark", "set", "--allow-backwards", "-r", "@", "rolling-control-center")

    _jj(repo, "new", "-r", base_rev, "-m", "worker B local edit")
    target.write_text("worker-b\n", encoding="utf-8")
    _jj(repo, "describe", "-m", "worker B local edit")
    b_original_rev = _commit_id(repo, "@")

    subprocess.run(
        ["jj", "--repository", str(repo), "rebase", "-s", "@", "-d", a_rev],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    conflicted_b_rev = _commit_id(repo, "@")
    assert "src/router.py" in _jj_text(repo, "resolve", "--list", "-r", "@", "--no-pager")

    _jj(repo, "new", "@", "-m", "worker B resolves materialized conflict")
    target.write_text("resolved-by-b\n", encoding="utf-8")
    resolution_child_rev = _commit_id(repo, "@")
    assert "src/router.py" in _jj_text(repo, "resolve", "--list", "-r", "@-", "--no-pager")
    return {
        "base_rev": base_rev,
        "a_rev": a_rev,
        "b_original_rev": b_original_rev,
        "conflicted_b_rev": conflicted_b_rev,
        "resolution_child_rev": resolution_child_rev,
    }


def _seed_managed_real_ab_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ConvergenceStore, Path, str, dict[str, str], dict[str, Any], dict[str, Any]]:
    monkeypatch.setenv("CONTROL_CENTER_MESH_STATE_DB_PATH", str(tmp_path / "mesh.db"))
    monkeypatch.setenv("CONTROLCENTER_WORKSPACE_STATE_DIR", str(tmp_path / "workspace-state"))
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_TRANSACTION_EXECUTOR", "1")
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_RUST_BINARY", str(_rust_binary()))
    repo = _init_jj_repo(tmp_path)
    revs = _seed_real_ab_conflict_resolution(repo)
    project_root = tmp_path / "project-root"
    project_root.mkdir()
    store = ConvergenceStore(db_path=tmp_path / "mesh.db")
    repo_id = "repo-a"
    sync_group_id = "sync-repo-a"
    workspace_id = "workspace::repo-a::agent-up-worker.b"
    store.ensure_repo_row(repo_id=repo_id, project_key=str(project_root))
    store.ensure_sync_group(
        sync_group_id=sync_group_id,
        repo_id=repo_id,
        group_head_ref="rolling-control-center",
        last_green_rev=revs["a_rev"],
    )
    store.upsert_workspace(
        {
            "workspace_id": workspace_id,
            "repo_id": repo_id,
            "project_key": str(project_root),
            "owner_agent_id": "agent-up-worker.b",
            "path": str(repo),
            "substrate": "jj",
            "workspace_mode": "rolling",
            "surface_mode": "headless",
            "workspace_role": "worker",
            "workspace_lifecycle": "disposable",
            "entry_mode": "agent-up",
            "sync_group_id": sync_group_id,
            "group_head_ref": "rolling-control-center",
            "publish_target": "rolling-control-center",
            "stale_state": "fresh",
            "conflict_state": "publish_conflict",
            "last_assimilated_rev": revs["base_rev"],
            "runtime_state": "unknown",
            "refresh_pending": False,
            "refresh_blocked_reason": "publish_conflict",
            "peer_rewrite_state": "idle",
        }
    )
    packet = ConflictRouter(store=store).create_conflict_packet(
        conflict_kind="publish",
        sync_group_id=sync_group_id,
        source_workspace_id=workspace_id,
        source_agent_id="agent-up-worker.b",
        source_revision=revs["conflicted_b_rev"],
        incumbent_workspace_id="workspace::repo-a::live",
        incumbent_revision=revs["a_rev"],
        affected_workspace_id=workspace_id,
        affected_agent_id="agent-up-worker.b",
        conflicted_paths=["src/router.py"],
        unpublished_root_rev=revs["base_rev"],
        unpublished_tip_rev=revs["conflicted_b_rev"],
        unpublished_commit_count=1,
        unpublished_commits=[
            {
                "rev": revs["conflicted_b_rev"],
                "description": "worker B local edit",
                "changed_paths": ["src/router.py"],
            }
        ],
    )
    context = {
        "workspace_id": workspace_id,
        "repo_id": repo_id,
        "lane_id": "agent-up-worker.b",
        "workspace_role": "worker",
        "workspace_lifecycle": "disposable",
        "workspace_path": str(repo),
        "last_assimilated_rev": revs["base_rev"],
        "runtime_cutover_required": False,
        "runtime_cutover_state": "already_current",
        "runtime_cutover_reason": "test_runtime_current",
        "runtime_next_install_command": None,
        "sync_core_transaction_journal_path": str(tmp_path / "journal/managed-ab-transaction.jsonl"),
        "authoritative_live_workspace": {"last_assimilated_rev": revs["a_rev"]},
        "publish_target": {
            "live_head": revs["a_rev"],
            "publish_bookmark": "rolling-control-center",
            "group_head_ref": "rolling-control-center",
        },
        "preflight_block": {"code": "unpublished_range_conflict", "reason": "unpublished_range_conflict"},
        "unpublished_range": {
            "current_parent_rev": revs["conflicted_b_rev"],
            "has_uncommitted_change": True,
            "has_local_commit": True,
            "unpublished_commit_count": 1,
            "unpublished_tip_rev": revs["conflicted_b_rev"],
            "unpublished_conflict_commit_count": 1,
            "unpublished_conflict_commit_ids": [revs["conflicted_b_rev"]],
            "first_unpublished_conflict_paths": ["src/router.py"],
            "working_copy_changed_paths": ["src/router.py"],
        },
    }
    return store, repo, workspace_id, revs, packet.as_dict(), context


def _context(
    *,
    conflict: bool = False,
    execute: bool = False,
    publish_bookmark: str | None = None,
    affected_paths: list[str] | None = None,
) -> dict[str, Any]:
    paths = affected_paths or ["src/router.py"]
    context: dict[str, Any] = {
        "selected_workspace": {
            "workspace_id": "workspace::control-center::agent-up-worker.b",
            "lane_id": "agent-up-worker.b",
            "workspace_role": "worker",
            "workspace_lifecycle": "disposable",
        },
        "sync_group": {
            "sync_group_id": "sync-control-center",
            "peer_debt_state": "advisory",
            "peer_heads": [
                {"lane_id": "agent-up-worker.a", "revision": "head-1001", "published": True},
                {"lane_id": "agent-up-worker.b", "revision": "head-1010x", "published": False},
            ],
        },
        "live_target": {
            "repo_id": "control-center",
            "live_rev": "head-1001",
            "live_root_state": "advanced",
        },
        "source_state": {
            "workspace_rev": "head-1010x",
            "source_rev": "head-1010x",
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
        "transaction_candidate": {
            "phases": ["prepare", "retry", "publish", "refresh", "fold"],
            "affected_paths": paths,
            "scenario_id": "multi_worker_ab_conflict" if conflict else "dirty_publish_no_conflict",
            "execute": execute,
        },
    }
    if publish_bookmark:
        context["transaction_candidate"]["publish_bookmark"] = publish_bookmark
    if conflict:
        context["conflict_context"] = {
            "conflict_kind": "publish",
            "base_rev": "head-1",
            "conflicted_paths": paths,
            "materialized_conflict_paths": paths,
            "semantic_paths": paths,
            "side_context": {
                "base": {"revision": "head-1"},
                "live": {"revision": "head-1001"},
                "worker": {"revision": "head-1010x"},
            },
        }
    return context


def _request(
    repo: Path,
    journal: Path,
    *,
    conflict: bool = False,
    execute: bool = False,
    publish_bookmark: str | None = None,
    affected_paths: list[str] | None = None,
) -> dict[str, Any]:
    request = build_sync_core_transaction_candidate_request(
        workspace_id="workspace::control-center::agent-up-worker.b",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=_context(
            conflict=conflict,
            execute=execute,
            publish_bookmark=publish_bookmark,
            affected_paths=affected_paths,
        ),
        requested_operation="continue_after_resolution" if execute else "sync_transaction",
        transaction_id="sync-core-transaction-candidate-test",
        correlation_id="corr-sync-core-transaction-candidate-test",
        idempotency_key="idem-sync-core-transaction-candidate-test",
        recovery_journal_path=str(journal),
    )
    if execute:
        request["feature_flags"]["rust_sync_core_transaction_executor"] = True
    return validate_sync_core_request(request)


def _run(
    repo: Path,
    journal: Path,
    *,
    conflict: bool = False,
    execute: bool = False,
    publish_bookmark: str | None = None,
    affected_paths: list[str] | None = None,
) -> dict[str, Any]:
    return invoke_sync_core_once(
        _request(
            repo,
            journal,
            conflict=conflict,
            execute=execute,
            publish_bookmark=publish_bookmark,
            affected_paths=affected_paths,
        ),
        runner=RustSyncCoreRunner(_rust_binary()),
    )


def _journal_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _command_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        strings: list[str] = []
        for item in value:
            strings.extend(_command_strings(item))
        return strings
    if isinstance(value, dict):
        strings: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            if key_text in {"sync_runtime_metrics", "brief_metrics", "graph_metrics", "adapter_identity"}:
                continue
            if key_text in {
                "command",
                "next_command",
                "next_exact_command",
                "continue_command",
                "remediation_command",
                "recommended_next_actions",
                "next_actions",
                "diagnostic_commands",
            }:
                strings.extend(_command_strings(item))
            elif isinstance(item, (dict, list)):
                strings.extend(_command_strings(item))
        return strings
    return []


def _assert_no_raw_jj_guidance(payload: dict[str, Any]) -> None:
    budget = payload.get("agent_facing_command_budget")
    if isinstance(budget, dict):
        assert int(budget.get("raw_jj_command_count") or 0) == 0
    leaked = [command for command in _command_strings(payload) if RAW_JJ_COMMAND_RE.search(command)]
    assert leaked == []


def test_transaction_candidate_plans_all_phases_and_journals(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    journal = tmp_path / "journal/transaction.jsonl"

    response = _run(repo, journal)

    assert response["engine_mode_actual"] == "rust_transaction_candidate"
    assert response["authority_state"] == "rust_transaction_candidate"
    assert response["decision_class"] == "clean_merge"
    assert response["mutation_plan"]["mutation_class"] == "full_sync_transaction_candidate"
    assert response["mutation_plan"]["safe_to_apply"] is True
    assert response["mutation_plan"]["policy"]["python_fallback_required"] is True
    assert response["journal_record"]["state"] == "journaled"
    assert response["journal_record"]["recovery_handle"]
    phases = {item["phase"] for item in response["mutation_plan"]["transaction_phases"]}
    assert {"prepare", "retry", "publish", "refresh", "fold"} <= phases
    assert _journal_lines(journal)[0]["operation_kind"] == "full_sync_transaction_candidate"


def test_multi_worker_replay_materializes_semantic_conflict(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    response = _run(repo, tmp_path / "journal/multi-worker.jsonl", conflict=True)

    assert response["decision_class"] == "materialized_conflict"
    assert response["selected_workspace_state"] == "conflicted"
    assert response["conflict_authority"] == "semantic_resolution_required"
    assert response["conflict_packet_candidate"]["conflicted_paths"] == ["src/router.py"]
    assert response["next_agent_up_action"]["command"] == "agent-up sync --probe --brief --json"
    assert response["fallback"]["python_fallback_available"] is True


def test_transaction_candidate_executes_disposable_fold_publish_and_idempotent_replay(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    journal = tmp_path / "journal/executor.jsonl"
    _seed_resolved_child(repo)
    before_parent = _commit_id(repo, "@-")
    before_op = _operation_id(repo)

    response = _run(
        repo,
        journal,
        conflict=True,
        execute=True,
        publish_bookmark="rolling-control-center",
    )

    assert response["journal_record"]["state"] == "applied"
    assert response["journal_record"]["mutation_performed"] is True
    assert response["journal_record"]["transaction_executor_requested"] is True
    assert response["journal_record"]["transaction_executor_enabled"] is True
    assert response["journal_record"]["before_op_id"] == before_op
    assert response["journal_record"]["after_op_id"] != before_op
    assert response["journal_record"]["published_revision"]
    assert response["journal_record"]["published_revision_reachable"] is True
    assert response["mutation_plan"]["published_revision_reachable"] is True
    assert response["telemetry"]["mutation_performed"] is True
    assert _commit_id(repo, "@-") != before_parent
    assert _commit_id(repo, "rolling-control-center") == _commit_id(repo, "@-")
    assert (repo / "src/router.py").read_text(encoding="utf-8") == "resolved\n"

    second = _run(
        repo,
        journal,
        conflict=True,
        execute=True,
        publish_bookmark="rolling-control-center",
    )
    assert second["journal_record"]["state"] == "recovered"
    assert second["journal_record"]["idempotency_replay"] is True
    assert second["journal_record"]["mutation_performed"] is False
    assert second["journal_record"]["published_revision_reachable"] is True
    assert _commit_id(repo, "rolling-control-center") == _commit_id(repo, "@-")


def test_agent_up_bridge_replay_runs_real_ab_conflict_transaction_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repo, workspace_id, revs, packet, context = _seed_managed_real_ab_conflict(tmp_path, monkeypatch)
    before_op = _operation_id(repo)
    checkpoint_calls = 0

    monkeypatch.setattr(agent_up_sync_engine, "managed_identity_guard_result", lambda *args, **kwargs: None)

    def refreshed_context(*args: Any, **kwargs: Any) -> dict[str, Any]:
        published = _commit_id(repo, "rolling-control-center")
        return {
            **context,
            "preflight_block": None,
            "blocked_reason": None,
            "workspace_sync_state": "fresh",
            "workspace_freshness_state": "fresh",
            "last_assimilated_rev": published,
            "authoritative_live_workspace": {"last_assimilated_rev": published},
            "publish_target": {
                "live_head": published,
                "publish_bookmark": "rolling-control-center",
                "group_head_ref": "rolling-control-center",
            },
            "unpublished_range": {
                **context["unpublished_range"],
                "has_uncommitted_change": False,
                "has_local_commit": False,
                "unpublished_commit_count": 0,
                "unpublished_conflict_commit_count": 0,
                "unpublished_conflict_commit_ids": [],
                "first_unpublished_conflict_paths": [],
                "working_copy_changed_paths": [],
            },
        }

    def checkpoint_publish(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        published = _commit_id(repo, "rolling-control-center")
        assert published == _commit_id(repo, "@-")
        assert (repo / "src/router.py").read_text(encoding="utf-8") == "resolved-by-b\n"
        store.update_sync_group("sync-repo-a", last_green_rev=published)
        store.update_workspace(
            workspace_id,
            conflict_state="clear",
            refresh_blocked_reason=None,
            last_assimilated_rev=published,
            stale_state="fresh",
            refresh_pending=False,
        )
        return (
            0,
            {
                "workspace_id": workspace_id,
                "publish_outcome": {
                    "outcome": "green",
                    "source_publish_outcome": "green",
                    "published_rev": published,
                    "unpublished_commit_count": 1,
                    "unpublished_commits": [
                        {
                            "rev": revs["conflicted_b_rev"],
                            "description": "worker B local edit",
                            "changed_paths": ["src/router.py"],
                        }
                    ],
                },
                "refresh_outcome": {"outcome": "green", "revision": published},
            },
        )

    monkeypatch.setattr(agent_up_sync_engine, "load_commit_preflight_payload", refreshed_context)
    monkeypatch.setattr(agent_up_sync_engine, "run_workspace_checkpoint", checkpoint_publish)

    result = execute_sync_transaction(
        SyncEngineRequest(
            workspace_id=workspace_id,
            commit_message="default agent-up sync",
            json_mode=True,
            brief_json=True,
            preflight_context=context,
        )
    )

    assert result.returncode == 0
    assert checkpoint_calls == 1
    assert _operation_id(repo) != before_op
    assert _commit_id(repo, "rolling-control-center") == _commit_id(repo, "@-")
    assert _commit_id(repo, "rolling-control-center") != revs["a_rev"]
    assert result.payload["next_exact_command"] is None
    assert "jj " not in json.dumps(result.payload.get("next_agent_up_action") or {})
    repair = result.payload["internal_continue_repair"]
    assert repair["state"] == "repaired"
    assert repair["repair_class"] == "semantic_materialized_conflict_continuation_fold"
    rust = repair["rust_transaction_candidate"]
    assert rust["engine_mode_actual"] == "rust_transaction_candidate"
    assert rust["journal_record"]["state"] == "applied"
    assert rust["journal_record"]["mutation_performed"] is True
    assert rust["journal_record"]["before_op_id"] == before_op
    assert rust["journal_record"]["after_op_id"] != before_op
    assert rust["journal_record"]["published_revision_reachable"] is True
    assert repair["fold_result"]["mutation_performed"] is True
    resolved_packet = next(
        row for row in store.list_conflict_packets(include_resolved=True) if row["conflict_id"] == packet["conflict_id"]
    )
    assert resolved_packet["resolution_state"] == "resolved"

    replay = agent_up_sync_engine._sync_core_transaction_candidate_for_materialized_fold(
        request=SyncEngineRequest(
            workspace_id=workspace_id,
            commit_message="default agent-up sync",
            json_mode=True,
            brief_json=True,
            preflight_context=context,
        ),
        workspace=store.workspace_row(workspace_id) or {},
        context_before=context,
        packet=packet,
        materialized_paths=["src/router.py"],
        controller=agent_up_sync_engine.JJRepoController(),
        live_anchor={"packet_live_rev": revs["a_rev"], "current_live_rev": _commit_id(repo, "rolling-control-center")},
    )
    assert replay["journal_record"]["state"] == "recovered"
    assert replay["journal_record"]["idempotency_replay"] is True
    assert replay["journal_record"]["mutation_performed"] is False
    assert replay["journal_record"]["published_revision_reachable"] is True


def test_managed_ab_sync_replay_resolves_conflict_and_publishes_without_raw_jj_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROL_CENTER_MESH_STATE_DB_PATH", str(tmp_path / "mesh.db"))
    monkeypatch.setenv("CONTROLCENTER_WORKSPACE_STATE_DIR", str(tmp_path / "workspace-state"))
    monkeypatch.setenv("AGENT_UP_CODE_INTELLIGENCE_SYNC_REFRESH", "0")
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_SHADOW", "0")
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_TRANSACTION_EXECUTOR", "1")
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_RUST_BINARY", str(_rust_binary()))

    repo_id = f"repo-{tmp_path.name}"
    repo_root = bootstrap_git_repo(tmp_path)
    store = ConvergenceStore(db_path=tmp_path / "mesh.db")
    controller = WorkspaceController(store=store)
    handle_a = controller.create_or_reuse_workspace(
        repo_id=repo_id,
        project_key=str(repo_root),
        lane_id="products.a",
        entry_mode="agent-up",
        surface_mode="headless",
        workspace_mode="rolling",
        open_mode="url_only",
    )
    handle_b = controller.create_or_reuse_workspace(
        repo_id=repo_id,
        project_key=str(repo_root),
        lane_id="products.b",
        entry_mode="agent-up",
        surface_mode="headless",
        workspace_mode="rolling",
        open_mode="url_only",
    )

    (Path(handle_a.path) / "file.txt").write_text("from-a\n", encoding="utf-8")
    (Path(handle_b.path) / "file.txt").write_text("from-b\n", encoding="utf-8")

    monkeypatch.chdir(handle_a.path)
    published_a = execute_sync_transaction(
        SyncEngineRequest(
            workspace_id=handle_a.workspace_id,
            commit_message="publish worker A",
            json_mode=True,
            brief_json=True,
        )
    )
    assert published_a.returncode == 0
    assert published_a.payload["source_publish_outcome"] == "green"
    assert published_a.payload["workspace_final_state"] == "fresh"
    _assert_no_raw_jj_guidance(published_a.payload)

    monkeypatch.chdir(handle_b.path)
    conflicted_b = execute_sync_transaction(
        SyncEngineRequest(
            workspace_id=handle_b.workspace_id,
            commit_message="publish worker B",
            json_mode=True,
            brief_json=True,
        )
    )
    assert conflicted_b.returncode == 11
    assert conflicted_b.payload["source_publish_outcome"] == "publish_conflict"
    assert conflicted_b.payload["continue_command"] == (
        f"agent-up sync --workspace-id {handle_b.workspace_id} -m \"<resolution summary>\" --brief --json"
    )
    assert conflicted_b.payload["next_exact_command"] is None
    _assert_no_raw_jj_guidance(conflicted_b.payload)

    packets = [
        packet
        for packet in store.list_conflict_packets(include_resolved=True)
        if packet["source_workspace_id"] == handle_b.workspace_id
    ]
    assert len(packets) == 1
    assert packets[0]["resolution_state"] == "open"
    assert packets[0]["resolution_action"] == "resolve_materialized_files"
    assert packets[0]["materialized_conflict_paths"] == ["file.txt"]
    assert packets[0]["diagnostic_commands"] == []
    assert "<<<<<<<" in (Path(handle_b.path) / "file.txt").read_text(encoding="utf-8")

    (Path(handle_b.path) / "file.txt").write_text("resolved-by-b\n", encoding="utf-8")
    before_resolved_op = _operation_id(Path(handle_b.path))
    resolved_b = execute_sync_transaction(
        SyncEngineRequest(
            workspace_id=handle_b.workspace_id,
            commit_message=None,
            json_mode=True,
            brief_json=True,
        )
    )

    assert resolved_b.returncode == 0
    assert resolved_b.payload["source_publish_outcome"] == "green"
    assert resolved_b.payload["workspace_final_state"] == "fresh"
    assert resolved_b.payload["next_exact_command"] is None
    assert resolved_b.payload["internal_continue_repair"]["state"] == "repaired"
    assert (
        resolved_b.payload["internal_continue_repair"]["repair_class"]
        == "semantic_materialized_conflict_continuation_fold"
    )
    rust = resolved_b.payload["internal_continue_repair"]["rust_transaction_candidate"]
    assert rust["engine_mode_actual"] == "rust_transaction_candidate"
    assert rust["journal_record"]["state"] == "applied"
    assert rust["journal_record"]["mutation_performed"] is True
    assert rust["journal_record"]["before_op_id"] == before_resolved_op
    assert rust["journal_record"]["after_op_id"] != before_resolved_op
    assert rust["journal_record"]["published_revision_reachable"] is True
    assert resolved_b.payload["internal_continue_repair"]["fold_result"]["mutation_performed"] is True
    _assert_no_raw_jj_guidance(resolved_b.payload)

    live_row = store.live_surface_workspace_row(repo_id=repo_id)
    assert live_row is not None
    published_rev = str(resolved_b.payload["published_rev"])
    assert str(live_row["last_assimilated_rev"]) == published_rev
    assert _commit_id(Path(live_row["path"]), published_rev) == published_rev
    assert (Path(live_row["path"]) / "file.txt").read_text(encoding="utf-8") == "resolved-by-b\n"

    resolved_packets = [
        packet
        for packet in store.list_conflict_packets(include_resolved=True)
        if packet["source_workspace_id"] == handle_b.workspace_id
    ]
    assert resolved_packets[0]["resolution_state"] == "resolved"


def test_crash_retry_replay_is_idempotent_by_journal_key(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    journal = tmp_path / "journal/crash-retry.jsonl"

    first = _run(repo, journal)
    second = _run(repo, journal)

    lines = _journal_lines(journal)
    assert len(lines) == 2
    assert first["journal_record"]["idempotency_key"] == "idem-sync-core-transaction-candidate-test"
    assert second["journal_record"]["idempotency_key"] == first["journal_record"]["idempotency_key"]
    assert {line["state"] for line in lines} == {"journaled"}


def test_python_fallback_is_receipt_visible(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    request = _request(repo, tmp_path / "journal/fallback.jsonl")

    def failing_runner(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("forced candidate failure")

    response = invoke_sync_core_with_fallback(request, runner=failing_runner)

    assert response["engine_mode_actual"] == "python_fallback"
    assert response["authority_state"] == "python_fallback"
    assert response["fallback"]["python_fallback_available"] is True
    assert response["python_fallback_reason"].startswith("rust_runner_failed:")
    assert response["next_agent_up_action"]["command"] == "agent-up sync --probe --brief --json"


def test_transaction_candidate_request_requires_mutation_allowed(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    request = _request(repo, tmp_path / "journal/schema.jsonl")
    assert request["engine_mode_requested"] == "rust_transaction_candidate"
    assert request["mutation_allowed"] is True

    bad_request = dict(request)
    bad_request["mutation_allowed"] = False
    with pytest.raises(SyncCoreSchemaError):
        validate_sync_core_request(bad_request)
