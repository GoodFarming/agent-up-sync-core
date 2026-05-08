#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$ROOT"

gate="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gate)
      gate="${2:-}"
      [[ -n "$gate" ]] || { echo "ERROR: --gate requires a value" >&2; exit 2; }
      shift 2
      ;;
    --gate=*)
      gate="${1#*=}"
      [[ -n "$gate" ]] || { echo "ERROR: --gate requires a value" >&2; exit 2; }
      shift
      ;;
  -h|--help)
      cat <<'EOF'
Usage:
  bash Apps/control_center/scripts/validation/validate_agent_up_sync_core_rust_transaction_kernel.sh [--gate contract-schema|contract-stub|python-corpus|python-corpus-no-rust|rust-scaffold|rust-shadow-classifier|rust-read-authority|jj-lib-read-adapter|rust-performance|rust-performance-clean|rust-performance-conflict|guarded-mutation|rust-guarded-mutation|rust-guarded-mutation-fold|transaction-candidate|rust-transaction-candidate|rollout|rust-rollout-canary|deployment-readiness|rust-deployment-readiness|open-source-readiness|all]
EOF
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      exit 2
      ;;
  esac
done

pytest_cmd=(uv run --with pytest python -m pytest -q)

run_static() {
  python3 -m py_compile \
    Apps/control_center/backend/convergence/agent_up_sync_core_schema.py \
    Apps/control_center/backend/convergence/agent_up_sync_core_bridge.py
  python3 -m json.tool Apps/control_center/tests/sync_core_corpus/sync_core_contract_examples.json >/dev/null
}

run_python_corpus_static() {
  python3 -m json.tool Apps/control_center/tests/sync_core_corpus/sync_core_python_expected_decisions.json >/dev/null
}

run_contract_schema() {
  run_static
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_rust_contract.py \
    -k "contract_schema or negative_examples or shadow_request"
}

run_contract_stub() {
  run_static
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_rust_contract.py
}

run_python_corpus() {
  run_python_corpus_static
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_python_corpus.py
}

run_rust_scaffold() {
  cargo test -p agent-up-sync-core
  cargo test -p agent-up-sync-core --features cli-jj-adapter
  cargo test -p agent-up-sync-core --no-default-features
  cargo test -p agent-up-sync-core --all-features
  cargo test -p agent-up-sync-core --test cli_library_parity
  cargo fmt --check
  cargo clippy -p agent-up-sync-core --all-targets --all-features -- -D warnings
  python3 - <<'PY'
import json
import subprocess

metadata = json.loads(subprocess.check_output(["cargo", "metadata", "--format-version", "1", "--no-deps"]))
packages = {package["name"]: package for package in metadata["packages"]}
package = packages.get("agent-up-sync-core")
if not package:
    raise SystemExit("agent-up-sync-core package missing from cargo metadata")
if package.get("license") != "Apache-2.0":
    raise SystemExit("agent-up-sync-core must declare Apache-2.0 license")
if "serde" not in {dependency["name"] for dependency in package.get("dependencies", [])}:
    raise SystemExit("agent-up-sync-core dependency metadata missing serde")
PY
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_rust_scaffold.py
}

run_rust_shadow_classifier() {
  run_rust_scaffold
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_shadow_classifier.py
}

run_rust_read_authority() {
  run_rust_shadow_classifier
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_read_authority.py
}

run_jj_lib_read_adapter() {
  run_static
  cargo check -p agent-up-sync-core --no-default-features --features jj-lib-adapter
  cargo test -p agent-up-sync-core --no-default-features --features jj-lib-adapter
  cargo clippy -p agent-up-sync-core --all-targets --no-default-features --features jj-lib-adapter -- -D warnings
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_read_authority.py \
    -k "jj_lib or read_authority_fallback"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_performance.py \
    -k "jj_lib"
}

run_rust_performance_clean() {
  run_rust_read_authority
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_performance.py \
    -k "clean_noop or code_intelligence"
}

run_rust_performance_conflict() {
  run_rust_read_authority
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_performance.py \
    -k "conflict_packet or performance_budget"
}

run_rust_performance() {
  run_rust_read_authority
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_performance.py
  cargo bench -p agent-up-sync-core --bench sync_transaction_classes -- --test
}

run_rust_guarded_mutation_fold() {
  run_rust_performance
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_guarded_mutation.py \
    -k "semantic_fold"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave15/test_agent_up_semantic_conflict_continuation_fold.py \
    -k "rust_guarded_mutation"
}

run_rust_guarded_mutation() {
  run_rust_performance
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_guarded_mutation.py
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave15/test_agent_up_semantic_conflict_continuation_fold.py \
    -k "rust_guarded_mutation"
}

run_rust_transaction_candidate() {
  run_rust_guarded_mutation
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_transaction_candidate.py
  cargo test -p agent-up-sync-core state_machine --all-features
}

run_rust_rollout_canary() {
  run_rust_transaction_candidate
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_rollout.py \
    -k "flags or rollback or canary or op_drift"
}

run_rust_deployment_readiness() {
  run_rust_rollout
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python \
    Apps/control_center/scripts/validation/generate_agent_up_sync_core_deployment_canary_matrix.py \
    --output Apps/control_center/test_artifacts/sync-core-canary/deployment-matrix.json
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python - <<'PY'
import json
from pathlib import Path

from Apps.control_center.backend.convergence.agent_up_sync_core_bridge import (
    TRANSACTION_AUTHORITY_MODES,
    summarize_sync_core_deployment_canary_matrix,
)

evidence = Path("Apps/control_center/test_artifacts/sync-core-canary/deployment-matrix.json")
if not evidence.exists():
    raise SystemExit(f"deployment canary matrix evidence missing: {evidence}")
payload = json.loads(evidence.read_text(encoding="utf-8"))
authority_mode = str(payload.get("authority_mode") or "")
if authority_mode not in TRANSACTION_AUTHORITY_MODES:
    raise SystemExit(f"deployment evidence is not transaction authority: {authority_mode or '<missing>'}")
summary = summarize_sync_core_deployment_canary_matrix(payload.get("canary_matrix") or payload)
if not summary["complete"]:
    raise SystemExit(f"deployment canary matrix incomplete: {json.dumps(summary, sort_keys=True)}")
print("sync_core_deployment_readiness=pass")
PY
}

run_open_source_readiness() {
  python3 - <<'PY'
from pathlib import Path
root = Path("Apps/control_center/rust/agent-up-sync-core")
required = [
    "LICENSE",
    "README.md",
    "ARCHITECTURE.md",
    "SCHEMAS.md",
    "SAFETY.md",
    "EXAMPLES.md",
    "BENCHMARKS.md",
    "MSRV.md",
    "RELEASE.md",
]
missing = [name for name in required if not (root / name).exists()]
if missing:
    raise SystemExit(f"missing sync-core open-source readiness files: {missing}")
cargo = (root / "Cargo.toml").read_text()
for marker in ['version = "0.1.0"', 'rust-version = "1.89"', 'license = "Apache-2.0"', 'jj-lib = { version = "=0.40.0"']:
    if marker not in cargo:
        raise SystemExit(f"Cargo.toml missing {marker}")
PY
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_rollout.py \
    -k "open_source"
}

run_rust_rollout() {
  run_rust_rollout_canary
  run_open_source_readiness
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_rollout.py \
    -k "guidance"
}

run_all() {
  run_contract_stub
  run_python_corpus
  run_rust_scaffold
  run_rust_shadow_classifier
  run_rust_read_authority
  run_rust_performance
  run_rust_guarded_mutation
  run_rust_transaction_candidate
  run_rust_rollout
}

case "$gate" in
  contract-schema)
    run_contract_schema
    ;;
  contract-stub)
    run_contract_stub
    ;;
  all)
    run_all
    ;;
  python-corpus|python-corpus-no-rust)
    run_python_corpus
    ;;
  rust-scaffold)
    run_rust_scaffold
    ;;
  rust-shadow-classifier)
    run_rust_shadow_classifier
    ;;
  rust-read-authority|read-authority)
    run_rust_read_authority
    ;;
  jj-lib-read-adapter)
    run_jj_lib_read_adapter
    ;;
  rust-performance-clean)
    run_rust_performance_clean
    ;;
  rust-performance-conflict)
    run_rust_performance_conflict
    ;;
  rust-performance|performance)
    run_rust_performance
    ;;
  rust-guarded-mutation-fold)
    run_rust_guarded_mutation_fold
    ;;
  guarded-mutation|rust-guarded-mutation)
    run_rust_guarded_mutation
    ;;
  transaction-candidate|rust-transaction-candidate)
    run_rust_transaction_candidate
    ;;
  rollout|rust-rollout)
    run_rust_rollout
    ;;
  rust-rollout-canary)
    run_rust_rollout_canary
    ;;
  deployment-readiness|rust-deployment-readiness)
    run_rust_deployment_readiness
    ;;
  open-source-readiness)
    run_open_source_readiness
    ;;
  *)
    echo "ERROR: unsupported gate '$gate'" >&2
    exit 2
    ;;
esac

echo "PASS: Agent-Up sync-core Rust transaction kernel validation passed for gate '$gate'"
