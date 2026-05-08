from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    attach_shadow_metadata_to_receipt,
    invoke_sync_core_once,
)
from Apps.control_center.backend.convergence.agent_up_sync_core_schema import (
    SyncCoreSchemaError,
    build_contract_response_example,
    validate_sync_core_request,
    validate_sync_core_response,
)


ROOT = Path(__file__).resolve().parents[4]
EXAMPLES_PATH = ROOT / "Apps/control_center/tests/sync_core_corpus/sync_core_contract_examples.json"


def _examples() -> dict[str, Any]:
    return json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))


def test_contract_schema_accepts_complete_examples() -> None:
    examples = _examples()
    request = validate_sync_core_request(examples["valid_request"])
    response = validate_sync_core_response(examples["valid_response"])

    assert request["engine_mode_requested"] == "rust_shadow"
    assert request["requested_operation"] == "classify"
    assert request["mutation_allowed"] is False
    assert response["engine_mode_actual"] == "rust_shadow"
    assert response["authority_state"] == "rust_shadow_observed"
    assert response["decision_class"] == "noop"
    assert response["selected_workspace_state"] == "clean"
    assert response["source_provenance_state"] == "none_or_clean"
    assert response["live_root_state"] == "unchanged"
    assert response["conflict_authority"] == "none"
    assert response["runtime_relevance"] == "none"
    assert response["fallback"]["python_fallback_available"] is True
    assert {step["axis"] for step in response["state_machine_trace"]} == {
        "workspace",
        "source",
        "target_live",
        "conflict",
        "mutation",
        "output",
    }


def test_one_call_python_bridge_invokes_kernel_runner_once() -> None:
    examples = _examples()
    calls: list[dict[str, Any]] = []

    def runner(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        return build_contract_response_example(request)

    response = invoke_sync_core_once(examples["valid_request"], runner=runner)

    assert len(calls) == 1
    assert calls[0]["transaction_id"] == "sync-core-transaction-example"
    assert response["graph_metrics"]["kernel_call_count"] == 1
    assert response["engine_mode_actual"] == "rust_shadow"


def test_receipt_shadow_metadata_preserves_python_authority() -> None:
    examples = _examples()
    response = invoke_sync_core_once(examples["valid_request"])
    receipt = {
        "outcome": "boundary_green",
        "sync_engine_mode": "python",
        "source_publish_outcome": "skipped",
        "safe_to_continue": True,
    }

    updated = attach_shadow_metadata_to_receipt(receipt, response)

    assert updated["outcome"] == receipt["outcome"]
    assert updated["sync_engine_mode"] == "python"
    assert updated["source_publish_outcome"] == "skipped"
    assert updated["safe_to_continue"] is True
    assert updated["sync_core_shadow"]["engine_mode_actual"] == "rust_shadow"
    assert updated["sync_core_shadow"]["authority_state"] == "rust_shadow_observed"
    assert updated["sync_core_shadow"]["decision_class"] == "noop"
    assert updated["sync_core_shadow"]["selected_workspace_state"] == "clean"
    assert updated["sync_core_shadow"]["fallback"]["python_fallback_available"] is True
    assert updated["sync_core_shadow"]["graph_metrics"]["kernel_call_count"] == 1


def test_negative_examples_fail_closed_for_missing_provenance_or_ambiguous_authority() -> None:
    examples = _examples()
    response = deepcopy(examples["valid_response"])
    response.pop("provenance")
    with pytest.raises(SyncCoreSchemaError, match="provenance"):
        validate_sync_core_response(response)

    response = deepcopy(examples["valid_response"])
    response["authority_state"] = "unknown"
    with pytest.raises(SyncCoreSchemaError, match="authority_state"):
        validate_sync_core_response(response)


def test_negative_examples_fail_closed_for_shadow_mutation_and_multiple_calls() -> None:
    examples = _examples()
    response = deepcopy(examples["valid_response"])
    response["mutation_plan"] = {"operation": "publish"}
    with pytest.raises(SyncCoreSchemaError, match="mutation plan"):
        validate_sync_core_response(response)

    response = deepcopy(examples["valid_response"])
    response["graph_metrics"]["kernel_call_count"] = 2
    with pytest.raises(SyncCoreSchemaError, match="kernel_call_count"):
        validate_sync_core_response(response)


def test_shadow_request_rejects_mutation_authority() -> None:
    examples = _examples()
    request = deepcopy(examples["valid_request"])
    request["mutation_allowed"] = True
    with pytest.raises(SyncCoreSchemaError, match="cannot allow mutation"):
        validate_sync_core_request(request)


def test_contract_schema_rejects_spec_field_omissions_and_invalid_state_web() -> None:
    examples = _examples()

    request = deepcopy(examples["valid_request"])
    request.pop("requested_operation")
    with pytest.raises(SyncCoreSchemaError, match="requested_operation"):
        validate_sync_core_request(request)

    response = deepcopy(examples["valid_response"])
    response.pop("selected_workspace_state")
    with pytest.raises(SyncCoreSchemaError, match="selected_workspace_state"):
        validate_sync_core_response(response)

    response = deepcopy(examples["valid_response"])
    response["state_machine_trace"][0]["state"] = "not_a_workspace_state"
    with pytest.raises(SyncCoreSchemaError, match="unsupported"):
        validate_sync_core_response(response)
