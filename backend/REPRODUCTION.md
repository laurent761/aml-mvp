# Reproduction APIs

AML supports research-episode reproduction and the legacy finding replay used by
the operator UI. A verifier-backed finding and a successful fresh reproduction are
separate facts; seeds do not guarantee identical hosted-model responses.

## Research episodes

`POST /v1/research-episodes/{episode_id}/reproduce` creates a fresh pinned replay
session and returns HTTP 202. Supply a bearer token with `evaluation` or `operator`
scope, `X-AML-API-Version: aml.research.v1`, and an `Idempotency-Key`. The source must
belong to that owner, have finished executing, and contain completed step actions.

The worker replays those actions with the source bundle, policy, seed and limits in
a fresh episode. Read `GET /v1/research-sessions/{session_id}/reproduction` for progress,
success and observation divergence. Uncertain actions are not recovered by blindly
resubmitting them. See [research integration](docs/michelle_archive/RESEARCH_INTEGRATION.md) and the
[SDK](../sdk/README.md) for recovery and evaluation contracts.

## Legacy findings and the operator UI

`POST /v1/findings/{finding_id}/replay` requires operator access when authentication
is enabled and accepts:

```json
{
  "reproduction_only": true,
  "search_nearby_bypasses": false
}
```

With `reproduction_only: true`, the server preserves the source campaign's immutable
target/task, original policy and attacker configuration, and retains finding/episode
lineage. Exact replay uses recorded actions. Setting `search_nearby_bypasses: true`
requests adaptive, source-seeded mutation. Neither reproduction-only mode associates
a new policy with the finding or creates a hardening run.

An explicit conflicting `policy_version_id` returns HTTP 422 before mutations. The
response includes `replay_campaign_id`, mode, source episode and execution contract.
The flag defaults to `false` for compatibility: legacy callers can still request
policy replay/hardening behavior. The current AML UI always sends `true`.

Both contracts are implemented in this checkout. Deploy matching UI/backend source;
older backends may reject the reproduction-only flag. No additional migration is
needed for that flag; current research records require migrations through `0005`.
