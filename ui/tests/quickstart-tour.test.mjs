import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const { tourSelection, registrationCommands } = await vite.ssrLoadModule("/lib/quickstart.ts");
const { default: QuickstartTour, TourBudget, CommandBlock } = await vite.ssrLoadModule("/app/quickstart-tour.tsx");

test("launch selection requires the chosen mode and a task bound to the exact target version", () => {
  const bundles = [
    { target_version_id: "fixture-v1", execution_mode: "fixture" },
    { target_version_id: "model-v1", execution_mode: "model" },
  ];
  const tasks = [
    { attack_task_id: "fixture-task", target_version_id: "fixture-v1" },
    { attack_task_id: "model-task", target_version_id: "model-v1" },
  ];
  assert.equal(tourSelection(bundles, tasks, "model", "fixture-v1", "fixture-task"), null);
  assert.equal(tourSelection(bundles, tasks, "model", "model-v1", "fixture-task"), null);
  assert.equal(tourSelection(bundles, tasks, "model", "model-v1", "removed-task"), null);
  assert.equal(tourSelection([], tasks, "model", "model-v1", "model-task"), null);
  assert.deepEqual(tourSelection(bundles, tasks, "model", "model-v1", "model-task"), { versionId: "model-v1", taskId: "model-task" });
});

test("model instructions export the running supervisor profile and register the model bundle through a writable mount", () => {
  const model = registrationCommands("model");
  const fixture = registrationCommands("fixture");
  assert.match(model, /capsule-supervisor python -m adversarial_agent_mvp.bundle_cli inference-profile/);
  assert.match(model, /adversarial-bundle reference --image .* --inference-profile/);
  assert.match(model, /register \/app\/var\/uploads\/finance-model.json/);
  assert.match(fixture, /register \/app\/var\/uploads\/finance-reference.json/);
  assert.doesNotMatch(fixture, /inference-profile|force-recreate/);
  for (const command of [model, fixture]) {
    assert.doesNotMatch(command, /compose cp|register \/tmp/);
    assert.match(command, /validate \/app\/var\/uploads\//);
  }
});

test("the guide is available offline and explains model versus fixture before any launch", () => {
  const html = renderToStaticMarkup(React.createElement(QuickstartTour, {
    apiBase: "", connected: false, tasks: [], onRefresh() {}, onNavigate() {}, onConnection() {}, onCampaign() {},
  }));
  assert.match(html, /Quickstart steps/);
  assert.match(html, /Real model/);
  assert.match(html, /Scripted fixture/);
  assert.match(html, /Connect the API before launching/);
  assert.match(html, /default attacker uses heuristic strategies/);
  assert.doesNotMatch(html, /Queue campaign/);
});

test("budget preview preserves zero cost and exposes all six task limits", () => {
  const html = renderToStaticMarkup(React.createElement(TourBudget, { task: { document: {
    max_episodes: 1, max_steps_per_episode: 3, max_model_tokens: 5000,
    max_total_cost: 0, max_wall_time_seconds: 120, max_concurrency: 1,
  } } }));
  assert.equal((html.match(/<dt>/g) ?? []).length, 6);
  assert.match(html, /\$0\.00/);
  assert.match(html, /120s/);
  assert.doesNotMatch(html, /NaN|undefined/);
});

test("copyable commands render as escaped text with a keyboard accessible code block", () => {
  const html = renderToStaticMarkup(React.createElement(CommandBlock, { title: "Registration", command: '<script>alert("x")</script>' }));
  assert.match(html, /aria-label="Copy Registration"/);
  assert.match(html, /tabindex="0"/);
  assert.doesNotMatch(html, /<script>/);
});
