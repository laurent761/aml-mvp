import { createHash } from "node:crypto";
import type { Locator, Page } from "@playwright/test";
import { test, expect, navigate } from "./fixtures";

async function submitCreation(page: Page, dialog: Locator, button: string, endpoint: string) {
  const saved = page.waitForResponse(response =>
    new URL(response.url()).pathname === endpoint && response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: button, exact: true }).click();
  const response = await saved;
  expect(response.status(), await response.text()).toBe(201);
  await expect(dialog).not.toBeVisible();
  return response.json();
}

test("a target, digest-pinned version, and bounded task persist with the correct identity and inspectable budgets", async ({ healthyPage: page, request }, testInfo) => {
  const suffix = `${testInfo.project.name}-${testInfo.testId}`;
  const name = `Registration ${suffix}`;
  const digest = createHash("sha256").update(suffix).digest("hex");
  const image = `ghcr.io/aml-tests/browser-agent@sha256:${digest}`;
  const objective = `Verify isolated payment approval for ${suffix}`;
  await navigate(page, "Targets");

  await test.step("Create a stable target identity through the browser", async () => {
    await page.getByRole("button", { name: "Register target", exact: true }).first().click();
    const dialog = page.getByRole("dialog", { name: "Register target", exact: true });
    await dialog.getByLabel("Display name").fill(name);
    await submitCreation(page, dialog, "Register target", "/v1/targets");
    await expect(page.getByRole("row").filter({ hasText: name })).toContainText("No version");
  });

  // Read back the identity to choose the exact record, independent of other projects' data.
  const targetsResponse = await request.get("/v1/targets?limit=500");
  expect(targetsResponse.status()).toBe(200);
  const targets = (await targetsResponse.json()).filter((row: { name: string }) => row.name === name);
  expect(targets).toHaveLength(1);
  const targetId: string = targets[0].target_id;
  let versionId: string;
  let manifest: Record<string, unknown>;

  await test.step("Attach and inspect a digest-pinned immutable manifest", async () => {
    await page.getByRole("button", { name: "Add version", exact: true }).first().click();
    const dialog = page.getByRole("dialog", { name: "Add immutable target version", exact: true });
    await dialog.getByLabel("Target", { exact: true }).selectOption(targetId);
    const editor = dialog.getByLabel("TargetManifest JSON");
    manifest = {
      ...JSON.parse(await editor.inputValue()),
      target_name: `browser-${digest.slice(0, 16)}`,
      image,
    };
    await editor.fill(JSON.stringify(manifest, null, 2));
    const version = await submitCreation(page, dialog, "Add version", "/v1/target-versions");
    versionId = version.target_version_id;
    expect(version.target_id).toBe(targetId);
    expect(version.image).toBe(image);
    expect(version.image_digest).toBe(`sha256:${digest}`);

    const versionRow = page.getByRole("row").filter({ has: page.getByText(image, { exact: true }) });
    await expect(versionRow).toHaveCount(1);
    await versionRow.getByRole("button", { name: /^Inspect target version / }).click();
    const inspector = page.getByRole("dialog", { name: /^Target version / });
    await expect(inspector.locator("pre")).toContainText(versionId);
    const record = JSON.parse(await inspector.locator("pre").innerText());
    expect(record.target_id).toBe(targetId);
    expect(record.manifest).toMatchObject(manifest);
    await page.keyboard.press("Escape");
    await expect(inspector).not.toBeVisible();
  });

  let taskId: string;
  let task: Record<string, unknown>;
  await test.step("Bind explicit limits to that version and inspect the saved task", async () => {
    await page.getByRole("button", { name: "Attack task", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Create attack task", exact: true });
    await dialog.getByLabel("Target version", { exact: true }).selectOption(versionId);
    const editor = dialog.getByLabel("AttackTask JSON");
    task = {
      ...JSON.parse(await editor.inputValue()),
      objective,
      available_channels: ["user_message"],
      max_steps_per_episode: 3,
      max_episodes: 2,
      max_model_tokens: 80,
      max_total_cost: 0.25,
      max_wall_time_seconds: 45,
      max_concurrency: 1,
      random_seed: 42,
    };
    await editor.fill(JSON.stringify(task, null, 2));
    const created = await submitCreation(page, dialog, "Create task", "/v1/attack-tasks");
    taskId = created.attack_task_id;
    const taskRow = page.getByRole("row").filter({ hasText: objective });
    await expect(taskRow).toHaveCount(1);
    await expect(taskRow.getByRole("cell").nth(3)).toHaveText("2");
    await expect(taskRow.getByRole("cell").nth(4)).toHaveText("80");
    await taskRow.getByRole("button").click();
    const inspector = page.getByRole("dialog", { name: /^Attack task / });
    await expect(inspector.locator("pre")).toContainText(taskId);
    const record = JSON.parse(await inspector.locator("pre").innerText());
    expect(record.target_version_id).toBe(versionId);
    expect(record.document).toMatchObject({ ...task, target_version_id: versionId });
    await page.keyboard.press("Escape");
  });

  await test.step("Reload and confirm each relationship survived persistence", async () => {
    await page.reload();
    await expect(page.getByText("API ready", { exact: true })).toHaveCount(1);
    const targetRow = page.getByRole("row").filter({ hasText: name });
    await expect(targetRow).toHaveCount(1);
    await expect(targetRow.getByRole("cell").nth(2)).toHaveText("1");
    await expect(targetRow.getByRole("cell").nth(3)).toHaveText("1");
    await expect(page.getByRole("row").filter({ hasText: objective })).toHaveCount(1);

    const persistedVersion = await request.get(`/v1/target-versions/${versionId}`);
    expect(persistedVersion.status()).toBe(200);
    expect((await persistedVersion.json()).manifest).toMatchObject(manifest);
    const persistedTask = await request.get(`/v1/attack-tasks/${taskId}`);
    expect(persistedTask.status()).toBe(200);
    expect((await persistedTask.json()).document).toMatchObject({ ...task, target_version_id: versionId });
  });
});

test("server budget validation preserves the task editor and a corrected retry creates one task", async ({ healthyPage: page, request, apiFailures }, testInfo) => {
  const objective = `Budget validation ${testInfo.project.name}-${testInfo.testId}`;
  await navigate(page, "Targets");
  await page.getByRole("button", { name: "Attack task", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Create attack task", exact: true });
  const versionId = await dialog.getByLabel("Target version", { exact: true }).inputValue();
  expect(versionId).not.toBe("");
  const editor = dialog.getByLabel("AttackTask JSON");
  const document = { ...JSON.parse(await editor.inputValue()), objective, max_episodes: 0 };
  const invalid = JSON.stringify(document, null, 2);
  await editor.fill(invalid);
  apiFailures.expectHttp({ method: "POST", path: "/v1/attack-tasks", status: 422, reason: "Submit max_episodes=0 to verify server validation and corrected retry" });

  const rejected = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/v1/attack-tasks" && response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Create task", exact: true }).click();
  const response = await rejected;
  expect(response.status()).toBe(422);
  const failure = await response.json();
  expect(failure.detail).toEqual(expect.arrayContaining([
    expect.objectContaining({ loc: ["body", "max_episodes"] }),
  ]));
  await expect(page.locator("[data-sonner-toast]").filter({ hasText: "max_episodes" })).toBeVisible();
  await expect(editor).toHaveValue(invalid);
  await expect(dialog.getByRole("button", { name: "Create task", exact: true })).toBeEnabled();

  const matchingTasks = async () => {
    const result = await request.get(`/v1/attack-tasks?target_version_id=${versionId}&limit=500`);
    expect(result.status()).toBe(200);
    return (await result.json()).filter((row: { document: { objective: string } }) => row.document.objective === objective);
  };
  expect(await matchingTasks()).toHaveLength(0);
  await editor.fill(JSON.stringify({ ...document, max_episodes: 2 }, null, 2));
  const created = await submitCreation(page, dialog, "Create task", "/v1/attack-tasks");
  const saved = await matchingTasks();
  expect(saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({
    attack_task_id: created.attack_task_id,
    target_version_id: versionId,
    document: { objective, max_episodes: 2 },
  });
  await expect(page.getByRole("row").filter({ hasText: objective })).toHaveCount(1);
});
