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
