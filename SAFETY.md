# Safety Model

The core is designed to fail closed.

Rules:

- no mutation without `mutation_allowed=true`;
- no mutation without journal, protected source, recovery handle, and
  idempotency key;
- no arbitrary semantic auto-merge;
- generated policy must be explicit and path-classified;
- stale worker stacks with broad authority-surface overlap must block before
  mutation;
- stale live or sync-group basis must fail closed with an explicit Agent-Up
  refresh/sync action;
- fallback must remain receipt-visible;
- raw JJ commands are internal adapter detail, not caller guidance;
- degraded states must include reason codes and the next Agent-Up action.

Crash/retry behavior is validated by replaying the same idempotency key against
the journal. A rerun may complete, roll forward, or block with evidence; it must
not hide or abandon source work.
