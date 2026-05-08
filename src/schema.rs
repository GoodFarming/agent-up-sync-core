use crate::error::StructuredError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub const REQUEST_SCHEMA_ID: &str = "control-center.agent-up.sync-core.request.v0.1";
pub const RESPONSE_SCHEMA_ID: &str = "control-center.agent-up.sync-core.response.v0.1";
pub const API_VERSION: &str = "agent-up-sync-core.v0.1";
pub const SCHEMA_VERSION: &str = "v0.1";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncCoreRequest {
    pub schema_id: String,
    pub schema_version: String,
    pub api_version: String,
    pub transaction_id: String,
    pub workspace_id: String,
    pub repo_path: String,
    pub workspace_path: String,
    #[serde(default)]
    pub live_root_path: Option<String>,
    #[serde(default)]
    pub live_root_authority_ref: Option<String>,
    pub sync_group_id: String,
    pub requested_operation: String,
    pub python_context: Value,
    pub adapter_profile: String,
    pub feature_flags: Value,
    pub engine_mode_requested: String,
    pub mutation_allowed: bool,
    pub deadline_ms: u64,
    pub recovery_journal_path: String,
    pub correlation_id: String,
    pub idempotency_key: String,
}

impl SyncCoreRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_id != REQUEST_SCHEMA_ID {
            return Err(format!("request.schema_id expected {REQUEST_SCHEMA_ID}"));
        }
        if self.schema_version != SCHEMA_VERSION {
            return Err(format!("request.schema_version expected {SCHEMA_VERSION}"));
        }
        if self.api_version != API_VERSION {
            return Err(format!("request.api_version expected {API_VERSION}"));
        }
        if self.transaction_id.trim().is_empty()
            || self.workspace_id.trim().is_empty()
            || self.repo_path.trim().is_empty()
            || self.workspace_path.trim().is_empty()
            || self.sync_group_id.trim().is_empty()
            || self.requested_operation.trim().is_empty()
            || self.adapter_profile.trim().is_empty()
            || self.engine_mode_requested.trim().is_empty()
            || self.recovery_journal_path.trim().is_empty()
            || self.correlation_id.trim().is_empty()
            || self.idempotency_key.trim().is_empty()
        {
            return Err("request contains an empty required text field".to_string());
        }
        if self
            .live_root_path
            .as_deref()
            .unwrap_or("")
            .trim()
            .is_empty()
            && self
                .live_root_authority_ref
                .as_deref()
                .unwrap_or("")
                .trim()
                .is_empty()
        {
            return Err(
                "request.live_root_path or request.live_root_authority_ref is required".to_string(),
            );
        }
        if self.mutation_allowed
            && matches!(
                self.engine_mode_requested.as_str(),
                "python_authoritative"
                    | "rust_shadow"
                    | "rust_read_authoritative"
                    | "python_fallback"
                    | "parity_failed"
            )
        {
            return Err("read-only sync-core request cannot allow mutation".to_string());
        }
        if !self.mutation_allowed && self.engine_mode_requested == "rust_mutation_authoritative" {
            return Err("rust_mutation_authoritative request must allow mutation".to_string());
        }
        if !self.mutation_allowed && self.engine_mode_requested == "rust_transaction_candidate" {
            return Err("rust_transaction_candidate request must allow mutation".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StateTraceStep {
    pub axis: String,
    pub state: String,
    pub evidence_ref: String,
    pub receipt_field: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Provenance {
    pub workspace_rev: String,
    pub source_rev: String,
    pub live_rev: String,
    pub sync_group_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GraphMetrics {
    pub kernel_call_count: u64,
    pub repo_lock_time_ms: f64,
    pub graph_nodes_scanned: u64,
    pub conflict_count: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Fallback {
    pub python_fallback_available: bool,
    pub fallback_reason: Option<String>,
    pub fallback_command: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepoRevisionFacts {
    pub commit_id: String,
    pub change_id: String,
    pub description: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepoFacts {
    pub repo_path: String,
    pub workspace_path: String,
    pub root_path: String,
    pub current: RepoRevisionFacts,
    pub parent: Option<RepoRevisionFacts>,
    pub operation_id: String,
    pub conflict_count: usize,
    pub conflicted_paths: Vec<String>,
    pub adapter_profile: String,
    pub adapter_version: String,
    pub mutation_performed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SyncCoreResponse {
    pub schema_id: String,
    pub schema_version: String,
    pub api_version: String,
    pub transaction_id: String,
    pub engine_mode_actual: String,
    pub authority_state: String,
    pub decision_class: String,
    pub selected_workspace_state: String,
    pub source_provenance_state: String,
    pub live_root_state: String,
    pub conflict_authority: String,
    pub runtime_relevance: String,
    pub provenance: Provenance,
    pub conflict_packet_candidate: Value,
    pub mutation_plan: Value,
    pub journal_record: Value,
    pub next_agent_up_action: Value,
    pub python_fallback_reason: Option<String>,
    pub parity_state: String,
    pub latency_ms: f64,
    pub graph_metrics: GraphMetrics,
    pub degraded_reason: String,
    pub decision_confidence: f64,
    pub reason_codes: Vec<String>,
    pub inspected_fact_classes: Vec<String>,
    pub decision_drivers: Vec<String>,
    pub feedback_observation: Value,
    pub state_machine_trace: Vec<StateTraceStep>,
    pub adapter_identity: Value,
    pub fallback: Fallback,
    pub telemetry: Value,
    pub degraded: Value,
    pub errors: Vec<StructuredError>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repo_facts: Option<RepoFacts>,
}

pub fn contract_request_for_repo(repo_path: impl Into<String>) -> SyncCoreRequest {
    let repo_path = repo_path.into();
    SyncCoreRequest {
        schema_id: REQUEST_SCHEMA_ID.to_string(),
        schema_version: SCHEMA_VERSION.to_string(),
        api_version: API_VERSION.to_string(),
        transaction_id: "sync-core-rust-scaffold-transaction".to_string(),
        workspace_id: "workspace::control-center::agent-up-worker.rust-scaffold".to_string(),
        repo_path: repo_path.clone(),
        workspace_path: repo_path.clone(),
        live_root_path: Some(repo_path.clone()),
        live_root_authority_ref: None,
        sync_group_id: "sync-control-center".to_string(),
        requested_operation: "classify".to_string(),
        python_context: json!({
            "selected_workspace": {
                "lane_id": "agent-up-worker.rust-scaffold",
                "workspace_role": "worker",
                "workspace_lifecycle": "disposable"
            },
            "sync_group": {"peer_debt_state": "advisory"},
            "live_target": {"repo_id": "control-center", "live_rev": "live-rev-rust-scaffold"},
            "source_state": {
                "workspace_rev": "workspace-rev-rust-scaffold",
                "source_rev": "source-rev-rust-scaffold",
                "authored_state": "clean"
            },
            "runtime_context": {
                "runtime_cutover_required": false,
                "runtime_stage_content_current": true
            }
        }),
        adapter_profile: "cli-jj".to_string(),
        feature_flags: json!({"rust_sync_core_enabled": false, "rust_sync_core_shadow": true}),
        engine_mode_requested: "rust_shadow".to_string(),
        mutation_allowed: false,
        deadline_ms: 5000,
        recovery_journal_path: "/tmp/agent-up-sync-core-rust-scaffold-journal.jsonl".to_string(),
        correlation_id: "corr-sync-core-rust-scaffold".to_string(),
        idempotency_key: "idem-sync-core-rust-scaffold".to_string(),
    }
}
