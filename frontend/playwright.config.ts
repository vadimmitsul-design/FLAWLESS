import { defineConfig } from "@playwright/test";

const baseURL = process.env.CABINET_TEST_URL || "http://127.0.0.1:3000";
if (!["127.0.0.1", "localhost"].includes(new URL(baseURL).hostname)) {
  throw new Error("Cabinet smoke tests must target a local preview.");
}

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 20_000,
  use: { baseURL },
  reporter: "list",
});
