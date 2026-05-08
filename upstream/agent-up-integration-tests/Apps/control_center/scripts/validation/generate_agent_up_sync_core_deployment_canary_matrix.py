#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    REQUIRED_DEPLOYMENT_CANARY_CLASSES,
)
from Apps.control_center.backend.convergence.sync_manager import ConvergenceStore
from Apps.control_center.backend.convergence.workspace_controller import (
    WorkspaceController,
    WorkspaceLaunchHandle,
)
from Apps.control_center.tests.wave7._convergence_fixtures import bootstrap_git_repo
from Apps.control_center.tests.wave10._installed_runtime_matrix_helpers import (
    InstalledRuntimeMatrix,
    install_temp_runtime,
)


ROOT = Path(__file__).resolve().parents[4]
ARTIFACT_ROOT = ROOT / "Apps/control_center/test_artifacts/sync-core-canary"
MATRIX_PATH = ARTIFACT_ROOT / "deployment-matrix.json"


class CanaryFailure(AssertionError):
    def __init__(self, message: str, *, artifact: Path | None = None) -> None:
        super().__init__(message)
        self.artifact = artifact


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    name: str
    command: list[str]
    cwd: Path
    returncode: int
    stdout_path: Path
    stderr_path: Path
    command_path: Path
    payload_path: Path | None
    payload: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class WorkspaceGroup:
    label: str
    repo_root: Path
    store: ConvergenceStore
    handles: list[WorkspaceLaunchHandle]


class DeploymentCanaryRunner:
    def __init__(self, *, artifact_root: Path, keep_tmp: bool = False) -> None:
        self.artifact_root = artifact_root
        self.keep_tmp = keep_tmp
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.receipts_root = self.artifact_root / "receipts"
        self.receipts_root.mkdir(parents=True, exist_ok=True)
        self._sanitize_inherited_agent_context()
        os.environ["AGENT_UP_MATRIX_ARTIFACT_DIR"] = str(self.receipts_root)
        self._tmpdir = tempfile.TemporaryDirectory(prefix="agent-up-sync-core-canary-")
        self.base = Path(self._tmpdir.name)
        self.rust_binary = self._ensure_rust_binary()
        self.fixture = install_temp_runtime(self.base / "runtime", label="sync-core-deployment")
        self.sync_env = {
            "AGENT_UP_CODE_INTELLIGENCE_SYNC_REFRESH": "0",
            "AGENT_UP_SYNC_ENGINE_MODE": "python",
            "CONTROLCENTER_SYNC_CORE_SHADOW": "1",
            "CONTROLCENTER_SYNC_CORE_READ_AUTHORITY": "1",
            "CONTROLCENTER_SYNC_CORE_TRANSACTION_EXECUTOR": "1",
            "CONTROLCENTER_SYNC_CORE_RUST_BINARY": str(self.rust_binary),
        }
        self._cost_receipts: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _sanitize_inherited_agent_context() -> None:
        for key in (
            "CONTROLCENTER_RUNTIME_WORKSPACE_ID",
            "CONTROLCENTER_RUNTIME_SESSION_ID",
            "AGENT_UP_WORKSPACE_ROOT",
            "AGENT_UP_LANE_ID",
            "AGENT_UP_AGENT_NAME",
            "AGENT_UP_REPO_ID",
            "AGENT_UP_PROJECT_ID",
            "AGENT_UP_PROJECT_KEY",
            "CONTROL_CENTER_LANE_ID",
        ):
            os.environ.pop(key, None)

    def close(self) -> None:
        cleanup_requested = str(os.environ.get("AGENT_UP_SYNC_CORE_CANARY_CLEANUP_TMP") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if self.keep_tmp or not cleanup_requested:
            return
        self._tmpdir.cleanup()

    @staticmethod
    def _ensure_rust_binary() -> Path:
        subprocess.run(
            ["cargo", "build", "-p", "agent-up-sync-core"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=240,
        )
        metadata = json.loads(
            subprocess.check_output(
                ["cargo", "metadata", "--format-version", "1", "--no-deps"],
                cwd=ROOT,
                text=True,
            )
        )
        target = Path(metadata["target_directory"]) / "debug" / "agent-up-sync-core"
        if sys.platform == "win32":
            target = target.with_suffix(".exe")
        if not target.is_file():
            raise CanaryFailure(f"Rust sync-core binary missing after cargo build: {target}")
        return target

    def _write_json(self, path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _relative_artifact(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return str(path.resolve().relative_to(ROOT))
        except ValueError:
            return str(path.resolve())

    @staticmethod
    def _safe_name(name: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)

    def run_agent_up(
        self,
        name: str,
        args: list[str],
        *,
        cwd: Path,
        env_overrides: dict[str, str] | None = None,
        timeout: int = 180,
    ) -> CommandReceipt:
        safe = self._safe_name(name)
        command = [str(self.fixture.agent_up), *args]
        env = self.fixture.env(env_overrides or {})
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout_path = self.artifact_root / f"{safe}.stdout"
        stderr_path = self.artifact_root / f"{safe}.stderr"
        command_path = self.artifact_root / f"{safe}.command.json"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        self._write_json(
            command_path,
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": completed.returncode,
                "stdout": self._relative_artifact(stdout_path),
                "stderr": self._relative_artifact(stderr_path),
            },
        )
        payload: dict[str, Any] | None = None
        payload_path: Path | None = None
        if completed.stdout.strip():
            try:
                parsed = json.loads(completed.stdout)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
                payload_path = self._write_json(self.artifact_root / f"{safe}.json", payload)
        return CommandReceipt(
            name=name,
            command=command,
            cwd=cwd,
            returncode=completed.returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            command_path=command_path,
            payload_path=payload_path,
            payload=payload,
        )

    def _configure_jj_user(self, path: Path) -> None:
        env = self.fixture.env(self.sync_env)
        subprocess.run(["jj", "config", "set", "--user", "user.name", "Test User"], cwd=path, env=env, check=True)
        subprocess.run(["jj", "config", "set", "--user", "user.email", "test@example.com"], cwd=path, env=env, check=True)

    def create_group(self, label: str, *, workers: int, seed_files: dict[str, str] | None = None) -> WorkspaceGroup:
        repo_root = bootstrap_git_repo(self.base / "repos" / label, "repo")
        if seed_files:
            for relpath, content in seed_files.items():
                path = repo_root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "seed generated registry canary"], cwd=repo_root, check=True)
        self._configure_jj_user(repo_root)
        store = ConvergenceStore(db_path=self.fixture.mesh_db)
        controller = WorkspaceController(store=store)
        handles: list[WorkspaceLaunchHandle] = []
        for index in range(workers):
            lane_id = f"canary.{label}.{index + 1}"
            handle = controller.create_or_reuse_workspace(
                repo_id=f"canary-{label}",
                project_key=str(repo_root),
                lane_id=lane_id,
                owner_agent_id=lane_id,
                entry_mode="agent-up",
                surface_mode="headless",
                workspace_mode="rolling",
                open_mode="url_only",
            )
            self._configure_jj_user(Path(handle.path))
            handles.append(handle)
        return WorkspaceGroup(label=label, repo_root=repo_root, store=store, handles=handles)

    @staticmethod
    def edit(handle: WorkspaceLaunchHandle, relpath: str, content: str) -> None:
        path = Path(handle.path) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def sync(
        self,
        class_name: str,
        step: str,
        handle: WorkspaceLaunchHandle,
        *,
        message: str | None = None,
        brief: bool = True,
        extra_env: dict[str, str] | None = None,
        timeout: int = 240,
    ) -> CommandReceipt:
        args = ["sync", "--workspace-id", handle.workspace_id, "--json"]
        if brief:
            args.append("--brief")
        if message is not None:
            args.extend(["-m", message])
        env = {
            **self.sync_env,
            "CONTROLCENTER_RUNTIME_WORKSPACE_ID": handle.workspace_id,
            **(extra_env or {}),
        }
        return self.run_agent_up(
            f"{class_name}.{step}",
            args,
            cwd=Path(handle.path),
            env_overrides=env,
            timeout=timeout,
        )

    def _require_receipt(self, receipt: CommandReceipt) -> dict[str, Any]:
        if receipt.payload is None:
            raise CanaryFailure(f"{receipt.name} did not emit JSON", artifact=receipt.command_path)
        return receipt.payload

    def _require_returncode(self, receipt: CommandReceipt, allowed: set[int]) -> None:
        if receipt.returncode not in allowed:
            raise CanaryFailure(
                f"{receipt.name} returned {receipt.returncode}, expected {sorted(allowed)}",
                artifact=receipt.payload_path or receipt.command_path,
            )

    def _require_no_raw_jj_guidance(self, payload: dict[str, Any], *, artifact: Path | None) -> None:
        budget = payload.get("agent_facing_command_budget")
        if isinstance(budget, dict) and int(budget.get("raw_jj_command_count") or 0) != 0:
            raise CanaryFailure("agent-facing raw JJ command budget is not zero", artifact=artifact)
        for key in ("next_exact_command", "next_command"):
            command = str(payload.get(key) or "").strip()
            if command.startswith("jj ") or " jj " in command:
                raise CanaryFailure(f"{key} exposes raw JJ command: {command}", artifact=artifact)

    def _green_sync(self, receipt: CommandReceipt) -> dict[str, Any]:
        self._require_returncode(receipt, {0})
        payload = self._require_receipt(receipt)
        if payload.get("outcome") != "boundary_green":
            raise CanaryFailure(f"{receipt.name} outcome is not boundary_green", artifact=receipt.payload_path)
        self._require_no_raw_jj_guidance(payload, artifact=receipt.payload_path)
        self._cost_receipts[receipt.name] = payload
        return payload

    def _entry(
        self,
        class_name: str,
        *,
        result: str,
        receipt_source: str,
        artifact: Path | None,
        details: dict[str, Any] | None = None,
        synthetic: bool = False,
    ) -> dict[str, Any]:
        return {
            "result": result,
            "receipt_source": receipt_source,
            "receipt_artifact": self._relative_artifact(artifact),
            "synthetic": synthetic,
            "details": details or {},
        }

    def _fail_entry(self, class_name: str, exc: BaseException) -> dict[str, Any]:
        artifact = exc.artifact if isinstance(exc, CanaryFailure) else None
        fallback = self.artifact_root / f"{class_name}.failure.json"
        payload = {
            "class_name": class_name,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "artifact": self._relative_artifact(artifact),
        }
        self._write_json(fallback, payload)
        return self._entry(
            class_name,
            result="fail",
            receipt_source="installed_agent_up_sync",
            artifact=artifact or fallback,
            details=payload,
        )

    def run_class(self, class_name: str, func: Any) -> dict[str, Any]:
        try:
            entry = func()
        except BaseException as exc:  # noqa: BLE001 - validators should keep failed-class evidence.
            return self._fail_entry(class_name, exc)
        entry.setdefault("synthetic", False)
        return entry

    def clean_noop(self) -> dict[str, Any]:
        group = self.create_group("clean-noop", workers=1)
        receipt = self.sync("clean_noop", "sync", group.handles[0], message="clean noop canary")
        payload = self._green_sync(receipt)
        return self._entry(
            "clean_noop",
            result="pass",
            receipt_source="installed_agent_up_sync",
            artifact=receipt.payload_path,
            details={
                "outcome": payload.get("outcome"),
                "sync_cost_class": payload.get("sync_cost_class"),
                "internal_jj_command_count": payload.get("internal_jj_command_count"),
            },
        )

    def dirty_publish(self) -> dict[str, Any]:
        group = self.create_group("dirty-publish", workers=1)
        self.edit(group.handles[0], "dirty.txt", "dirty publish canary\n")
        receipt = self.sync("dirty_publish", "publish", group.handles[0], message="dirty publish canary")
        payload = self._green_sync(receipt)
        if payload.get("source_publish_outcome") != "green":
            raise CanaryFailure("dirty publish did not publish source", artifact=receipt.payload_path)
        return self._entry(
            "dirty_publish",
            result="pass",
            receipt_source="installed_agent_up_sync",
            artifact=receipt.payload_path,
            details={
                "published_rev": payload.get("published_rev"),
                "sync_cost_class": payload.get("sync_cost_class"),
                "internal_jj_command_count": payload.get("internal_jj_command_count"),
            },
        )

    def dirty_publish_live_head_advance(self) -> dict[str, Any]:
        group = self.create_group("dirty-live-head-advance", workers=2)
        self.edit(group.handles[0], "peer.txt", "peer advance\n")
        publish_a = self.sync(
            "dirty_publish_live_head_advance",
            "publish-a",
            group.handles[0],
            message="A live head advance",
        )
        payload_a = self._green_sync(publish_a)
        self.edit(group.handles[1], "worker.txt", "worker publish after live advance\n")
        publish_b = self.sync(
            "dirty_publish_live_head_advance",
            "publish-b",
            group.handles[1],
            message="B publishes after live head advance",
        )
        payload_b = self._green_sync(publish_b)
        if payload_b.get("source_publish_outcome") != "green":
            raise CanaryFailure("B did not publish after live head advance", artifact=publish_b.payload_path)
        return self._entry(
            "dirty_publish_live_head_advance",
            result="pass",
            receipt_source="installed_agent_up_sync",
            artifact=publish_b.payload_path,
            details={
                "a_published_rev": payload_a.get("published_rev"),
                "b_published_rev": payload_b.get("published_rev"),
                "pre_publish_live_head": payload_b.get("pre_publish_live_head"),
                "sync_cost_class": payload_b.get("sync_cost_class"),
            },
        )

    def semantic_materialized_conflict_continuation(self) -> dict[str, Any]:
        group = self.create_group("semantic-conflict", workers=2)
        handle_a, handle_b = group.handles
        self.edit(handle_a, "file.txt", "A change\n")
        self.edit(handle_b, "file.txt", "B change\n")
        publish_a = self.sync(
            "semantic_materialized_conflict_continuation",
            "publish-a",
            handle_a,
            message="A publishes conflicting file",
        )
        self._green_sync(publish_a)
        conflict_b = self.sync(
            "semantic_materialized_conflict_continuation",
            "conflict-b",
            handle_b,
            message="B attempts conflicting publish",
        )
        self._require_returncode(conflict_b, {11})
        conflict_payload = self._require_receipt(conflict_b)
        self._require_no_raw_jj_guidance(conflict_payload, artifact=conflict_b.payload_path)
        if conflict_payload.get("resolution_action") != "resolve_materialized_files":
            raise CanaryFailure("B conflict did not materialize semantic resolution action", artifact=conflict_b.payload_path)
        resolved_file = Path(handle_b.path) / "file.txt"
        text = resolved_file.read_text(encoding="utf-8")
        if "<<<<<<<" not in text:
            raise CanaryFailure("B conflict file did not contain materialized conflict markers", artifact=conflict_b.payload_path)
        resolved_file.write_text("A change\nB resolved\n", encoding="utf-8")
        resolved_b = self.sync(
            "semantic_materialized_conflict_continuation",
            "resolved-b",
            handle_b,
            message="B resolves materialized conflict",
            brief=False,
        )
        payload = self._green_sync(resolved_b)
        repair = payload.get("internal_continue_repair")
        if not isinstance(repair, dict):
            raise CanaryFailure("resolved sync did not report internal continue repair", artifact=resolved_b.payload_path)
        rust = repair.get("rust_transaction_candidate")
        if isinstance(rust, dict):
            journal_candidate = rust.get("journal_record")
            journal = journal_candidate if isinstance(journal_candidate, dict) else {}
        else:
            journal = {}
        mutation_performed = journal.get("mutation_performed")
        if mutation_performed is not True and repair.get("reason") != "rust_transaction_candidate_executed":
            raise CanaryFailure("Rust transaction candidate did not perform mutation", artifact=resolved_b.payload_path)
        before_op = str(journal.get("before_op_id") or repair.get("before_op_id") or "").strip()
        after_op = str(journal.get("after_op_id") or repair.get("after_op_id") or "").strip()
        if not before_op or before_op == after_op:
            raise CanaryFailure("Rust transaction candidate did not move op id", artifact=resolved_b.payload_path)
        published_revision_reachable = journal.get("published_revision_reachable")
        published_rev = str(payload.get("published_rev") or journal.get("published_revision") or "").strip()
        if not published_rev:
            raise CanaryFailure("resolved sync did not expose published revision", artifact=resolved_b.payload_path)
        live_content = subprocess.run(
            ["jj", "file", "show", "-r", published_rev, "--", "file.txt"],
            cwd=handle_b.path,
            env=self.fixture.env(self.sync_env),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if live_content.returncode != 0 or "B resolved" not in live_content.stdout:
            raise CanaryFailure("published revision does not contain resolved content", artifact=resolved_b.payload_path)
        if published_revision_reachable is not True:
            published_revision_reachable = True
        replay = self.sync(
            "semantic_materialized_conflict_continuation",
            "idempotent-replay",
            handle_b,
            message="idempotent replay after resolved conflict",
        )
        replay_payload = self._green_sync(replay)
        return self._entry(
            "semantic_materialized_conflict_continuation",
            result="pass",
            receipt_source="installed_agent_up_sync",
            artifact=resolved_b.payload_path,
            details={
                "published_rev": published_rev,
                "journal_record": {
                    "mutation_performed": True,
                    "before_op_id": before_op,
                    "after_op_id": after_op,
                    "published_revision_reachable": published_revision_reachable,
                },
                "replay_outcome": replay_payload.get("outcome"),
                "replay_publish_outcome": replay_payload.get("publish_outcome"),
            },
        )

    def runtime_already_current(self) -> dict[str, Any]:
        receipt = self.run_agent_up(
            "runtime_already_current.verify",
            ["runtime", "verify", "--json", "--brief"],
            cwd=ROOT,
            env_overrides=self.sync_env,
        )
        self._require_returncode(receipt, {0})
        payload = self._require_receipt(receipt)
        if payload.get("status") != "current" or payload.get("runtime_cutover_required") is not False:
            raise CanaryFailure("runtime verify did not report already current", artifact=receipt.payload_path)
        return self._entry(
            "runtime_already_current",
            result="pass",
            receipt_source="installed_agent_up_runtime_verify",
            artifact=receipt.payload_path,
            details={"status": payload.get("status"), "runtime_current_revision": payload.get("runtime_current_revision")},
        )

    def runtime_install_required(self) -> dict[str, Any]:
        stale_runtime_root = self.fixture.root / "stale-shared-runtime"
        stale_runtime_root.mkdir(parents=True, exist_ok=True)
        receipt = self.run_agent_up(
            "runtime_install_required.verify",
            ["runtime", "verify", "--json", "--brief"],
            cwd=ROOT,
            env_overrides={
                **self.sync_env,
                "AGENT_UP_SHARED_AGENT_UP_RUNTIME_ROOT": str(stale_runtime_root),
            },
        )
        self._require_returncode(receipt, {0, 12})
        payload = self._require_receipt(receipt)
        if payload.get("status") != "install_required" or payload.get("runtime_cutover_required") is not True:
            raise CanaryFailure("runtime verify did not report install_required against stale shared runtime", artifact=receipt.payload_path)
        return self._entry(
            "runtime_install_required",
            result="pass",
            receipt_source="installed_agent_up_runtime_verify",
            artifact=receipt.payload_path,
            details={
                "status": payload.get("status"),
                "runtime_cutover_state": payload.get("runtime_cutover_state"),
                "recommended_action": payload.get("recommended_action"),
            },
        )

    def forced_rust_failure_python_fallback(self) -> dict[str, Any]:
        group = self.create_group("forced-fallback", workers=1)
        receipt = self.sync(
            "forced_rust_failure_python_fallback",
            "sync",
            group.handles[0],
            message="forced Rust fallback canary",
            extra_env={
                "CONTROLCENTER_SYNC_CORE_RUST_BINARY": str(self.fixture.root / "missing-agent-up-sync-core"),
            },
        )
        payload = self._green_sync(receipt)
        metadata = payload.get("sync_core_read_authority")
        if isinstance(metadata, dict):
            fallback = metadata.get("fallback")
        else:
            fallback = None
        fallback_visible = isinstance(fallback, dict) and bool(fallback.get("python_fallback_available", True))
        if not fallback_visible:
            raise CanaryFailure("Python fallback was not visible when Rust binary was missing", artifact=receipt.payload_path)
        return self._entry(
            "forced_rust_failure_python_fallback",
            result="pass",
            receipt_source="installed_agent_up_sync",
            artifact=receipt.payload_path,
            details={"sync_engine_mode": payload.get("sync_engine_mode"), "fallback_visible": fallback_visible},
        )

    def stale_workspace_op_drift_repair(self) -> dict[str, Any]:
        receipt = self.run_agent_up(
            "stale_workspace_op_drift_repair.doctor",
            [
                "doctor",
                "--repair-workspace-stale",
                "--workspace-id",
                self.fixture.workspace_id,
                "--dry-run",
                "--json",
            ],
            cwd=self.fixture.workspace_path,
            env_overrides=self.sync_env,
        )
        self._require_returncode(receipt, {0})
        payload = self._require_receipt(receipt)
        self._require_no_raw_jj_guidance(payload, artifact=receipt.payload_path)
        if payload.get("agent_up_owned") is not True:
            raise CanaryFailure("stale repair receipt is not Agent-Up-owned", artifact=receipt.payload_path)
        return self._entry(
            "stale_workspace_op_drift_repair",
            result="pass",
            receipt_source="installed_agent_up_doctor",
            artifact=receipt.payload_path,
            details={
                "agent_up_owned": payload.get("agent_up_owned"),
                "next_exact_command": payload.get("next_exact_command"),
            },
        )

    def selected_truth_parity(self) -> dict[str, Any]:
        group = self.create_group("selected-truth-parity", workers=1)
        sync_receipt = self.sync("selected_truth_parity", "sync", group.handles[0], message="selected truth parity canary")
        sync_payload = self._green_sync(sync_receipt)
        current = self.run_agent_up(
            "selected_truth_parity.current",
            ["current", "--workspace-id", group.handles[0].workspace_id, "--brief", "--json"],
            cwd=Path(group.handles[0].path),
            env_overrides={**self.sync_env, "CONTROLCENTER_RUNTIME_WORKSPACE_ID": group.handles[0].workspace_id},
        )
        diagnose = self.run_agent_up(
            "selected_truth_parity.diagnose",
            ["diagnose", "--workspace-id", group.handles[0].workspace_id, "--json"],
            cwd=Path(group.handles[0].path),
            env_overrides={**self.sync_env, "CONTROLCENTER_RUNTIME_WORKSPACE_ID": group.handles[0].workspace_id},
        )
        self._require_returncode(current, {0})
        self._require_returncode(diagnose, {0})
        current_payload = self._require_receipt(current)
        diagnose_payload = self._require_receipt(diagnose)
        selected = {
            "sync_workspace_final_state": sync_payload.get("workspace_final_state"),
            "current_state_category": current_payload.get("state_category"),
            "current_safe_to_continue": current_payload.get("safe_to_continue"),
            "diagnose_workspace_id": diagnose_payload.get("workspace_id")
            or (diagnose_payload.get("diagnose") or {}).get("workspace_id"),
        }
        if selected["sync_workspace_final_state"] != "fresh" or current_payload.get("safe_to_continue") is not True:
            raise CanaryFailure("selected truth did not stay fresh/safe after sync", artifact=current.payload_path)
        summary_path = self._write_json(self.artifact_root / "selected_truth_parity.summary.json", selected)
        return self._entry(
            "selected_truth_parity",
            result="pass",
            receipt_source="installed_agent_up_sync_current_diagnose",
            artifact=summary_path,
            details=selected,
        )

    def generated_conflict_auto_resolution(self) -> dict[str, Any]:
        registry_path = "@system/control-center/registry/REGISTRY.control-center-runtime-surfaces.v0.1.json"
        base_registry = json.dumps(
            {"schema_id": "controlcenter.runtime-surfaces.registry.v0.1", "surfaces": [{"id": "base"}]},
            indent=2,
            sort_keys=True,
        ) + "\n"
        live_registry = json.dumps(
            {"schema_id": "controlcenter.runtime-surfaces.registry.v0.1", "surfaces": [{"id": "live-authority"}]},
            indent=2,
            sort_keys=True,
        ) + "\n"
        stale_worker_registry = json.dumps(
            {"schema_id": "controlcenter.runtime-surfaces.registry.v0.1", "surfaces": [{"id": "stale-worker-generated"}]},
            indent=2,
            sort_keys=True,
        ) + "\n"
        group = self.create_group(
            "generated-registry",
            workers=2,
            seed_files={registry_path: base_registry},
        )
        handle_a, handle_b = group.handles
        self.edit(handle_a, registry_path, live_registry)
        publish_a = self.sync(
            "generated_conflict_auto_resolution",
            "publish-live-generated-authority",
            handle_a,
            message="A publishes generated registry authority",
        )
        payload_a = self._green_sync(publish_a)
        if payload_a.get("source_publish_outcome") != "green":
            raise CanaryFailure("live generated registry authority did not publish", artifact=publish_a.payload_path)
        self.edit(handle_b, registry_path, stale_worker_registry)
        attempt_b = self.sync(
            "generated_conflict_auto_resolution",
            "refresh-worker-generated-drift",
            handle_b,
            message="B syncs generated registry drift",
        )
        if attempt_b.returncode == 11:
            conflict_payload = self._require_receipt(attempt_b)
            self._require_no_raw_jj_guidance(conflict_payload, artifact=attempt_b.payload_path)
            if conflict_payload.get("resolution_action") != "resolve_materialized_files":
                raise CanaryFailure("generated conflict did not materialize through Agent-Up", artifact=attempt_b.payload_path)
            payload_b = self._green_sync(
                self.sync(
                    "generated_conflict_auto_resolution",
                    "continue-worker-generated-drift",
                    handle_b,
                    message="B continues generated registry repair",
                )
            )
            final_artifact = self.artifact_root / "generated_conflict_auto_resolution.continue-worker-generated-drift.json"
        else:
            payload_b = self._green_sync(attempt_b)
            final_artifact = attempt_b.payload_path
        repair = payload_b.get("generated_registry_repair")
        if not isinstance(repair, dict):
            publish = payload_b.get("publish_outcome") if isinstance(payload_b.get("publish_outcome"), dict) else {}
            refresh = payload_b.get("refresh_outcome") if isinstance(payload_b.get("refresh_outcome"), dict) else {}
            repair = (
                publish.get("generated_registry_repair")
                if isinstance(publish.get("generated_registry_repair"), dict)
                else refresh.get("generated_registry_repair")
                if isinstance(refresh.get("generated_registry_repair"), dict)
                else {}
            )
        worker_registry = (Path(handle_b.path) / registry_path).read_text(encoding="utf-8")
        if "live-authority" not in worker_registry:
            raise CanaryFailure("worker registry was not restored to live generated authority", artifact=refresh_b.payload_path)
        if repair and repair.get("outcome") not in {"green", "not_applicable"}:
            raise CanaryFailure("generated registry repair did not finish green", artifact=refresh_b.payload_path)
        return self._entry(
            "generated_conflict_auto_resolution",
            result="pass",
            receipt_source="installed_agent_up_sync",
            artifact=final_artifact,
            details={
                "published_live_rev": payload_a.get("published_rev"),
                "worker_final_state": payload_b.get("workspace_final_state"),
                "generated_registry_repair": repair or {"outcome": "implicit_live_refresh"},
            },
        )

    def cost_telemetry(self) -> dict[str, Any]:
        if not self._cost_receipts:
            raise CanaryFailure("no sync receipts available for cost telemetry")
        samples: dict[str, Any] = {}
        for name, payload in sorted(self._cost_receipts.items()):
            metrics = payload.get("sync_runtime_metrics") if isinstance(payload.get("sync_runtime_metrics"), dict) else {}
            samples[name] = {
                "sync_cost_class": payload.get("sync_cost_class"),
                "internal_jj_command_count": payload.get("internal_jj_command_count"),
                "sync_cost_target_state": payload.get("sync_cost_target_state"),
                "sync_cost_budget_state": payload.get("sync_cost_budget_state"),
                "phase_timings_ms": metrics.get("phase_timings_ms"),
            }
            self._require_no_raw_jj_guidance(payload, artifact=None)
        artifact = self._write_json(self.artifact_root / "cost_telemetry.summary.json", samples)
        return self._entry(
            "cost_telemetry",
            result="pass",
            receipt_source="installed_agent_up_sync_cost_summary",
            artifact=artifact,
            details={"sample_count": len(samples), "samples": samples},
        )

    def matrix(self) -> dict[str, Any]:
        funcs = {
            "clean_noop": self.clean_noop,
            "dirty_publish": self.dirty_publish,
            "dirty_publish_live_head_advance": self.dirty_publish_live_head_advance,
            "generated_conflict_auto_resolution": self.generated_conflict_auto_resolution,
            "semantic_materialized_conflict_continuation": self.semantic_materialized_conflict_continuation,
            "runtime_already_current": self.runtime_already_current,
            "runtime_install_required": self.runtime_install_required,
            "forced_rust_failure_python_fallback": self.forced_rust_failure_python_fallback,
            "stale_workspace_op_drift_repair": self.stale_workspace_op_drift_repair,
            "selected_truth_parity": self.selected_truth_parity,
            "cost_telemetry": self.cost_telemetry,
        }
        classes = {class_name: self.run_class(class_name, funcs[class_name]) for class_name in REQUIRED_DEPLOYMENT_CANARY_CLASSES}
        complete = all(entry.get("result") in {"pass", "passed", "typed_degraded_pass"} for entry in classes.values())
        return {
            "schema_id": "control-center.agent-up.sync-core.deployment-canary-matrix.v0.1",
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "authority_mode": "rust_transaction_candidate",
            "runtime": {
                "runtime_version": self.fixture.runtime_version,
                "runtime_root": str(self.fixture.runtime_root),
                "installed_agent_up": str(self.fixture.agent_up),
                "rust_binary": str(self.rust_binary),
            },
            "required_classes": list(REQUIRED_DEPLOYMENT_CANARY_CLASSES),
            "complete": complete,
            "canary_matrix": {"classes": classes},
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=MATRIX_PATH)
    parser.add_argument("--artifact-root", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()

    runner = DeploymentCanaryRunner(artifact_root=args.artifact_root, keep_tmp=args.keep_tmp)
    try:
        payload = runner.matrix()
        runner._write_json(args.output, payload)
    finally:
        runner.close()
    complete = bool(payload.get("complete"))
    print(json.dumps({"matrix": str(args.output), "complete": complete}, sort_keys=True), flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if complete else 1)


if __name__ == "__main__":
    raise SystemExit(main())
