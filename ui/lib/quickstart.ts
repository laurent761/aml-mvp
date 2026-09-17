export type TourMode = "fixture" | "model";
export type TourSelection = { versionId: string; taskId: string };
export type TourBundle = {
  bundle_id: string;
  target_version_id: string;
  execution_mode: string;
  scenario: { name: string; legitimate_task: string; attack_objective: string };
};
export type TourTask = {
  attack_task_id: string;
  target_version_id: string;
  document: Record<string, unknown>;
};

export const tourSteps = ["Choose an agent", "Register the target", "Check the task", "Launch a campaign", "Read the evidence"];

export const modelEnvironment = `# Edit these values in backend/.env
TARGET_MODEL_PROVIDER=hosted_openai_compatible
TARGET_MODEL_BASE_URL=https://YOUR_PROVIDER/v1/
TARGET_MODEL_NAME=YOUR_MODEL_ID
TARGET_MODEL_API_KEY=YOUR_KEY
# Enter the provider's current prices to enable priced cost limits.
TARGET_MODEL_INPUT_COST_PER_MILLION=0
TARGET_MODEL_OUTPUT_COST_PER_MILLION=0`;

export function registrationCommands(mode: TourMode): string {
  if (mode === "fixture") return "bash scripts/prepare-reference.sh";
  return `# Apply the target settings, then export the exact running profile.
mkdir -p var/bundles
docker compose up -d --no-deps --force-recreate capsule-supervisor
docker compose exec -T capsule-supervisor python -m adversarial_agent_mvp.bundle_cli inference-profile --output /tmp/target-profile.json
docker compose exec -T capsule-supervisor cat /tmp/target-profile.json > var/bundles/target-profile.json
bash scripts/prepare-reference.sh --inference-profile var/bundles/target-profile.json`;

}

/** Never substitute a fixture, another target, or a stale selected task. */
export function tourSelection(bundles: TourBundle[], tasks: TourTask[], mode: TourMode, versionId: string, taskId: string): TourSelection | null {
  const bundle = bundles.find(row => row.target_version_id === versionId && row.execution_mode === mode);
  const task = tasks.find(row => row.attack_task_id === taskId && row.target_version_id === bundle?.target_version_id);
  return bundle && task ? { versionId: bundle.target_version_id, taskId: task.attack_task_id } : null;
}

/** Preselect only unambiguous registrations; retain explicit choices for validation. */
export function tourDefaults(bundles: TourBundle[], tasks: TourTask[], mode: TourMode, selectedVersion = "", selectedTask = ""): TourSelection {
  const matching = bundles.filter(row => row.execution_mode === mode);
  const versionId = selectedVersion || (matching.length === 1 ? matching[0].target_version_id : "");
  const validVersion = matching.some(row => row.target_version_id === versionId);
  const matchingTasks = validVersion ? tasks.filter(row => row.target_version_id === versionId) : [];
  return { versionId, taskId: selectedTask || (matchingTasks.length === 1 ? matchingTasks[0].attack_task_id : "") };
}
