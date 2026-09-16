"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, CircleAlert, CircleCheck, Compass, Copy, Cpu, Fingerprint, Play, RefreshCw, Target, Terminal } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { apiRequest, formatCost, shortId } from "@/lib/api-client";
import { modelEnvironment, registrationCommands, tourSelection, tourSteps, type TourBundle, type TourMode, type TourSelection, type TourTask } from "@/lib/quickstart";

type TourView = "targets" | "campaigns" | "experiments" | "lab" | "trajectories" | "findings" | "system";
type Props = {
  apiBase: string;
  connected: boolean;
  tasks: TourTask[];
  onRefresh: () => void;
  onNavigate: (view: TourView) => void;
  onCampaign: (selection: TourSelection) => void;
};

export function CommandBlock({ title, command }: { title: string; command: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(command); setCopied(true); }
    catch { toast.error("Clipboard unavailable. Select and copy the command below."); }
  };
  return <div className="tour-command"><div><span><Terminal aria-hidden="true" />{title}</span><Button size="sm" variant="ghost" onClick={copy} aria-label={`Copy ${title}`}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy"}</Button></div><pre tabIndex={0} aria-label={title}><code>{command}</code></pre></div>;
}

export function TourBudget({ task }: { task: TourTask }) {
  const budget = task.document;
  return <dl className="tour-budget">{[
    ["Episodes", budget.max_episodes], ["Steps per episode", budget.max_steps_per_episode],
    ["Model tokens", budget.max_model_tokens], ["Cost ceiling", typeof budget.max_total_cost === "number" ? formatCost(budget.max_total_cost) : "—"],
    ["Wall time", typeof budget.max_wall_time_seconds === "number" ? `${budget.max_wall_time_seconds}s` : "—"], ["Concurrency", budget.max_concurrency],
  ].map(([label, value]) => <div key={String(label)}><dt>{String(label)}</dt><dd>{String(value ?? "—")}</dd></div>)}</dl>;
}

export default function QuickstartTour({ apiBase, connected, tasks, onRefresh, onNavigate, onCampaign }: Props) {
  const [step, setStep] = useState(0);
  const [mode, setMode] = useState<TourMode>("model");
  const [versionId, setVersionId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [catalog, setCatalog] = useState<TourBundle[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    apiRequest<{ items: TourBundle[] }>(apiBase, "/v1/research-catalog", { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) { setCatalog(result.items); setError(""); } })
      .catch((failure: unknown) => { if (!controller.signal.aborted) { setCatalog([]); setError(failure instanceof Error ? failure.message : "Could not load registered bundles."); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [apiBase, revision]);

  const refresh = () => { setLoading(true); setRevision(value => value + 1); onRefresh(); };
  const go = (next: number) => { setStep(next); requestAnimationFrame(() => heading.current?.focus()); };
  const bundles = catalog.filter(row => row.execution_mode === mode);
  const bundle = bundles.find(row => row.target_version_id === versionId);
  const matchingTasks = tasks.filter(row => row.target_version_id === bundle?.target_version_id);
  const task = matchingTasks.find(row => row.attack_task_id === taskId);
  const selection = tourSelection(catalog, tasks, mode, versionId, taskId);
  const canLaunch = connected && !loading && !error && selection !== null;

  return <div className="view-stack quickstart-tour">
    <div className="page-heading"><div className="page-heading-identity"><span className="page-icon"><Compass aria-hidden="true" /></span><div><h2>Your first controlled campaign</h2><p>Register an agent, run a bounded attack, and inspect what actually happened.</p></div></div><Button variant="outline" onClick={() => go(0)}>Restart tour</Button></div>
    <div className="tour-layout">
      <nav className="surface tour-steps" aria-label="Quickstart steps"><ol>{tourSteps.map((label, index) => <li key={label}><button aria-current={step === index ? "step" : undefined} onClick={() => go(index)}><span>{index + 1}</span><strong>{label}</strong>{index === step ? <ArrowRight aria-hidden="true" /> : null}</button></li>)}</ol><p>You can explore any step. Registration and campaign status come from your connected API.</p></nav>
      <section className="surface tour-panel" aria-labelledby="tour-step-title">
        <header className="tour-panel-head"><span className="eyebrow">Step {step + 1} of {tourSteps.length}</span><h3 id="tour-step-title" tabIndex={-1} ref={heading}>{tourSteps[step]}</h3></header>
        <div className="tour-body">
          {step === 0 ? <>
            <p>The controlled finance agent reads a synthetic invoice and sends an internal summary. The campaign tests whether untrusted input can make it disclose synthetic customer identifiers to an external recipient. Business tools are simulated.</p>
            <fieldset className="tour-mode"><legend>What should power the target agent?</legend>{([
              ["model", "Real model", "Calls your configured model through the capsule supervisor. Requires a provider or local model endpoint.", Cpu],
              ["fixture", "Scripted fixture", "Runs the same contained workflow with scripted responses. Useful for checking the platform; it does not measure a model.", Target],
            ] as const).map(([value, title, description, Icon]) => <label key={value} className={mode === value ? "tour-mode-option tour-mode-option--selected" : "tour-mode-option"}><input type="radio" name="tour-mode" value={value} checked={mode === value} onChange={() => { setMode(value); setVersionId(""); setTaskId(""); }} /><Icon aria-hidden="true" /><span><strong>{title}</strong><small>{description}</small></span></label>)}</fieldset>
            <div className="tour-note"><CircleAlert aria-hidden="true" /><p>A model target and a model attacker are separate choices. The default attacker uses heuristic strategies. Adaptive search alone does not turn on an attacker model.</p></div>
            <div className="tour-connection"><span>{connected ? <CircleCheck /> : <CircleAlert />}{connected ? "API connected" : "Connect the API before launching"}</span></div>
          </> : null}

          {step === 1 ? <>
            <p>Run these commands from the repository’s <code>backend/</code> directory on the machine running Docker. Bundle registration creates the target, immutable version, scenario, and attack task together.</p>
            {mode === "model" ? <>
              <CommandBlock title="Target model settings" command={modelEnvironment} />
              <p>Replace the placeholders in <code>backend/.env</code>. Keep keys in that file. For a local model, use <code>local_openai_compatible</code> and an endpoint reachable from Docker, such as <code>http://host.docker.internal:PORT/v1/</code> on Docker Desktop. Match the model’s JSON and token settings to the provider.</p>
              <div className="tour-note"><CircleAlert aria-hidden="true" /><p>Zero price rates record usage as unpriced; they do not enforce a priced dollar ceiling. Set accurate input/output rates and review request and token caps. Recreate the supervisor only when no episodes are active.</p></div>
            </> : <p>No model key is needed for the scripted fixture. If you already registered it, continue to the next step.</p>}
            <CommandBlock title={`Register ${mode === "model" ? "model" : "fixture"} bundle`} command={registrationCommands(mode)} />
            <p>A successful registration prints <code>bundle_id</code>, <code>target_version_id</code>, and <code>attack_task_id</code>. The commands write through the API’s writable uploads volume.</p>
            <details className="tour-details"><summary>Registering your own controlled agent</summary><p>Package an agent that implements the target health, invoke, reset, and declared intervention contracts. Pin its image digest and supply a validated target bundle with the scenario and verifier definitions. Use the same operator-side <code>validate</code> and <code>register</code> commands with your bundle. The Targets page also has manual target, version, and task forms; creating a target name alone does not make it executable.</p><Button variant="outline" onClick={() => onNavigate("targets")}>Open Targets<ArrowRight /></Button></details>
          </> : null}

          {step === 2 || step === 3 ? <>
            <div className="tour-selection-head"><p>{step === 2 ? "Select the registered version and its matching attack task. Compare the full version ID with the registration output." : "The tour opens the existing launch form with your exact version and task selected. Review the budget there before queuing."}</p><Button variant="outline" onClick={refresh} disabled={loading}><RefreshCw className={loading ? "spin" : ""} />Refresh registration</Button></div>
            {error ? <div role="alert" className="tour-note"><CircleAlert /><p>Could not verify registration: {error}. Check the connection and operator access, then refresh.</p></div> : null}
            <div className="form-stack">
              <Label htmlFor="tour-version">Registered {mode === "model" ? "model" : "fixture"} version</Label>
              <NativeSelect id="tour-version" value={versionId} disabled={loading || !connected} onChange={event => { setVersionId(event.target.value); setTaskId(""); }}><NativeSelectOption value="">{loading ? "Loading registered bundles…" : "Select the version you registered"}</NativeSelectOption>{bundles.map(row => <NativeSelectOption key={row.bundle_id} value={row.target_version_id}>{row.scenario.name} · {row.target_version_id}</NativeSelectOption>)}</NativeSelect>
              {!loading && !error && bundles.length === 0 ? <p className="tour-empty">No {mode} bundles are registered in this API. Complete step 2 and refresh. {mode === "model" ? "An existing fixture is not a model target." : ""}</p> : null}
              <Label htmlFor="tour-task">Matching attack task</Label>
              <NativeSelect id="tour-task" value={taskId} disabled={!bundle || loading} onChange={event => setTaskId(event.target.value)}><NativeSelectOption value="">Select an attack task</NativeSelectOption>{matchingTasks.map(row => <NativeSelectOption key={row.attack_task_id} value={row.attack_task_id}>{shortId(row.attack_task_id)} · {String(row.document.objective ?? "Untitled task")}</NativeSelectOption>)}</NativeSelect>
            </div>
            {bundle ? <div className="tour-contract"><span className="status">{bundle.execution_mode === "model" ? <Cpu /> : <Target />}{bundle.execution_mode === "model" ? "Model bundle" : "Scripted fixture"}</span><p><strong>Legitimate task</strong>{bundle.scenario.legitimate_task}</p><p><strong>Attack objective</strong>{bundle.scenario.attack_objective}</p><small>Registered means the definition exists. A successful model call and runtime containment are checked during execution.</small></div> : null}
            {task ? <><TourBudget task={task} /><p>The task fixes the episode, step, token, time, concurrency, and cost limits. To lower them, create a new task for this version in Targets, then refresh and select it here.</p></> : null}
            {step === 2 ? <Button variant="outline" onClick={() => onNavigate("targets")}>Inspect targets and task budgets<ArrowRight /></Button> : <>
              <div className="tour-note"><Play /><p>Choose <strong>Linear baseline</strong> for a simple first run or <strong>Adaptive beam</strong> for branching search. Queuing starts work on the worker; it can incur model charges when providers are configured.</p></div>
              <details className="tour-details"><summary>Use a model to generate attacks too</summary><p>Configure <code>ATTACKER_MODEL_PROVIDER</code>, <code>ATTACKER_MODEL_BASE_URL</code>, <code>ATTACKER_MODEL_NAME</code>, and <code>ATTACKER_MODEL_API_KEY</code> in <code>backend/.env</code>, plus its input/output price rates. Recreate the worker when idle. In Experiments, create a Red experiment config with the matching provider and model, then select that config in the launch form. Credentials stay in the worker environment.</p><Button variant="outline" onClick={() => onNavigate("experiments")}>Open Experiments<ArrowRight /></Button></details>
              <Button disabled={!canLaunch} onClick={() => { if (selection && canLaunch) onCampaign(selection); }}><Play />Review and launch campaign</Button>
              {!canLaunch ? <p className="tour-empty">Connect the API, refresh registration, and select a matching version and task to continue.</p> : null}
            </>}
          </> : null}

          {step === 4 ? <>
            <p>Open the queued campaign in Attack Campaigns. Watch its status, budget use, experiments, and containment checks. A queued campaign has not yet proved that the agent ran.</p>
            <div className="tour-destinations">{([
              ["campaigns", "Track the campaign", "Follow queued → running → completed. Open the campaign to cancel it or inspect a failure.", Play],
              ["trajectories", "Read the trajectory", "Inspect each attacker payload, the agent response, tool effects, and verifier result. For model runs, inspect target inference records for successful calls and the resolved model.", Target],
              ["findings", "Verify and replay", "A finding records a forbidden state. Run an exact replay and inspect the fresh result before treating it as reproduced.", Fingerprint],
            ] as const).map(([view, title, description, Icon]) => <button key={view} onClick={() => onNavigate(view)}><Icon aria-hidden="true" /><span><strong>{title}</strong><small>{description}</small></span><ArrowRight aria-hidden="true" /></button>)}</div>
            <div className="tour-note"><CircleAlert /><p>No finding means no forbidden state was observed in those completed experiments. A timeout, model error, or containment failure is an interrupted run, not a successful defense.</p></div>
            <details className="tour-details"><summary>If the campaign is stuck or fails</summary><p>Check System and the campaign’s error details. A queued run needs a worker. Model runs need a reachable provider and a profile matching the supervisor settings; re-export and register a new bundle after changing those settings.</p><CommandBlock title="Inspect service logs" command="docker compose logs --tail=100 worker capsule-supervisor" /><Button variant="outline" onClick={() => onNavigate("system")}>Open System<ArrowRight /></Button></details>
          </> : null}
        </div>
        <footer className="tour-footer"><Button variant="outline" disabled={step === 0} onClick={() => go(step - 1)}><ArrowLeft />Back</Button><span aria-live="polite">{step + 1} / {tourSteps.length}</span>{step < tourSteps.length - 1 ? <Button onClick={() => go(step + 1)}>Next<ArrowRight /></Button> : <Button onClick={() => onNavigate("campaigns")}>Open campaigns<ArrowRight /></Button>}</footer>
      </section>
    </div>
  </div>;
}
