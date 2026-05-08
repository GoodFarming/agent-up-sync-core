use serde::{Deserialize, Serialize};
use std::error::Error;
use std::fmt::{Display, Formatter};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StructuredError {
    pub code: String,
    pub message: String,
    pub adapter_profile: String,
    pub repo_path: String,
    pub deadline_ms: u64,
    pub mutation_safe: bool,
    pub raw_jj_guidance: bool,
}

#[derive(Debug, Clone)]
pub struct SyncCoreError {
    structured: StructuredError,
}

impl SyncCoreError {
    pub fn new(
        code: impl Into<String>,
        message: impl Into<String>,
        adapter_profile: impl Into<String>,
        repo_path: impl Into<String>,
        deadline_ms: u64,
    ) -> Self {
        Self {
            structured: StructuredError {
                code: code.into(),
                message: message.into(),
                adapter_profile: adapter_profile.into(),
                repo_path: repo_path.into(),
                deadline_ms,
                mutation_safe: true,
                raw_jj_guidance: false,
            },
        }
    }

    pub fn structured(&self) -> StructuredError {
        self.structured.clone()
    }
}

impl Display for SyncCoreError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.structured.code, self.structured.message)
    }
}

impl Error for SyncCoreError {}
