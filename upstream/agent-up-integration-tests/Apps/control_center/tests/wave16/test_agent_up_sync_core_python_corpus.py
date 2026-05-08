from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
CORPUS_PATH = ROOT / "Apps/control_center/tests/sync_core_corpus/sync_core_python_expected_decisions.json"

REQUIRED_FIXTURE_IDS = {
    "sync_clean_noop_no_local_commit",
    "sync_clean_refresh_after_root_advance",
    "sync_dirty_publish_no_conflict",
    "sync_head_advance_contention_retry",
    "sync_prepared_retry_no_local_commit_lost_visibility",
    "sync_already_published_no_local_commit",
    "sync_generated_registry_live_authority",
    "sync_generated_registry_worker_authored",
    "sync_generated_artifact_churn_ignored",
    "sync_semantic_conflict_ab_resolve_fold_publish",
    "sync_semantic_conflict_unrelated_edit_refused",
    "sync_semantic_conflict_markers_remaining",
    "sync_semantic_conflict_stale_packet",
    "sync_semantic_conflict_multi_commit_range",
    "sync_semantic_conflict_live_advances_again",
    "sync_semantic_conflict_idempotent_repeat",
    "sync_runtime_source_advanced_content_same",
    "sync_runtime_install_bounce_already_current",
    "sync_runtime_content_changed_install_required",
    "sync_selected_truth_refresh_pending_clean",
    "sync_op_sibling_drift",
    "sync_board_manifest_projection_conflict",
    "sync_command_budget_clean_noop",
    "sync_command_budget_dirty_publish",
    "sync_cross_repo_managed_noop",
    "sync_crash_retry_idempotency_journal",
}

COMMON_RECEIPT_INVARIANTS = {
    "no_raw_jj_worker_guidance",
    "one_agent_up_next_action_when_not_green",
    "source_live_runtime_outcomes_separated",
    "selected_workspace_truth_explicit",
    "python_fallback_available",
    "idempotency_rule_present",
    "cost_class_present",
}


def _corpus() -> dict[str, Any]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _fixtures() -> dict[str, dict[str, Any]]:
    fixtures = _corpus()["fixtures"]
    return {str(fixture["fixture_id"]): fixture for fixture in fixtures}


def test_python_corpus_manifest_schema_and_required_fixtures() -> None:
    corpus = _corpus()
    fixtures = _fixtures()

    assert corpus["schema_id"] == "control-center.agent-up.sync-core.python-expected-decisions.v0.1"
    assert corpus["schema_version"] == "v0.1"
    assert corpus["rust_required"] is False
    assert corpus["authority_mode"] == "python_authoritative"
    assert set(corpus["required_receipt_invariants"]) >= COMMON_RECEIPT_INVARIANTS
    assert set(fixtures) == REQUIRED_FIXTURE_IDS
    assert len(fixtures) == len(corpus["fixtures"])


def test_every_fixture_has_python_expected_decision_and_receipt_invariants() -> None:
    corpus = _corpus()
    decision_classes = set(corpus["decision_classes"])

    for fixture_id, fixture in _fixtures().items():
        decision = fixture["expected_decision"]
        assert decision["decision_class"] in decision_classes, fixture_id
        for key in (
            "expected_receipt_state",
            "source_effect",
            "live_effect",
            "runtime_effect",
            "selected_workspace_state",
            "next_agent_up_action",
        ):
            assert decision[key], f"{fixture_id} missing expected_decision.{key}"
        assert fixture["mutation_permission"]["state"] in {"disallowed", "allowed_internal", "fail_closed"}
        assert isinstance(fixture["mutation_permission"]["allowed_internal_actions"], list)
        assert fixture["proof"]["proof_level"], fixture_id
        assert fixture["proof"]["replay_status"], fixture_id
        assert fixture["receipt_invariants"], fixture_id


def test_cost_telemetry_exists_for_every_fixture_and_public_replay() -> None:
    cost_classes = set(_corpus()["cost_classes"])

    for fixture_id, fixture in _fixtures().items():
        cost = fixture["cost"]
        assert cost["cost_class"] in cost_classes, fixture_id
        assert cost["measurement_status"], fixture_id
        assert isinstance(cost["latency_budget_ms"], int) and cost["latency_budget_ms"] > 0
        assert "observed_jj_command_count" in cost
        if fixture["proof"]["proof_level"] in {"installed_runtime_replay", "live_receipt"}:
            assert cost["measurement_status"].startswith("measured_"), fixture_id


def test_idempotency_and_recovery_rules_prevent_source_loss() -> None:
    fixtures = _fixtures()

    for fixture_id, fixture in fixtures.items():
        idempotency = fixture["idempotency"]
        assert idempotency["repeat_behavior"], fixture_id
        assert idempotency["source_loss_allowed"] is False, fixture_id
        assert isinstance(idempotency["recovery_journal_required"], bool), fixture_id

    crash_retry = fixtures["sync_crash_retry_idempotency_journal"]
    assert crash_retry["idempotency"]["recovery_journal_required"] is True
    assert crash_retry["idempotency"]["repeat_behavior"] == "roll_forward_or_stop_with_recovery"


def test_negative_no_fixture_exposes_raw_jj_as_worker_next_command() -> None:
    for fixture_id, fixture in _fixtures().items():
        decision = fixture["expected_decision"]
        action = str(decision["next_agent_up_action"])
        assert not action.startswith("jj "), fixture_id
        assert "jj " not in action, fixture_id
        assert "no_raw_jj_worker_guidance" in fixture["receipt_invariants"], fixture_id


def test_python_corpus_runs_without_rust() -> None:
    corpus = _corpus()
    assert corpus["rust_required"] is False
    for fixture in corpus["fixtures"]:
        assert fixture.get("requires_rust") in (None, False), fixture["fixture_id"]
