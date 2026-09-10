# Reproduction-only replay API

POST `/v1/findings/{finding_id}/replay` accepts optional `reproduction_only` (default false).

With `reproduction_only: true`, the server uses the source campaign's immutable target/task and recorded runtime conditions. It retains source finding/episode lineage and attacker configuration. Exact replay uses the recorded episode; nearby search requests source-seeded mutation. Neither mode associates a new policy with the finding nor creates a hardening run.

An explicit conflicting `policy_version_id` produces HTTP 422 before mutations. Existing callers omitting the flag retain the prior API behavior. No schema migration is required.

The refactored AML UI always sends this flag. Deploy both corrected source components together before using reproduction from that UI.
