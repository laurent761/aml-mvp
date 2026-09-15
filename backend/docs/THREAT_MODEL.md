# Capsule and Customer Deployment Threat Model

## Security objective

Run an opaque, potentially hostile agent artifact without allowing it to create real-world effects, reach customer production systems, observe verifier-private state, or modify the trusted Blue enforcement boundary.

This threat model covers the Docker Compose research profile and the interface to a future audited Firecracker/KVM runner. It does not claim that Docker provides a hostile multi-tenant boundary equivalent to a microVM.

Reviewed for the INF-01–INF-12 implementation. See [current status](../../PROJECT_STATUS.md)
and [research access and operations](RESEARCH_INTEGRATION.md).

## Trust boundaries

| Component | Trust level | Authority |
|---|---|---|
| Target OCI image | Untrusted | Capsule-local compute and its synthetic inputs only |
| Blue gateway | Trusted data plane | Evaluate policy and route effects to virtual state |
| Capsule supervisor | Highly trusted control plane | Docker or remote-runner lifecycle operations |
| Target inference broker | Trusted, supervisor-owned | Restricted target requests to the pinned model endpoint; provider credentials and usage reservations |
| Campaign worker | Trusted orchestrator | Campaign state, public observations, reward, budgets |
| Research worker / registered attacker runtime | Trusted execution owner / operator-approved external service | Durable commands and public-context generation; runtime weight identity is attested, not independently inspected |
| Verifier | Trusted/private | Synthetic-world events and terminal evidence |
| PostgreSQL | Trusted persistence | Operational state and immutable event metadata |
| MinIO | Trusted persistence | Evidence and experiment artifacts |
| MLflow and telemetry | Trusted metadata sinks | Sanitized metrics, versions, references, and traces |
| Research API client | Authenticated, owner-scoped researcher | Sessions, runs, datasets and artifacts subject to research/evaluation/evidence scopes |
| Operator API client | Authorized operator | Target, runtime, suite, campaign, task and legacy policy administration |

The Docker socket is equivalent to root-level control of the Docker host. Only the capsule-supervisor service receives it. The target, Blue gateway, worker, API, MLflow, and telemetry collector must never receive the socket.

## Adversary capabilities

Assume the target image can:

- Run arbitrary code as its image user
- Send malformed or oversized protocol messages
- Attempt prompt injection, policy bypass, and confused-deputy attacks
- Probe its network and local container environment
- Attempt DNS, metadata, private-network, host-filesystem, socket, and process escape paths
- Crash, hang, fork, allocate memory, or consume its execution budget
- Fabricate visible results and effect identifiers
- Attempt to infer hidden verifier or policy state through timing and error differences

Also assume a registered manifest may be malicious. Absolute target URLs must not turn the trusted worker into an SSRF client. OCI tags may move and therefore are not reproducible identities; customer campaigns should launch a verified digest.

## Required controls

### Target boundary

- One internal network per episode
- No host network, host mounts, Docker socket, external DNS, or real credentials
- Read-only root filesystem, dropped capabilities, `no-new-privileges`, PID/memory/CPU/time budgets
- Capsule-local target invocation and health paths only
- Network path only to the episode Blue gateway
- Target inference only through Blue's scoped mailbox and the supervisor broker; no generic outbound proxy
- Image digest verification before launch
- Deterministic teardown on every exit path

### Blue and verifier boundary

- Blue runs outside the target process and image
- Every MCP/HTTP consequence enters through authenticated, episode-scoped ingress
- Campaign policy is loaded by immutable version
- `ALLOW_REAL` is rejected in capsule mode
- Virtual services contain synthetic state only
- Private verifier events are never included in attacker-model context
- Inference and business-tool capabilities have separate scopes; provider credentials stay outside target and Blue
- Intervention overlays affect declared public content only; trusted virtual effects and verifier events remain authoritative
- Effect attempts, decisions, state events, and evidence retain campaign/episode/step lineage

### Control plane

- Supervisor lifecycle endpoints require a constant-time bearer-token check
- Supervisor concurrency is bounded
- Worker calls the supervisor; it does not receive Docker authority
- Customer-provided endpoint values are normalized to capsule-local routes
- PostgreSQL, MinIO, supervisor, and Blue are not publicly exposed
- Supervisor-owned containers and networks are labeled by capsule, episode, and role
- Supervisor startup removes labeled resources orphaned by a prior process crash
- Runtime network-boundary checks are persisted as sanitized immutable episode evidence
- Public API ingress is loopback-only by default; remote use requires configured application bearer authentication and deployment-managed TLS
- `/v1/*` routes enforce bearer authentication when configured; legacy administration requires operator scope
- Research records enforce ownership and scopes; admission uses atomic owner quota reservations
- Production API startup requires research authentication; per-process rate limits need shared ingress enforcement when scaled
- Uncertain in-flight research actions are marked indeterminate and retired, without automatic replay
- Development signing keys are rejected outside development

### Persistence and telemetry

- Evidence objects have recorded size and SHA-256 and are verified on retrieval
- Database and object storage are backed up consistently
- Logs are structured and exclude bodies, prompts, authorization headers, credentials, and raw secrets
- Secret-like MLflow parameters are hashed; experiment payloads remain bounded
- OTLP export uses a customer-approved destination and transport security outside the local profile

## Firecracker boundary

`CAPSULE_RUNTIME=firecracker` means the local supervisor delegates to an external runner implementing the same lifecycle contract. This repository does not contain a jailer, kernel/rootfs builder, network filter, snapshot manager, or KVM configuration and therefore does not claim Firecracker isolation is validated.

Before pilot use, independently validate:

- Dedicated jailer identity and filesystem
- Kernel and rootfs provenance
- Tap/network namespace egress policy
- Metadata and host-service denial
- vCPU, memory, disk, process, and wall-time limits
- vsock/control-channel authentication
- Snapshot and tenant-state separation
- Cleanup after host/process crashes
- Audit logging without guest secret exposure

## Residual risks

- Docker shares the host kernel and is suitable only for the internal research profile.
- A Docker-socket compromise of the supervisor compromises the Docker host.
- A vulnerable Blue parser or virtual service could cross the trusted boundary.
- In-memory episode state may be lost on an abrupt Blue restart unless events are durably reconstructed.
- Traffic and timing may reveal limited policy information even when verifier state is private.
- Default local Compose credentials are public development values.
- The included OpenTelemetry debug exporter is not durable storage.
- Local development permits an implicit operator when authentication is disabled and no tokens are configured; remote deployments must enable the implemented bearer controls.
- Token distribution/rotation, TLS, aggregate request-rate enforcement and external provider security remain deployment responsibilities.
- Abrupt supervisor/host loss can interrupt in-memory inference before full audit persistence; durable research recovery cannot prove provider cancellation or reconstruct missing provider usage.
- Stored checkpoint integrity does not prove a serving runtime loaded those weights; external training and GPU usage are reported by the researcher.

## Security release gates

A customer-side release requires zero known containment violations in real runtime tests, a completed cleanup inventory after fault injection, dependency and image provenance review, secret rotation, backup/restore evidence, and explicit acceptance of all remaining residual risks. Mocked Docker command tests are necessary unit coverage but are not containment proof.
