import { createHash } from "node:crypto";
import type { APIRequestContext } from "@playwright/test";
import { test, expect } from "./fixtures";

async function createRecord(request: APIRequestContext, endpoint: string, data: unknown) {
  const response = await request.post(endpoint, { data });
  expect(response.status(), await response.text()).toBe(201);
  return response.json();
}

test("campaign launch requires current target readiness and a fresh acknowledgement after each selection", async ({ healthyPage: page, request }, testInfo) => {
  testInfo.annotations.push({
    type: "scope",
    description: "Browser launch-gate contract: only image readiness responses are controlled; target/task records use the real API. No campaign is submitted.",
  });
  const suffix = `${testInfo.project.name}-${testInfo.testId}`;
  const target = await createRecord(request, "/v1/targets", { name: `Campaign gates ${suffix}` });
  const versions: { target_version_id: string; image: string }[] = [];
  const tasks: string[][] = [];
  for (const index of [0, 1]) {
    const digest = createHash("sha256").update(`${suffix}-${index}`).digest("hex");
    const version = await createRecord(request, "/v1/target-versions", {
      target_id: target.target_id,
      manifest: {
        target_name: `gate-${digest.slice(0, 12)}`,
        image: `ghcr.io/aml-tests/gates@sha256:${digest}`,
        entrypoint: ["python", "-m", "agent"],
        healthcheck_url: "http://target:8080/healthz",
        invoke_url: "http://target:8080/invoke",
        reset_url: "http://target:8080/reset",
        input_protocol: "http",
        tool_transports: ["http"],
        resource_limits: { cpu_count: 1, memory_mb: 512, pids_limit: 128, timeout_seconds: 60 },
      },
    });
    versions.push(version);
    tasks[index] = [];
    for (const taskIndex of index === 0 ? [0, 1] : [0]) {
      const task = await createRecord(request, "/v1/attack-tasks", {
        target_version_id: version.target_version_id,
        objective: `Gate objective ${suffix}-${index}-${taskIndex}`,
        forbidden_states: [{ verifier_id: "unapproved-payment", kind: "unapproved_payment", parameters: { minimum_amount: 100 }, severity: 1 }],
        available_channels: ["user_message"],
        max_steps_per_episode: 3,
        max_episodes: 2 + taskIndex,
        max_model_tokens: 80,
        max_total_cost: 0.25,
        max_wall_time_seconds: 45,
        max_concurrency: 1,
      });
      tasks[index].push(task.attack_task_id);
    }
  }

  // The browser suite has no execution host. Controlling only this boundary lets
  // us exercise each UI condition independently of Docker/image availability.
  const readiness = new Map(versions.map(version => [version.target_version_id, "READY"]));
  readiness.set(versions[0].target_version_id, "UNAVAILABLE");
  await page.route(/\/v1\/target-versions\/[^/]+\/(?:prepare|readiness)$/, async route => {
    const versionId = new URL(route.request().url()).pathname.split("/").at(-2)!;
    const version = versions.find(candidate => candidate.target_version_id === versionId);
    if (!version) return route.continue();
    const ready = readiness.get(versionId) === "READY";
    await route.fulfill({
      status: 200,
      json: {
        status: ready ? "READY" : "UNAVAILABLE",
        code: ready ? "image_available" : "image_missing",
        message: ready ? "The selected image is available." : "The selected image is not available.",
        image: version.image,
        image_id: ready ? "sha256:controlled-local-image" : null,
        recoverable: false,
        restored: false,
        checked_at: "2026-09-17T00:00:00Z",
      },
    });
  });
  const campaignSubmissions: string[] = [];
  page.on("request", outgoing => {
    if (outgoing.method() === "POST" && new URL(outgoing.url()).pathname === "/v1/campaigns") {
      campaignSubmissions.push(outgoing.url());
    }
  });
  await page.reload();
  await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "New campaign", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Start campaign", exact: true });
  const versionSelect = dialog.getByLabel("Target version", { exact: true });
  const taskSelect = dialog.getByLabel("Attack task", { exact: true });
  const acknowledgement = dialog.getByRole("checkbox", { name: "I reviewed the complete task budget and want to queue this campaign." });
  const queue = dialog.getByRole("button", { name: "Queue campaign", exact: true });
  const readinessRegion = dialog.getByRole("region", { name: "Target readiness", exact: true });

  await test.step("Acknowledging a budget cannot bypass an unavailable target", async () => {
    await versionSelect.selectOption(versions[0].target_version_id);
    await taskSelect.selectOption(tasks[0][0]);
    await expect(readinessRegion.getByText("Target unavailable", { exact: true })).toBeVisible();
    await expect(dialog.getByRole("region", { name: "Campaign budget envelope" })).toBeVisible();
    await acknowledgement.check();
    await expect(acknowledgement).toBeChecked();
    await expect(queue).toBeDisabled();
  });

  await test.step("A ready target still requires explicit budget acknowledgement", async () => {
    await acknowledgement.uncheck();
    readiness.set(versions[0].target_version_id, "READY");
    await readinessRegion.getByRole("button", { name: "Check again", exact: true }).click();
    await expect(readinessRegion.getByText("Ready to start", { exact: true })).toBeVisible();
    await expect(queue).toBeDisabled();
    await acknowledgement.check();
    await expect(queue).toBeEnabled();
  });

  await test.step("Changing the task invalidates the previous budget acknowledgement", async () => {
    await taskSelect.selectOption(tasks[0][1]);
    await expect(acknowledgement).not.toBeChecked();
    await expect(queue).toBeDisabled();
    await acknowledgement.check();
    await expect(queue).toBeEnabled();
  });

  await test.step("Changing versions selects that version's task and requires a fresh acknowledgement", async () => {
    await versionSelect.selectOption(versions[1].target_version_id);
    await expect(taskSelect).toHaveValue(tasks[1][0]);
    await expect(readinessRegion.getByText("Ready to start", { exact: true })).toBeVisible();
    await expect(acknowledgement).not.toBeChecked();
    await expect(queue).toBeDisabled();
    await acknowledgement.check();
    await expect(queue).toBeEnabled();
  });

  await test.step("Losing readiness disables launch even after acknowledgement", async () => {
    readiness.set(versions[1].target_version_id, "UNAVAILABLE");
    await readinessRegion.getByRole("button", { name: "Check again", exact: true }).click();
    await expect(readinessRegion.getByText("Target unavailable", { exact: true })).toBeVisible();
    await expect(acknowledgement).toBeChecked();
    await expect(queue).toBeDisabled();
  });
  expect(campaignSubmissions).toEqual([]);
});
