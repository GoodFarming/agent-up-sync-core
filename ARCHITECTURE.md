# Architecture

`agent-up-sync-core` is a small Rust library plus a thin CLI wrapper.

## Boundary

Callers provide a single structured request for the selected workspace, live
target, sync-group context, runtime relevance, and feature flags. The core opens
one repository snapshot through a `JjAdapter`, computes the state web, and
returns one structured response. Agent-Up then renders receipts and applies
policy.

## Adapter Isolation

JJ access is hidden behind `JjAdapter`. The current adapter can use the JJ CLI;
future `jj-lib` support must stay behind the same trait. Public request/response
schemas must not expose JJ internal types.

## Authority Progression

1. `rust_shadow`: observation only.
2. `rust_read_authoritative`: graph/conflict/source orientation.
3. `rust_mutation_authoritative`: narrow journaled mutation classes.
4. `rust_transaction_candidate`: full transaction candidate behind feature flag.
5. rollout lock: installed canary plus rollback/fallback proof.
