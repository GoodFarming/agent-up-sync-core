use agent_up_sync_core::{contract_request_for_repo, run_shadow_transaction, SyncCoreResponse};
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn temp_repo_path(name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "agent-up-sync-core-{name}-{}-{nanos}",
        std::process::id()
    ))
}

fn init_jj_repo(name: &str) -> PathBuf {
    let path = temp_repo_path(name);
    fs::create_dir_all(&path).expect("create temp repo");
    let output = Command::new("jj")
        .arg("git")
        .arg("init")
        .arg(&path)
        .output()
        .expect("spawn jj git init");
    assert!(
        output.status.success(),
        "jj git init failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    path
}

#[test]
fn cli_and_library_return_same_read_only_shadow_decision() {
    let repo = init_jj_repo("parity");
    let request = contract_request_for_repo(repo.to_string_lossy().to_string());
    let library_response = run_shadow_transaction(request.clone());

    let mut child = Command::new(env!("CARGO_BIN_EXE_agent-up-sync-core"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn agent-up-sync-core binary");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(
            serde_json::to_string(&request)
                .expect("request json")
                .as_bytes(),
        )
        .expect("write request");
    let output = child.wait_with_output().expect("wait for binary");
    assert!(
        output.status.success(),
        "sync-core CLI failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let cli_response: SyncCoreResponse =
        serde_json::from_slice(&output.stdout).expect("parse response");

    assert_eq!(cli_response.engine_mode_actual, "rust_shadow");
    assert_eq!(cli_response.authority_state, "rust_shadow_observed");
    assert_eq!(cli_response.decision_class, library_response.decision_class);
    assert_eq!(
        cli_response.selected_workspace_state,
        library_response.selected_workspace_state
    );
    assert_eq!(cli_response.graph_metrics.kernel_call_count, 1);
    assert_eq!(cli_response.mutation_plan, serde_json::json!({}));
    assert_eq!(cli_response.journal_record, serde_json::json!({}));
    assert!(!cli_response.repo_facts.as_ref().unwrap().mutation_performed);
    assert_eq!(cli_response.repo_facts.as_ref().unwrap().conflict_count, 0);
}

#[test]
fn invalid_repo_returns_structured_fallback_safe_error_response() {
    let repo = temp_repo_path("missing");
    let request = contract_request_for_repo(repo.to_string_lossy().to_string());
    let response = run_shadow_transaction(request);

    assert_eq!(response.decision_class, "degraded");
    assert_eq!(response.degraded_reason, "adapter_failure");
    assert_eq!(response.graph_metrics.kernel_call_count, 1);
    assert!(response.fallback.python_fallback_available);
    assert_eq!(response.errors.len(), 1);
    assert!(response.errors[0].mutation_safe);
    assert!(!response.errors[0].raw_jj_guidance);
    assert!(response.mutation_plan.as_object().unwrap().is_empty());
    assert!(response.journal_record.as_object().unwrap().is_empty());
}
