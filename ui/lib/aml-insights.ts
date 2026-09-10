/** Presentation-only projections over the existing AML API contracts. */
export const activeCampaignStatuses = new Set(["CREATED", "VALIDATING", "READY", "RUNNING", "CANCELLING"]);

type Episode = { episode_id: string; campaign_id: string; status: string; terminal_success: boolean; error?: string | null };
type Campaign = { campaign_id: string; target_version_id: string; attack_task_id: string; policy_version_id?: string | null; run_kind: string; red_config_id?: string | null; search_mode: string; status: string; tokens_used: number; cost_used: number };

export function episodeOutcome(episode: Episode): "success" | "error" | "unfinished" | "no_success" {
  if (episode.error || ["FAILED", "ERROR", "CANCELLED"].includes(episode.status)) return "error";
  if (episode.terminal_success) return "success";
  if (["DESTROYED", "EXHAUSTED", "SUCCEEDED", "COMPLETED"].includes(episode.status)) return "no_success";
  return "unfinished";
}

export function outcomeSummary(episodes: Episode[]) {
  const counts = { success: 0, error: 0, unfinished: 0, no_success: 0 };
  for (const episode of episodes) counts[episodeOutcome(episode)]++;
  return { ...counts, total: episodes.length, evaluated: counts.success + counts.no_success };
}

export function comparisonKey(campaign: Campaign): string {
  // An immutable task pins budgets, seed and attack surface. Do not mix defenses or replay runs.
  return JSON.stringify([campaign.target_version_id, campaign.attack_task_id, campaign.policy_version_id ?? null, campaign.run_kind]);
}

export function experimentResult(campaign: Campaign, episodes: Episode[]) {
  const observed = episodes.filter((episode) => episode.campaign_id === campaign.campaign_id);
  const summary = outcomeSummary(observed);
  return { ...summary, rate: summary.evaluated ? summary.success / summary.evaluated : null };
}

export function budgetPercent(used: number, limit: unknown): number | null {
  return typeof limit === "number" && Number.isFinite(limit) && limit > 0
    ? Math.max(0, Math.min(100, used / limit * 100)) : null;
}

export function asObject(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function textValue(value: unknown, fallback = "Not recorded"): string {
  if (value === undefined || value === null || value === "") return fallback;
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
