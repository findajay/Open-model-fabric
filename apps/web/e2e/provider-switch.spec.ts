import { type Page, expect, test } from "@playwright/test";

const OLLAMA_URL = process.env.E2E_OLLAMA_URL ?? "http://127.0.0.1:18434";
const OLLAMA_MODEL = process.env.E2E_OLLAMA_MODEL ?? "qwen2.5:0.5b";
const OPENAI_URL = process.env.E2E_OPENAI_URL ?? "http://127.0.0.1:18001/v1";
const OPENAI_MODEL = process.env.E2E_OPENAI_MODEL ?? "Qwen/Qwen2.5-0.5B-Instruct";
const EMULATED = !process.env.E2E_OLLAMA_URL && !process.env.E2E_OPENAI_URL;
const SHOTS = process.env.OMF_E2E_SCREENSHOTS; // directory to save README screenshots

// The tests share one API instance and build on each other (providers added in the first
// test are used later), so they run serially in order.
test.describe.configure({ mode: "serial" });

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false });
}

async function addProvider(
  page: Page,
  p: { adapter: string; id: string; name: string; url: string; keyRef?: string; locality?: string },
) {
  await page.goto("/providers");
  await page.getByRole("button", { name: "Add provider" }).click();
  const form = page.getByTestId("add-provider-form");
  await form.getByLabel("Adapter", { exact: true }).selectOption(p.adapter);
  await form.getByLabel("Provider id").fill(p.id);
  await form.getByLabel("Display name").fill(p.name);
  await form.getByLabel("Base URL").fill(p.url);
  if (p.keyRef) await form.getByLabel(/API key reference/).fill(p.keyRef);
  if (p.locality) await form.getByLabel("Data locality").selectOption(p.locality);
  await form.getByRole("button", { name: "Test connectivity" }).click();
  await expect(form.getByRole("status")).toContainText(/model\(s\)/);
  await form.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByTestId(`provider-${p.id}`)).toContainText("up");
}

async function ask(page: Page, provider: string, model: string, question: string) {
  await page.getByLabel("Provider", { exact: true }).selectOption(provider);
  await page.getByLabel("Model", { exact: true }).selectOption(model);
  const before = await page.getByTestId("message-assistant").count();
  await page.getByRole("textbox", { name: "Message" }).fill(question);
  await page.getByRole("button", { name: "Send" }).click();
  const reply = page.getByTestId("message-assistant").nth(before);
  // streamed: wait for the completion metadata (timings) to appear
  await expect(reply.getByTestId("message-meta")).toContainText("total", { timeout: 30_000 });
  return reply;
}

test("same chat UI switches mock -> Ollama -> remote OpenAI-compatible server", async ({ page }) => {
  // 1. Register two more inference providers at runtime, through the UI.
  await addProvider(page, { adapter: "ollama", id: "ollama-local", name: "Ollama", url: OLLAMA_URL });
  await addProvider(page, {
    adapter: "openai_compatible",
    id: "remote",
    name: "Remote OpenAI-compatible",
    url: OPENAI_URL,
    keyRef: "env:REMOTE_LLM_API_KEY",
    locality: "external",
  });
  await shot(page, "providers");

  // The browser never sees the provider credential, only its reference.
  const providersJson = await (await page.request.get("/api/v1/providers")).text();
  expect(providersJson).toContain("env:REMOTE_LLM_API_KEY");
  expect(providersJson).not.toContain("sk-e2e-0123456789abcdef");

  // 2. One conversation, three engines.
  await page.goto("/playground");
  const mock = await ask(page, "mock", "mock-chat", "What is an inference gateway?");
  await expect(mock).toContainText("Mock response");
  await expect(mock.getByTestId("message-meta")).toContainText("mock/mock-chat");

  const ollama = await ask(page, "ollama-local", OLLAMA_MODEL, "What is an inference gateway?");
  await expect(ollama.getByTestId("message-meta")).toContainText(`ollama-local/${OLLAMA_MODEL}`);
  if (EMULATED) await expect(ollama).toContainText("Ollama emulator");

  const remote = await ask(page, "remote", OPENAI_MODEL, "What is an inference gateway?");
  await expect(remote.getByTestId("message-meta")).toContainText(`remote/${OPENAI_MODEL}`);
  if (EMULATED) await expect(remote).toContainText("Remote OpenAI-compatible emulator");
  await shot(page, "playground");

  // 3. Capabilities drive the controls: image upload only for a vision model.
  await page.getByLabel("Provider", { exact: true }).selectOption("ollama-local");
  await page.getByLabel("Model", { exact: true }).selectOption(OLLAMA_MODEL);
  await expect(page.getByRole("button", { name: "Attach image" })).toHaveCount(0);
  if (EMULATED) {
    await page.getByLabel("Model", { exact: true }).selectOption("qwen2.5vl:3b");
    await expect(page.getByRole("button", { name: "Attach image" })).toBeVisible();
  }

  // 4. The conversation was persisted and every request recorded with provider + model.
  await page.goto("/");
  for (const p of ["mock", "ollama-local", "remote"]) {
    await expect(page.getByRole("cell", { name: new RegExp(`^${p} /`) }).first()).toBeVisible();
  }
  await shot(page, "dashboard");
});

test("unavailable provider produces a meaningful error and the UI keeps working", async ({ page }) => {
  await page.goto("/providers");
  await page.getByRole("button", { name: "Add provider" }).click();
  const form = page.getByTestId("add-provider-form");
  await form.getByLabel("Adapter", { exact: true }).selectOption("ollama");
  await form.getByLabel("Provider id").fill("offline");
  await form.getByLabel("Base URL").fill("http://127.0.0.1:9");
  await form.getByRole("button", { name: "Save provider" }).click();
  await expect(page.getByTestId("provider-offline")).toContainText("down");
  await expect(page.getByTestId("provider-offline")).toContainText("provider_unavailable");

  await page.goto("/playground");
  await page.getByLabel("Provider", { exact: true }).selectOption("mock");
  await page.getByRole("textbox", { name: "Message" }).fill("still working?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("message-assistant").last()).toContainText("Mock response");
});

test("streaming can be stopped from the UI", async ({ page }) => {
  await page.goto("/playground");
  await page.getByLabel("Provider", { exact: true }).selectOption("ollama-local");
  await page.getByLabel("Model", { exact: true }).selectOption(OLLAMA_MODEL);
  await page.getByRole("textbox", { name: "Message" }).fill("Tell me a very long story ".repeat(20));
  await page.getByRole("button", { name: "Send" }).click();
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.getByTestId("message-assistant").last()).toContainText(/request_cancelled|cancelled/i);
  await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
});
