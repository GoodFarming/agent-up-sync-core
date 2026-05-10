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
  bash Apps/control_center/scripts/validation/validate_agent_up_sync_core_parity_expansion.sh [--gate red-baseline|source-parity|conflict-packet|source-provenance|unpublished-range|stale-worker-stack|stale-basis|bridge-handoff|cost|rollback|installed-live-canary|rust|all]
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
    Apps/control_center/backend/convergence/agent_up_sync_core_bridge.py \
    Apps/control_center/backend/convergence/agent_up_sync_engine.py \
    Apps/control_center/backend/convergence/sync_state_web.py \
    Apps/control_center/backend/convergence/operator_truth.py
  cargo check -p agent-up-sync-core --all-features
}

run_rust() {
  cargo fmt --check
  cargo clippy -p agent-up-sync-core --all-targets --all-features -- -D warnings
  cargo test -p agent-up-sync-core --all-features
}

run_source_parity() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave19/test_agent_up_sync_core_parity_expansion.py
}

run_selector() {
  local selector="$1"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave19/test_agent_up_sync_core_parity_expansion.py \
    -k "$selector"
}

run_bridge_handoff() {
  run_selector "topology_handoff or sync_state_web or conflict_packet"
}

run_cost() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave19/test_agent_up_sync_core_parity_expansion.py \
    -k "cost or sync_state_web or topology_handoff"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave11/test_agent_up_sync_performance_budget_contract.py \
    -k cost
}

run_rollback() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave16/test_agent_up_sync_core_transaction_candidate.py \
    -k "python_fallback"
}

run_installed_live_canary() {
  local installed_agent_up="${AGENT_UP_SCENARIO_AGENT_UP_BIN:-$HOME/.local/bin/agent-up}"
  [[ -x "$installed_agent_up" ]] || {
    echo "ERROR: installed agent-up is not executable: $installed_agent_up" >&2
    exit 1
  }

  local runtime_file="/tmp/agent-up-pce75-runtime-verify.json"
  "$installed_agent_up" runtime verify --json --brief >"$runtime_file"
  python3 - "$runtime_file" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
if payload.get("safe_to_continue") is not True:
    raise SystemExit("runtime verify is not safe_to_continue")
if payload.get("runtime_cutover_required") is True:
    raise SystemExit("runtime verify still requires cutover")
if payload.get("runtime_stage_content_current") is not True:
    raise SystemExit("runtime stage content is not current")
PY

  local control_root="${AGENT_UP_CONTROL_CENTER_ROOT:-$HOME/control-center}"
  [[ -d "$control_root" ]] || {
    echo "ERROR: control root is not present for installed canary: $control_root" >&2
    exit 1
  }
  (cd "$control_root" && cargo build -p agent-up-sync-core >/dev/null)

  local probe_file="/tmp/agent-up-pce75-installed-sync-probe.json"
  "$installed_agent_up" sync --probe --json >"$probe_file"
  python3 - "$probe_file" <<'PY'
import json
import re
import sys
from collections.abc import Mapping, Sequence

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
read = payload.get("sync_core_read_authority")
if not isinstance(read, Mapping):
    raise SystemExit("installed sync probe did not include sync_core_read_authority")
snapshot = payload.get("sync_state_web_snapshot")
if not isinstance(snapshot, Mapping):
    raise SystemExit("installed sync probe did not include sync_state_web_snapshot")

required_read_fields = (
    "topology_authority",
    "topology_summary",
    "conflict_packet_authority",
    "continuation_eligibility",
    "source_provenance",
    "no_local_commit_meaning",
    "unpublished_range_shape",
    "unpublished_range",
    "range_age_class",
    "authority_surface_overlap",
    "generated_surface_policy",
    "stale_worker_stack_guard",
    "live_basis_state",
    "sync_group_basis_state",
    "live_basis",
    "python_post_rust_graph_recompute_count",
    "cost_split",
)
missing = [field for field in required_read_fields if field not in read]
if missing:
    raise SystemExit(f"installed sync read authority missing PCE75 fields: {missing}")
if read.get("python_post_rust_graph_recompute_count") != 0:
    raise SystemExit("Python recomputed graph archaeology after Rust handoff")
if read.get("worker_raw_jj_guidance") is not False:
    raise SystemExit("read authority leaked raw JJ worker guidance")

required_snapshot_fields = (
    "source_provenance",
    "no_local_commit_meaning",
    "unpublished_range_shape",
    "unpublished_range",
    "range_age_class",
    "authority_surface_overlap",
    "generated_surface_policy",
    "stale_worker_stack_guard",
    "live_basis_state",
    "sync_group_basis_state",
    "live_basis",
    "python_post_rust_graph_recompute_count",
    "cost_split",
)
missing_snapshot = [field for field in required_snapshot_fields if field not in snapshot]
if missing_snapshot:
    raise SystemExit(f"sync-state web snapshot missing PCE75 fields: {missing_snapshot}")
if snapshot.get("python_post_rust_graph_recompute_count") != 0:
    raise SystemExit("sync-state web reports Python graph recomputation after Rust handoff")
if snapshot.get("worker_raw_jj_transfer_allowed") is not False:
    raise SystemExit("sync-state web permits raw JJ worker transfer")

raw_jj = re.compile(r"(^|[;&|]\s*)jj(\s|$)")
def commands(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"command", "next_command", "next_exact_command", "continue_command", "after_resolving_files_command"}:
                yield from commands(item)
            elif isinstance(item, (Mapping, Sequence)) and not isinstance(item, (str, bytes)):
                yield from commands(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from commands(item)

leaked = [command for command in commands(payload) if raw_jj.search(command)]
if leaked:
    raise SystemExit(f"installed sync probe leaked raw JJ guidance: {leaked}")
PY
  run_source_parity
  AGENT_UP_SCENARIO_AGENT_UP_BIN="$installed_agent_up" \
    PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "${pytest_cmd[@]}" \
    Apps/control_center/tests/wave19/test_agent_up_sync_core_parity_expansion.py \
    -k "installed_agent_up_sync_ab_conflict"
}

case "$gate" in
  red-baseline|source-parity)
    run_static
    run_source_parity
    ;;
  conflict-packet)
    run_static
    run_selector "conflict_packet"
    ;;
  source-provenance)
    run_static
    run_selector "source_provenance or no_local_commit"
    ;;
  unpublished-range)
    run_static
    run_selector "stale_worker_stack or sync_state_web"
    ;;
  stale-worker-stack)
    run_static
    run_selector "stale_worker_stack"
    ;;
  stale-basis)
    run_static
    run_selector "stale_basis"
    ;;
  bridge-handoff)
    run_static
    run_bridge_handoff
    ;;
  cost)
    run_static
    run_cost
    ;;
  rollback)
    run_static
    run_rollback
    ;;
  installed-live-canary)
    run_static
    run_installed_live_canary
    ;;
  rust)
    run_static
    run_rust
    ;;
  all)
    run_static
    run_rust
    run_source_parity
    run_bridge_handoff
    run_cost
    run_rollback
    run_installed_live_canary
    ;;
  *)
    echo "ERROR: unsupported gate: $gate" >&2
    exit 2
    ;;
esac
