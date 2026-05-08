use agent_up_sync_core::{
    contract_request_for_repo, run_shadow_transaction_with_adapter, StubJjAdapter, SyncCoreRequest,
};
use serde_json::{json, Value};
use std::time::Instant;

fn request(class_name: &str) -> SyncCoreRequest {
    let mut request =
        contract_request_for_repo(format!("/tmp/agent-up-sync-core-bench-{class_name}"));
    request.engine_mode_requested = "rust_read_authoritative".to_string();
    request.feature_flags = json!({
        "rust_sync_core_enabled": true,
        "rust_sync_core_read_authority": true,
        "rust_sync_core_mutation": false
    });
    request.mutation_allowed = false;
    if class_name == "conflict_packet" {
        request.python_context["source_state"]["authored_state"] = json!("prepared");
        request.python_context["source_state"]["source_provenance_state"] = json!("prepared");
        request.python_context["live_target"]["live_root_state"] = json!("advanced");
        request.python_context["conflict_context"] = json!({
            "conflict_packet_id": "bench-conflict-packet",
            "conflict_kind": "publish",
            "base_rev": "bench-base",
            "conflicted_paths": [
                "Apps/control_center/backend/convergence/agent_up_sync_engine.py",
                "frontend/cockpit/dist/assets/index.js"
            ],
            "semantic_paths": ["Apps/control_center/backend/convergence/agent_up_sync_engine.py"],
            "generated_artifact_paths": ["frontend/cockpit/dist/assets/index.js"],
            "side_context": {
                "base": {"revision": "bench-base"},
                "live": {"revision": "bench-live"},
                "worker": {"revision": "bench-worker"}
            }
        });
    }
    if class_name == "dirty_preflight" {
        request.python_context["source_state"]["authored_state"] = json!("prepared");
        request.python_context["source_state"]["source_provenance_state"] = json!("prepared");
    }
    request
}

fn budget(response: &Value) -> &Value {
    &response["telemetry"]["performance_budget"]
}

fn main() {
    let mut rows = Vec::new();
    for class_name in ["clean_noop", "dirty_preflight", "conflict_packet"] {
        let request = request(class_name);
        let started = Instant::now();
        let response = run_shadow_transaction_with_adapter(request, &StubJjAdapter);
        let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        let response_value = serde_json::to_value(&response).expect("response serializes");
        let row = json!({
            "class": class_name,
            "decision_class": response.decision_class,
            "latency_ms": elapsed_ms,
            "kernel_latency_ms": response.latency_ms,
            "budget": budget(&response_value),
            "kernel_call_count": response.graph_metrics.kernel_call_count,
            "mutation_performed": response.repo_facts.map(|facts| facts.mutation_performed).unwrap_or(false)
        });
        if row["kernel_call_count"] != 1 {
            panic!("benchmark class {class_name} did not preserve one kernel call");
        }
        if row["budget"]["one_kernel_call"] != true {
            panic!("benchmark class {class_name} did not report one_kernel_call");
        }
        rows.push(row);
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "schema_id": "control-center.agent-up.sync-core.benchmark-report.v0.1",
            "classes": rows
        }))
        .expect("benchmark report serializes")
    );
}
