import { Beaker, Cpu, CircleHelp, Layers } from "lucide-react";
import { formatCost, formatNumber } from "../lib/api-client";

export interface UsageSummary {
  execution_kind: "real" | "simulated" | "mixed" | "unknown" | "none";
  real_calls: number; simulated_calls: number; unknown_calls: number;
  runtime_tokens: number; provider_tokens: number; estimated_tokens: number; simulated_tokens: number; unknown_tokens: number;
  priced_cost: number; unpriced_calls: number;
}
export function combineUsage(rows: { usage_summary?: UsageSummary | null; tokens_used?: number }[]): UsageSummary {
  const total: UsageSummary = { execution_kind: "none", real_calls: 0, simulated_calls: 0, unknown_calls: 0, runtime_tokens: 0, provider_tokens: 0, estimated_tokens: 0, simulated_tokens: 0, unknown_tokens: 0, priced_cost: 0, unpriced_calls: 0 };
  for (const row of rows) {
    const u = row.usage_summary;
    if (!u) { total.unknown_calls++; total.unknown_tokens += row.tokens_used ?? 0; total.unpriced_calls++; continue; }
    for (const key of Object.keys(total) as (keyof UsageSummary)[]) {
      if (key !== "execution_kind") total[key] += u[key] ?? 0;
    }
  }
  const kinds = (["real", "simulated", "unknown"] as const).filter(k => total[`${k}_calls`] > 0);
  total.execution_kind = kinds.length > 1 ? "mixed" : kinds[0] ?? "none";
  return total;
}
export function UsageBadge({ usage }: { usage?: UsageSummary | null }) {
  const kind = usage?.execution_kind ?? "unknown";
  const Icon = kind === "simulated" ? Beaker : kind === "real" ? Cpu : kind === "mixed" ? Layers : CircleHelp;
  const label = { simulated: "Simulation", real: "Real model calls", mixed: "Mixed activity", unknown: "Usage source unknown", none: "No model calls recorded" }[kind];
  return <span className={`usage-badge usage-badge--${kind}`}><Icon aria-hidden="true" />{label}</span>;
}
export function usageCost(usage?: UsageSummary | null): string {
  if (!usage) return "Pricing unknown";
  if (usage.execution_kind === "none") return "No recorded spend";
  if (usage.execution_kind === "simulated") return "No model charges";
  if (usage.unpriced_calls) return usage.priced_cost > 0 ? `${formatCost(usage.priced_cost)} priced portion · pricing incomplete` : "Pricing not configured";
  return `${formatCost(usage.priced_cost)} estimated cost`;
}
export function UsagePanel({ usage, compact = false }: { usage?: UsageSummary | null; compact?: boolean }) {
  return <div className={`usage-panel${compact ? " usage-panel--compact" : ""}`} aria-label="Model usage and provenance">
    <UsageBadge usage={usage} />
    {usage ? <div className="usage-breakdown">
      {usage.real_calls > 0 ? <span>{formatNumber(usage.real_calls)} real model call attempts</span> : null}
      {usage.provider_tokens > 0 ? <span>{formatNumber(usage.provider_tokens)} provider-reported tokens</span> : null}
      {usage.runtime_tokens > 0 ? <span>{formatNumber(usage.runtime_tokens)} runtime-reported tokens</span> : null}
      {usage.estimated_tokens > 0 ? <span>{formatNumber(usage.estimated_tokens)} estimated tokens</span> : null}
      {usage.simulated_calls > 0 ? <span>{formatNumber(usage.simulated_tokens)} simulated token units</span> : null}
      {usage.unknown_calls > 0 ? <span>{formatNumber(usage.unknown_tokens)} tokens · source unknown</span> : null}
    </div> : <span>Usage provenance was not recorded.</span>}
    <strong className="usage-cost">{usageCost(usage)}</strong>
    {!compact && usage?.simulated_calls ? <small>Simulated units measure scripted or heuristic activity; they are not tokens billed by a model provider.</small> : null}
    {!compact && usage?.unpriced_calls ? <small>Rates are missing or were not recorded for some calls. A zero recorded cost does not mean those calls were free.</small> : null}
  </div>;
}
export function AttackerModeNotice({ provider }: { provider?: string }) {
  const simulated = provider === "heuristic";
  return <div className={`usage-panel usage-badge--${simulated ? "simulated" : provider ? "real" : "unknown"}`}>
    <strong>{simulated ? "Simulated attacker · no attacker model calls" : provider ? "Real model attacker · model calls may incur charges" : "Backend default · attacker mode not yet confirmed"}</strong>
    <small>{simulated ? "A heuristic generates attacks. Target model calls, if configured, are tracked separately." : provider ? "Usage will distinguish provider-reported counts from estimates. Costs require configured prices." : "Choose an explicit attacker configuration to confirm its mode before launch. Recorded usage will identify what actually ran."}</small>
  </div>;
}
