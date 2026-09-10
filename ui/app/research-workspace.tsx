"use client";

import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiRequest, formatCost, formatDate, shortId } from "@/lib/api-client";

type RecordRow = { id: string; created_at?: string; document: Record<string, unknown> };
export type ResearchSessionRow = {
  id: string; run_id: string | null; state: string; episode_id: string | null;
  step_index: number; stop_reason: string | null; created_at: string;
  configuration: { mode: string; group_id: string | null; scenario_version_id: string; limits: { max_steps: number } };
};
type OperationRow = { id: string; status: string; kind: string; input: Record<string, unknown>; result: {
  episode_id?: string;
  public_observation?: { target_response?: string; visible_errors?: string[]; delivery_receipt?: { status: string; slot_id?: string; delivery_point?: string } };
  outcome?: { terminal_success: boolean | null; termination_reason: string | null; truncation_reason: string | null; execution_status: string };
  usage?: { model_tokens: number; cost: number; latency_ms: number };
} | null };

export function ResearchSessionTable({ sessions, onSelect }: { sessions: ResearchSessionRow[]; onSelect: (id: string) => void }) {
  return <div className="research-table-scroll"><table className="research-table"><caption className="sr-only">Research sessions created through the SDK or research API</caption><thead><tr><th>Session / run</th><th>Control</th><th>Progress</th><th>Status</th><th>Stop reason</th></tr></thead><tbody>
    {sessions.map(session => <tr key={session.id}><td><button className="inspector-link" onClick={() => onSelect(session.id)}>{shortId(session.id)}</button><small className="cell-note">{session.run_id ? shortId(session.run_id) : "No parent run"}</small></td><td>{session.configuration.mode}</td><td>{session.step_index} / {session.configuration.limits.max_steps} steps</td><td>{session.state.replaceAll("_", " ")}</td><td>{session.stop_reason?.replaceAll("_", " ") ?? "—"}</td></tr>)}
  </tbody></table>{sessions.length === 0 ? <p className="research-empty">No research sessions match this view.</p> : null}</div>;
}

export function ResearchTrajectory({ operations, onEvidence }: { operations: OperationRow[]; onEvidence?: (episodeId: string) => void }) {
  return <div className="view-stack">{operations.map(operation => {
    const result = operation.result;
    const receipt = result?.public_observation?.delivery_receipt;
    return <article className="surface research-operation" key={operation.id}><div className="section-head"><h4>{operation.kind} · {operation.status}</h4><small>{shortId(operation.id)}</small></div>
      {result?.public_observation?.target_response ? <p className="research-response">{result.public_observation.target_response}</p> : null}
      {receipt ? <p>Delivery: <strong>{receipt.status.replaceAll("_", " ")}</strong> · {receipt.slot_id ?? "No slot"} · {receipt.delivery_point?.replaceAll("_", " ")}</p> : null}
      {result?.outcome ? <dl className="research-facts"><div><dt>Verified outcome</dt><dd>{result.outcome.terminal_success === true ? "Forbidden state reached" : result.outcome.terminal_success === false ? "No forbidden state recorded" : "Unknown"}</dd></div><div><dt>Execution</dt><dd>{result.outcome.execution_status}</dd></div><div><dt>Stopped by</dt><dd>{result.outcome.termination_reason ?? result.outcome.truncation_reason ?? "Still active"}</dd></div></dl> : null}
      {result?.usage ? <p>{result.usage.model_tokens} model tokens · {formatCost(result.usage.cost)} · {result.usage.latency_ms} ms</p> : null}
      {result?.episode_id && onEvidence ? <Button variant="outline" size="sm" onClick={() => onEvidence(result.episode_id!)}>Inspect episode evidence</Button> : null}
      {result?.public_observation?.visible_errors?.map((error, index) => <p role="status" key={index}>{error}</p>)}
      <details><summary>Recorded input and result</summary><pre className="inspector-json">{JSON.stringify(operation, null, 2)}</pre></details>
    </article>;
  })}{operations.length === 0 ? <p className="research-empty">This session has no recorded commands yet.</p> : null}</div>;
}

export default function ResearchWorkspace({ apiBase, view, search }: { apiBase: string; view: string; search: string }) {
  const [sessions, setSessions] = useState<ResearchSessionRow[]>([]);
  const [records, setRecords] = useState<Record<string, RecordRow[]>>({});
  const [catalog, setCatalog] = useState<Array<{ bundle_id: string; execution_mode: string; readiness: string; last_session?: { state: string; stop_reason: string | null } | null; scenario: { name: string; version: string; split: string } }>>([]);
  const [selected, setSelected] = useState("");
  const [operations, setOperations] = useState<OperationRow[]>([]);
  const [detail, setDetail] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    const load = async () => {
      const options = { signal: abort.signal };
      try {
        if (view === "targets") {
          const data = await apiRequest<{ items: typeof catalog }>(apiBase, "/v1/research-catalog", options);
          setCatalog(data.items);
        } else if (view === "system") {
          setHealth(await apiRequest(apiBase, "/v1/research-health", options));
        } else {
          const paths = view === "experiments" ? ["research-runs", "evaluations"] : view === "learning" || view === "evidence" ? ["dataset-snapshots", "checkpoints", "evaluations"] : [];
          const sessionData = await apiRequest<{ items: ResearchSessionRow[] }>(apiBase, "/v1/research-sessions", options);
          setSessions(sessionData.items);
          const results = await Promise.allSettled(paths.map(async path => [path, (await apiRequest<{ items: RecordRow[] }>(apiBase, `/v1/${path}`, options)).items] as const));
          setRecords(Object.fromEntries(results.flatMap(result => result.status === "fulfilled" ? [result.value] : [])));
          const failure = results.find(result => result.status === "rejected");
          if (failure?.status === "rejected") throw failure.reason;
        }
        if (!abort.signal.aborted) setError("");
      } catch (reason) {
        if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "Research records could not be loaded.");
      } finally {
        if (!abort.signal.aborted) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 6000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, [apiBase, view, revision]);

  useEffect(() => {
    if (!selected) return;
    const abort = new AbortController();
    const load = () => apiRequest<{ items: OperationRow[] }>(apiBase, `/v1/research-sessions/${selected}/trajectory?limit=500`, { signal: abort.signal })
      .then(data => setOperations(data.items)).catch(reason => { if (!abort.signal.aborted) setError(String(reason)); });
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, [apiBase, selected]);

  const inspect = async (path: string) => {
    try { setDetail(await apiRequest(apiBase, path)); } catch (reason) { setError(String(reason)); }
  };
  const matches = (value: unknown) => JSON.stringify(value).toLowerCase().includes(search.toLowerCase());
  return <section className="surface research-workspace" aria-label="Research integration"><div className="section-head"><div><h3>{view === "targets" ? "Runnable target bundles" : view === "system" ? "Research services" : "Research records"}</h3><p>{view === "targets" ? "Registered scenarios and their declared execution mode." : "Records shared with the Python SDK."}</p></div><Button variant="outline" size="sm" onClick={() => setRevision(value => value + 1)}><RefreshCw />Refresh research</Button></div>
    {error ? <p className="research-error" role="alert">{error}</p> : null}
    {loading ? <p role="status" className="research-empty">Loading research records…</p> : null}
    {view === "targets" ? <div className="research-record-grid">{catalog.filter(matches).map(item => <article key={item.bundle_id}><h4>{item.scenario.name}</h4><p>{item.scenario.version} · {item.scenario.split} · {item.execution_mode}</p><small>{item.readiness}; live acceptance not asserted</small>{item.last_session ? <p>Latest session: {item.last_session.state} · {item.last_session.stop_reason ?? "No stop reported"}</p> : null}</article>)}</div> : null}
    {view === "system" && health ? <pre className="inspector-json">{JSON.stringify(health, null, 2)}</pre> : null}
    {["experiments", "lab", "trajectories", "evidence"].includes(view) ? <ResearchSessionTable sessions={sessions.filter(matches).filter(s => view !== "lab" || ["queued", "ready", "active", "episode_done"].includes(s.state))} onSelect={id => { setSelected(id); setOperations([]); }} /> : null}
    {Object.entries(records).map(([kind, rows]) => <div key={kind} className="research-record-section"><h4>{kind.replaceAll("-", " ")}</h4><div className="research-record-grid">{rows.filter(matches).map(row => <button key={row.id} onClick={() => void inspect(`/v1/${kind}/${row.id}`)}><strong>{String(row.document.name ?? shortId(row.id))}</strong><small>{formatDate(row.created_at)}</small><span>{kind === "research-runs" ? String(row.document.execution_mode) : String(row.document.kind ?? row.document.split ?? "View executed results")}</span></button>)}</div>{rows.length === 0 ? <p className="research-empty">No records yet.</p> : null}</div>)}
    {selected ? <section className="research-record-section"><div className="section-head"><h4>Session {shortId(selected)}</h4><Button variant="ghost" onClick={() => setSelected("")}>Close trajectory</Button></div><ResearchTrajectory operations={operations} onEvidence={id => void inspect(`/v1/research-episodes/${id}/evidence`)} /></section> : null}
    {detail ? <section className="research-record-section"><Button variant="ghost" onClick={() => setDetail(null)}>Close record</Button><pre className="inspector-json">{JSON.stringify(detail, null, 2)}</pre></section> : null}
  </section>;
}
