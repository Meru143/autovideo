import { execSync } from "node:child_process";
import os from "node:os";
import { defineConfig } from "@playwright/test";

// The run's record (AGENTS.md): the JSON report keeps the command, revision and environment beside the results
export default defineConfig({
  testDir: "e2e",
  workers: 1, // one browser at a time: the laptop is rendering videos
  reporter: [["list"], ["json", { outputFile: "../../results/e2e/console-playwright.json" }]],
  metadata: {
    command: ["npx playwright", ...process.argv.slice(2)].join(" "),
    revision: execSync("git describe --always --dirty", { encoding: "utf8" }).trim(),
    environment: `node ${process.version}, ${os.type()} ${os.release()} ${os.arch()}`,
  },
  use: {
    baseURL: "http://127.0.0.1:5199",
    channel: "chrome", // the installed Chrome: no browser download
    timezoneId: "Asia/Kolkata", // the laptop's zone, which the console shows times in
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: {
    // a fresh scratch factory every run; the quotes keep cmd.exe from reading the slashes as switches
    command: '".venv/Scripts/python.exe" -m factory console --fixture factory/out/console-e2e --port 5199 --key e2e --no-browser',
    cwd: "../../..", // the worktree root
    url: "http://127.0.0.1:5199/api/health", // a 401 without the key still says the server is up
    timeout: 300_000, // the fixture is built before the server listens
    reuseExistingServer: false,
  },
});
