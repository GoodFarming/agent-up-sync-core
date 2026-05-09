mod adapter;
mod error;
mod schema;

#[cfg(feature = "jj-lib-adapter")]
pub use adapter::JjLibAdapter;
pub use adapter::{CliJjAdapter, JjAdapter, StubJjAdapter};
pub use error::{StructuredError, SyncCoreError};
pub use schema::{
    contract_request_for_repo, Fallback, GraphMetrics, Provenance, RepoFacts, RepoRevisionFacts,
    StateTraceStep, SyncCoreRequest, SyncCoreResponse, API_VERSION, REQUEST_SCHEMA_ID,
    RESPONSE_SCHEMA_ID, SCHEMA_VERSION,
};

use serde_json::json;
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

const CLEAN_NOOP_LATENCY_BUDGET_MS: f64 = 250.0;
const DIRTY_PREFLIGHT_LATENCY_BUDGET_MS: f64 = 1000.0;
const CONFLICT_PACKET_LATENCY_BUDGET_MS: f64 = 2000.0;
const LARGE_DEGRADED_LATENCY_BUDGET_MS: f64 = 5000.0;
const DEFAULT_MEMORY_BUDGET_BYTES: u64 = 32 * 1024 * 1024;
const DEFAULT_OUTPUT_BUDGET_BYTES: u64 = 256 * 1024;

pub fn run_shadow_transaction(request: SyncCoreRequest) -> SyncCoreResponse {
    if request.adapter_profile == "stub" {
        run_shadow_transaction_with_adapter(request, &StubJjAdapter)
    } else if request.adapter_profile == "jj-lib" {
        #[cfg(feature = "jj-lib-adapter")]
        {
            run_shadow_transaction_with_adapter(request, &JjLibAdapter)
        }
        #[cfg(not(feature = "jj-lib-adapter"))]
        {
            let started = Instant::now();
            let error = SyncCoreError::new(
                "jj_lib_adapter_not_compiled",
                "adapter_profile=jj-lib requested but the jj-lib-adapter feature is not enabled",
                "jj-lib",
                request.repo_path.clone(),
                request.deadline_ms,
            );
            degraded_response(&request, started.elapsed().as_secs_f64() * 1000.0, error)
        }
    } else {
        run_shadow_transaction_with_adapter(request, &CliJjAdapter)
    }
}

pub fn run_shadow_transaction_with_adapter<A: JjAdapter + ?Sized>(
    request: SyncCoreRequest,
    adapter: &A,
) -> SyncCoreResponse {
    let started = Instant::now();
    if let Err(message) = request.validate() {
        let error = SyncCoreError::new(
            "request_validation_failed",
            message,
            request.adapter_profile.clone(),
            request.repo_path.clone(),
            request.deadline_ms,
        );
        return degraded_response(&request, started.elapsed().as_secs_f64() * 1000.0, error);
    }
    match adapter.read_repo_facts(&request) {
        Ok(facts) => success_response(&request, started.elapsed().as_secs_f64() * 1000.0, facts),
        Err(error) => degraded_response(&request, started.elapsed().as_secs_f64() * 1000.0, error),
    }
}

fn success_response(
    request: &SyncCoreRequest,
    latency_ms: f64,
    facts: RepoFacts,
) -> SyncCoreResponse {
    let engine_mode_actual = if request.engine_mode_requested == "rust_transaction_candidate"
        && request.mutation_allowed
    {
        "rust_transaction_candidate"
    } else if request.engine_mode_requested == "rust_mutation_authoritative"
        && request.mutation_allowed
    {
        "rust_mutation_authoritative"
    } else if request.engine_mode_requested == "rust_read_authoritative" {
        "rust_read_authoritative"
    } else {
        "rust_shadow"
    };
    let authority_state = match engine_mode_actual {
        "rust_transaction_candidate" => "rust_transaction_candidate",
        "rust_mutation_authoritative" => "rust_mutation_authoritative",
        "rust_read_authoritative" => "rust_read_authoritative",
        _ => "rust_shadow_observed",
    };
    let context_conflict_paths = conflict_paths_from_context(&request.python_context);
    let conflicted_paths = if facts.conflicted_paths.is_empty() {
        context_conflict_paths
    } else {
        facts.conflicted_paths.clone()
    };
    let has_conflicts = !conflicted_paths.is_empty();
    let authored_state = request
        .python_context
        .pointer("/source_state/authored_state")
        .and_then(|value| value.as_str())
        .unwrap_or("clean");
    let inferred_source_provenance_state = if facts.working_copy_dirty
        || facts.changed_path_count > 0
        || matches!(
            authored_state,
            "authored" | "dirty" | "local_commit" | "prepared" | "protected"
        ) {
        "authored"
    } else {
        "none_or_clean"
    };
    let source_provenance_state = request
        .python_context
        .pointer("/source_state/source_provenance_state")
        .and_then(|value| value.as_str())
        .filter(|value| {
            matches!(
                *value,
                "none_or_clean"
                    | "authored"
                    | "prepared"
                    | "protected"
                    | "published"
                    | "recoverable"
                    | "missing"
            )
        })
        .unwrap_or(inferred_source_provenance_state);
    let live_root_state = request
        .python_context
        .pointer("/live_target/live_root_state")
        .and_then(|value| value.as_str())
        .filter(|value| {
            matches!(
                *value,
                "unchanged" | "advanced" | "conflicted" | "unavailable"
            )
        })
        .unwrap_or("unchanged");
    let runtime_relevance = if request
        .python_context
        .pointer("/runtime_context/runtime_cutover_required")
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
    {
        "install_required"
    } else if request
        .python_context
        .pointer("/runtime_context/runtime_cutover_state")
        .and_then(|value| value.as_str())
        == Some("already_current")
    {
        "already_current"
    } else if request
        .python_context
        .pointer("/runtime_context/runtime_stage_content_current")
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
    {
        "runtime_stage_unchanged"
    } else {
        "none"
    };
    let decision_class = if has_conflicts {
        "materialized_conflict"
    } else if matches!(
        source_provenance_state,
        "authored" | "prepared" | "protected" | "published"
    ) {
        "clean_merge"
    } else {
        "noop"
    };
    let selected_workspace_state = if has_conflicts {
        "conflicted"
    } else if decision_class == "clean_merge"
        && matches!(
            source_provenance_state,
            "authored" | "prepared" | "protected"
        )
    {
        "dirty"
    } else {
        "clean"
    };
    let path_classifications = classify_conflict_paths(&conflicted_paths, &request.python_context);
    let semantic_count = path_classifications
        .iter()
        .filter(|item| item.surface_class == "semantic")
        .count();
    let generated_count = path_classifications
        .iter()
        .filter(|item| item.surface_class == "generated")
        .count();
    let conflict_authority = if has_conflicts && semantic_count > 0 && generated_count > 0 {
        "mixed_policy"
    } else if has_conflicts && generated_count > 0 {
        "generated_policy"
    } else if has_conflicts {
        "semantic_resolution_required"
    } else {
        "none"
    };
    let conflict_axis_state = if conflict_authority == "mixed_policy" {
        "mixed"
    } else if conflict_authority == "generated_policy" {
        "generated"
    } else if has_conflicts {
        "semantic"
    } else {
        "none"
    };
    let guarded_mutation = if engine_mode_actual == "rust_transaction_candidate" {
        build_transaction_candidate_decision(
            request,
            &facts,
            &conflicted_paths,
            source_provenance_state,
            live_root_state,
            conflict_authority,
            conflict_axis_state,
        )
    } else if engine_mode_actual == "rust_mutation_authoritative" {
        build_guarded_mutation_decision(
            request,
            &facts,
            &conflicted_paths,
            &path_classifications,
            source_provenance_state,
            live_root_state,
        )
    } else {
        GuardedMutationDecision::disallowed()
    };
    let decision_class_effective = guarded_mutation
        .decision_class_override
        .unwrap_or(decision_class);
    let selected_workspace_state_effective = guarded_mutation
        .selected_workspace_state_override
        .unwrap_or(selected_workspace_state);
    let conflict_authority_effective = guarded_mutation
        .conflict_authority_override
        .as_deref()
        .unwrap_or(conflict_authority);
    let conflict_axis_state_effective = guarded_mutation
        .conflict_axis_state_override
        .as_deref()
        .unwrap_or(conflict_axis_state);
    let output_axis_state = guarded_mutation.output_axis_state.unwrap_or({
        if decision_class_effective == "clean_merge" {
            "green_merge"
        } else {
            decision_class_effective
        }
    });
    let conflict_packet_candidate = if has_conflicts {
        build_conflict_packet_candidate(request, &facts, &conflicted_paths, &path_classifications)
    } else {
        json!({})
    };
    let graph_nodes_scanned = if facts.parent.is_some() { 2 } else { 1 };
    let conflict_count = conflicted_paths.len() as u64;
    let adapter_subprocess_count = adapter_subprocess_count(&facts.adapter_profile);
    let adapter_jj_command_count = adapter_jj_command_count(&facts.adapter_profile);
    let adapter_reason_code = match facts.adapter_profile.as_str() {
        "jj-lib" => "jj_lib_adapter_repo_snapshot",
        "stub" => "stub_adapter_repo_facts",
        _ => "cli_jj_adapter_repo_facts",
    };
    let reason_codes = vec![
        adapter_reason_code.to_string(),
        if engine_mode_actual == "rust_read_authoritative" {
            "rust_read_authority".to_string()
        } else if engine_mode_actual == "rust_transaction_candidate" {
            "rust_transaction_candidate".to_string()
        } else if engine_mode_actual == "rust_mutation_authoritative" {
            "rust_guarded_mutation_authority".to_string()
        } else {
            "python_authority_preserved".to_string()
        },
        format!("decision_class:{decision_class_effective}"),
    ];
    let inspected_fact_classes = vec![
        "repo_root".to_string(),
        "workspace_head".to_string(),
        "parent_head".to_string(),
        "operation_id".to_string(),
        "conflict_summary".to_string(),
        "working_copy_status".to_string(),
        "python_context.source_state".to_string(),
        "python_context.runtime_context".to_string(),
        "python_context.conflict_context".to_string(),
        "conflict_side_context".to_string(),
    ];
    let decision_drivers = vec![
        "mutation_disallowed".to_string(),
        "read_only_repo_facts".to_string(),
        "source_provenance_state".to_string(),
        "conflict_count".to_string(),
        "conflict_packet_side_context".to_string(),
        "generated_surface_classification".to_string(),
        if engine_mode_actual == "rust_transaction_candidate" {
            "transaction_candidate_state_machine".to_string()
        } else if engine_mode_actual == "rust_mutation_authoritative" {
            "guarded_mutation_policy".to_string()
        } else {
            "mutation_disallowed_by_mode".to_string()
        },
    ];
    let algorithmic_budget_class =
        algorithmic_budget_class(decision_class_effective, has_conflicts);
    let latency_budget_ms = latency_budget_ms(algorithmic_budget_class);
    let output_bytes_estimate =
        estimate_output_bytes(&conflict_packet_candidate, &facts, &path_classifications);
    let memory_bytes_estimate = estimate_memory_bytes(
        output_bytes_estimate,
        facts.parent.is_some(),
        paths_len(&conflicted_paths),
    );
    let latency_budget_state = budget_state(latency_ms, latency_budget_ms);
    let memory_budget_state = budget_state(
        memory_bytes_estimate as f64,
        DEFAULT_MEMORY_BUDGET_BYTES as f64,
    );
    let output_budget_state = budget_state(
        output_bytes_estimate as f64,
        DEFAULT_OUTPUT_BUDGET_BYTES as f64,
    );
    let performance_degraded_reason = if latency_budget_state == "pass"
        && memory_budget_state == "pass"
        && output_budget_state == "pass"
    {
        Value::Null
    } else {
        json!("budget_overrun")
    };
    let performance_budget = json!({
        "schema_id": "control-center.agent-up.sync-core.performance-budget.v0.1",
        "algorithmic_budget_class": algorithmic_budget_class,
        "latency_budget_ms": latency_budget_ms,
        "latency_budget_state": latency_budget_state,
        "memory_bytes_estimate": memory_bytes_estimate,
        "memory_budget_bytes": DEFAULT_MEMORY_BUDGET_BYTES,
        "memory_budget_state": memory_budget_state,
        "repo_lock_time_ms": 0.0,
        "repo_lock_budget_ms": 250.0,
        "repo_lock_budget_state": "pass",
        "output_bytes_estimate": output_bytes_estimate,
        "output_budget_bytes": DEFAULT_OUTPUT_BUDGET_BYTES,
        "output_budget_state": output_budget_state,
        "graph_nodes_scanned": graph_nodes_scanned,
        "conflict_count": conflict_count,
        "inspected_fact_count": inspected_fact_classes.len(),
        "decision_driver_count": decision_drivers.len(),
        "degraded_state_frequency_observed": 0,
        "one_kernel_call": true,
        "degraded_reason": performance_degraded_reason
    });
    SyncCoreResponse {
        schema_id: RESPONSE_SCHEMA_ID.to_string(),
        schema_version: SCHEMA_VERSION.to_string(),
        api_version: API_VERSION.to_string(),
        transaction_id: request.transaction_id.clone(),
        engine_mode_actual: engine_mode_actual.to_string(),
        authority_state: authority_state.to_string(),
        decision_class: decision_class_effective.to_string(),
        selected_workspace_state: selected_workspace_state_effective.to_string(),
        source_provenance_state: source_provenance_state.to_string(),
        live_root_state: live_root_state.to_string(),
        conflict_authority: conflict_authority_effective.to_string(),
        runtime_relevance: runtime_relevance.to_string(),
        provenance: Provenance {
            workspace_rev: facts.current.commit_id.clone(),
            source_rev: facts.current.commit_id.clone(),
            live_rev: request
                .python_context
                .pointer("/live_target/live_rev")
                .and_then(|value| value.as_str())
                .unwrap_or(&facts.current.commit_id)
                .to_string(),
            sync_group_id: request.sync_group_id.clone(),
        },
        conflict_packet_candidate,
        mutation_plan: guarded_mutation.mutation_plan,
        journal_record: guarded_mutation.journal_record,
        next_agent_up_action: guarded_mutation.next_agent_up_action,
        python_fallback_reason: None,
        parity_state: "not_compared".to_string(),
        latency_ms,
        graph_metrics: GraphMetrics {
            kernel_call_count: 1,
            repo_lock_time_ms: 0.0,
            graph_nodes_scanned,
            conflict_count,
        },
        degraded_reason: "not_applicable".to_string(),
        decision_confidence: 0.92,
        reason_codes,
        inspected_fact_classes,
        decision_drivers,
        feedback_observation: json!({
            "state": "pending_next_sync",
            "expected_next_observation": "python_authoritative_outcome_compared"
        }),
        state_machine_trace: state_trace(
            selected_workspace_state_effective,
            source_provenance_state,
            live_root_state,
            conflict_axis_state_effective,
            guarded_mutation.mutation_axis_state,
            output_axis_state,
        ),
        adapter_identity: json!({
            "adapter_profile": facts.adapter_profile,
            "adapter_version": facts.adapter_version,
            "adapter_subprocess_count": adapter_subprocess_count,
            "adapter_jj_command_count": adapter_jj_command_count,
            "repo_snapshot_count": 1,
            "working_copy_dirty": facts.working_copy_dirty,
            "changed_path_count": facts.changed_path_count,
            "compatibility": adapter_compatibility(&facts.adapter_profile),
            "jj_internal_schema_exposed": false
        }),
        fallback: Fallback {
            python_fallback_available: true,
            fallback_reason: None,
            fallback_command: "agent-up sync --probe --brief --json".to_string(),
        },
        telemetry: json!({
            "kernel_call_count": 1,
            "latency_ms": latency_ms,
            "repo_lock_time_ms": 0.0,
            "adapter_subprocess_count": adapter_subprocess_count,
            "adapter_jj_command_count": adapter_jj_command_count,
            "repo_snapshot_count": 1,
            "working_copy_dirty": facts.working_copy_dirty,
            "changed_path_count": facts.changed_path_count,
            "mutation_performed": guarded_mutation.mutation_performed,
            "performance_budget": performance_budget
        }),
        degraded: json!({"state": false, "reason": null}),
        errors: Vec::new(),
        repo_facts: Some(facts),
    }
}

fn adapter_subprocess_count(adapter_profile: &str) -> u64 {
    match adapter_profile {
        "jj-lib" | "stub" => 0,
        _ => 6,
    }
}

fn adapter_jj_command_count(adapter_profile: &str) -> u64 {
    match adapter_profile {
        "jj-lib" | "stub" => 0,
        _ => 6,
    }
}

fn adapter_version_for_profile(adapter_profile: &str) -> &'static str {
    match adapter_profile {
        "jj-lib" => "jj-lib.v0.40.0-read-only",
        "stub" => "stub.v0.1",
        _ => "jj-cli.v0.40-compatible",
    }
}

fn adapter_compatibility(adapter_profile: &str) -> Value {
    match adapter_profile {
        "jj-lib" => json!({
            "jj_lib_version": "0.40.0",
            "jj_cli_compatibility": "0.40.0",
            "repo_store": ["git", "simple"],
            "op_store": "simple_op_store",
            "op_heads": "simple_op_heads_store",
            "index": "default",
            "msrv": "1.89",
            "license": "Apache-2.0",
            "platform_support": ["linux", "macos", "windows"],
            "mismatch_behavior": "typed_python_fallback"
        }),
        "stub" => json!({
            "mismatch_behavior": "test_stub_only"
        }),
        _ => json!({
            "jj_cli_compatibility": "0.40.0",
            "mismatch_behavior": "typed_python_fallback"
        }),
    }
}

fn paths_len(paths: &[String]) -> usize {
    paths.iter().map(|path| path.len()).sum()
}

fn algorithmic_budget_class(decision_class: &str, has_conflicts: bool) -> &'static str {
    if has_conflicts {
        "conflict_packet"
    } else if decision_class == "clean_merge" {
        "dirty_preflight"
    } else {
        "clean_noop"
    }
}

fn latency_budget_ms(class: &str) -> f64 {
    match class {
        "clean_noop" => CLEAN_NOOP_LATENCY_BUDGET_MS,
        "dirty_preflight" => DIRTY_PREFLIGHT_LATENCY_BUDGET_MS,
        "conflict_packet" => CONFLICT_PACKET_LATENCY_BUDGET_MS,
        _ => LARGE_DEGRADED_LATENCY_BUDGET_MS,
    }
}

fn budget_state(value: f64, budget: f64) -> &'static str {
    if value <= budget {
        "pass"
    } else {
        "over_budget"
    }
}

fn estimate_output_bytes(
    packet: &Value,
    facts: &RepoFacts,
    classifications: &[PathClassification],
) -> u64 {
    serde_json::to_string(packet)
        .map(|value| value.len())
        .unwrap_or(0) as u64
        + facts.current.commit_id.len() as u64
        + facts.current.change_id.len() as u64
        + classifications
            .iter()
            .map(|item| item.path.len() as u64 + 32)
            .sum::<u64>()
}

fn estimate_memory_bytes(output_bytes_estimate: u64, has_parent: bool, paths_len: usize) -> u64 {
    64 * 1024 + output_bytes_estimate + paths_len as u64 + if has_parent { 1024 } else { 0 }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PathClassification {
    path: String,
    surface_class: &'static str,
    authority: &'static str,
}

#[derive(Debug, Clone)]
struct GuardedMutationDecision {
    mutation_plan: Value,
    journal_record: Value,
    next_agent_up_action: Value,
    mutation_axis_state: &'static str,
    output_axis_state: Option<&'static str>,
    decision_class_override: Option<&'static str>,
    selected_workspace_state_override: Option<&'static str>,
    conflict_authority_override: Option<String>,
    conflict_axis_state_override: Option<String>,
    mutation_performed: bool,
}

#[derive(Debug, Clone)]
struct TransactionExecution {
    state: String,
    blocked_reason: Option<String>,
    mutation_performed: bool,
    transaction_class: String,
    before_op_id: String,
    after_op_id: String,
    published_revision: Option<String>,
    published_revision_reachable: bool,
    conflict_materialized: bool,
    materialized_conflicted_paths: Vec<String>,
    executed_phases: Vec<String>,
    idempotency_replay: bool,
    details: Value,
}

impl TransactionExecution {
    fn planner_only(op_id: &str, transaction_class: String) -> Self {
        Self {
            state: "journaled".to_string(),
            blocked_reason: None,
            mutation_performed: false,
            transaction_class,
            before_op_id: op_id.to_string(),
            after_op_id: op_id.to_string(),
            published_revision: None,
            published_revision_reachable: false,
            conflict_materialized: false,
            materialized_conflicted_paths: Vec::new(),
            executed_phases: Vec::new(),
            idempotency_replay: false,
            details: json!({}),
        }
    }

    fn blocked(op_id: &str, transaction_class: &str, reason: String) -> Self {
        Self {
            state: "blocked".to_string(),
            blocked_reason: Some(reason),
            mutation_performed: false,
            transaction_class: transaction_class.to_string(),
            before_op_id: op_id.to_string(),
            after_op_id: op_id.to_string(),
            published_revision: None,
            published_revision_reachable: false,
            conflict_materialized: false,
            materialized_conflicted_paths: Vec::new(),
            executed_phases: Vec::new(),
            idempotency_replay: false,
            details: json!({}),
        }
    }
}

impl GuardedMutationDecision {
    fn disallowed() -> Self {
        Self {
            mutation_plan: json!({}),
            journal_record: json!({}),
            next_agent_up_action: json!({"action": "continue", "command": null}),
            mutation_axis_state: "disallowed",
            output_axis_state: None,
            decision_class_override: None,
            selected_workspace_state_override: None,
            conflict_authority_override: None,
            conflict_axis_state_override: None,
            mutation_performed: false,
        }
    }
}

fn context_array(context: &Value, pointer: &str) -> Vec<String> {
    context
        .pointer(pointer)
        .and_then(|value| value.as_array())
        .map(|values| {
            values
                .iter()
                .filter_map(|value| value.as_str().map(str::trim))
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn context_bool(context: &Value, pointer: &str) -> bool {
    context
        .pointer(pointer)
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
}

fn context_text<'a>(context: &'a Value, pointer: &str) -> Option<&'a str> {
    context
        .pointer(pointer)
        .and_then(|value| value.as_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn transaction_candidate_phases(request: &SyncCoreRequest) -> Vec<String> {
    let mut phases = context_array(&request.python_context, "/transaction_candidate/phases");
    for required in ["prepare", "retry", "publish", "refresh", "fold"] {
        if !phases.iter().any(|phase| phase == required) {
            phases.push(required.to_string());
        }
    }
    phases
}

fn transaction_candidate_class(request: &SyncCoreRequest) -> String {
    context_text(
        &request.python_context,
        "/transaction_candidate/transaction_class",
    )
    .or_else(|| {
        context_text(
            &request.python_context,
            "/transaction_candidate/operation_kind",
        )
    })
    .or_else(|| {
        context_text(
            &request.python_context,
            "/transaction_candidate/scenario_id",
        )
    })
    .map(str::to_string)
    .unwrap_or_else(|| "full_sync_transaction_candidate".to_string())
}

fn build_transaction_candidate_decision(
    request: &SyncCoreRequest,
    facts: &RepoFacts,
    conflicted_paths: &[String],
    source_provenance_state: &str,
    live_root_state: &str,
    conflict_authority: &str,
    conflict_axis_state: &str,
) -> GuardedMutationDecision {
    let feature_enabled = context_bool(
        &request.feature_flags,
        "/rust_sync_core_transaction_candidate",
    );
    let phases = transaction_candidate_phases(request);
    let affected_paths = if conflicted_paths.is_empty() {
        context_array(
            &request.python_context,
            "/transaction_candidate/affected_paths",
        )
    } else {
        conflicted_paths.to_vec()
    };
    let recovery_handle = mutation_recovery_handle(request, facts);
    let source_rev = context_text(&request.python_context, "/source_state/source_rev")
        .unwrap_or(&facts.current.commit_id)
        .to_string();
    let live_rev = context_text(&request.python_context, "/live_target/live_rev")
        .unwrap_or(&facts.current.commit_id)
        .to_string();
    let transaction_class = transaction_candidate_class(request);
    let prepared_rev = context_text(&request.python_context, "/source_state/prepared_rev")
        .or_else(|| context_text(&request.python_context, "/source_state/prepared_revision"))
        .unwrap_or(&facts.current.commit_id)
        .to_string();
    let executor_requested =
        context_bool(&request.python_context, "/transaction_candidate/execute")
            || request.requested_operation == "continue_after_resolution";
    let executor_enabled = context_bool(
        &request.feature_flags,
        "/rust_sync_core_transaction_executor",
    );
    let mut blocked_reason = None;
    if !feature_enabled {
        blocked_reason = Some("transaction_candidate_feature_flag_disabled".to_string());
    } else if !request.mutation_allowed {
        blocked_reason = Some("transaction_candidate_mutation_not_allowed".to_string());
    } else if executor_requested && !executor_enabled {
        blocked_reason = Some("transaction_executor_feature_flag_disabled".to_string());
    }
    let safe_to_apply_before_execution = blocked_reason.is_none();
    let mut execution =
        TransactionExecution::planner_only(&facts.operation_id, transaction_class.clone());
    if executor_requested && safe_to_apply_before_execution {
        let before_journal = json!({
            "schema_id": "control-center.agent-up.sync-core.transaction-journal.v0.1",
            "transaction_id": request.transaction_id.clone(),
            "journal_id": format!("journal-{}-before", request.idempotency_key),
            "operation_kind": "full_sync_transaction_candidate",
            "transaction_class": transaction_class.clone(),
            "state": "before_mutation",
            "blocked_reason": null,
            "workspace_id": request.workspace_id.clone(),
            "sync_group_id": request.sync_group_id.clone(),
            "source_revision": source_rev.clone(),
            "base_live_revision": live_rev.clone(),
            "prepared_revision": prepared_rev.clone(),
            "working_copy_child_revision": facts.current.commit_id.clone(),
            "affected_paths": affected_paths.clone(),
            "transaction_phases": phases.iter().map(|phase| json!({
                "phase": phase,
                "state": "pending",
                "owner": "rust_transaction_candidate",
                "journaled": true,
                "rollback": "python_fallback"
            })).collect::<Vec<Value>>(),
            "before_op_id": facts.operation_id.clone(),
            "after_op_id": facts.operation_id.clone(),
            "recovery_handle": recovery_handle.clone(),
            "recovery_action": "disable rust transaction candidate and run agent-up sync --probe --brief --json",
            "idempotency_key": request.idempotency_key.clone(),
            "mutation_performed": false,
            "live_root_state": live_root_state,
            "source_provenance_state": source_provenance_state,
            "python_fallback_available": true,
            "journal_stage": "before_mutation"
        });
        match write_guarded_journal(&request.recovery_journal_path, &before_journal) {
            Ok(()) => execution = execute_transaction_candidate(request, facts, &affected_paths),
            Err(reason) => {
                blocked_reason = Some(reason);
                execution = TransactionExecution::blocked(
                    &facts.operation_id,
                    &transaction_class,
                    "journal_write_failed".to_string(),
                );
            }
        }
    }
    if execution.blocked_reason.is_some() {
        blocked_reason = execution.blocked_reason.clone();
    }
    let safe_to_apply = blocked_reason.is_none();
    let state = if safe_to_apply && execution.mutation_performed {
        "applied"
    } else if safe_to_apply && execution.idempotency_replay {
        "recovered"
    } else if safe_to_apply {
        "journaled"
    } else {
        "blocked"
    };
    let phase_records: Vec<Value> = phases
        .iter()
        .map(|phase| {
            let executed = execution.executed_phases.iter().any(|value| value == phase);
            json!({
                "phase": phase,
                "state": if !safe_to_apply {
                    "blocked"
                } else if executed {
                    "applied"
                } else if execution.idempotency_replay {
                    "recovered"
                } else if executor_requested {
                    "planned_not_executed"
                } else {
                    "planned"
                },
                "owner": "rust_transaction_candidate",
                "journaled": safe_to_apply,
                "rollback": "python_fallback"
            })
        })
        .collect();
    let journal_record = json!({
        "schema_id": "control-center.agent-up.sync-core.transaction-journal.v0.1",
        "transaction_id": request.transaction_id.clone(),
        "journal_id": format!("journal-{}", request.idempotency_key),
        "operation_kind": "full_sync_transaction_candidate",
        "transaction_class": execution.transaction_class.clone(),
        "state": state,
        "blocked_reason": blocked_reason.clone(),
        "workspace_id": request.workspace_id.clone(),
        "sync_group_id": request.sync_group_id.clone(),
        "source_revision": source_rev,
        "base_live_revision": live_rev,
        "prepared_revision": prepared_rev,
        "working_copy_child_revision": facts.current.commit_id.clone(),
        "affected_paths": affected_paths,
        "materialized_conflicted_paths": execution.materialized_conflicted_paths.clone(),
        "transaction_phases": phase_records,
        "before_op_id": execution.before_op_id.clone(),
        "after_op_id": execution.after_op_id.clone(),
        "recovery_handle": recovery_handle.clone(),
        "recovery_action": "disable rust transaction candidate and run agent-up sync --probe --brief --json",
        "idempotency_key": request.idempotency_key.clone(),
        "mutation_performed": execution.mutation_performed,
        "conflict_materialized": execution.conflict_materialized,
        "transaction_executor_requested": executor_requested,
        "transaction_executor_enabled": executor_enabled,
        "idempotency_replay": execution.idempotency_replay,
        "execution_state": execution.state.clone(),
        "published_revision": execution.published_revision.clone(),
        "published_revision_reachable": execution.published_revision_reachable,
        "execution_details": execution.details.clone(),
        "live_root_state": live_root_state,
        "source_provenance_state": source_provenance_state,
        "python_fallback_available": true
    });
    let journal_write_result =
        write_guarded_journal(&request.recovery_journal_path, &journal_record);
    let (journal_record, safe_to_apply, blocked_reason) = match journal_write_result {
        Ok(()) => (journal_record, safe_to_apply, blocked_reason),
        Err(reason) => {
            let mut record = journal_record;
            record["state"] = json!("blocked");
            record["blocked_reason"] = json!(reason);
            (record, false, Some("journal_write_failed".to_string()))
        }
    };
    let phase_records = journal_record
        .get("transaction_phases")
        .cloned()
        .unwrap_or_else(|| json!([]));
    let mutation_plan = json!({
        "schema_id": "control-center.agent-up.sync-core.transaction-plan.v0.1",
        "plan_id": format!("plan-{}", request.idempotency_key),
        "mutation_class": "full_sync_transaction_candidate",
        "transaction_class": journal_record.get("transaction_class").cloned().unwrap_or(Value::Null),
        "safe_to_apply": safe_to_apply,
        "blocked_reason": blocked_reason,
        "journal_required": true,
        "journal_path": request.recovery_journal_path.clone(),
        "journal_id": journal_record.get("journal_id").cloned().unwrap_or(Value::Null),
        "source_protected": true,
        "recovery_handle": recovery_handle,
        "affected_paths": journal_record.get("affected_paths").cloned().unwrap_or_else(|| json!([])),
        "materialized_conflicted_paths": journal_record.get("materialized_conflicted_paths").cloned().unwrap_or_else(|| json!([])),
        "transaction_phases": phase_records,
        "execution_owner": "rust_transaction_candidate",
        "feature_flag": "rust_sync_core_transaction_candidate",
        "mutation_performed": execution.mutation_performed,
        "conflict_materialized": execution.conflict_materialized,
        "transaction_executor_requested": executor_requested,
        "transaction_executor_enabled": executor_enabled,
        "idempotency_replay": execution.idempotency_replay,
        "execution_state": execution.state.clone(),
        "published_revision": execution.published_revision.clone(),
        "published_revision_reachable": execution.published_revision_reachable,
        "idempotency_key": request.idempotency_key.clone(),
        "policy": {
            "python_fallback_required": true,
            "semantic_auto_merge": false,
            "default_activation_allowed": false,
            "journal_before_mutation": true,
            "faithful_disposable_executor_required_for_rollout": true
        }
    });
    GuardedMutationDecision {
        mutation_plan,
        journal_record,
        next_agent_up_action: if safe_to_apply && execution.conflict_materialized {
            json!({
                "action": "resolve_materialized_files",
                "command": "agent-up sync -m \"<resolution summary>\" --brief --json",
                "after_resolving_files_command": "agent-up sync -m \"<resolution summary>\" --brief --json",
                "continue_command": "agent-up sync -m \"<resolution summary>\" --brief --json",
                "worker_raw_jj_guidance": false
            })
        } else if safe_to_apply {
            json!({"action": "continue", "command": "agent-up sync --probe --brief --json", "worker_raw_jj_guidance": false})
        } else {
            json!({"action": "python_fallback", "command": "agent-up sync --probe --brief --json", "worker_raw_jj_guidance": false})
        },
        mutation_axis_state: if safe_to_apply && execution.mutation_performed {
            "applied"
        } else if safe_to_apply && execution.idempotency_replay {
            "recovered"
        } else if safe_to_apply {
            "journaled"
        } else {
            "blocked"
        },
        output_axis_state: Some(if !safe_to_apply {
            "blocked"
        } else if execution.conflict_materialized || !conflicted_paths.is_empty() {
            "materialized_conflict"
        } else {
            "green_merge"
        }),
        decision_class_override: Some(if !safe_to_apply {
            "blocked"
        } else if execution.conflict_materialized || !conflicted_paths.is_empty() {
            "materialized_conflict"
        } else {
            "clean_merge"
        }),
        selected_workspace_state_override: Some(if !safe_to_apply {
            "stale"
        } else if execution.conflict_materialized || !conflicted_paths.is_empty() {
            "conflicted"
        } else {
            "clean"
        }),
        conflict_authority_override: Some(if execution.conflict_materialized {
            "rolling_live_head".to_string()
        } else if conflicted_paths.is_empty() {
            "none".to_string()
        } else {
            conflict_authority.to_string()
        }),
        conflict_axis_state_override: Some(if execution.conflict_materialized {
            "semantic".to_string()
        } else if conflicted_paths.is_empty() {
            "none".to_string()
        } else {
            conflict_axis_state.to_string()
        }),
        mutation_performed: execution.mutation_performed,
    }
}

fn guarded_affected_paths(request: &SyncCoreRequest, fallback_paths: &[String]) -> Vec<String> {
    for pointer in [
        "/guarded_mutation/affected_paths",
        "/guarded_mutation/materialized_paths",
        "/conflict_context/materialized_conflict_paths",
        "/conflict_context/generated_artifact_paths",
        "/conflict_context/generated_surface_paths",
        "/conflict_context/conflicted_paths",
    ] {
        let paths = context_array(&request.python_context, pointer);
        if !paths.is_empty() {
            return paths;
        }
    }
    fallback_paths.to_vec()
}

fn guarded_mutation_kind(request: &SyncCoreRequest) -> String {
    if let Some(value) = context_text(
        &request.python_context,
        "/guarded_mutation/requested_mutation",
    ) {
        return value.to_string();
    }
    if request.requested_operation == "continue_after_resolution" {
        return "semantic_conflict_continuation_fold".to_string();
    }
    if !context_array(
        &request.python_context,
        "/conflict_context/generated_artifact_paths",
    )
    .is_empty()
    {
        return "generated_artifact_cleanup".to_string();
    }
    "blocked_unknown_guarded_mutation".to_string()
}

fn safe_relative_path(path: &str) -> bool {
    let path = Path::new(path);
    !path.is_absolute()
        && path
            .components()
            .all(|component| !matches!(component, std::path::Component::ParentDir))
}

fn path_is_generated(path: &str, classifications: &[PathClassification]) -> bool {
    classifications
        .iter()
        .any(|item| item.path == path && item.surface_class == "generated")
}

fn mutation_recovery_handle(request: &SyncCoreRequest, facts: &RepoFacts) -> String {
    context_text(&request.python_context, "/guarded_mutation/recovery_handle")
        .or_else(|| {
            context_text(
                &request.python_context,
                "/source_state/prepared_revision_recovery/handle",
            )
        })
        .or_else(|| context_text(&request.python_context, "/source_state/recovery_handle"))
        .map(str::to_string)
        .unwrap_or_else(|| format!("sync-core-recovery-{}", facts.current.commit_id))
}

fn write_guarded_journal(path: &str, record: &Value) -> Result<(), String> {
    let journal_path = PathBuf::from(path);
    if let Some(parent) = journal_path.parent() {
        fs::create_dir_all(parent).map_err(|exc| format!("journal_parent_create_failed:{exc}"))?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&journal_path)
        .map_err(|exc| format!("journal_open_failed:{exc}"))?;
    let line =
        serde_json::to_string(record).map_err(|exc| format!("journal_serialize_failed:{exc}"))?;
    writeln!(file, "{line}").map_err(|exc| format!("journal_write_failed:{exc}"))?;
    Ok(())
}

fn applied_journal_for_key(path: &str, idempotency_key: &str) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    text.lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|record| {
            record.get("idempotency_key").and_then(Value::as_str) == Some(idempotency_key)
                && record.get("state").and_then(Value::as_str) == Some("applied")
                && record
                    .get("mutation_performed")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
        })
}

fn contains_conflict_markers(
    workspace_path: &str,
    paths: &[String],
) -> Result<Option<String>, String> {
    let workspace = Path::new(workspace_path);
    for path in paths {
        if !safe_relative_path(path) {
            return Err(format!("unsafe_transaction_path:{path}"));
        }
        let target = workspace.join(path);
        if !target.exists() {
            continue;
        }
        let text = fs::read_to_string(&target)
            .map_err(|exc| format!("conflict_marker_read_failed:{path}:{exc}"))?;
        if text.contains("<<<<<<<")
            || text.contains("%%%%%%%")
            || text.contains("=======")
            || text.contains(">>>>>>>")
        {
            return Ok(Some(path.clone()));
        }
    }
    Ok(None)
}

fn run_jj_mutation(
    repo_path: &str,
    args: &[String],
    request: &SyncCoreRequest,
) -> Result<String, String> {
    let output = Command::new("jj")
        .arg("--repository")
        .arg(repo_path)
        .args(args)
        .current_dir(&request.workspace_path)
        .output()
        .map_err(|exc| format!("jj_spawn_failed:{exc}"))?;
    if output.status.success() {
        return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
    }
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    Err(format!(
        "jj_mutation_failed:{}:{}:{}",
        request.transaction_id,
        args.join(" "),
        stderr
    ))
}

fn set_publish_bookmark(
    request: &SyncCoreRequest,
    bookmark: &str,
    revision: &str,
) -> Result<Option<String>, String> {
    let bookmark = bookmark.trim();
    if bookmark.is_empty() {
        return Ok(None);
    }
    if bookmark.contains(char::is_whitespace) || bookmark.starts_with('-') {
        return Err(format!("unsafe_publish_bookmark:{bookmark}"));
    }
    let revision = revision.trim();
    if revision.is_empty() || revision.contains(char::is_whitespace) || revision.starts_with('-') {
        return Err(format!("unsafe_publish_revision:{revision}"));
    }
    run_jj_mutation(
        &request.repo_path,
        &[
            "bookmark".to_string(),
            "set".to_string(),
            "--allow-backwards".to_string(),
            "-r".to_string(),
            revision.to_string(),
            bookmark.to_string(),
        ],
        request,
    )?;
    let rev = CliJjAdapter::revision_facts(&request.repo_path, bookmark, request)
        .map_err(|exc| format!("publish_bookmark_read_failed:{}", exc.structured().code))?;
    Ok(Some(rev.commit_id))
}

fn execute_transaction_candidate(
    request: &SyncCoreRequest,
    facts: &RepoFacts,
    affected_paths: &[String],
) -> TransactionExecution {
    let transaction_class = transaction_candidate_class(request);
    if let Some(previous) =
        applied_journal_for_key(&request.recovery_journal_path, &request.idempotency_key)
    {
        return TransactionExecution {
            state: "recovered".to_string(),
            blocked_reason: None,
            mutation_performed: false,
            transaction_class: previous
                .get("transaction_class")
                .and_then(Value::as_str)
                .unwrap_or(&transaction_class)
                .to_string(),
            before_op_id: previous
                .get("before_op_id")
                .and_then(Value::as_str)
                .unwrap_or(&facts.operation_id)
                .to_string(),
            after_op_id: previous
                .get("after_op_id")
                .and_then(Value::as_str)
                .unwrap_or(&facts.operation_id)
                .to_string(),
            published_revision: previous
                .get("published_revision")
                .and_then(Value::as_str)
                .map(str::to_string),
            published_revision_reachable: previous
                .get("published_revision_reachable")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            conflict_materialized: previous
                .get("conflict_materialized")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            materialized_conflicted_paths: previous
                .get("materialized_conflicted_paths")
                .and_then(Value::as_array)
                .map(|paths| {
                    paths
                        .iter()
                        .filter_map(|value| value.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default(),
            executed_phases: vec!["idempotency_replay".to_string()],
            idempotency_replay: true,
            details: json!({"recovered_from_journal": previous.get("journal_id").cloned().unwrap_or(Value::Null)}),
        };
    }
    if matches!(
        transaction_class.as_str(),
        "dirty_publish"
            | "dirty_publish_no_conflict"
            | "dirty_publish_live_head_advance"
            | "publish_conflict_materialize"
            | "publish_retry"
            | "dirty_publish_or_publish_conflict"
    ) || matches!(
        request.requested_operation.as_str(),
        "prepare_publish" | "publish_retry"
    ) {
        return execute_publish_transaction_candidate(
            request,
            facts,
            affected_paths,
            transaction_class,
        );
    }
    if affected_paths.is_empty() {
        return TransactionExecution {
            state: "blocked".to_string(),
            blocked_reason: Some("missing_affected_paths".to_string()),
            mutation_performed: false,
            transaction_class,
            before_op_id: facts.operation_id.clone(),
            after_op_id: facts.operation_id.clone(),
            published_revision: None,
            published_revision_reachable: false,
            conflict_materialized: false,
            materialized_conflicted_paths: Vec::new(),
            executed_phases: Vec::new(),
            idempotency_replay: false,
            details: json!({}),
        };
    }
    for path in affected_paths {
        if !safe_relative_path(path) {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(format!("unsafe_transaction_path:{path}")),
                mutation_performed: false,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: facts.operation_id.clone(),
                published_revision: None,
                published_revision_reachable: false,
                conflict_materialized: false,
                materialized_conflicted_paths: Vec::new(),
                executed_phases: Vec::new(),
                idempotency_replay: false,
                details: json!({}),
            };
        }
    }
    match contains_conflict_markers(&request.workspace_path, affected_paths) {
        Ok(Some(path)) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(format!("conflict_markers_present:{path}")),
                mutation_performed: false,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: facts.operation_id.clone(),
                published_revision: None,
                published_revision_reachable: false,
                conflict_materialized: false,
                materialized_conflicted_paths: Vec::new(),
                executed_phases: Vec::new(),
                idempotency_replay: false,
                details: json!({}),
            };
        }
        Err(reason) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(reason),
                mutation_performed: false,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: facts.operation_id.clone(),
                published_revision: None,
                published_revision_reachable: false,
                conflict_materialized: false,
                materialized_conflicted_paths: Vec::new(),
                executed_phases: Vec::new(),
                idempotency_replay: false,
                details: json!({}),
            };
        }
        Ok(None) => {}
    }
    let mut squash_args = vec![
        "squash".to_string(),
        "--use-destination-message".to_string(),
    ];
    squash_args.extend(affected_paths.iter().cloned());
    if let Err(reason) = run_jj_mutation(&request.repo_path, &squash_args, request) {
        return TransactionExecution {
            state: "blocked".to_string(),
            blocked_reason: Some(reason),
            mutation_performed: false,
            transaction_class,
            before_op_id: facts.operation_id.clone(),
            after_op_id: facts.operation_id.clone(),
            published_revision: None,
            published_revision_reachable: false,
            conflict_materialized: false,
            materialized_conflicted_paths: Vec::new(),
            executed_phases: Vec::new(),
            idempotency_replay: false,
            details: json!({}),
        };
    }
    let bookmark = context_text(
        &request.python_context,
        "/transaction_candidate/publish_bookmark",
    )
    .unwrap_or("");
    let published_revision = match set_publish_bookmark(request, bookmark, "@-") {
        Ok(rev) => rev,
        Err(reason) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(reason),
                mutation_performed: true,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: facts.operation_id.clone(),
                published_revision: None,
                published_revision_reachable: false,
                conflict_materialized: true,
                materialized_conflicted_paths: affected_paths.to_vec(),
                executed_phases: vec!["fold".to_string()],
                idempotency_replay: false,
                details: json!({"partial_mutation": "fold_applied_bookmark_failed"}),
            };
        }
    };
    let after_facts = match CliJjAdapter.read_repo_facts(request) {
        Ok(value) => value,
        Err(exc) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(format!(
                    "post_mutation_fact_read_failed:{}",
                    exc.structured().code
                )),
                mutation_performed: true,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: facts.operation_id.clone(),
                published_revision,
                published_revision_reachable: false,
                conflict_materialized: true,
                materialized_conflicted_paths: affected_paths.to_vec(),
                executed_phases: vec!["fold".to_string(), "publish".to_string()],
                idempotency_replay: false,
                details: json!({"partial_mutation": "post_fact_read_failed"}),
            };
        }
    };
    let parent_rev = after_facts
        .parent
        .as_ref()
        .map(|parent| parent.commit_id.as_str())
        .unwrap_or("");
    let current_rev = after_facts.current.commit_id.as_str();
    let published_revision_reachable = published_revision
        .as_deref()
        .map(|rev| {
            !rev.is_empty()
                && (parent_rev.starts_with(rev)
                    || rev.starts_with(parent_rev)
                    || current_rev.starts_with(rev)
                    || rev.starts_with(current_rev))
        })
        .unwrap_or(
            parent_rev
                != facts
                    .parent
                    .as_ref()
                    .map(|parent| parent.commit_id.as_str())
                    .unwrap_or(""),
        );
    TransactionExecution {
        state: "applied".to_string(),
        blocked_reason: None,
        mutation_performed: true,
        transaction_class,
        before_op_id: facts.operation_id.clone(),
        after_op_id: after_facts.operation_id,
        published_revision,
        published_revision_reachable,
        conflict_materialized: true,
        materialized_conflicted_paths: affected_paths.to_vec(),
        executed_phases: vec![
            "fold".to_string(),
            "publish".to_string(),
            "refresh".to_string(),
        ],
        idempotency_replay: false,
        details: json!({
            "post_parent_revision": parent_rev,
            "working_copy_revision": after_facts.current.commit_id,
        }),
    }
}

fn execute_publish_transaction_candidate(
    request: &SyncCoreRequest,
    facts: &RepoFacts,
    affected_paths: &[String],
    transaction_class: String,
) -> TransactionExecution {
    let bookmark_advance_only = context_bool(
        &request.python_context,
        "/transaction_candidate/bookmark_advance_only",
    ) || context_bool(
        &request.python_context,
        "/transaction_candidate/prepared_bookmark_advance_only",
    );
    if affected_paths.is_empty() && !bookmark_advance_only {
        return blocked_publish_execution(
            facts,
            transaction_class,
            "missing_affected_paths".to_string(),
            false,
        );
    }
    for path in affected_paths {
        if !safe_relative_path(path) {
            return blocked_publish_execution(
                facts,
                transaction_class,
                format!("unsafe_transaction_path:{path}"),
                false,
            );
        }
    }
    let bookmark = context_text(
        &request.python_context,
        "/transaction_candidate/publish_bookmark",
    )
    .unwrap_or("");
    if bookmark.trim().is_empty() {
        return blocked_publish_execution(
            facts,
            transaction_class,
            "missing_publish_bookmark".to_string(),
            false,
        );
    }
    if bookmark_advance_only {
        let published_revision = match set_publish_bookmark(request, bookmark, "@-") {
            Ok(rev) => rev,
            Err(reason) => {
                return blocked_publish_execution(facts, transaction_class, reason, false);
            }
        };
        let after_publish = match CliJjAdapter.read_repo_facts(request) {
            Ok(value) => value,
            Err(exc) => {
                return TransactionExecution {
                    state: "blocked".to_string(),
                    blocked_reason: Some(format!(
                        "post_publish_fact_read_failed:{}",
                        exc.structured().code
                    )),
                    mutation_performed: true,
                    transaction_class,
                    before_op_id: facts.operation_id.clone(),
                    after_op_id: facts.operation_id.clone(),
                    published_revision,
                    published_revision_reachable: false,
                    conflict_materialized: false,
                    materialized_conflicted_paths: Vec::new(),
                    executed_phases: vec!["publish".to_string()],
                    idempotency_replay: false,
                    details: json!({"partial_mutation": "bookmark_advance_post_fact_read_failed"}),
                }
            }
        };
        let parent_rev = after_publish
            .parent
            .as_ref()
            .map(|parent| parent.commit_id.as_str())
            .unwrap_or("");
        let current_rev = after_publish.current.commit_id.as_str();
        let published_revision_reachable = published_revision
            .as_deref()
            .map(|rev| {
                !rev.is_empty()
                    && (parent_rev.starts_with(rev)
                        || rev.starts_with(parent_rev)
                        || current_rev.starts_with(rev)
                        || rev.starts_with(current_rev))
            })
            .unwrap_or(false);
        return TransactionExecution {
            state: "applied".to_string(),
            blocked_reason: None,
            mutation_performed: true,
            transaction_class,
            before_op_id: facts.operation_id.clone(),
            after_op_id: after_publish.operation_id,
            published_revision,
            published_revision_reachable,
            conflict_materialized: false,
            materialized_conflicted_paths: Vec::new(),
            executed_phases: vec![
                "prepare".to_string(),
                "publish".to_string(),
                "refresh".to_string(),
            ],
            idempotency_replay: false,
            details: json!({
                "transaction_outcome": "dirty_publish_bookmark_advanced",
                "bookmark_advance_only": true,
                "post_parent_revision": parent_rev,
                "working_copy_revision": after_publish.current.commit_id
            }),
        };
    }
    let live_rev = match context_text(&request.python_context, "/live_target/live_rev") {
        Some(value) if value != "unknown-live-rev" => value.to_string(),
        _ => {
            return blocked_publish_execution(
                facts,
                transaction_class,
                "missing_rolling_live_head".to_string(),
                false,
            )
        }
    };
    let source_revset = context_text(
        &request.python_context,
        "/transaction_candidate/source_revset",
    )
    .unwrap_or("@-")
    .to_string();
    if source_revset.contains(char::is_whitespace) || source_revset.starts_with('-') {
        return blocked_publish_execution(
            facts,
            transaction_class,
            format!("unsafe_source_revset:{source_revset}"),
            false,
        );
    }
    if let Err(reason) = run_jj_mutation(
        &request.repo_path,
        &[
            "rebase".to_string(),
            "-s".to_string(),
            source_revset.clone(),
            "-d".to_string(),
            live_rev.clone(),
        ],
        request,
    ) {
        return blocked_publish_execution(facts, transaction_class, reason, false);
    }
    let after_prepare = match CliJjAdapter.read_repo_facts(request) {
        Ok(value) => value,
        Err(exc) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(format!(
                    "post_prepare_fact_read_failed:{}",
                    exc.structured().code
                )),
                mutation_performed: true,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: facts.operation_id.clone(),
                published_revision: None,
                published_revision_reachable: false,
                conflict_materialized: false,
                materialized_conflicted_paths: Vec::new(),
                executed_phases: vec!["prepare".to_string(), "retry".to_string()],
                idempotency_replay: false,
                details: json!({"partial_mutation": "prepare_applied_post_fact_read_failed"}),
            }
        }
    };
    if !after_prepare.conflicted_paths.is_empty() {
        return TransactionExecution {
            state: "applied".to_string(),
            blocked_reason: None,
            mutation_performed: true,
            transaction_class: "publish_conflict_materialize".to_string(),
            before_op_id: facts.operation_id.clone(),
            after_op_id: after_prepare.operation_id,
            published_revision: None,
            published_revision_reachable: false,
            conflict_materialized: true,
            materialized_conflicted_paths: after_prepare.conflicted_paths.clone(),
            executed_phases: vec!["prepare".to_string(), "retry".to_string()],
            idempotency_replay: false,
            details: json!({
                "transaction_outcome": "publish_conflict_materialized",
                "conflict_authority": "rolling_live_head",
                "source_revset": source_revset,
                "live_rev": live_rev,
                "conflicted_paths": after_prepare.conflicted_paths
            }),
        };
    }
    let published_revision = match set_publish_bookmark(request, bookmark, &source_revset) {
        Ok(rev) => rev,
        Err(reason) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(reason),
                mutation_performed: true,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: after_prepare.operation_id,
                published_revision: None,
                published_revision_reachable: false,
                conflict_materialized: false,
                materialized_conflicted_paths: Vec::new(),
                executed_phases: vec!["prepare".to_string(), "retry".to_string()],
                idempotency_replay: false,
                details: json!({"partial_mutation": "prepare_applied_bookmark_failed"}),
            }
        }
    };
    let after_publish = match CliJjAdapter.read_repo_facts(request) {
        Ok(value) => value,
        Err(exc) => {
            return TransactionExecution {
                state: "blocked".to_string(),
                blocked_reason: Some(format!(
                    "post_publish_fact_read_failed:{}",
                    exc.structured().code
                )),
                mutation_performed: true,
                transaction_class,
                before_op_id: facts.operation_id.clone(),
                after_op_id: after_prepare.operation_id,
                published_revision,
                published_revision_reachable: false,
                conflict_materialized: false,
                materialized_conflicted_paths: Vec::new(),
                executed_phases: vec![
                    "prepare".to_string(),
                    "retry".to_string(),
                    "publish".to_string(),
                ],
                idempotency_replay: false,
                details: json!({"partial_mutation": "publish_applied_post_fact_read_failed"}),
            }
        }
    };
    let parent_rev = after_publish
        .parent
        .as_ref()
        .map(|parent| parent.commit_id.as_str())
        .unwrap_or("");
    let current_rev = after_publish.current.commit_id.as_str();
    let published_revision_reachable = published_revision
        .as_deref()
        .map(|rev| {
            !rev.is_empty()
                && (parent_rev.starts_with(rev)
                    || rev.starts_with(parent_rev)
                    || current_rev.starts_with(rev)
                    || rev.starts_with(current_rev))
        })
        .unwrap_or(false);
    TransactionExecution {
        state: "applied".to_string(),
        blocked_reason: None,
        mutation_performed: true,
        transaction_class,
        before_op_id: facts.operation_id.clone(),
        after_op_id: after_publish.operation_id,
        published_revision,
        published_revision_reachable,
        conflict_materialized: false,
        materialized_conflicted_paths: Vec::new(),
        executed_phases: vec![
            "prepare".to_string(),
            "retry".to_string(),
            "publish".to_string(),
            "refresh".to_string(),
        ],
        idempotency_replay: false,
        details: json!({
            "transaction_outcome": "dirty_publish_green",
            "source_revset": source_revset,
            "live_rev": live_rev,
            "post_parent_revision": parent_rev,
            "working_copy_revision": after_publish.current.commit_id
        }),
    }
}

fn blocked_publish_execution(
    facts: &RepoFacts,
    transaction_class: String,
    reason: String,
    mutation_performed: bool,
) -> TransactionExecution {
    TransactionExecution {
        state: "blocked".to_string(),
        blocked_reason: Some(reason),
        mutation_performed,
        transaction_class,
        before_op_id: facts.operation_id.clone(),
        after_op_id: facts.operation_id.clone(),
        published_revision: None,
        published_revision_reachable: false,
        conflict_materialized: false,
        materialized_conflicted_paths: Vec::new(),
        executed_phases: Vec::new(),
        idempotency_replay: false,
        details: json!({}),
    }
}

fn remove_generated_artifacts(
    workspace_path: &str,
    paths: &[String],
) -> Result<Vec<String>, String> {
    let workspace = Path::new(workspace_path);
    let mut removed = Vec::new();
    for path in paths {
        if !safe_relative_path(path) {
            return Err(format!("unsafe_generated_path:{path}"));
        }
        let target = workspace.join(path);
        if !target.exists() {
            continue;
        }
        if target.is_dir() {
            fs::remove_dir_all(&target)
                .map_err(|exc| format!("generated_dir_remove_failed:{path}:{exc}"))?;
        } else {
            fs::remove_file(&target)
                .map_err(|exc| format!("generated_file_remove_failed:{path}:{exc}"))?;
        }
        removed.push(path.clone());
    }
    Ok(removed)
}

fn build_guarded_mutation_decision(
    request: &SyncCoreRequest,
    facts: &RepoFacts,
    fallback_paths: &[String],
    classifications: &[PathClassification],
    source_provenance_state: &str,
    live_root_state: &str,
) -> GuardedMutationDecision {
    let mutation_kind = guarded_mutation_kind(request);
    let affected_paths = guarded_affected_paths(request, fallback_paths);
    let worker_intent_paths = context_array(
        &request.python_context,
        "/conflict_context/worker_intent_paths",
    );
    let recovery_handle = mutation_recovery_handle(request, facts);
    let source_rev = context_text(&request.python_context, "/source_state/source_rev")
        .unwrap_or(&facts.current.commit_id)
        .to_string();
    let live_rev = context_text(&request.python_context, "/live_target/live_rev")
        .unwrap_or(&facts.current.commit_id)
        .to_string();
    let prepared_rev = context_text(&request.python_context, "/source_state/prepared_rev")
        .or_else(|| context_text(&request.python_context, "/source_state/prepared_revision"))
        .unwrap_or(&facts.current.commit_id)
        .to_string();
    let packet_id = context_text(
        &request.python_context,
        "/conflict_context/conflict_packet_id",
    )
    .or_else(|| {
        context_text(
            &request.python_context,
            "/guarded_mutation/conflict_packet_id",
        )
    })
    .unwrap_or("sync-core-guarded-mutation");
    let mut blocked_reason: Option<String> = None;
    let mut mutation_performed = false;
    let mut removed_paths: Vec<String> = Vec::new();
    let mut mutation_class = mutation_kind.as_str();
    let mut safe_to_apply = true;
    let mut execution_owner = "rust_core";

    if affected_paths.is_empty() {
        blocked_reason = Some("missing_affected_paths".to_string());
    }
    if !matches!(
        source_provenance_state,
        "prepared" | "protected" | "recoverable" | "authored" | "published"
    ) {
        blocked_reason
            .get_or_insert_with(|| "source_revision_not_protected_or_recoverable".to_string());
    }

    if mutation_kind == "generated_artifact_cleanup"
        || mutation_kind == "generated_registry_restore"
    {
        mutation_class = if mutation_kind == "generated_registry_restore" {
            "generated_registry_restore"
        } else {
            "generated_artifact_cleanup"
        };
        for path in &affected_paths {
            if worker_intent_paths
                .iter()
                .any(|candidate| candidate == path)
            {
                blocked_reason = Some("worker_authored_generated_surface".to_string());
                break;
            }
            if !path_is_generated(path, classifications) {
                blocked_reason = Some(format!("path_not_generated:{path}"));
                break;
            }
            if !safe_relative_path(path) {
                blocked_reason = Some(format!("unsafe_generated_path:{path}"));
                break;
            }
        }
        if blocked_reason.is_none() {
            match remove_generated_artifacts(&request.workspace_path, &affected_paths) {
                Ok(paths) => {
                    removed_paths = paths;
                    mutation_performed = true;
                }
                Err(reason) => blocked_reason = Some(reason),
            }
        }
    } else if mutation_kind == "semantic_conflict_continuation_fold" {
        mutation_class = "semantic_conflict_continuation_fold";
        execution_owner = "python_fold_executor_after_rust_guard";
        let materialized_paths = context_array(
            &request.python_context,
            "/conflict_context/materialized_conflict_paths",
        );
        let changed_paths =
            context_array(&request.python_context, "/guarded_mutation/changed_paths");
        let materialized = if materialized_paths.is_empty() {
            affected_paths.clone()
        } else {
            materialized_paths
        };
        if context_bool(&request.python_context, "/guarded_mutation/stale_packet")
            || context_bool(&request.python_context, "/conflict_context/stale_packet")
        {
            blocked_reason = Some("stale_conflict_packet_revision_anchor".to_string());
        } else if context_bool(
            &request.python_context,
            "/guarded_mutation/multi_commit_range",
        ) || context_bool(
            &request.python_context,
            "/conflict_context/multi_commit_range",
        ) {
            blocked_reason = Some("multi_commit_unpublished_range_not_auto_folded".to_string());
        } else if context_bool(
            &request.python_context,
            "/guarded_mutation/conflict_markers_present",
        ) {
            blocked_reason = Some("conflict_markers_present".to_string());
        } else if !changed_paths.is_empty()
            && changed_paths
                .iter()
                .any(|path| !materialized.iter().any(|allowed| allowed == path))
        {
            blocked_reason = Some("changed_paths_outside_materialized_conflict_paths".to_string());
        }
        mutation_performed = false;
    } else {
        blocked_reason = Some("unsupported_guarded_mutation".to_string());
    }

    if blocked_reason.is_some() {
        safe_to_apply = false;
    }
    let state = if safe_to_apply && mutation_performed {
        "applied"
    } else if safe_to_apply {
        "journaled"
    } else {
        "blocked"
    };
    let journal_record = json!({
        "schema_id": "control-center.agent-up.sync-core.mutation-journal.v0.1",
        "transaction_id": request.transaction_id.clone(),
        "journal_id": format!("journal-{}", request.idempotency_key),
        "operation_kind": mutation_class,
        "state": state,
        "blocked_reason": blocked_reason.clone(),
        "workspace_id": request.workspace_id.clone(),
        "sync_group_id": request.sync_group_id.clone(),
        "conflict_packet_id": packet_id,
        "source_revision": source_rev,
        "base_live_revision": live_rev,
        "prepared_revision": prepared_rev,
        "working_copy_child_revision": facts.current.commit_id.clone(),
        "affected_paths": affected_paths.clone(),
        "removed_paths": removed_paths.clone(),
        "before_op_id": facts.operation_id.clone(),
        "after_op_id": facts.operation_id.clone(),
        "recovery_handle": recovery_handle.clone(),
        "recovery_action": "agent-up sync --probe --brief --json",
        "idempotency_key": request.idempotency_key.clone(),
        "mutation_performed": mutation_performed,
        "live_root_state": live_root_state,
    });
    let journal_write_result =
        write_guarded_journal(&request.recovery_journal_path, &journal_record);
    let (journal_record, safe_to_apply, blocked_reason) = match journal_write_result {
        Ok(()) => (journal_record, safe_to_apply, blocked_reason),
        Err(reason) => {
            let mut record = journal_record;
            record["state"] = json!("blocked");
            record["blocked_reason"] = json!(reason);
            (record, false, Some("journal_write_failed".to_string()))
        }
    };
    let mutation_plan = json!({
        "schema_id": "control-center.agent-up.sync-core.mutation-plan.v0.1",
        "plan_id": format!("plan-{}", request.idempotency_key),
        "mutation_class": mutation_class,
        "safe_to_apply": safe_to_apply,
        "blocked_reason": blocked_reason,
        "journal_required": true,
        "journal_path": request.recovery_journal_path.clone(),
        "journal_id": journal_record.get("journal_id").cloned().unwrap_or(Value::Null),
        "source_protected": true,
        "recovery_handle": recovery_handle.clone(),
        "affected_paths": journal_record.get("affected_paths").cloned().unwrap_or_else(|| json!([])),
        "execution_owner": execution_owner,
        "mutation_performed": mutation_performed,
        "idempotency_key": request.idempotency_key.clone(),
        "policy": {
            "semantic_auto_merge": false,
            "generated_policy_only_for_non_worker_intent": true,
            "path_subset_required": true,
            "stale_packet_blocks": true
        }
    });
    GuardedMutationDecision {
        mutation_plan,
        journal_record,
        next_agent_up_action: if safe_to_apply {
            json!({"action": "continue", "command": "agent-up sync --probe --brief --json"})
        } else {
            json!({"action": "blocked", "command": "agent-up sync --probe --brief --json"})
        },
        mutation_axis_state: if safe_to_apply && mutation_performed {
            "applied"
        } else if safe_to_apply {
            "journaled"
        } else {
            "blocked"
        },
        output_axis_state: Some(if safe_to_apply {
            "green_merge"
        } else {
            "blocked"
        }),
        decision_class_override: Some(if !safe_to_apply {
            "blocked"
        } else if mutation_kind == "generated_artifact_cleanup"
            || mutation_kind == "generated_registry_restore"
        {
            "generated_policy_applied"
        } else {
            "clean_merge"
        }),
        selected_workspace_state_override: Some(if safe_to_apply { "clean" } else { "stale" }),
        conflict_authority_override: Some(
            if mutation_kind == "semantic_conflict_continuation_fold" {
                "semantic_resolution_required"
            } else {
                "generated_policy"
            }
            .to_string(),
        ),
        conflict_axis_state_override: Some(
            if mutation_kind == "semantic_conflict_continuation_fold" {
                "semantic"
            } else {
                "generated"
            }
            .to_string(),
        ),
        mutation_performed,
    }
}

fn conflict_paths_from_context(context: &Value) -> Vec<String> {
    let mut paths = Vec::new();
    for pointer in [
        "/conflict_context/conflicted_paths",
        "/conflict_context/semantic_paths",
        "/conflict_context/generated_artifact_paths",
        "/conflict_context/generated_surface_paths",
    ] {
        if let Some(values) = context.pointer(pointer).and_then(|value| value.as_array()) {
            for value in values {
                if let Some(path) = value
                    .as_str()
                    .map(str::trim)
                    .filter(|path| !path.is_empty())
                {
                    if !paths.iter().any(|existing| existing == path) {
                        paths.push(path.to_string());
                    }
                }
            }
        }
    }
    paths
}

fn classify_conflict_paths(paths: &[String], context: &Value) -> Vec<PathClassification> {
    paths
        .iter()
        .map(|path| {
            let worker_intent = context
                .pointer("/conflict_context/worker_intent_paths")
                .and_then(|value| value.as_array())
                .map(|values| {
                    values
                        .iter()
                        .any(|value| value.as_str() == Some(path.as_str()))
                })
                .unwrap_or(false);
            let generated_by_context = context
                .pointer("/conflict_context/generated_artifact_paths")
                .and_then(|value| value.as_array())
                .map(|values| {
                    values
                        .iter()
                        .any(|value| value.as_str() == Some(path.as_str()))
                })
                .unwrap_or(false);
            let generated_surface_by_context = context
                .pointer("/conflict_context/generated_surface_paths")
                .and_then(|value| value.as_array())
                .map(|values| {
                    values
                        .iter()
                        .any(|value| value.as_str() == Some(path.as_str()))
                })
                .unwrap_or(false);
            let generated_by_path = path.contains("/dist/")
                || path.ends_with(".generated")
                || path.ends_with(".generated.json")
                || path.contains("@system/control-center/registry/")
                || path.contains("REGISTRY.control-center-runtime-surfaces");
            if (generated_by_context || generated_surface_by_context || generated_by_path)
                && !worker_intent
            {
                PathClassification {
                    path: path.clone(),
                    surface_class: "generated",
                    authority: "live_generated_authority",
                }
            } else {
                PathClassification {
                    path: path.clone(),
                    surface_class: "semantic",
                    authority: "worker_resolution_required",
                }
            }
        })
        .collect()
}

fn side_revision(context: &Value, side: &str, fallback: &str) -> String {
    let pointer = format!("/conflict_context/side_context/{side}/revision");
    context
        .pointer(&pointer)
        .and_then(|value| value.as_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or(fallback)
        .to_string()
}

fn build_conflict_packet_candidate(
    request: &SyncCoreRequest,
    facts: &RepoFacts,
    paths: &[String],
    classifications: &[PathClassification],
) -> Value {
    let live_rev = request
        .python_context
        .pointer("/live_target/live_rev")
        .and_then(|value| value.as_str())
        .unwrap_or(&facts.current.commit_id);
    let worker_rev = request
        .python_context
        .pointer("/source_state/source_rev")
        .and_then(|value| value.as_str())
        .unwrap_or(&facts.current.commit_id);
    let base_rev = request
        .python_context
        .pointer("/conflict_context/base_rev")
        .and_then(|value| value.as_str())
        .or_else(|| {
            facts
                .parent
                .as_ref()
                .map(|parent| parent.commit_id.as_str())
        })
        .unwrap_or("unknown-base");
    let semantic_paths: Vec<String> = classifications
        .iter()
        .filter(|item| item.surface_class == "semantic")
        .map(|item| item.path.clone())
        .collect();
    let generated_paths: Vec<String> = classifications
        .iter()
        .filter(|item| item.surface_class == "generated")
        .map(|item| item.path.clone())
        .collect();
    json!({
        "schema_id": "control-center.agent-up.sync-core.conflict-packet-candidate.v0.1",
        "packet_kind": "read_authority_candidate",
        "conflict_kind": request.python_context.pointer("/conflict_context/conflict_kind").and_then(|value| value.as_str()).unwrap_or("publish"),
        "conflict_authority": if !semantic_paths.is_empty() && !generated_paths.is_empty() {
            "mixed_policy"
        } else if !generated_paths.is_empty() {
            "generated_policy"
        } else {
            "semantic_resolution_required"
        },
        "conflicted_paths": paths,
        "semantic_paths": semantic_paths,
        "generated_artifact_paths": generated_paths,
        "path_classifications": classifications.iter().map(|item| json!({
            "path": item.path,
            "surface_class": item.surface_class,
            "authority": item.authority,
            "worker_intent_required": item.surface_class == "semantic",
        })).collect::<Vec<Value>>(),
        "side_context": {
            "base": {
                "label": "base",
                "revision": side_revision(&request.python_context, "base", base_rev),
            },
            "live": {
                "label": "live",
                "revision": side_revision(&request.python_context, "live", live_rev),
            },
            "worker": {
                "label": "worker",
                "revision": side_revision(&request.python_context, "worker", worker_rev),
            }
        },
        "provenance_refs": {
            "workspace_id": request.workspace_id,
            "sync_group_id": request.sync_group_id,
            "source_rev": worker_rev,
            "live_rev": live_rev,
            "base_rev": base_rev,
        },
        "policy_context": {
            "semantic_auto_merge": false,
            "generated_policy_is_deterministic": !generated_paths.is_empty(),
            "mutation_performed": false,
            "next_agent_up_action": "resolve_materialized_files_then_agent_up_sync"
        },
        "usefulness": {
            "raw_jj_required": false,
            "routine_current_required": false,
            "routine_diagnose_required": false,
            "operator_ambiguity_expected": false,
        }
    })
}

fn degraded_response(
    request: &SyncCoreRequest,
    latency_ms: f64,
    error: SyncCoreError,
) -> SyncCoreResponse {
    let structured = error.structured();
    let (engine_mode_actual, authority_state) = if matches!(
        request.engine_mode_requested.as_str(),
        "rust_read_authoritative" | "rust_mutation_authoritative" | "rust_transaction_candidate"
    ) {
        ("python_fallback", "python_fallback")
    } else {
        ("rust_shadow", "rust_shadow_observed")
    };
    SyncCoreResponse {
        schema_id: RESPONSE_SCHEMA_ID.to_string(),
        schema_version: SCHEMA_VERSION.to_string(),
        api_version: API_VERSION.to_string(),
        transaction_id: request.transaction_id.clone(),
        engine_mode_actual: engine_mode_actual.to_string(),
        authority_state: authority_state.to_string(),
        decision_class: "degraded".to_string(),
        selected_workspace_state: "stale".to_string(),
        source_provenance_state: "missing".to_string(),
        live_root_state: "unavailable".to_string(),
        conflict_authority: "stale_packet_blocked".to_string(),
        runtime_relevance: "none".to_string(),
        provenance: Provenance {
            workspace_rev: "unknown".to_string(),
            source_rev: "unknown".to_string(),
            live_rev: "unknown".to_string(),
            sync_group_id: request.sync_group_id.clone(),
        },
        conflict_packet_candidate: json!({}),
        mutation_plan: json!({}),
        journal_record: json!({}),
        next_agent_up_action: json!({"action": "use_python_fallback", "command": "agent-up sync --probe --brief --json"}),
        python_fallback_reason: Some(structured.code.clone()),
        parity_state: "degraded".to_string(),
        latency_ms,
        graph_metrics: GraphMetrics {
            kernel_call_count: 1,
            repo_lock_time_ms: 0.0,
            graph_nodes_scanned: 0,
            conflict_count: 0,
        },
        degraded_reason: "adapter_failure".to_string(),
        decision_confidence: 0.2,
        reason_codes: vec![
            "degraded".to_string(),
            "fallback_required".to_string(),
            structured.code.clone(),
        ],
        inspected_fact_classes: vec!["request".to_string(), "adapter_error".to_string()],
        decision_drivers: vec![
            "adapter_error".to_string(),
            "mutation_disallowed".to_string(),
        ],
        feedback_observation: json!({"state": "not_observed"}),
        state_machine_trace: state_trace(
            "stale",
            "missing",
            "unavailable",
            "stale_packet",
            "disallowed",
            "degraded",
        ),
        adapter_identity: json!({
            "adapter_profile": structured.adapter_profile,
            "adapter_version": adapter_version_for_profile(&structured.adapter_profile),
            "adapter_subprocess_count": 0,
            "adapter_jj_command_count": 0,
            "repo_snapshot_count": 0,
            "compatibility": adapter_compatibility(&structured.adapter_profile),
            "jj_internal_schema_exposed": false
        }),
        fallback: Fallback {
            python_fallback_available: true,
            fallback_reason: Some(structured.code.clone()),
            fallback_command: "agent-up sync --probe --brief --json".to_string(),
        },
        telemetry: json!({
            "kernel_call_count": 1,
            "latency_ms": latency_ms,
            "repo_lock_time_ms": 0.0,
            "adapter_subprocess_count": 0,
            "adapter_jj_command_count": 0,
            "repo_snapshot_count": 0,
            "mutation_performed": false,
            "performance_budget": {
                "schema_id": "control-center.agent-up.sync-core.performance-budget.v0.1",
                "algorithmic_budget_class": "fallback_recovery",
                "latency_budget_ms": LARGE_DEGRADED_LATENCY_BUDGET_MS,
                "latency_budget_state": budget_state(latency_ms, LARGE_DEGRADED_LATENCY_BUDGET_MS),
                "memory_bytes_estimate": 64 * 1024,
                "memory_budget_bytes": DEFAULT_MEMORY_BUDGET_BYTES,
                "memory_budget_state": "pass",
                "repo_lock_time_ms": 0.0,
                "repo_lock_budget_ms": 250.0,
                "repo_lock_budget_state": "pass",
                "output_bytes_estimate": 0,
                "output_budget_bytes": DEFAULT_OUTPUT_BUDGET_BYTES,
                "output_budget_state": "pass",
                "graph_nodes_scanned": 0,
                "conflict_count": 0,
                "inspected_fact_count": 2,
                "decision_driver_count": 2,
                "degraded_state_frequency_observed": 1,
                "one_kernel_call": true,
                "degraded_reason": "adapter_failure"
            }
        }),
        degraded: json!({"state": true, "reason": "adapter_failure"}),
        errors: vec![structured],
        repo_facts: None,
    }
}

fn state_trace(
    workspace: &str,
    source: &str,
    target_live: &str,
    conflict: &str,
    mutation: &str,
    output: &str,
) -> Vec<StateTraceStep> {
    vec![
        StateTraceStep {
            axis: "workspace".to_string(),
            state: workspace.to_string(),
            evidence_ref: "jj_adapter.workspace_head".to_string(),
            receipt_field: "selected_workspace_state".to_string(),
        },
        StateTraceStep {
            axis: "source".to_string(),
            state: source.to_string(),
            evidence_ref: "jj_adapter.workspace_head".to_string(),
            receipt_field: "source_provenance_state".to_string(),
        },
        StateTraceStep {
            axis: "target_live".to_string(),
            state: target_live.to_string(),
            evidence_ref: "python_context.live_target".to_string(),
            receipt_field: "live_root_state".to_string(),
        },
        StateTraceStep {
            axis: "conflict".to_string(),
            state: conflict.to_string(),
            evidence_ref: "jj_adapter.conflict_summary".to_string(),
            receipt_field: "conflict_authority".to_string(),
        },
        StateTraceStep {
            axis: "mutation".to_string(),
            state: mutation.to_string(),
            evidence_ref: "request.mutation_allowed_false".to_string(),
            receipt_field: "mutation_plan".to_string(),
        },
        StateTraceStep {
            axis: "output".to_string(),
            state: output.to_string(),
            evidence_ref: "decision_class".to_string(),
            receipt_field: "decision_class".to_string(),
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_journal(name: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "agent-up-sync-core-{name}-{}-{nanos}.jsonl",
            std::process::id()
        ));
        let _ = fs::remove_file(&path);
        path.to_string_lossy().to_string()
    }

    fn transaction_candidate_request(
        journal_path: String,
        feature_enabled: bool,
        with_conflict: bool,
    ) -> SyncCoreRequest {
        let mut request =
            contract_request_for_repo("/tmp/agent-up-sync-core-transaction-candidate");
        request.adapter_profile = "stub".to_string();
        request.requested_operation = "sync_transaction".to_string();
        request.engine_mode_requested = "rust_transaction_candidate".to_string();
        request.mutation_allowed = true;
        request.recovery_journal_path = journal_path;
        request.idempotency_key = "idem-state-machine-transaction-candidate".to_string();
        request.feature_flags = json!({
            "rust_sync_core_enabled": true,
            "rust_sync_core_transaction_candidate": feature_enabled
        });
        request.python_context = json!({
            "selected_workspace": {
                "lane_id": "agent-up-worker.b",
                "workspace_role": "worker",
                "workspace_lifecycle": "disposable"
            },
            "sync_group": {"peer_debt_state": "advisory"},
            "live_target": {
                "repo_id": "control-center",
                "live_rev": "head-1001",
                "live_root_state": "advanced"
            },
            "source_state": {
                "workspace_rev": "head-1010x",
                "source_rev": "head-1010x",
                "prepared_rev": "prepared-1010x",
                "authored_state": "prepared",
                "source_provenance_state": "prepared"
            },
            "runtime_context": {
                "runtime_cutover_required": false,
                "runtime_cutover_state": "already_current",
                "runtime_stage_content_current": true
            },
            "transaction_candidate": {
                "phases": ["prepare", "retry", "publish", "refresh", "fold"],
                "affected_paths": ["src/router.py"]
            },
            "conflict_context": if with_conflict {
                json!({
                    "conflict_kind": "publish",
                    "conflicted_paths": ["src/router.py"],
                    "semantic_paths": ["src/router.py"],
                    "base_rev": "head-1",
                    "side_context": {
                        "base": {"revision": "head-1"},
                        "live": {"revision": "head-1001"},
                        "worker": {"revision": "head-1010x"}
                    }
                })
            } else {
                json!({})
            }
        });
        request
    }

    #[test]
    fn stub_adapter_returns_schema_compatible_shadow_response() {
        let request = contract_request_for_repo("/tmp/agent-up-sync-core-stub");
        let response = run_shadow_transaction_with_adapter(request, &StubJjAdapter);
        assert_eq!(response.engine_mode_actual, "rust_shadow");
        assert_eq!(response.authority_state, "rust_shadow_observed");
        assert_eq!(response.graph_metrics.kernel_call_count, 1);
        assert!(response.mutation_plan.as_object().unwrap().is_empty());
        assert!(response.journal_record.as_object().unwrap().is_empty());
        assert!(response.fallback.python_fallback_available);
        assert!(!response.telemetry["mutation_performed"].as_bool().unwrap());
    }

    #[test]
    fn request_validation_failure_is_degraded_and_fallback_safe() {
        let mut request = contract_request_for_repo("");
        request.repo_path = "".to_string();
        let response = run_shadow_transaction_with_adapter(request, &StubJjAdapter);
        assert_eq!(response.decision_class, "degraded");
        assert_eq!(
            response.python_fallback_reason.as_deref(),
            Some("request_validation_failed")
        );
        assert_eq!(response.errors.len(), 1);
        assert!(response.errors[0].mutation_safe);
        assert!(!response.errors[0].raw_jj_guidance);
    }

    #[test]
    fn state_machine_transaction_candidate_journals_all_phases() {
        let journal = temp_journal("state-machine-green");
        let request = transaction_candidate_request(journal.clone(), true, false);
        let response = run_shadow_transaction_with_adapter(request, &StubJjAdapter);
        assert_eq!(response.engine_mode_actual, "rust_transaction_candidate");
        assert_eq!(response.authority_state, "rust_transaction_candidate");
        assert_eq!(response.decision_class, "clean_merge");
        assert_eq!(response.journal_record["state"], "journaled");
        assert_eq!(
            response.mutation_plan["mutation_class"],
            "full_sync_transaction_candidate"
        );
        assert_eq!(response.mutation_plan["safe_to_apply"], true);
        assert_eq!(
            response.mutation_plan["policy"]["python_fallback_required"],
            true
        );
        let phases = response.mutation_plan["transaction_phases"]
            .as_array()
            .unwrap();
        for phase in ["prepare", "retry", "publish", "refresh", "fold"] {
            assert!(phases
                .iter()
                .any(|item| item.get("phase").and_then(Value::as_str) == Some(phase)));
        }
        let journal_text = fs::read_to_string(journal).unwrap();
        assert!(journal_text.contains("full_sync_transaction_candidate"));
        assert!(journal_text.contains("idem-state-machine-transaction-candidate"));
    }

    #[test]
    fn state_machine_transaction_candidate_materializes_semantic_conflict() {
        let journal = temp_journal("state-machine-conflict");
        let request = transaction_candidate_request(journal, true, true);
        let response = run_shadow_transaction_with_adapter(request, &StubJjAdapter);
        assert_eq!(response.engine_mode_actual, "rust_transaction_candidate");
        assert_eq!(response.decision_class, "materialized_conflict");
        assert_eq!(response.selected_workspace_state, "conflicted");
        assert_eq!(response.conflict_authority, "semantic_resolution_required");
        assert_eq!(response.state_machine_trace.len(), 6);
        assert!(response
            .state_machine_trace
            .iter()
            .any(|step| step.axis == "mutation" && step.state == "journaled"));
        assert!(response.fallback.python_fallback_available);
    }

    #[test]
    fn state_machine_transaction_candidate_blocks_without_feature_flag() {
        let journal = temp_journal("state-machine-blocked");
        let request = transaction_candidate_request(journal, false, false);
        let response = run_shadow_transaction_with_adapter(request, &StubJjAdapter);
        assert_eq!(response.engine_mode_actual, "rust_transaction_candidate");
        assert_eq!(response.decision_class, "blocked");
        assert_eq!(
            response.mutation_plan["blocked_reason"],
            "transaction_candidate_feature_flag_disabled"
        );
        assert_eq!(response.journal_record["state"], "blocked");
        assert_eq!(response.next_agent_up_action["action"], "python_fallback");
    }
}
