import assert from "node:assert/strict";
import test from "node:test";

import { createViteTestServer } from "./helpers.mjs";

const vite = await createViteTestServer();
const { episodeOutcome, outcomeSummary, experimentResult, comparisonKey, budgetPercent, asObject, textValue } = await vite.ssrLoadModule("/lib/aml-insights.ts");
const { combineUsage, usageCost } = await vite.ssrLoadModule("/app/usage-summary.tsx");

const emptyUsage = Object.freeze({
  execution_kind: "none",
  real_calls: 0, simulated_calls: 0, unknown_calls: 0,
  runtime_tokens: 0, provider_tokens: 0, estimated_tokens: 0, simulated_tokens: 0, unknown_tokens: 0,
  priced_cost: 0, unpriced_calls: 0,
});
const episode = (id, patch = {}) => Object.freeze({ episode_id: id, campaign_id: "campaign-a", status: "DESTROYED", terminal_success: false, error: null, ...patch });

test("failure and cancellation take precedence over stale success flags", () => {
  for (const status of ["FAILED", "ERROR", "CANCELLED"]) {
    assert.equal(episodeOutcome(episode(status, { status, terminal_success: true })), "error", status);
  }
  assert.equal(episodeOutcome(episode("crashed-after-verification", { status: "COMPLETED", terminal_success: true, error: "evidence persistence failed" })), "error");
  assert.equal(episodeOutcome(episode("verified", { status: "SUCCEEDED", terminal_success: true, error: "" })), "success");
});

test("lifecycle names alone cannot fabricate a verified success", () => {
  for (const status of ["DESTROYED", "EXHAUSTED", "SUCCEEDED", "COMPLETED"]) {
    assert.equal(episodeOutcome(episode(status, { status })), "no_success", status);
  }
  assert.equal(episodeOutcome(episode("verified-before-teardown", { status: "VERIFYING", terminal_success: true })), "success");
});

test("outcome partitions conserve records and rates exclude errors across orderings", () => {
  const rows = Object.freeze([
    episode("success-1", { terminal_success: true }),
    episode("success-2", { status: "SUCCEEDED", terminal_success: true }),
    episode("negative-1"), episode("negative-2", { status: "EXHAUSTED" }),
    episode("cancelled", { status: "CANCELLED" }), episode("crashed", { error: "worker lost" }),
    episode("queued", { status: "PENDING" }), episode("future", { status: "FUTURE_STATE" }),
  ]);
  const expected = { success: 2, no_success: 2, error: 2, unfinished: 2, total: 8, evaluated: 4 };
  for (const ordering of [rows, [...rows].reverse(), [...rows.slice(3), ...rows.slice(0, 3)]]) {
    const summary = outcomeSummary(ordering);
    assert.deepEqual(summary, expected);
    assert.equal(summary.success + summary.no_success + summary.error + summary.unfinished, summary.total);
    assert.deepEqual(experimentResult({ campaign_id: "campaign-a" }, ordering), { ...expected, rate: 0.5 });
  }
});

test("zero observed success differs from no evaluated evidence", () => {
  const campaign = { campaign_id: "campaign-a" };
  assert.equal(experimentResult(campaign, [episode("negative")]).rate, 0);
  assert.equal(experimentResult(campaign, [episode("positive", { terminal_success: true })]).rate, 1);
  assert.equal(experimentResult(campaign, [episode("cancelled", { status: "CANCELLED" })]).rate, null);
  assert.equal(experimentResult(campaign, [episode("foreign", { campaign_id: "campaign-b", terminal_success: true })]).rate, null);
});

test("comparison keys remain unambiguous when identifiers contain delimiters or quotes", () => {
  const common = { policy_version_id: null, run_kind: "ATTACK" };
  const first = { ...common, target_version_id: 'target|task,"quoted"', attack_task_id: "task" };
  const second = { ...common, target_version_id: "target", attack_task_id: 'task,"quoted"|task' };
  assert.notEqual(comparisonKey(first), comparisonKey(second));
  assert.deepEqual(JSON.parse(comparisonKey(first)), ['target|task,"quoted"', "task", null, "ATTACK"]);
});

test("budget projections reject absent or non-finite limits and clamp valid percentages", () => {
  for (const limit of [undefined, null, "100", false, {}, [], -1, NaN, Infinity, -Infinity]) {
    assert.equal(budgetPercent(50, limit), null, String(limit));
  }
  assert.equal(budgetPercent(-5, 100), 0);
  assert.equal(budgetPercent(0, 100), 0);
  assert.equal(budgetPercent(1, 8), 12.5);
  assert.equal(budgetPercent(100, 100), 100);
  assert.equal(budgetPercent(10_000, 100), 100);
});

test("unstructured evidence remains inspectable without coercing zero or false to missing data", () => {
  for (const value of [undefined, null, false, 0, "message", ["array"]]) {
    assert.deepEqual(asObject(value), {});
  }
  const object = Object.freeze({ decision: false, count: 0, text: "<script>evidence</script>" });
  assert.equal(asObject(object), object);
  assert.equal(textValue(false), "false");
  assert.equal(textValue(0), "0");
  assert.equal(textValue("<script>evidence</script>"), "<script>evidence</script>");
  assert.equal(textValue(object), '{\n  "decision": false,\n  "count": 0,\n  "text": "<script>evidence</script>"\n}');
  for (const value of [undefined, null, ""]) assert.equal(textValue(value, "No observation recorded"), "No observation recorded");
});

test("usage aggregation preserves every provenance counter and cost without modifying source rows", () => {
  const real = Object.freeze({ ...emptyUsage, execution_kind: "real", real_calls: 3, runtime_tokens: 50, provider_tokens: 70, estimated_tokens: 30, priced_cost: 1.25, unpriced_calls: 1 });
  const simulated = Object.freeze({ ...emptyUsage, execution_kind: "simulated", simulated_calls: 4, simulated_tokens: 250 });
  const unknown = Object.freeze({ ...emptyUsage, execution_kind: "unknown", unknown_calls: 2, unknown_tokens: 80, priced_cost: 0.5, unpriced_calls: 2 });
  const rows = Object.freeze([Object.freeze({ usage_summary: real }), Object.freeze({ usage_summary: simulated }), Object.freeze({ usage_summary: unknown })]);
  const expected = { ...emptyUsage, execution_kind: "mixed", real_calls: 3, simulated_calls: 4, unknown_calls: 2, runtime_tokens: 50, provider_tokens: 70, estimated_tokens: 30, simulated_tokens: 250, unknown_tokens: 80, priced_cost: 1.75, unpriced_calls: 3 };

  assert.deepEqual(combineUsage(rows), expected);
  assert.deepEqual(combineUsage([...rows].reverse()), expected);
  assert.deepEqual(combineUsage([{ usage_summary: combineUsage(rows.slice(0, 2)) }, rows[2]]), expected, "page aggregates must compose without reclassifying usage");
  assert.equal(real.provider_tokens, 70);
  assert.equal(simulated.simulated_tokens, 250);
  assert.equal(unknown.unknown_tokens, 80);
});

test("empty and partial usage records produce finite totals while legacy rows remain unknown and unpriced", () => {
  assert.deepEqual(combineUsage([]), emptyUsage);
  assert.deepEqual(combineUsage([{ usage_summary: { execution_kind: "real", real_calls: 1, provider_tokens: 42 } }]), { ...emptyUsage, execution_kind: "real", real_calls: 1, provider_tokens: 42 });
  const combined = combineUsage([{ tokens_used: 17 }, { usage_summary: null, tokens_used: 23 }, {}, { usage_summary: emptyUsage, tokens_used: 999 }]);
  assert.deepEqual(combined, { ...emptyUsage, execution_kind: "unknown", unknown_calls: 3, unknown_tokens: 40, unpriced_calls: 3 });
  assert.equal(usageCost(combined), "Pricing not configured");
});

for (const [kinds, expected] of [
  [["real"], "real"], [["simulated"], "simulated"], [["unknown"], "unknown"],
  [["real", "simulated"], "mixed"], [["real", "unknown"], "mixed"], [["simulated", "unknown"], "mixed"],
  [["real", "simulated", "unknown"], "mixed"],
]) {
  test(`usage classification follows recorded calls for ${kinds.join(" and ")}`, () => {
    const rows = kinds.map(kind => ({ usage_summary: { ...emptyUsage, execution_kind: kind, [`${kind}_calls`]: 1 } }));
    assert.equal(combineUsage(rows).execution_kind, expected);
    rows.push({ usage_summary: emptyUsage });
    assert.equal(combineUsage(rows).execution_kind, expected, "empty records must not change provenance");
  });
}
