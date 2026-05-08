# Release Notes

## 0.1.0

Initial Agent-Up JJ Sync Core kernel scaffold:

- library API and thin CLI;
- JJ adapter trait with CLI adapter;
- schema-validated request/response;
- shadow, read-authority, guarded-mutation, and transaction-candidate modes;
- conflict packet candidates;
- transaction journals;
- fallback and rollback-ready rollout receipts;
- benchmark classes for clean, dirty, and conflict transactions.

The crate is not published to crates.io yet. `publish = false` remains in
`Cargo.toml` until external packaging is deliberately approved.

## GitHub Mirror Export

Control Center remains the source authority. Publish/update the sibling Git
mirror with:

```bash
python Apps/control_center/scripts/publish/export_agent_up_sync_core.py \
  --target /home/adam/publish/agent-up-jj-sync-core \
  --apply \
  --init-git \
  --validate
```

The exporter refuses nested targets under Control Center, writes
`upstream/SOURCE.json`, generates the standalone `Cargo.lock`, and installs the
mirror CI workflow.
