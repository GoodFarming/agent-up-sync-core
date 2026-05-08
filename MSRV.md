# MSRV

Minimum supported Rust version: 1.89.

The crate declares this through `rust-version = "1.89"` in `Cargo.toml`.
This is required by the pinned `jj-lib = 0.40.0` read adapter.

Compatibility matrix:

- Rust: 1.89 minimum, validated locally with Rust 1.91.
- JJ CLI parity target: `jj 0.40.0`.
- `jj-lib`: pinned to `=0.40.0`.
- Repo format: git/simple commit stores, `simple_op_store`,
  `simple_op_heads_store`, default index, local working copy.
- Platform posture: Linux validated first; macOS/Windows remain CI targets for
  public mirror rollout.
- License: Apache-2.0 crate with Apache-2.0 `jj-lib` dependency.
- Mismatch behavior: typed degraded response with receipt-visible Python
  fallback; no raw `jj` guidance is emitted by sync-core.
