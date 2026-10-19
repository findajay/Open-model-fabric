import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end acceptance test: the React chat UI switches between a mock provider, an
 * Ollama server and a remote OpenAI-compatible server without frontend or domain code
 * changes. By default the Ollama and OpenAI-compatible servers are the protocol
 * emulators in tests/emulators (no model weights needed); set E2E_OLLAMA_URL /
 * E2E_OPENAI_URL (+ E2E_OPENAI_MODEL, E2E_OLLAMA_MODEL) to run against real servers.
 */
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const python = process.env.OMF_PYTHON ?? path.join(repo, ".venv/bin/python");
const dataDir = process.env.OMF_E2E_DATA_DIR ?? path.join(os.tmpdir(), "omf-e2e");
const PORTS = { api: 18080, web: 14173, ollama: 18434, openai: 18001 };
export const E2E_API_KEY = "sk-e2e-0123456789abcdef";

interface WebServer {
  command: string;
  cwd?: string;
  url: string;
  env: Record<string, string>;
  reuseExistingServer: boolean;
  timeout: number;
}

const servers: WebServer[] = [
  {
    command: `rm -rf ${dataDir} && ${python} -m omf_api --config configs/development.yaml --port ${PORTS.api}`,
    cwd: repo,
    url: `http://127.0.0.1:${PORTS.api}/health/ready`,
    env: { OMF_DATA_DIR: dataDir, REMOTE_LLM_API_KEY: E2E_API_KEY, OMF_LOG_LEVEL: "WARNING" },
    reuseExistingServer: false,
    timeout: 60_000,
  },
  {
    command: `pnpm exec vite build && pnpm exec vite preview --port ${PORTS.web} --strictPort`,
    url: `http://127.0.0.1:${PORTS.web}`,
    env: { OMF_API_URL: `http://127.0.0.1:${PORTS.api}` },
    reuseExistingServer: false,
    timeout: 120_000,
  },
];
if (!process.env.E2E_OLLAMA_URL) {
  servers.push({
    command: `${python} -m tests.emulators.ollama_emulator --port ${PORTS.ollama}`,
    cwd: repo,
    url: `http://127.0.0.1:${PORTS.ollama}/api/version`,
    env: {},
    reuseExistingServer: false,
    timeout: 30_000,
  });
}
if (!process.env.E2E_OPENAI_URL) {
  servers.push({
    command: `${python} -m tests.emulators.openai_emulator --port ${PORTS.openai} --api-key ${E2E_API_KEY}`,
    cwd: repo,
    url: `http://127.0.0.1:${PORTS.openai}/v1/models`,
    env: {},
    reuseExistingServer: false,
    timeout: 30_000,
  });
}

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORTS.web}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: servers,
});
