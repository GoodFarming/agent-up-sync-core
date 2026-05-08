# agent-up-sync-core

`agent-up-sync-core` is a library-first Rust convergence kernel for JJ-backed
workspaces. Agent-Up is the first caller, but the crate boundary is intentionally
usable by other systems that need fast repository orientation, source
provenance, conflict classification, and journaled safe mutation planning.

## Current Authority

- public worker command remains `agent-up sync`;
- Python remains UX, policy, receipt rendering, runtime install, planning
  integration, feature flags, and fallback;
- Rust owns schema-checked shadow, read-authority, guarded-mutation, and
  transaction-candidate decisions only when the caller requests those modes;
- transaction-candidate executor mode is an explicit flag that can perform the
  resolved-conflict fold/publish path in disposable/canary-safe contexts and
  reports `mutation_performed`, before/after operation ids, published revision
  reachability, and idempotency replay state;
- Python fallback is required in every mode;
- no JJ internals are exposed in the public schema.

## API Shape

The library accepts one `SyncCoreRequest` and returns one `SyncCoreResponse`.
The CLI binary is a thin JSON wrapper around the same library call.

Key response families:

- state machine trace: workspace, source, target/live, conflict, mutation, output;
- provenance: workspace/source/live revisions and sync group id;
- conflict packet candidate: base/live/worker side context and path classes;
- mutation plan and journal: present only for authorized mutation modes;
- telemetry: latency, memory/output estimates, repo lock time, graph facts, and
  degraded-state reason;
- fallback: receipt-visible Python fallback command and reason.

## Safety Model

Rust may classify deeply and apply deterministic generated-surface policy, but it
does not invent semantic merge intent. Unresolved semantic conflicts must be
materialized for the caller. Mutation authority requires a preflight plan,
journal record, protected source revision, recovery handle, before/after op ids,
and idempotency key.

## Validation

Standalone crate validation:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-features
cargo bench --bench sync_transaction_classes -- --test
```

Control Center upstream validation:

```bash
bash Apps/control_center/scripts/validation/validate_agent_up_sync_core_rust_transaction_kernel.sh --gate rollout
```

See `ARCHITECTURE.md`, `SCHEMAS.md`, `SAFETY.md`, `EXAMPLES.md`,
`BENCHMARKS.md`, `RELEASE.md`, and `MSRV.md` for the open-source readiness
packet.
