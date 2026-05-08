# Examples

## Read Authority

```json
{
  "engine_mode_requested": "rust_read_authoritative",
  "requested_operation": "classify",
  "mutation_allowed": false
}
```

Expected result: one response with conflict/source/live orientation, no mutation
plan, no journal record, and Python fallback available.

## Transaction Candidate

```json
{
  "engine_mode_requested": "rust_transaction_candidate",
  "requested_operation": "sync_transaction",
  "mutation_allowed": true,
  "feature_flags": {
    "rust_sync_core_enabled": true,
    "rust_sync_core_transaction_candidate": true
  }
}
```

Expected result: a full transaction candidate plan covering prepare, retry,
publish, refresh, and fold, plus a journal record and rollback handle.
