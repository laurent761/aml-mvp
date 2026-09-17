"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { apiRequest, formatDate } from "@/lib/api-client";

export interface ImageReadiness {
  status: "READY" | "UNAVAILABLE";
  code: string;
  message: string;
  image: string;
  image_id?: string | null;
  recoverable: boolean;
  restored: boolean;
  checked_at: string;
  technical_detail?: string | null;
}

export function TargetReadiness({ apiBase, versionId, autoPrepare = false, onReady }: {
  apiBase: string; versionId: string; autoPrepare?: boolean; onReady?: (versionId: string, ready: boolean) => void;
}) {
  const [result, setResult] = useState<ImageReadiness | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [request, setRequest] = useState({ revision: 0, restore: autoPrepare });
  useEffect(() => {
    const controller = new AbortController();
    onReady?.(versionId, false);
    apiRequest<ImageReadiness>(apiBase, `/v1/target-versions/${encodeURIComponent(versionId)}/${request.restore ? "prepare" : "readiness"}`, {
      method: request.restore ? "POST" : "GET", signal: controller.signal,
    }).then((value) => {
      if (controller.signal.aborted) return;
      setResult(value); setError(""); onReady?.(versionId, value.status === "READY");
    }).catch(() => {
      if (!controller.signal.aborted) setError("Readiness could not be checked. Check the API connection and try again.");
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [apiBase, versionId, request, onReady]);
  const check = (restore: boolean) => { setBusy(true); setResult(null); setError(""); setRequest((value) => ({ revision: value.revision + 1, restore })); };
  return <section className="target-readiness" aria-label="Target readiness" aria-busy={busy}>
    <div role="status" aria-live="polite"><strong>{busy ? (request.restore ? "Preparing target…" : "Checking target…") : result?.status === "READY" ? "Ready to start" : "Target unavailable"}</strong>
      <p>{busy ? "Verifying this exact version on the execution host." : error || result?.message}</p>
      {!busy && result?.status === "READY" && !result.recoverable ? <p>This legacy version is available locally but has no saved registry copy. Prepare a new version to enable automatic recovery.</p> : null}
      {!busy && result?.restored ? <p>The saved image was restored. Your selected version is unchanged.</p> : null}
    </div>
    {!busy ? <div className="button-row"><Button size="sm" variant="outline" onClick={() => check(result?.recoverable ?? false)}>{result?.status === "UNAVAILABLE" && result.recoverable ? "Restore this version" : "Check again"}</Button></div> : null}
    {!busy && result ? <details className="raw-details"><summary>Technical details</summary><p>Checked {formatDate(result.checked_at)}</p><p>{result.technical_detail}</p><pre>{JSON.stringify({ code: result.code, image: result.image, local_image_id: result.image_id }, null, 2)}</pre></details> : null}
  </section>;
}

export function ExecutionProblem({ error, noActions, apiBase, versionId, onSelectVersion }: {
  error: string; noActions: boolean; apiBase: string; versionId?: string; onSelectVersion?: () => void;
}) {
  const imageMissing = /no such image|saved image|restore this target|target image|target version is unavailable/i.test(error);
  return <section className="inline-failure" role="alert">
    <h3>{noActions ? "Could not start — no security result" : "Execution interrupted"}</h3>
    <p>{imageMissing ? "The saved image for this target version is unavailable." : "An execution problem stopped this run."} {noActions ? "No attacks ran. This run produced no security result." : "Interrupted experiments are excluded from security success rates. Any completed evidence remains available."}</p>
    {imageMissing && versionId ? <TargetReadiness key={versionId} apiBase={apiBase} versionId={versionId} /> : null}
    {onSelectVersion ? <Button variant="outline" size="sm" onClick={onSelectVersion}>Select another version</Button> : null}
    <details className="raw-details"><summary>Recorded error details</summary><pre>{error}</pre></details>
    <p>Recovery does not change this historical result. Start a new campaign after the target is ready.</p>
  </section>;
}
