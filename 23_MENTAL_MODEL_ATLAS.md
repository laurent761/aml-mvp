# AML MVP Mental Model Atlas

This atlas preserves the conceptual models from the original numbered design
documents. The `01_…`–`22_…` headings identify specification topics; those individual
source documents are not included in this checkout.

**Implementation note (2026-09-14):** these diagrams combine delivered architecture
and proposed research/product behavior. Use [current project status](PROJECT_STATUS.md)
for shipped scope and [the interactive guide](architecture-guide.html) for the current
SDK/session workflow. One Python finance target with fixture/model modes is supplied.
INF-01–INF-12 infrastructure is implemented; real-model acceptance and benchmark
improvement remain unproven. Specific differences are identified below.

---

## 01_MVP_PRODUCT_DEFINITION.md

### Mental model — what the product is

```mermaid
flowchart TD
    U["User / researcher"] --> C["Attack Campaign"]
    T["Target artifact + interface manifest"] --> C
    S["Scenario + environment version"] --> C
    B["Hard budgets"] --> C

    C --> X["Experiments"]
    X --> R["Trajectories"]
    R --> F{"Private forbidden-state predicate"}
    F -- "not reached" --> L["Learning from failure / near miss"]
    F -- "reached" --> V["Verifier-backed finding"]
    V --> REP["Separate fresh reproduction result"]
    V --> L
    L --> X

    X --> O["Outputs"]
    O --> O1["Campaign summary"]
    O --> O2["Evidence + replay"]
    O --> O3["Attack-family statistics"]
    O --> O4["Cost / token accounting"]

    N["Not this MVP"] --> N1["Governance / compliance posture"]
    N --> N2["Static scanner"]
    N --> N3["Production attacks"]
    N --> N4["Custom RL training"]
```

**Mental shortcut:** AML runs controlled experiments against a sealed target and
checks a falsifiable forbidden-state objective. Search can reuse experience;
benchmark improvement and reproduction require separate evidence.


---

## 02_SYSTEM_ARCHITECTURE.md

Current routing: the worker reaches the target through the supervisor's trusted
Blue proxy. Blue and the target are the two containers on the capsule's internal
network; the safety boundary below is a trust distinction. Research results expose
outcomes/rewards separately; automatic attacker-model context receives public data only.

### Mental model — runtime architecture and trust boundaries

```mermaid
flowchart LR
    subgraph Control["Control Plane"]
      API["API / Orchestrator"]
      DB["PostgreSQL"]
      OBJ["Artifacts / Object Store"]
    end

    subgraph RedZone["Red Boundary"]
      RED["Red Engine<br/>planner + search + mutation"]
      MG["Model Gateway"]
    end

    subgraph Capsule["Sealed Target Capsule"]
      TARGET["Opaque Target Agent"]
    end

    subgraph Safety["Trusted Safety Boundary"]
      GW["Safety Gateway"]
      WORLD["Virtual Services / World"]
      VER["Private Verifiers"]
    end

    API --> RED
    RED --> TARGET
    TARGET --> GW
    GW --> WORLD
    WORLD --> VER
    VER --> API
    TARGET --> RED
    RED --> MG
    API --> DB
    API --> OBJ

    VER -. "trusted search accounting only; not model context" .-> RED
    WORLD -. "private state never exposed" .-> VER
    GW -. "blocks real-world effects" .-> TARGET
```

**Mental shortcut:** Red can see only the same public surface an attacker would see. The target is treated as hostile. Only the trusted Safety/Verifier layer can see private world state, and that layer turns consequences into reward and terminal proof.


---

## 03_TECHNICAL_STACK_AND_REPOSITORY.md

### Mental model — how the MVP should be engineered

```mermaid
flowchart TD
    P["Research velocity first"] --> M["Python modular monolith"]
    M --> API["FastAPI control API"]
    M --> WORK["Async campaign / experiment workers"]
    M --> RED["Red/search modules"]
    M --> ENV["World + verifier modules"]
    M --> SAFE["Runtime / gateway adapters"]

    API --> PG["PostgreSQL"]
    WORK --> PG
    WORK --> OBJ["Object storage"]
    RED --> MODEL["Model provider via Model Gateway"]
    WORK --> CONT["Docker first<br/>Firecracker later"]

    B["Replaceable interfaces"] --> B1["Model provider"]
    B --> B2["Capsule runtime"]
    B --> B3["Queue"]
    B --> B4["Artifact store"]
    B --> B5["Search policy"]

    X["Avoid prematurely"] --> X1["Microservices"]
    X --> X2["Kafka"]
    X --> X3["Kubernetes dependency"]
    X --> X4["Custom distributed RL infra"]
```

**Mental shortcut:** optimize for iteration speed without creating architectural dead ends. Keep one Python codebase, but make the expensive boundaries replaceable so scale-out can happen later without rewriting the domain model.


---

## 04_DOMAIN_MODEL_AND_CONTRACTS.md

### Mental model — the vocabulary that keeps every component aligned

```mermaid
flowchart LR
    TM["TargetManifest"] --> TV["TargetVersion"]
    CS["AttackCampaignSpec"] --> C["Campaign"]
    TV --> C
    C --> E["Experiment"]

    E --> A["RedAction"]
    A --> O["PublicObservation"]
    O --> SR["StepRecord"]
    A --> FX["EffectAttempt"]
    FX --> SR
    V["VerifierSignal"] --> SR
    SR --> T["Trajectory"]
    T --> X["VerifiedExploit"]

    ENV["Environment API"] --> FX
    ENV --> V

    P["Visibility rule"] -.-> O
    P -. "Red sees public data only" .-> A
    P -. "Private state stays verifier-side" .-> V
```

**Mental shortcut:** these contracts are the spine of the platform. Red actions, public observations, effect attempts, verifier signals, and immutable step records are deliberately separated so hidden ground truth can never leak into attacker context.


---

## 05_TARGETS_AND_TARGET_CONTRACT.md

Implemented target endpoints are health, invoke and reset; research-session creation
belongs to the control API. Bundles declare `fixture` or `model` mode. Replay is a
platform operation, not a third target mode. No real-model benchmark result is recorded.

### Mental model — what makes any agent testable by AML

```mermaid
flowchart TD
    IMG["Immutable OCI target image"] --> MAN["Target manifest"]
    MAN --> API["Standard black-box API"]
    CONTROL["Control API"] --> S1["Create research session"]
    S1 --> API
    API --> S2["Invoke"]
    API --> S3["Reset"]
    API --> S4["Health / readiness"]

    API --> MODES["Execution modes"]
    MODES --> F["Fixture<br/>deterministic dev"]
    MODES --> L["Model<br/>operator-configured inference"]
    CONTROL --> R["Fresh reproduction / replay"]

    RESET["Reset invariant"] --> BASE["Same scenario baseline every episode"]
    BASE --> FAIR["Fair comparison + branch replay"]

    Q["Quality gate"] --> Q1["Benign utility works"]
    Q --> Q2["No hidden data leaks"]
    Q --> Q3["Deterministic reset"]
    Q --> Q4["Containment compatible"]
```

**Mental shortcut:** a target is not “a prompt.” It is a versioned, opaque runnable agent system with a stable invocation/reset contract, so many frameworks can be compared under identical experimental conditions.


---

## 06_TARGET_AGENTS_BENCHMARK.md

**Proposed benchmark design, not a shipped agent inventory.** Only the custom Python
`finance-invoice-summary` development scenario (1.1.0) is supplied. LangGraph/OpenAI
Agents SDK implementations, held-out variants and formal generalization evidence
remain future work. Simulator/verifier primitives do not establish coverage of every
benchmark case pictured below.

### Mental model — the benchmark world

```mermaid
flowchart TD
    W["Proposed Finance Operations benchmark world"] --> A["Planned multiple target implementations"]
    A --> A1["Custom Python"]
    A --> A2["LangGraph"]
    A --> A3["OpenAI Agents SDK"]

    W --> TOOLS["Shared realistic tool surface"]
    TOOLS --> T1["Customer / tenant data"]
    TOOLS --> T2["Vendor + invoice"]
    TOOLS --> T3["Approval + payment"]
    TOOLS --> T4["Email / communication"]
    TOOLS --> T5["Memory + documents"]

    W --> FS["Machine-verifiable forbidden states"]
    FS --> F1["F1 Unauthorized privileged payment"]
    FS --> F2["F2 Stale approval reused after change"]
    FS --> F3["F3 Cross-tenant disclosure"]
    FS --> F4["F4 Synthetic PII exfiltration"]

    A --> VAR["Variation axes / vulnerability profiles"]
    VAR --> SPLIT["Dev → Validation → Frozen test"]
    SPLIT --> CLAIM["Measure generalization, not memorization"]
```

**Mental shortcut:** keep the world constant and vary the agents. That isolates whether Red learns transferable adversarial behavior rather than overfitting to one framework, one prompt, or one hidden shortcut.


---

## 07_ATTACK_CAMPAIGNS_AND_ORCHESTRATION.md

This is the legacy campaign model. Durable research sessions add expected step
indices and operation idempotency; uncertain in-flight actions become `indeterminate`
and retire the episode. They are not retried by the conceptual failure arrow below.

### Mental model — how a campaign becomes reliable work

```mermaid
stateDiagram-v2
    [*] --> Created
    Created --> Queued
    Queued --> Running
    Running --> Completed: objective/budget finished
    Running --> Cancelling: user/system cancel
    Cancelling --> Cancelled
    Running --> Failed: unrecoverable failure
    Failed --> Queued: reclaim/retry if safe

    state Running {
      [*] --> ProvisionExperiment
      ProvisionExperiment --> ExecuteSteps
      ExecuteSteps --> RecordTrajectory
      RecordTrajectory --> Evaluate
      Evaluate --> ProvisionExperiment: more budget / search branches
      Evaluate --> [*]: stop condition
    }
```

```mermaid
flowchart LR
    B["Campaign hard budgets"] --> O["Orchestrator"]
    O --> Q["Durable work queue"]
    Q --> W["Workers with leases"]
    W --> E["Isolated experiments"]
    E --> R["Persist state + lineage"]
    R --> O

    B --> B1["episodes"]
    B --> B2["steps"]
    B --> B3["tokens / cost"]
    B --> B4["wall time"]
    B --> B5["concurrency"]
```

**Mental shortcut:** campaigns are durable budgeted jobs, not long HTTP requests. The orchestrator must survive crashes, cancel cleanly, reclaim expired work, and preserve enough state that experiments remain reproducible.


---

## 08_EXPERIMENTS_AND_EVALUATION.md

### Mental model — proving the attacker is actually better

```mermaid
flowchart TD
    Q["Same targets + same scenarios + matched budgets"] --> S["Compare attacker systems"]
    S --> B1["Static attack suite"]
    S --> B2["Naive LLM attacker"]
    S --> B3["Adaptive Red"]

    B3 --> ABL["Ablations"]
    ABL --> A1["No strategy memory"]
    ABL --> A2["No retrieval"]
    ABL --> A3["No mutation / branching"]
    ABL --> A4["Alternative reward"]

    B1 --> M["Metrics"]
    B2 --> M
    B3 --> M
    M --> M1["Attack success rate"]
    M --> M2["Unique forbidden states"]
    M --> M3["Cost / episodes / steps to success"]
    M --> M4["Reproduction rate"]
    M --> M5["Held-out learning lift"]
    M --> M6["Containment violations = 0"]

    M --> P{"Promotion gate passed?"}
    P -- "No" --> ITER["Keep current Red version"]
    P -- "Yes" --> PROM["Promote new Red version"]
```

**Mental shortcut:** evaluation is a controlled scientific comparison. Adaptive Red only “wins” if it beats simpler baselines under matched budgets and keeps that advantage on frozen held-out targets.


---

## 09_RED_INTELLIGENCE_ENGINE.md

Legacy campaigns include linear/adaptive search and strategy memory. Managed research
sessions use linear Red with an approved runtime and disable strategy memory.
External sessions accept actions from the researcher's SDK loop.

### Mental model — the attacker brain

```mermaid
flowchart LR
    OBS["Public trajectory context"] --> CTX["Context builder"]
    MEM["Retrieved strategies / prior experience"] --> CTX
    OBJ["Campaign objective + allowed channels"] --> CTX

    CTX --> PLAN["Planner / strategy selection"]
    PLAN --> GEN["Candidate generator"]
    GEN --> SCORE["Search controller scores candidates"]
    SCORE --> ACT["RedAction"]
    ACT --> TARGET["Opaque target"]
    TARGET --> NEW["Public observation"]
    NEW --> REWARD["Reward / progress signal"]
    REWARD --> UPDATE["Update search state"]
    UPDATE --> PLAN

    CH["Attacker channels"] --> GEN
    CH --> C1["Direct text"]
    CH --> C2["Documents"]
    CH --> C4["Declared tool-result replacement"]

    PRIVATE["Private verifier state"] -. "never enters Red context" .-> CTX
```

**Mental shortcut:** Red is a search system wrapped around one or more attacker models. The model proposes actions; the search controller decides what to try, what to branch, what to mutate, and how to spend the remaining budget.


---

## 10_SEARCH_REWARD_AND_LEARNING.md

The diagram describes legacy search memory. External training metadata, dataset
snapshots and checkpoint storage are implemented, but AML does not train weights.
Paired research evaluation disables cross-run strategy memory; no held-out learning
lift is claimed. There is no separate direct-memory-poisoning action channel.

### Mental model — how experience becomes better search

```mermaid
flowchart TD
    N["Search node<br/>trajectory prefix + state summary"] --> P["Priority score"]
    P --> X["Expand promising node"]
    X --> C["Generate / mutate candidates"]
    C --> RUN["Execute candidate"]
    RUN --> R["Reward signal"]

    R --> R1["terminal success"]
    R --> R2["forbidden-state progress"]
    R --> R3["novelty"]
    R --> R4["efficiency / cost"]

    R --> ST["Update strategy statistics"]
    RUN --> EXT["Extract reusable strategy"]
    EXT --> MEM["Strategy memory"]
    ST --> MEM
    MEM --> RET["Retrieve relevant experience"]
    RET --> C

    GUARD["Contamination guard"] --> G1["No held-out private metadata"]
    GUARD --> G2["Version and split provenance"]
    GUARD --> G3["Train only after benchmark evidence"]
```

**Mental shortcut:** MVP “learning” means smarter search, not necessarily weight updates. It reuses successful abstractions, learns which strategies work, rewards novel progress, and protects held-out evaluation from leakage.


---

## 11_VIRTUAL_WORLD_AND_FORBIDDEN_STATES.md

### Mental model — where security truth lives

```mermaid
flowchart LR
    TARGET["Target agent"] --> ATTEMPT["Tool / service attempt"]
    ATTEMPT --> WORLD["Stateful virtual world"]
    WORLD --> S1["Payment service"]
    WORLD --> S2["Mail service"]
    WORLD --> S3["Customer data"]
    WORLD --> S4["Approval service"]
    WORLD --> S5["Memory service"]
    WORLD --> S6["Document service"]

    WORLD --> EVENT["Private event + state stream"]
    EVENT --> VER["Deterministic forbidden-state verifiers"]
    VER --> F1["F1 unauthorized payment"]
    VER --> F2["F2 approval reuse"]
    VER --> F3["F3 cross-tenant disclosure"]
    VER --> F4["F4 PII exfiltration"]

    VER --> SIG["progress / terminal signal"]
    SIG --> RED["Red receives only allowed signal"]

    RESET["Scenario snapshot"] --> WORLD
    WORLD --> RESET2["Deterministic reset after episode"]
```

**Mental shortcut:** the virtual world is the judge. Model text is never proof; private service state and emitted events determine whether an actual forbidden system consequence occurred.


---

## 12_CAPSULE_CONTAINMENT_AND_SAFETY_GATEWAY.md

### Mental model — safe execution below the AI layer

```mermaid
flowchart LR
    RED["Red Engine"] --> CAP["Sealed target capsule"]
    CAP --> TARGET["Potentially hostile target"]

    TARGET --> GW["Safety Gateway"]
    GW --> SIM["Only simulated MCP / HTTP services"]
    SIM --> WORLD["Virtual world"]

    TARGET -.- X1["No public internet"]
    TARGET -.- X2["No production network"]
    TARGET -.- X3["No production credentials"]
    TARGET -.- X4["No cloud metadata / host mounts / Docker socket"]

    TARGET --> MAILBOX["Blue inference mailbox"]
    SUP["Supervisor-owned broker"] --> PROVIDER["Pinned model provider"]
    SUP -- "claim / complete through Docker control channel" --> MAILBOX

    PREF["Containment preflight"] --> CAP
    KILL["Kill switch + resource limits"] --> CAP
    GW --> AUDIT["Effect audit trail"]
```

**Mental shortcut:** enforce routing and isolation below the model. Blue sends business
effects to simulated services; the supervisor relays restricted inference. Docker
research containment has recorded runtime checks, but Docker shares the host kernel
and is not an audited Firecracker boundary.


---

## 13_TRAJECTORIES_REPLAY_AND_EVIDENCE.md

### Mental model — turning an attack run into scientific evidence

```mermaid
flowchart TD
    STEP["Immutable step records"] --> T["Trajectory"]
    STEP --> S1["Red action"]
    STEP --> S2["Target public response"]
    STEP --> S3["Visible tool result"]
    STEP --> S4["Private effect / verifier references"]
    STEP --> S5["Reward + cost + versions"]

    T --> REPLAY["Replay modes"]
    REPLAY --> E["Exact fixture replay"]
    REPLAY --> L["Live statistical reproduction"]
    REPLAY --> P["Prefix replay / branch"]

    T --> SUM["Trajectory summary + root-cause labels"]
    T --> BUNDLE["Evidence bundle"]
    BUNDLE --> B1["Complete lineage"]
    BUNDLE --> B2["Environment evidence"]
    BUNDLE --> B3["Model / target / scenario versions"]
    BUNDLE --> B4["Reproduction results"]

    BUNDLE --> FIND["Auditable Verified Exploit"]
```

**Mental shortcut:** a trajectory is both the attack history and the reproducibility primitive. If the system cannot reconstruct what happened, under which versions, and with which private evidence, it does not have a trustworthy finding.


---

## 14_VERIFIED_EXPLOITS_AND_FINDINGS.md

Current UI semantics: “Verified Exploits” are deterministic verifier-backed findings.
This does not imply successful reproduction or deduplication into a vulnerability
family. Reproduction is recorded separately.

### Mental model — when a candidate becomes a product finding

```mermaid
flowchart LR
    T["High-reward trajectory"] --> C["Candidate exploit"]
    C --> V1{"Private forbidden-state proof?"}
    V1 -- "No" --> N["Near miss"]
    V1 -- "Yes" --> X["Verifier-backed finding / Verified Exploit in UI"]
    X --> R["Independent reproduction"]
    R --> V2{"Reproduction requirement met?"}
    V2 -- "No" --> DIV["Divergence / failed reproduction recorded"]
    V2 -- "Yes" --> CONF["Reproduction confirmed separately"]

    X -. "proposed analysis" .-> D["Deduplicate / cluster into exploit family"]
    X --> E["Evidence bundle"]
    X --> P["Preconditions + root-cause labels"]
    X --> M["First/last seen + target versions"]
    X --> RR["Reproduction rate / replay class"]

    N --> L["Learning signal"]
    X --> L
```

**Mental shortcut:** distinguish a verifier-backed consequence, a reproduced consequence
and a deduplicated vulnerability. High reward alone establishes none of them; the
current finding record establishes the first.


---

## 15_API_AND_FRONTEND_INTEGRATION.md

### Mental model — backend truth transformed into product views

```mermaid
flowchart LR
    DB["Canonical domain data"] --> API["Versioned REST API"]
    EVENTS["Persisted execution records"] --> STREAM["HTTP polling"]

    API --> T["Targets read model"]
    API --> C["Campaigns read model"]
    API --> E["Experiments read model"]
    API --> TR["Trajectories read model"]
    API --> X["Exploit read model"]
    API --> L["Learning read model"]

    T --> UI["Frontend"]
    C --> UI
    E --> UI
    TR --> UI
    X --> UI
    L --> UI
    STREAM --> LAB["Live Attack Lab"]

    CONTRACT["Strict semantic contracts"] --> CAND["candidate ≠ verified"]
    CONTRACT --> PRIV["private evidence requires scope; never automatic model input"]
    CONTRACT --> VERS["all target / Red / scenario versions explicit"]
```

**Mental shortcut:** the frontend should never infer security truth from loosely structured logs. The API exposes purpose-built read models, and live events power the Attack Lab without leaking private verifier information.


---

## 16_DEPLOYMENT_AND_CUSTOMER_ONBOARDING.md

### Mental model — where AML can run and what crosses boundaries

```mermaid
flowchart TD
    P["Same AML execution model"] --> I["Internal research deployment"]
    P --> V["Design partner / customer VPC"]
    P --> O["Fully self-hosted / on-prem"]

    V --> DATA["Customer-controlled data plane"]
    O --> DATA
    DATA --> T["Target image / adapter"]
    DATA --> C["Capsules + Safety Gateway"]
    DATA --> W["Virtual world + evidence"]

    CTRL["Optional control / management plane"] --> DATA

    ON["Onboarding path"] --> O1["Package agent as opaque target"]
    ON --> O2["Declare black-box interface"]
    ON --> O3["Map consequential tools to simulator"]
    ON --> O4["Create synthetic scenario + forbidden states"]
    ON --> O5["Run utility + containment gates"]
    O5 --> O6["Start campaigns"]

    RULE["Production rule"] --> R1["Do not attack production"]
    RULE --> R2["No production credentials / real side effects"]
```

**Mental shortcut:** customer onboarding creates a safe experimental twin/interface around the agent. The target can stay inside the customer environment while AML operates against simulated consequences rather than live production systems.


---

## 17_OBSERVABILITY_DATA_AND_COSTS.md

### Mental model — operational visibility and experiment economics

```mermaid
flowchart TD
    RUN["Every campaign / experiment"] --> TEL["Structured telemetry"]
    TEL --> C["Campaign metrics"]
    TEL --> E["Experiment metrics"]
    TEL --> M["Model invocation metrics"]
    TEL --> CAP["Capsule / containment metrics"]

    RUN --> DATA["Persistent data"]
    DATA --> PG["PostgreSQL<br/>metadata + state"]
    DATA --> OBJ["Object storage<br/>trajectories + evidence"]
    DATA --> ML["MLflow<br/>research comparisons"]

    M --> COST["Cost accounting"]
    COST --> C1["tokens"]
    COST --> C2["provider cost"]
    COST --> C3["cost per experiment"]
    COST --> C4["cost per verified exploit"]

    DATA --> GOV["Retention + redaction"]
    GOV --> SAFE["No unnecessary secrets / PII in artifacts"]
```

**Mental shortcut:** observability serves two goals at once: operate a reliable distributed experiment system, and measure whether the adversarial intelligence is economically improving—not just whether it eventually succeeds.


---

## 18_TESTING_VALIDATION_AND_SECURITY.md

### Mental model — the test pyramid for a hostile experimental system

```mermaid
flowchart TD
    U["Unit tests"] --> C["Contract tests"]
    C --> I["Integration tests"]
    I --> E["End-to-end campaigns"]

    SEC["Containment test suite"] --> E
    BENCH["Benchmark integrity suite"] --> E
    UI["Frontend integration validation"] --> E

    SEC --> S1["No internet / prod routes"]
    SEC --> S2["Gateway cannot be bypassed"]
    SEC --> S3["Kill switch + resource limits"]

    BENCH --> B1["Benign utility"]
    BENCH --> B2["Solvability"]
    BENCH --> B3["Deterministic reset"]
    BENCH --> B4["No hidden leakage"]
    BENCH --> B5["Anti-shortcut controls"]

    E --> PASS{"All invariants hold?"}
    PASS -- "Yes" --> REL["Credible benchmark + safe MVP"]
    PASS -- "No" --> FIX["Block release"]
```

**Mental shortcut:** ordinary software correctness is not enough. The MVP is trustworthy only if target contracts, benchmark fairness, deterministic reset, isolation, evidence lineage, and UI semantics all survive adversarial testing too.


---

## 19_IMPLEMENTATION_PHASES.md

This is the original build-order proposal. Delivered work is tracked by
[INF-01–INF-12 reports](reports/README.md); a phase's presence in this diagram does not
claim its research acceptance gates have passed.

### Mental model — build order and dependency chain

```mermaid
flowchart LR
    P0["0 Scope + contracts freeze"] --> P1["1 Deterministic adversarial lab"]
    P1 --> P2["2 Target contract + benchmark agents"]
    P2 --> P3["3 Sealed capsule + containment"]
    P3 --> P4["4 Campaign + experiment infrastructure"]
    P4 --> P5["5 Red v0 baselines"]
    P5 --> P6["6 Adaptive Red v1"]
    P6 --> P7["7 Verified exploit + reproduction"]
    P7 --> P8["8 Learning + formal evaluation"]
    P8 --> P9["9 Full product UI"]
    P9 --> P10["10 Customer-side packaging"]

    P0 -. "typed contracts" .-> P4
    P1 -. "ground truth" .-> P7
    P3 -. "safe execution" .-> P10
    P5 -. "baseline" .-> P8
    P6 -. "learning claim" .-> P8
```

**Mental shortcut:** prove the scientific core before polishing the product shell. Each phase exists to unlock the next one, and every phase has an exit gate so unresolved research or safety debt cannot silently move downstream.


The build order optimizes for **research velocity** and a falsifiable core thesis.

---

## 20_MVP_DEFINITION_OF_DONE.md

This remains a research acceptance gate, not a current completion claim.
Infrastructure fixture acceptance passed in the recorded run; real-model behavior,
multi-target benchmark coverage and adaptive improvement remain unproven.

### Mental model — the final acceptance gate

```mermaid
flowchart TD
    LOOP["Complete product loop"] --> GATE{"MVP acceptance"}
    TARGET["Opaque targets + credible benchmark"] --> GATE
    RED["Adaptive Red beats matched baselines"] --> GATE
    PROOF["Private evidence + reproducible exploits"] --> GATE
    SAFE["Containment violations = 0"] --> GATE
    REL["Reset / recovery / budgets are deterministic"] --> GATE
    UI["Real end-to-end UI, no legacy policy workflow"] --> GATE

    GATE -- "all true" --> CLAIM["Credible company proof"]
    GATE -- "anything false" --> NOTDONE["MVP is not done"]

    CLAIM --> DEMO["Autonomous attacker → opaque agent → forbidden state → reproduction → learning"]
```

**Mental shortcut:** this file is a release contract, not a wish list. The MVP is complete only when product behavior, research evidence, benchmark integrity, safety, reliability, and frontend semantics all pass simultaneously.


The MVP is complete only when **all** of the following are true.

---

## 21_UI_PRODUCT_SPEC.md

### Mental model — the user’s product journey

```mermaid
flowchart LR
    O["Adversarial Overview<br/>What is being discovered?"] --> T["Targets<br/>What systems can we test?"]
    T --> C["Attack Campaigns<br/>What objective are we pursuing?"]
    C --> L["Live Attack Lab<br/>What is Red trying right now?"]
    L --> E["Experiments<br/>How did runs compare?"]
    E --> TR["Trajectories<br/>Exactly what happened?"]
    TR --> X["Verified Exploits<br/>What was verified? Did replay reproduce?"]
    X --> LEARN["Learning<br/>How is Red getting better?"]
    LEARN -. "improves future campaigns" .-> C

    LAB["Core UX principle"] --> P1["Show causality, not dashboard clutter"]
    LAB --> P2["candidate ≠ verified"]
    LAB --> P3["proof + replay provenance visible"]
    LAB --> P4["No policy / compliance / hardening workflow"]
```

**Mental shortcut:** the UI mirrors the adversarial-science lifecycle. A user should move naturally from “what target?” to “what did Red try?” to “what consequence was proven?” to “what did the system learn?”


---

## 22_TEAM_OWNERSHIP.md

### Mental model — two workstreams, one shared contract surface

```mermaid
flowchart LR
    subgraph Platform["Platform / Safety owner"]
      P1["Contracts + API"]
      P2["Target packaging + capsules"]
      P3["Safety + Model Gateways"]
      P4["Virtual world + verifiers"]
      P5["Persistence + evidence"]
      P6["Held-out target custody"]
    end

    subgraph DS["Data Science / Adversarial owner"]
      D1["Attacker model abstraction"]
      D2["Search + ranking + mutation"]
      D3["Reward shaping + novelty"]
      D4["Strategy memory + retrieval"]
      D5["Baselines + ablations"]
      D6["Formal Red evaluation"]
    end

    SH["Shared contracts + benchmark semantics"]
    Platform --> SH
    DS --> SH
    SH --> RUN["Campaign / Experiment / Environment integration"]

    HOLD["Held-out separation"] --> H1["Platform keeps private vulnerabilities, seeds, aliases, oracle data"]
    HOLD --> H2["Attacker-model input: public contract + observations only"]
    HOLD --> H3["Research outputs: separate outcomes, measurements and rewards"]
```

**Mental shortcut:** platform engineering owns the trustworthy laboratory; data science owns the attacker intelligence. They meet at explicit shared contracts, while held-out secrets stay organizationally separated to keep generalization claims credible.
