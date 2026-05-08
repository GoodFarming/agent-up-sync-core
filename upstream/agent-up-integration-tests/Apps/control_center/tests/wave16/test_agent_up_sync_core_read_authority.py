from __future__ import annotations

from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import shutil
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
    resolve_rust_sync_core_binary,
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


@lru_cache(maxsize=1)
def _rust_binary_jj_lib() -> Path:
    subprocess.run(
        [
            "cargo",
            "build",
            "-p",
            "agent-up-sync-core",
            "--no-default-features",
            "--features",
            "jj-lib-adapter",
        ],
        cwd=ROOT,
        check=True,
    )
    binary = ROOT / "target/debug/agent-up-sync-core"
    copied = ROOT / "target/debug/agent-up-sync-core-jj-lib-test"
    shutil.copy2(binary, copied)
    copied.chmod(0o755)
    assert copied.exists()
    return copied


def _init_jj_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["jj", "git", "init", str(repo)], cwd=ROOT, check=True, capture_output=True, text=True)
    return repo


def _init_conflicted_jj_repo(tmp_path: Path) -> Path:
    repo = _init_jj_repo(tmp_path)
    subprocess.run(["jj", "describe", "-m", "base"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["jj", "file", "track", "file.txt"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["jj", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True, text=True)
    base = subprocess.check_output(
        ["jj", "log", "--no-graph", "-r", "@-", "-T", "change_id.short()"],
        cwd=repo,
        text=True,
    ).strip()
    (repo / "file.txt").write_text("left\n", encoding="utf-8")
    subprocess.run(["jj", "commit", "-m", "left"], cwd=repo, check=True, capture_output=True, text=True)
    left = subprocess.check_output(
        ["jj", "log", "--no-graph", "-r", "@-", "-T", "change_id.short()"],
        cwd=repo,
        text=True,
    ).strip()
    subprocess.run(["jj", "new", base], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "file.txt").write_text("right\n", encoding="utf-8")
    subprocess.run(["jj", "commit", "-m", "right"], cwd=repo, check=True, capture_output=True, text=True)
    right = subprocess.check_output(
        ["jj", "log", "--no-graph", "-r", "@-", "-T", "change_id.short()"],
        cwd=repo,
        text=True,
    ).strip()
    subprocess.run(["jj", "new", left, right], cwd=repo, check=True, capture_output=True, text=True)
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


def _request(repo: Path, *, include_generated: bool = True, adapter_profile: str = "cli-jj") -> dict[str, Any]:
    return build_sync_core_read_authority_request(
        workspace_id="workspace::control-center::agent-up-worker.martin",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=_read_authority_context(include_generated=include_generated),
        adapter_profile=adapter_profile,
        transaction_id="sync-core-read-authority-martin-packet",
        correlation_id="corr-sync-core-read-authority-martin-packet",
        idempotency_key="idem-sync-core-read-authority-martin-packet",
    )


def _clean_request(repo: Path, *, adapter_profile: str) -> dict[str, Any]:
    context = _read_authority_context(include_generated=False)
    context["selected_workspace"]["workspace_sync_state"] = "fresh"
    context["live_target"]["live_root_state"] = "unchanged"
    context["source_state"]["authored_state"] = "clean"
    context["source_state"]["source_provenance_state"] = "none_or_clean"
    context.pop("conflict_context", None)
    return build_sync_core_read_authority_request(
        workspace_id="workspace::control-center::agent-up-worker.martin",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=context,
        adapter_profile=adapter_profile,
        transaction_id=f"sync-core-read-authority-clean-{adapter_profile}",
        correlation_id=f"corr-sync-core-read-authority-clean-{adapter_profile}",
        idempotency_key=f"idem-sync-core-read-authority-clean-{adapter_profile}",
    )


def _state_request(
    repo: Path,
    *,
    adapter_profile: str,
    label: str,
    workspace_sync_state: str = "fresh",
    live_root_state: str = "unchanged",
    authored_state: str = "clean",
    source_provenance_state: str = "none_or_clean",
) -> dict[str, Any]:
    context = _read_authority_context(include_generated=False)
    context["selected_workspace"]["workspace_sync_state"] = workspace_sync_state
    context["live_target"]["live_root_state"] = live_root_state
    context["source_state"]["authored_state"] = authored_state
    context["source_state"]["source_provenance_state"] = source_provenance_state
    context.pop("conflict_context", None)
    return build_sync_core_read_authority_request(
        workspace_id="workspace::control-center::agent-up-worker.martin",
        workspace_path=str(repo),
        repo_path=str(repo),
        live_root_path=str(repo),
        sync_group_id="sync-control-center",
        python_context=context,
        adapter_profile=adapter_profile,
        transaction_id=f"sync-core-read-authority-{label}-{adapter_profile}",
        correlation_id=f"corr-sync-core-read-authority-{label}-{adapter_profile}",
        idempotency_key=f"idem-sync-core-read-authority-{label}-{adapter_profile}",
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


def test_jj_lib_adapter_reads_repo_snapshot_without_adapter_subprocesses(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    response = invoke_sync_core_once(
        _request(repo, adapter_profile="jj-lib"),
        runner=RustSyncCoreRunner(_rust_binary_jj_lib()),
    )
    adapter_identity = response["adapter_identity"]

    assert response["engine_mode_actual"] == "rust_read_authoritative"
    assert response["repo_facts"]["adapter_profile"] == "jj-lib"
    assert response["repo_facts"]["adapter_version"] == "jj-lib.v0.40.0-read-only"
    assert adapter_identity["adapter_profile"] == "jj-lib"
    assert adapter_identity["adapter_subprocess_count"] == 0
    assert adapter_identity["adapter_jj_command_count"] == 0
    assert adapter_identity["repo_snapshot_count"] == 1
    assert adapter_identity["compatibility"]["jj_lib_version"] == "0.40.0"
    assert response["mutation_plan"] == {}
    assert response["journal_record"] == {}


def test_jj_lib_read_authority_matches_cli_adapter_for_clean_and_conflict_packets(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    cli_runner = RustSyncCoreRunner(_rust_binary())
    jj_lib_runner = RustSyncCoreRunner(_rust_binary_jj_lib())

    for request_factory in (
        lambda adapter: _clean_request(repo, adapter_profile=adapter),
        lambda adapter: _request(repo, adapter_profile=adapter),
    ):
        cli_response = invoke_sync_core_once(request_factory("cli-jj"), runner=cli_runner)
        jj_lib_response = invoke_sync_core_once(request_factory("jj-lib"), runner=jj_lib_runner)
        for field in (
            "decision_class",
            "selected_workspace_state",
            "source_provenance_state",
            "live_root_state",
            "conflict_authority",
            "runtime_relevance",
        ):
            assert jj_lib_response[field] == cli_response[field]
        assert jj_lib_response["adapter_identity"]["adapter_subprocess_count"] == 0
        assert cli_response["adapter_identity"]["adapter_subprocess_count"] > 0


def test_jj_lib_parity_covers_refresh_no_local_already_published_and_head_advance(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    cli_runner = RustSyncCoreRunner(_rust_binary())
    jj_lib_runner = RustSyncCoreRunner(_rust_binary_jj_lib())
    cases = [
        {
            "label": "clean-refresh-probe",
            "workspace_sync_state": "refresh_pending",
            "live_root_state": "advanced",
            "authored_state": "clean",
            "source_provenance_state": "none_or_clean",
        },
        {
            "label": "already-published-no-local",
            "workspace_sync_state": "fresh",
            "live_root_state": "unchanged",
            "authored_state": "clean",
            "source_provenance_state": "published",
        },
        {
            "label": "head-advance-dirty-preflight",
            "workspace_sync_state": "refresh_pending",
            "live_root_state": "advanced",
            "authored_state": "prepared",
            "source_provenance_state": "prepared",
        },
    ]

    for case in cases:
        cli_response = invoke_sync_core_once(
            _state_request(repo, adapter_profile="cli-jj", **case),
            runner=cli_runner,
        )
        jj_lib_response = invoke_sync_core_once(
            _state_request(repo, adapter_profile="jj-lib", **case),
            runner=jj_lib_runner,
        )
        for field in (
            "decision_class",
            "selected_workspace_state",
            "source_provenance_state",
            "live_root_state",
            "conflict_authority",
            "runtime_relevance",
        ):
            assert jj_lib_response[field] == cli_response[field]
        assert jj_lib_response["adapter_identity"]["adapter_subprocess_count"] == 0
        assert jj_lib_response["fallback"]["python_fallback_available"] is True


def test_jj_lib_concurrent_readers_do_not_require_workspace_repair_or_raw_jj(tmp_path: Path) -> None:
    repo = _init_jj_repo(tmp_path)
    runner_path = _rust_binary_jj_lib()
    request = _clean_request(repo, adapter_profile="jj-lib")

    def invoke_once() -> dict[str, Any]:
        return invoke_sync_core_once(request, runner=RustSyncCoreRunner(runner_path))

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: invoke_once(), range(4)))

    for response in responses:
        assert response["engine_mode_actual"] == "rust_read_authoritative"
        assert response["adapter_identity"]["adapter_profile"] == "jj-lib"
        assert response["adapter_identity"]["adapter_subprocess_count"] == 0
        assert response["fallback"]["python_fallback_available"] is True
        assert "jj " not in str(response["next_agent_up_action"])


def test_preflight_probe_can_attach_jj_lib_read_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from Apps.control_center.backend.convergence.agent_up_sync_cli import _maybe_attach_preflight_read_authority

    repo = _init_jj_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_ADAPTER", "jj-lib")
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_RUST_BINARY", str(_rust_binary_jj_lib()))
    payload = {
        "receipt_schema_version": "v0.2",
        "outcome": "green",
        "state_category": "routine",
        "local_outcome": "clean",
        "workspace_freshness_state": "fresh",
        "rolling_advance_state": "unchanged",
        "runtime_cutover_required": False,
        "runtime_cutover_state": "already_current",
        "runtime_stage_content_current": True,
        "probe_mode": True,
        "sync_engine_mode": "preflight_probe",
        "sync_runtime_metrics": {},
        "unpublished_range": {"unpublished_commit_count": 0, "has_uncommitted_change": False},
    }

    updated = _maybe_attach_preflight_read_authority(payload, workspace_id="workspace::control-center::probe-test")
    metadata = updated["sync_core_read_authority"]

    assert updated["sync_engine_mode"] == "preflight_probe"
    assert metadata["engine_mode_actual"] == "rust_read_authoritative"
    assert metadata["parity_state"] == "matched"
    assert metadata["adapter_identity"]["adapter_profile"] == "jj-lib"
    assert metadata["adapter_identity"]["adapter_subprocess_count"] == 0
    assert metadata["adapter_identity"]["adapter_jj_command_count"] == 0
    assert metadata["adapter_identity"]["repo_snapshot_count"] == 1
    assert metadata["fallback"]["python_fallback_available"] is True
    assert metadata["binary_provenance"]["adapter_profile_requested"] == "jj-lib"
    assert metadata["binary_provenance"]["binary_sha256"]
    assert "jj " not in str(metadata)


def test_jj_lib_preflight_prefers_workspace_binary_over_stale_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from Apps.control_center.backend.convergence.agent_up_sync_cli import _maybe_attach_preflight_read_authority

    repo = _init_jj_repo(tmp_path)
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    shutil.copy2(_rust_binary(), path_bin / "agent-up-sync-core")
    (path_bin / "agent-up-sync-core").chmod(0o755)
    stale_release = repo / "target" / "release" / "agent-up-sync-core"
    stale_release.parent.mkdir(parents=True)
    shutil.copy2(_rust_binary(), stale_release)
    stale_release.chmod(0o755)
    workspace_binary = (
        repo
        / "Apps"
        / "control_center"
        / "rust"
        / "agent-up-sync-core"
        / "target"
        / "debug"
        / "agent-up-sync-core"
    )
    workspace_binary.parent.mkdir(parents=True)
    shutil.copy2(_rust_binary_jj_lib(), workspace_binary)
    workspace_binary.chmod(0o755)

    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", str(path_bin))
    monkeypatch.setenv("CONTROLCENTER_SYNC_CORE_ADAPTER", "jj-lib")
    monkeypatch.delenv("CONTROLCENTER_SYNC_CORE_RUST_BINARY", raising=False)

    resolved = resolve_rust_sync_core_binary(search_roots=[repo], adapter_profile="jj-lib")
    assert Path(str(resolved)).resolve() == workspace_binary.resolve()

    payload = {
        "receipt_schema_version": "v0.2",
        "outcome": "green",
        "state_category": "routine",
        "local_outcome": "clean",
        "workspace_freshness_state": "fresh",
        "rolling_advance_state": "unchanged",
        "runtime_cutover_required": False,
        "runtime_cutover_state": "already_current",
        "runtime_stage_content_current": True,
        "probe_mode": True,
        "sync_engine_mode": "preflight_probe",
        "sync_runtime_metrics": {},
        "unpublished_range": {"unpublished_commit_count": 0, "has_uncommitted_change": False},
    }

    updated = _maybe_attach_preflight_read_authority(payload, workspace_id="workspace::control-center::probe-test")
    metadata = updated["sync_core_read_authority"]

    assert metadata["engine_mode_actual"] == "rust_read_authoritative"
    assert metadata["parity_state"] == "matched"
    assert metadata["adapter_identity"]["adapter_profile"] == "jj-lib"
    assert metadata["adapter_identity"]["adapter_subprocess_count"] == 0
    assert metadata["adapter_identity"]["adapter_jj_command_count"] == 0
    assert metadata["fallback"]["python_fallback_available"] is True
    assert Path(metadata["binary_provenance"]["binary_realpath"]).resolve() == workspace_binary.resolve()
    assert metadata["binary_provenance"]["adapter_profile_requested"] == "jj-lib"
    assert metadata["binary_provenance"]["compile_profile"] == "debug"
    assert "jj " not in str(metadata)


def test_jj_lib_degraded_mismatch_states_return_typed_python_fallback(tmp_path: Path) -> None:
    runner = RustSyncCoreRunner(_rust_binary_jj_lib())
    unsupported = tmp_path / "not-a-jj-workspace"
    unsupported.mkdir()
    unsupported_response = invoke_sync_core_once(_clean_request(unsupported, adapter_profile="jj-lib"), runner=runner)

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt = _init_jj_repo(corrupt_root)
    shutil.rmtree(corrupt / ".jj" / "working_copy")
    corrupt_response = invoke_sync_core_once(_clean_request(corrupt, adapter_profile="jj-lib"), runner=runner)

    missing_op_root = tmp_path / "missing-op"
    missing_op_root.mkdir()
    missing_op = _init_jj_repo(missing_op_root)
    shutil.rmtree(missing_op / ".jj" / "repo" / "op_heads")
    missing_op_response = invoke_sync_core_once(_clean_request(missing_op, adapter_profile="jj-lib"), runner=runner)

    conflicted_root = tmp_path / "conflicted"
    conflicted_root.mkdir()
    conflicted = _init_conflicted_jj_repo(conflicted_root)
    conflict_response = invoke_sync_core_once(_clean_request(conflicted, adapter_profile="jj-lib"), runner=runner)

    reasons = {
        unsupported_response["python_fallback_reason"],
        corrupt_response["python_fallback_reason"],
        missing_op_response["python_fallback_reason"],
        conflict_response["python_fallback_reason"],
    }
    assert "jj_lib_workspace_load_failed" in reasons
    assert "jj_lib_missing_operation_state" in reasons
    assert "jj_lib_conflict_state_not_representable" in reasons
    for response in (unsupported_response, corrupt_response, missing_op_response, conflict_response):
        assert response["engine_mode_actual"] == "python_fallback"
        assert response["fallback"]["python_fallback_available"] is True
        assert response["adapter_identity"]["adapter_profile"] == "jj-lib"
        assert response["adapter_identity"]["adapter_subprocess_count"] == 0
        assert "jj " not in str(response["next_agent_up_action"])
        assert all(error["raw_jj_guidance"] is False for error in response["errors"])


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
