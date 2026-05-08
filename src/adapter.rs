use crate::error::SyncCoreError;
use crate::schema::{RepoFacts, RepoRevisionFacts, SyncCoreRequest};
#[cfg(feature = "jj-lib-adapter")]
use jj_lib::config::StackedConfig;
#[cfg(feature = "jj-lib-adapter")]
use jj_lib::repo::Repo as _;
#[cfg(feature = "jj-lib-adapter")]
use jj_lib::repo::StoreFactories;
#[cfg(feature = "jj-lib-adapter")]
use jj_lib::settings::UserSettings;
#[cfg(feature = "jj-lib-adapter")]
use jj_lib::workspace::{default_working_copy_factories, Workspace};
#[cfg(feature = "jj-lib-adapter")]
use pollster::FutureExt as _;
#[cfg(feature = "jj-lib-adapter")]
use serde_json::Value;
#[cfg(feature = "jj-lib-adapter")]
use std::fs;
#[cfg(feature = "jj-lib-adapter")]
use std::path::Path;
#[cfg(feature = "jj-lib-adapter")]
use std::path::PathBuf;
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

#[cfg(feature = "jj-lib-adapter")]
#[derive(Debug, Clone, Copy)]
pub struct JjLibAdapter;

#[cfg(feature = "jj-lib-adapter")]
impl JjLibAdapter {
    fn repo_dir_from_workspace(workspace_path: &Path) -> Option<PathBuf> {
        let jj_dir = workspace_path.join(".jj");
        let repo = jj_dir.join("repo");
        if repo.is_dir() {
            return Some(repo);
        }
        if repo.is_file() {
            let relative = fs::read_to_string(&repo).ok()?;
            return Some(jj_dir.join(relative.trim()));
        }
        None
    }

    fn preflight_operation_state(request: &SyncCoreRequest) -> Result<(), SyncCoreError> {
        let Some(repo_dir) = Self::repo_dir_from_workspace(Path::new(&request.workspace_path))
        else {
            return Ok(());
        };
        if !repo_dir.join("op_heads").exists() || !repo_dir.join("op_store").exists() {
            return Err(SyncCoreError::new(
                "jj_lib_missing_operation_state",
                "jj-lib adapter could not find required operation state directories",
                "jj-lib",
                request.repo_path.clone(),
                request.deadline_ms,
            ));
        }
        Ok(())
    }

    fn short_hex(id: &impl jj_lib::object_id::ObjectId) -> String {
        id.hex().chars().take(12).collect()
    }

    fn revision_facts(commit: &jj_lib::commit::Commit) -> RepoRevisionFacts {
        RepoRevisionFacts {
            commit_id: Self::short_hex(commit.id()),
            change_id: Self::short_hex(commit.change_id()),
            description: commit
                .description()
                .lines()
                .next()
                .unwrap_or("")
                .trim()
                .to_string(),
        }
    }

    fn context_conflict_paths(request: &SyncCoreRequest) -> Vec<String> {
        fn strings_at(value: Option<&Value>) -> Vec<String> {
            value
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .collect()
        }
        let conflict_context = request.python_context.get("conflict_context");
        let mut paths =
            strings_at(conflict_context.and_then(|value| value.get("conflicted_paths")));
        for path in strings_at(conflict_context.and_then(|value| value.get("semantic_paths"))) {
            if !paths.contains(&path) {
                paths.push(path);
            }
        }
        for path in
            strings_at(conflict_context.and_then(|value| value.get("generated_artifact_paths")))
        {
            if !paths.contains(&path) {
                paths.push(path);
            }
        }
        paths
    }
}

#[cfg(feature = "jj-lib-adapter")]
impl JjAdapter for JjLibAdapter {
    fn read_repo_facts(&self, request: &SyncCoreRequest) -> Result<RepoFacts, SyncCoreError> {
        let workspace_path = Path::new(&request.workspace_path);
        Self::preflight_operation_state(request)?;
        let settings =
            UserSettings::from_config(StackedConfig::with_defaults()).map_err(|exc| {
                SyncCoreError::new(
                    "jj_lib_settings_load_failed",
                    format!("failed to load jj-lib settings: {exc}"),
                    "jj-lib",
                    request.repo_path.clone(),
                    request.deadline_ms,
                )
            })?;
        let store_factories = StoreFactories::default();
        let working_copy_factories = default_working_copy_factories();
        let workspace = Workspace::load(
            &settings,
            workspace_path,
            &store_factories,
            &working_copy_factories,
        )
        .map_err(|exc| {
            SyncCoreError::new(
                "jj_lib_workspace_load_failed",
                format!("failed to load jj workspace through jj-lib: {exc}"),
                "jj-lib",
                request.repo_path.clone(),
                request.deadline_ms,
            )
        })?;
        let repo = workspace
            .repo_loader()
            .load_at_head()
            .block_on()
            .map_err(|exc| {
                SyncCoreError::new(
                    "jj_lib_repo_load_failed",
                    format!("failed to load jj repo head through jj-lib: {exc}"),
                    "jj-lib",
                    request.repo_path.clone(),
                    request.deadline_ms,
                )
            })?;
        let workspace_name = workspace.workspace_name();
        let current_id = repo
            .view()
            .get_wc_commit_id(workspace_name)
            .ok_or_else(|| {
                SyncCoreError::new(
                    "jj_lib_missing_working_copy_commit",
                    format!(
                        "workspace '{}' has no working-copy commit in jj view",
                        workspace_name.as_symbol()
                    ),
                    "jj-lib",
                    request.repo_path.clone(),
                    request.deadline_ms,
                )
            })?
            .clone();
        let current_commit = repo
            .store()
            .get_commit_async(&current_id)
            .block_on()
            .map_err(|exc| {
                SyncCoreError::new(
                    "jj_lib_current_commit_load_failed",
                    format!("failed to load current working-copy commit: {exc}"),
                    "jj-lib",
                    request.repo_path.clone(),
                    request.deadline_ms,
                )
            })?;
        let parent = current_commit
            .parent_ids()
            .first()
            .map(|parent_id| repo.store().get_commit_async(parent_id).block_on())
            .transpose()
            .map_err(|exc| {
                SyncCoreError::new(
                    "jj_lib_parent_commit_load_failed",
                    format!("failed to load parent commit: {exc}"),
                    "jj-lib",
                    request.repo_path.clone(),
                    request.deadline_ms,
                )
            })?
            .as_ref()
            .map(Self::revision_facts);
        let conflicted_paths = Self::context_conflict_paths(request);
        if current_commit.has_conflict() && conflicted_paths.is_empty() {
            return Err(SyncCoreError::new(
                "jj_lib_conflict_state_not_representable",
                "jj-lib detected structural conflicts but no materialized conflict path packet was available",
                "jj-lib",
                request.repo_path.clone(),
                request.deadline_ms,
            ));
        }
        Ok(RepoFacts {
            repo_path: request.repo_path.clone(),
            workspace_path: request.workspace_path.clone(),
            root_path: workspace.workspace_root().to_string_lossy().to_string(),
            current: Self::revision_facts(&current_commit),
            parent,
            operation_id: Self::short_hex(repo.op_id()),
            conflict_count: conflicted_paths.len(),
            conflicted_paths,
            adapter_profile: "jj-lib".to_string(),
            adapter_version: "jj-lib.v0.40.0-read-only".to_string(),
            mutation_performed: false,
        })
    }
}
