# Benchmarks

Run:

```bash
cargo bench --bench sync_transaction_classes -- --test
```

Budget classes:

- clean noop: under 250 ms;
- dirty preflight: under 1000 ms;
- conflict packet: under 2000 ms;
- degraded large case: under 5000 ms with typed degradation.

The Rust-era budget is latency, memory, repo lock time, output size, and
degraded-state quality. It is not a raw JJ command-count budget.

Adapter efficiency proof:

- CLI parity path may report adapter subprocess/JJ-command counts above zero.
- `jj-lib` read orientation must report `adapter_subprocess_count=0`,
  `adapter_jj_command_count=0`, and `repo_snapshot_count=1`.
- Performance gates compare Rust/CLI and Rust/`jj-lib` by transaction class and
  treat zero adapter subprocesses plus budget-pass latency as the first
  material read-path improvement.
