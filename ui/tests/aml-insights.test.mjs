import assert from "node:assert/strict";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({ appType: "custom", configFile: false, root, server: { middlewareMode: true } });
after(() => vite.close());
const { episodeOutcome, outcomeSummary, experimentResult, comparisonKey, budgetPercent } = await vite.ssrLoadModule("/lib/aml-insights.ts");
const episode = (id, status, terminal_success = false, error = null, campaign_id = "campaign-a") => ({ episode_id: id, campaign_id, status, terminal_success, error });
const campaign = { campaign_id: "campaign-a", target_version_id: "v1", attack_task_id: "task-a", policy_version_id: null, run_kind: "ATTACK", red_config_id: "config-a", search_mode: "adaptive", status: "COMPLETED", tokens_used: 500, cost_used: .01 };

test("teardown preserves outcome; active and unknown states never become evaluated negatives", () => {
  assert.equal(episodeOutcome(episode("1", "DESTROYED", true)), "success");
  assert.equal(episodeOutcome(episode("2", "DESTROYED", false, "worker crashed")), "error");
  assert.equal(episodeOutcome(episode("3", "DESTROYED")), "no_success");
  for (const state of ["PENDING", "PROVISIONING", "READY", "EXECUTING", "VERIFYING", "FUTURE_STATE"]) {
    assert.equal(episodeOutcome(episode(state, state)), "unfinished", state);
  }
});

test("observed rates exclude interrupted/active episodes and never mix campaigns", () => {
  const rows = [episode("1", "DESTROYED", true), episode("2", "EXHAUSTED"), episode("3", "READY"), episode("4", "DESTROYED", false, "error"), episode("5", "DESTROYED", true, null, "campaign-b")];
  assert.equal(experimentResult(campaign, rows).rate, .5);
  assert.equal(experimentResult(campaign, rows).evaluated, 2);
  assert.equal(experimentResult(campaign, [episode("3", "READY")]).rate, null);
  assert.deepEqual(outcomeSummary([]), { success: 0, error: 0, unfinished: 0, no_success: 0, total: 0, evaluated: 0 });
});

test("comparison isolates immutable target/task budgets, defenses and replay kinds", () => {
  for (const patch of [{ target_version_id: "v2" }, { attack_task_id: "different-budget-task" }, { policy_version_id: "policy-2" }, { run_kind: "EXACT_REPLAY" }]) {
    assert.notEqual(comparisonKey(campaign), comparisonKey({ ...campaign, ...patch }));
  }
  assert.equal(comparisonKey(campaign), comparisonKey({ ...campaign, red_config_id: "baseline", search_mode: "linear" }));
  assert.equal(comparisonKey(campaign), comparisonKey({ ...campaign, policy_version_id: undefined }));
});

test("missing budget is unknown and visible progress remains bounded", () => {
  assert.equal(budgetPercent(100, undefined), null);
  assert.equal(budgetPercent(100, 0), null);
  assert.equal(budgetPercent(150, 100), 100);
  assert.equal(budgetPercent(25, 100), 25);
});
