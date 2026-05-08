use crate::error::SyncCoreError;
use crate::schema::{RepoFacts, RepoRevisionFacts, SyncCoreRequest};
use std::process::Command;

pub trait JjAdapter {
    fn read_repo_facts(&self, request: &SyncCoreRequest) -> Result<RepoFacts, SyncCoreError>;
}

#[derive(Debug, Clone, Copy)]
pub struct StubJjAdapter;

impl JjAdapter for StubJjAdapter {
    fn read_repo_facts(&self, request: &SyncCoreRequest) -> Result<RepoFacts, SyncCoreError> {
        Ok(RepoFacts {
            repo_path: request.repo_path.clone(),
            workspace_path: request.workspace_path.clone(),
            root_path: request.repo_path.clone(),
            current: RepoRevisionFacts {
                commit_id: "stub-current".to_string(),
                change_id: "stub-change".to_string(),
                description: "stub read-only repo facts".to_string(),
            },
            parent: Some(RepoRevisionFacts {
                commit_id: "stub-parent".to_string(),
                change_id: "stub-parent-change".to_string(),
                description: "stub parent".to_string(),
            }),
            operation_id: "stub-operation".to_string(),
            conflict_count: 0,
            conflicted_paths: Vec::new(),
            adapter_profile: "stub".to_string(),
            adapter_version: "stub.v0.1".to_string(),
            mutation_performed: false,
        })
    }
}

#[derive(Debug, Clone, Copy)]
pub struct CliJjAdapter;

impl CliJjAdapter {
    pub(crate) fn run_jj(
        repo_path: &str,
        args: &[&str],
        request: &SyncCoreRequest,
    ) -> Result<String, SyncCoreError> {
        let output = Command::new("jj")
            .arg("--repository")
            .arg(repo_path)
            .args(args)
            .output()
            .map_err(|exc| {
                SyncCoreError::new(
                    "jj_cli_spawn_failed",
                    format!("failed to spawn jj: {exc}"),
                    "cli-jj",
                    repo_path,
                    request.deadline_ms,
                )
            })?;
        if output.status.success() {
            return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
        }
        Err(SyncCoreError::new(
            "jj_cli_command_failed",
            String::from_utf8_lossy(&output.stderr).trim().to_string(),
            "cli-jj",
            repo_path,
            request.deadline_ms,
        ))
    }

    fn run_jj_optional(
        repo_path: &str,
        args: &[&str],
        request: &SyncCoreRequest,
    ) -> Option<String> {
        Self::run_jj(repo_path, args, request)
            .ok()
            .filter(|value| !value.trim().is_empty())
    }

    pub(crate) fn revision_facts(
        repo_path: &str,
        rev: &str,
        request: &SyncCoreRequest,
    ) -> Result<RepoRevisionFacts, SyncCoreError> {
        let output = Self::run_jj(
            repo_path,
            &[
                "log",
                "--no-graph",
                "-r",
                rev,
                "-T",
                "commit_id.short() ++ \"\\n\" ++ change_id.short() ++ \"\\n\" ++ description.first_line()",
            ],
            request,
        )?;
        let mut lines = output.lines();
        Ok(RepoRevisionFacts {
            commit_id: lines.next().unwrap_or("unknown").trim().to_string(),
            change_id: lines.next().unwrap_or("unknown").trim().to_string(),
            description: lines.next().unwrap_or("").trim().to_string(),
        })
    }

    fn conflict_paths(
        repo_path: &str,
        request: &SyncCoreRequest,
    ) -> Result<Vec<String>, SyncCoreError> {
        let output = Command::new("jj")
            .arg("--repository")
            .arg(repo_path)
            .args(["resolve", "--list", "--no-pager"])
            .output()
            .map_err(|exc| {
                SyncCoreError::new(
                    "jj_cli_spawn_failed",
                    format!("failed to spawn jj resolve: {exc}"),
                    "cli-jj",
                    repo_path,
                    request.deadline_ms,
                )
            })?;
        if output.status.success() {
            let paths = String::from_utf8_lossy(&output.stdout)
                .lines()
                .map(str::trim)
                .filter(|line| !line.is_empty())
                .map(str::to_string)
                .collect();
            return Ok(paths);
        }
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("No conflicts found") {
            return Ok(Vec::new());
        }
        Err(SyncCoreError::new(
            "jj_cli_conflict_scan_failed",
            stderr.trim().to_string(),
            "cli-jj",
            repo_path,
            request.deadline_ms,
        ))
    }
}

impl JjAdapter for CliJjAdapter {
    fn read_repo_facts(&self, request: &SyncCoreRequest) -> Result<RepoFacts, SyncCoreError> {
        let repo_path = request.repo_path.as_str();
        let root_path = Self::run_jj(repo_path, &["root"], request)?;
        let current = Self::revision_facts(repo_path, "@", request)?;
        let parent = Self::run_jj_optional(
            repo_path,
            &[
                "log",
                "--no-graph",
                "-r",
                "@-",
                "-T",
                "commit_id.short() ++ \"\\n\" ++ change_id.short() ++ \"\\n\" ++ description.first_line()",
            ],
            request,
        )
        .map(|output| {
            let mut lines = output.lines();
            RepoRevisionFacts {
                commit_id: lines.next().unwrap_or("unknown").trim().to_string(),
                change_id: lines.next().unwrap_or("unknown").trim().to_string(),
                description: lines.next().unwrap_or("").trim().to_string(),
            }
        });
        let operation_output =
            Self::run_jj(repo_path, &["op", "log", "--no-graph", "-n", "1"], request)?;
        let operation_id = operation_output
            .split_whitespace()
            .next()
            .unwrap_or("unknown-operation")
            .to_string();
        let conflicted_paths = Self::conflict_paths(repo_path, request)?;
        Ok(RepoFacts {
            repo_path: request.repo_path.clone(),
            workspace_path: request.workspace_path.clone(),
            root_path,
            current,
            parent,
            operation_id,
            conflict_count: conflicted_paths.len(),
            conflicted_paths,
            adapter_profile: "cli-jj".to_string(),
            adapter_version: "jj-cli.v0.40-compatible".to_string(),
            mutation_performed: false,
        })
    }
}
