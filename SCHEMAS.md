# Schemas

The stable public boundary is JSON.

## Request

Schema id: `control-center.agent-up.sync-core.request.v0.1`

Required classes:

- transaction, correlation, and idempotency ids;
- workspace, repo, live target, and sync group identifiers;
- requested operation and engine mode;
- Python context carrying selected workspace, source state, live target,
  runtime context, and optional conflict context;
- feature flags;
- recovery journal path.

## Response

Schema id: `control-center.agent-up.sync-core.response.v0.1`

Required classes:

- authority mode and decision class;
- state-machine trace over workspace, source, target/live, conflict, mutation,
  and output axes;
- provenance;
- conflict packet candidate;
- mutation plan and journal record when mutation is authorized;
- next Agent-Up action;
- fallback, telemetry, degraded reason, confidence, reason codes, and decision
  drivers.

Schema changes require semver review and Python/Rust differential tests.
