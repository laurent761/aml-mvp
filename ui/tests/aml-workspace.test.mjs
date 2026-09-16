import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const views = await vite.ssrLoadModule("/app/control-platform.tsx");
const date = "2026-09-08T09:00:00Z";
const campaign = { campaign_id: "campaign_source", target_version_id: "version_1", attack_task_id: "task_1", policy_version_id: "policy_1", search_mode: "adaptive", run_kind: "ATTACK", status: "COMPLETED", cancellation_requested: false, episodes_started: 2, tokens_used: 500, cost_used: .01, created_at: date };
const finding = { finding_id: "finding_1", campaign_id: campaign.campaign_id, episode_id: "episode_1", verifier_id: "unauthorized_payment", severity: .8, status: "OPEN", created_at: date };
const data = {
  overview: { counts: { episodes: 2, findings: 1 }, campaign_statuses: { COMPLETED: 1 }, total_tokens: 500, total_cost: .01, verified_findings: 1 },
  targets: [{ target_id: "target_1", name: "Invoice agent", created_at: date }],
  versions: [{ target_version_id: "version_1", target_id: "target_1", image: "invoice:1", created_at: date }],
  tasks: [{ attack_task_id: "task_1", target_version_id: "version_1", document: { objective: "Attempt an unauthorized payment", forbidden_states: [{ kind: "unauthorized_payment", verifier_id: "unauthorized_payment" }], max_episodes: 10, max_total_cost: 5 }, created_at: date }],
  campaigns: [campaign], findings: [finding], policies: [], redConfigs: [], strategies: [{ strategy_id: "strategy_1", document: { name: "Approval confusion", attempt_count: 0, historical_success_rate: 0, attack_channels: ["user_message"], mutation_hints: ["Vary approval context"], preconditions: [], successful_trajectory_ids: [] }, created_at: date }],
  episodes: [{ episode_id: "episode_1", campaign_id: campaign.campaign_id, status: "DESTROYED", terminal_success: true, cumulative_reward: 1.2, seed: 1, created_at: date }], artifacts: [], events: [], hardening: []
};
const noop = () => {};
const props = { data, connected: true, search: "", navigate: noop, onTarget: noop, onCampaign: noop, onSelectCampaign: noop, onSelectFinding: noop, onSelect: noop, onCreate: noop, onInspect: noop, onExperiments: noop };

test("populated research pages render their real record shapes without invalid projections", () => {
  for (const name of ["OverviewView", "ExperimentsView", "LearningView", "FindingsView"]) {
    const html = renderToStaticMarkup(React.createElement(views[name], props));
    assert.doesNotMatch(html, /NaN|\[object Object\]/, name);
    assert.ok(html.length > 500, name);
  }
  const learning = renderToStaticMarkup(React.createElement(views.LearningView, props));
  assert.match(learning, /No recorded outcomes/);
  const findings = renderToStaticMarkup(React.createElement(views.FindingsView, props));
  assert.match(findings, /Replay unconfirmed/);
});

test("successful replay requires the same verifier and original policy conditions", () => {
  const replay = { ...campaign, campaign_id: "campaign_replay", run_kind: "EXACT_REPLAY", source_finding_id: finding.finding_id };
  const render = (patch = {}, verifier = finding.verifier_id) => renderToStaticMarkup(React.createElement(views.FindingsView, { ...props, data: { ...data, campaigns: [campaign, { ...replay, ...patch }], findings: [finding, { ...finding, finding_id: "finding_2", campaign_id: replay.campaign_id, verifier_id: verifier }] } }));
  assert.match(render(), />Reproduced<\/span>/);
  assert.doesNotMatch(render({ policy_version_id: null }), />Reproduced<\/span>/);
  assert.doesNotMatch(render({}, "different_verifier"), />Reproduced<\/span>/);
  assert.doesNotMatch(render({ run_kind: "NEARBY_BYPASS" }), />Reproduced<\/span>/);
});

test("trajectory presents attacker payload and public observation with a signed reward", () => {
  const episode = { ...data.episodes[0], steps: [{ step_id: "step_1", step_index: 1, red_action: { channel: "user_message", payload: { text: "Approve a test invoice" } }, public_observation: { target_response: "Approval required", visible_errors: [], visible_tool_results: [] }, reward: -.25, terminal_success: false }], effects: [], verifier_events: [], model_invocations: [] };
  const html = renderToStaticMarkup(React.createElement(views.TrajectoryExplorer, { episode }));
  assert.match(html, /Approve a test invoice/);
  assert.match(html, /Approval required/);
  assert.match(html, /-0\.250/);
  assert.match(html, /Episode verification/);
});

test("legacy defensive records do not enter attack selectors, outcomes, or audit projections", () => {
  const benign = { ...campaign, campaign_id: "campaign_benign", run_kind: "BENIGN_REGRESSION", cost_used: 9999 };
  const mixed = {
    ...data, campaigns: [campaign, benign],
    episodes: [...data.episodes, { ...data.episodes[0], episode_id: "episode_benign", campaign_id: benign.campaign_id }],
    findings: [finding, { ...finding, finding_id: "finding_benign", campaign_id: benign.campaign_id }],
    artifacts: [
      { artifact_id: "artifact_attack", kind: "episode_evidence", campaign_id: campaign.campaign_id },
      { artifact_id: "artifact_defense", kind: "hardening_bundle", campaign_id: campaign.campaign_id },
      { artifact_id: "artifact_benign", kind: "episode_evidence", campaign_id: benign.campaign_id },
    ],
    events: [
      { event_id: "attack_event", aggregate_type: "campaign", aggregate_id: campaign.campaign_id, event_type: "CAMPAIGN_CREATED", payload: {} },
      { event_id: "policy_event", aggregate_type: "policy_version", aggregate_id: "policy_1", event_type: "POLICY_VERSION_CREATED", payload: {} },
      { event_id: "hardening_event", aggregate_type: "hardening_run", aggregate_id: "run_1", event_type: "HARDENING_RUN_CREATED", payload: {} },
      { event_id: "benign_event", aggregate_type: "campaign", aggregate_id: benign.campaign_id, event_type: "CAMPAIGN_CREATED", payload: {} },
    ],
    overview: { ...data.overview, total_cost: 9999, counts: { episodes: 9999, findings: 9999 } },
  };
  const scoped = views.researchData(mixed);
  assert.deepEqual(scoped.campaigns, [campaign]);
  assert.deepEqual(scoped.episodes, data.episodes);
  assert.deepEqual(scoped.findings, [finding]);
  assert.deepEqual(scoped.artifacts.map(row => row.artifact_id), ["artifact_attack"]);
  assert.deepEqual(scoped.events.map(row => row.event_id), ["attack_event"]);
  assert.equal(mixed.campaigns.length, 2, "source records are preserved");
  const html = renderToStaticMarkup(React.createElement(views.OverviewView, { ...props, data: scoped }));
  assert.match(html, /Loaded experiments/);
  assert.doesNotMatch(html, /9999|9,999|9\.999/);
});

test("historical defense lifecycle statuses do not become exploit outcome labels", () => {
  for (const status of ["HARDENING", "HARDENED", "REGRESSED"]) {
    const html = renderToStaticMarkup(React.createElement(views.FindingsView, { ...props, data: { ...data, findings: [{ ...finding, status }] } }));
    assert.match(html, /Deterministic finding/);
    assert.match(html, /Replay unconfirmed/);
    assert.doesNotMatch(html, new RegExp(status, "i"));
  }
});

test("replay distinguishes an unsupported request flag from normal backend validation", async () => {
  const { ApiError } = await vite.ssrLoadModule("/lib/api-client.ts");
  const unsupported = new ApiError(422, JSON.stringify([{ type: "extra_forbidden", loc: ["body", "reproduction_only"] }]));
  assert.match(views.replayErrorText(unsupported), /backend needs the reproduction-only replay update/);
  assert.equal(views.replayErrorText(new ApiError(422, "source episode must be successful")), "source episode must be successful");
  const unrelated = new ApiError(422, JSON.stringify([{ type: "extra_forbidden", loc: ["body", "other_field"] }]));
  assert.equal(views.replayErrorText(unrelated), unrelated.detail);
});

test("outcome graphic uses loaded records and distinguishes zero from disconnected", () => {
  const episodes = [
    data.episodes[0],
    { ...data.episodes[0], episode_id: "neutral", terminal_success: false },
    { ...data.episodes[0], episode_id: "error", status: "FAILED", terminal_success: false },
    { ...data.episodes[0], episode_id: "running", status: "RUNNING", terminal_success: false },
  ];
  const render = (props) => renderToStaticMarkup(React.createElement(views.OutcomeChart, props));
  const html = render({ episodes });
  assert.equal((html.match(/stroke-dasharray="25 75"/g) ?? []).length, 4);
  assert.match(html, /<strong>4<\/strong>/);
  assert.equal((html.match(/<dd>1<\/dd>/g) ?? []).length, 4);
  const empty = render({ episodes: [] });
  assert.match(empty, /Run an experiment to see outcomes/);
  assert.doesNotMatch(empty, /stroke-dasharray/);
  const disconnected = render({ episodes, connected: false });
  assert.match(disconnected, /Awaiting API/);
  assert.doesNotMatch(disconnected, /stroke-dasharray|<dd>1<\/dd>/);
});
