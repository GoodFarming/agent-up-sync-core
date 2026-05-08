# Agent-Up JJ Sync Core

Agent-Up JJ Sync Core is a Rust convergence kernel for JJ-backed workspaces.
It is designed to answer one hard question quickly and safely:

> Given a selected workspace and a target live/root state, what is the exact
> safe sync state, what conflicts exist, what can be resolved deterministically,
> and what should the caller do next?

Agent-Up is the first caller. The crate boundary is intentionally library-first
so other systems can also use it for fast repository orientation, source
provenance, conflict classification, conflict packet generation, and journaled
safe mutation planning.

The current Cargo package and compatibility binary are still named
`agent-up-sync-core`. The public project name is `Agent-Up JJ Sync Core` because
the core is specifically about JJ workspace convergence.

## Why It Exists

Agent-Up manages many disposable worker workspaces converging into one live
root. The public worker contract is deliberately simple: ordinary agents run
`agent-up sync`; they should not become JJ operators.

Python can orchestrate that contract, but rich sync orientation through repeated
JJ CLI subprocesses is expensive and hard to reason about:

- each graph question can reopen the repo;
- subprocess startup and JSON/text parsing dominate routine paths;
- conflict side-context is easy to under-project;
- fallback and provenance can become ambiguous;
- internal JJ cost can stay high even when worker guidance is clean.

This Rust core exists to move the expensive, algorithmic part into one
transaction boundary: one request, one repo/workspace snapshot, one structured
decision.

## Advantages

- **Fast orientation**: `jj-lib` read mode opens one in-process snapshot instead
  of shelling out for every graph fact.
- **Explicit state machine**: workspace, source, live target, conflict,
  mutation, and output states are all represented in the response.
- **Richer conflict packets**: the core can assemble base/live/worker side
  context and classify semantic, generated, metadata, and mixed conflicts.
- **Safe authority progression**: shadow/read authority comes before guarded
  mutation, and Python fallback remains visible.
- **Journal-first mutation model**: mutation modes require protected revisions,
  recovery handles, before/after operation ids, and idempotency keys.
- **Agent-friendly receipts**: callers can expose one clear next action without
  leaking raw JJ commands to ordinary workers.
- **Reusable boundary**: the library has no UI dependency and no Agent-Up-only
  schema assumptions beyond the JSON request/response contract.

## How It Fits Agent-Up

In the full Agent-Up system:

- `agent-up sync` remains the public command;
- Python remains UX, policy, receipt rendering, runtime install, workpack
  integration, feature flags, and fallback;
- Rust owns schema-checked sync classification and, in guarded modes, narrow
  mutation planning/execution;
- runtime activation is separate from source publication;
- receipts must say whether the path was `rust_shadow`,
  `rust_read_authoritative`, `rust_transaction_candidate`, or `python_fallback`.

The first major efficiency target is read authority:

```text
CONTROLCENTER_SYNC_CORE_ADAPTER=jj-lib agent-up sync --probe --brief --json
```

For read orientation, `adapter_profile=jj-lib` must report:

- `adapter_subprocess_count=0`;
- `adapter_jj_command_count=0`;
- `repo_snapshot_count=1`;
- `parity_state=matched` or a typed degraded/fallback state.

## How The Design Emerged

The core came out of real multi-agent sync incidents:

- workers resolving materialized semantic conflicts still needed manual JJ fold
  operations;
- generated registry/artifact conflicts needed deterministic policy;
- `no_local_commit` was not the bug, ambiguous source provenance was;
- clean receipts could hide high internal JJ cost;
- agents needed conflict side-context, not raw VCS archaeology.

The design response was:

1. define a Python corpus as the behavioral constitution;
2. add a schema-first Rust/Python boundary;
3. run Rust in shadow mode before authority;
4. promote read-path authority before mutation;
5. require a recovery journal before mutation;
6. measure latency, repo lock time, memory, output size, and fallback quality
   instead of only counting JJ commands.

## Current Capabilities

- JSON request/response schema.
- Library API plus thin CLI wrapper.
- `CliJjAdapter` fallback.
- Read-only `JjLibAdapter` behind the `jj-lib-adapter` feature.
- Shadow and read-authority classifications.
- Generated/semantic conflict classification inputs.
- Conflict packet candidate output.
- Guarded mutation and transaction-candidate planning surfaces.
- Transaction candidate support for controlled resolved-conflict continuation
  paths in Agent-Up canaries.
- Performance budget telemetry.

## Deploying It

### Standalone Rust

Build and test:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-features
cargo bench --bench sync_transaction_classes -- --test
```

Run the CLI with a JSON `SyncCoreRequest` on stdin:

```bash
agent-up-sync-core < request.json
```

Use the default CLI adapter for compatibility, or compile with `jj-lib` support:

```bash
cargo build --no-default-features --features jj-lib-adapter
```

### Inside Agent-Up

Agent-Up calls the binary once per sync transaction. A typical read-authority
activation uses:

```bash
CONTROLCENTER_SYNC_CORE_PREFLIGHT_READ_AUTHORITY=1 \
CONTROLCENTER_SYNC_CORE_ADAPTER=jj-lib \
agent-up sync --probe --brief --json
```

The caller should keep Python fallback enabled until the local incident corpus
and live canaries prove parity for the relevant sync classes.

## Safety Model

Rust may classify deeply and apply deterministic generated-surface policy. It
does not invent semantic merge intent. Unresolved semantic conflicts must be
materialized for the caller.

Mutation authority requires:

- a preflight plan;
- a journal record;
- protected source revision evidence;
- before/after operation ids;
- a recovery handle;
- an idempotency key;
- fallback or rollback visibility.

## When Not To Use It

This is not a replacement for JJ, Git, or human semantic judgment. It is a
sync-orientation and convergence kernel. It is most useful when you need a
structured decision about a JJ workspace and a live/root target, especially in
multi-agent or automation-heavy systems.

## Validation

Prerequisites: Rust 1.89+ and `jj` 0.40.0 on `PATH` for CLI adapter parity
tests. The `jj-lib` adapter is pinned to `jj-lib = 0.40.0`.

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
bash Apps/control_center/scripts/validation/validate_agent_up_sync_core_rust_transaction_kernel.sh --gate jj-lib-read-adapter
```

See `ARCHITECTURE.md`, `SCHEMAS.md`, `SAFETY.md`, `EXAMPLES.md`,
`BENCHMARKS.md`, `RELEASE.md`, and `MSRV.md` for the open-source readiness
packet.
