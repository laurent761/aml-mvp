# Product

## Register

product

## Platform

web

## Users

Security engineers, AI safety researchers, and platform operators running controlled adversarial evaluations. They need to register opaque target images, launch and monitor campaigns, inspect causal evidence and reproduce forbidden-state outcomes without losing containment or provenance.

## Product Purpose

AML is the research workspace for autonomous adversarial testing of agent systems. Its canonical workflow is **Target → Attack Campaign → Experiments → Trajectories → Forbidden State → Verified Exploit → Learning**. An experiment maps to a backend episode; the trajectory is that episode's ordered actions and observations. The target remains a controlled black box.

Primary pages: Adversarial Overview, Targets, Attack Campaigns, Experiments, Live Attack Lab, Trajectories, Verified Exploits, and Learning. Evidence and system operations support the research workflow. Policy management and defensive hardening are outside this MVP interface. Preserve immutable target/task/configuration contracts, campaign cancellation, evidence export, exact replay and nearby attack mutations.

The POC console focuses on target registration, attack campaigns, experiments,
trajectories, verified findings, evidence, and strategy memory. Each page presents
its campaign workflow directly. Views use authenticated HTTP requests and retain
explicit owner/scope limits. The separate research-session, dataset, checkpoint,
and paired-evaluation record browsers are outside the POC interface.
The shipped controlled target is one finance reference agent; see
[project status](../README.md#current-implementation) for coverage and validation limits.

Verified exploits are deterministic finding records, not deduplicated vulnerabilities or automatically confirmed reproductions. Exact replay confirmation requires the same target version, task, original runtime conditions, and verifier. Strategy memory reports historical outcomes, not model weight training or proven learning uplift. Experiment comparisons are descriptive and use loaded records with explicit denominators; incomplete or interrupted episodes are excluded.

This is the standalone AML product. Its product model is active experimentation; it does not include asset scanning, governance/posture scoring, capability maps, or a production runtime recorder.

## Brand Personality

Precise, calm, forensic. The interface should feel like a trusted instrument used during consequential technical work: restrained enough for sustained attention, but visually distinctive through its silver atmosphere and obsidian navigation.

## Anti-references

Avoid generic SaaS landing pages, cyberpunk neon, glass-heavy security dashboards, terminal cosplay, decorative threat maps, oversized KPI theater, and interfaces that hide provenance behind simplified scores.

## Design Principles

- Put the current operational decision in the first viewport.
- Preserve lineage: versions, source findings, episodes, and artifacts remain visible wherever decisions are made.
- Separate observed public behavior from trusted private evidence.
- Use progressive disclosure so dense technical detail stays available without overwhelming routine workflows.
- Make destructive, costly, or containment-sensitive actions explicit and reversible where the backend permits.

## Accessibility & Inclusion

Target WCAG 2.2 AA. Maintain keyboard-complete workflows, visible focus, 44px touch targets on compact layouts, non-color status cues, readable contrast, reduced-motion behavior, resilient zoom, and plain-language errors.
