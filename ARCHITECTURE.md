# Agent-Up JJ Sync Core Architecture

`agent-up-sync-core` is a small Rust library plus a thin CLI wrapper.

## Boundary

Callers provide a single structured request for the selected workspace, live
target, sync-group context, runtime relevance, and feature flags. The core opens
one repository snapshot through a `JjAdapter`, computes the state web, and
returns one structured response. Agent-Up then renders receipts and applies
policy.

## Adapter Isolation

JJ access is hidden behind `JjAdapter`.

- `CliJjAdapter` preserves the subprocess fallback path.
- `JjLibAdapter` is read-only and opens one in-process JJ workspace/repo
  snapshot per sync-core request.
- Public request/response schemas expose adapter identity, compatibility,
  counters, provenance, and degraded/fallback state, but not JJ internal types.
- `adapter_profile=jj-lib` must report `adapter_subprocess_count=0` and
  `adapter_jj_command_count=0` for read orientation.
- Unsupported repo format, corrupt working copy, missing operation state, and
  conflict states that cannot be represented as a materialized packet return a
  typed degraded response with Python fallback.

## Authority Progression

1. `rust_shadow`: observation only.
2. `rust_read_authoritative`: graph/conflict/source orientation.
3. `rust_mutation_authoritative`: narrow journaled mutation classes.
4. `rust_transaction_candidate`: full transaction candidate behind feature flag.
5. rollout lock: installed canary plus rollback/fallback proof.

## Topology Authority

Read and transaction responses carry a single Rust-authored topology authority
block. It classifies conflict packet ownership, continuation eligibility,
source provenance, unpublished range shape and age, generated surface policy,
live/sync-group basis state, unsupported topology, and Rust/Python cost split.

Python may render and compact this metadata, but it must not re-run JJ graph
archaeology after Rust has returned an authoritative topology block.
