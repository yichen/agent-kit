// LIVE canary for the orchestrator extension. Drives the REAL executor
// (herdr commands, pane lifecycle, state file) against a real pi worker on
// the local ollama model, with a stubbed `gh` so no real PR or CI is touched.
//
// Usage: node orchestrator-canary.mjs <extensionDir> <repoRoot>
// Env:   AGENT_KIT_CANARY_KEEP=1 to keep the scratch dir on failure.

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [extDir, repoRoot] = process.argv.slice(2);
const scratch = mkdtempSync(join(tmpdir(), "orch-canary-"));
const statePath = join(scratch, "state.json");
const workerName = "orch-canary-1";
let workerPaneId = null;
const failures = [];

function step(label, fn) {
  try {
    const out = fn();
    console.log(`PASS ${label}`);
    return out;
  } catch (error) {
    failures.push(label);
    console.error(`FAIL ${label}: ${error.message}`);
    throw error;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(label, fn, timeoutMs = 240000, intervalMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(intervalMs);
  }
  throw new Error(`timeout waiting for ${label}${lastError ? `: ${lastError.message}` : ""}`);
}

const { validateConfig, launchGate } = await import(join(extDir, "lib.mjs"));
const executor = await import(join(extDir, "executor.mjs"));

// Stub `gh`: returns whatever JSON the control file currently holds, so the
// canary scripts CI states without touching GitHub.
const ghStub = join(scratch, "gh-stub");
const ghControl = join(scratch, "gh-control.json");
writeFileSync(ghStub, `#!/usr/bin/env bash\ncat '${ghControl}'\n`);
chmodSync(ghStub, 0o755);

function setGh(state, rollup) {
  writeFileSync(ghControl, JSON.stringify({ state, statusCheckRollup: rollup }));
}

const config = validateConfig(
  {
    pollSeconds: 2,
    stallSeconds: 2,
    idleNudgeSeconds: 2,
    mergeNudgeSeconds: 2,
    ciMaxRounds: 2,
    autoAnswerCap: 2,
    ciCommand: ghStub,
    statePath,
    worker: { namePrefix: "orch-canary", startArgs: ["--provider", "ollama", "--model", "qwen3.8:27b-mlx-128k"] },
  },
  {},
);
assert.equal(config.ok, true, `canary config invalid: ${config.ok ? "" : config.errors.join(";")}`);
const cfg = config.config;

try {
  // 1. Launch a real worker pane: split + pi agent start + initial prompt.
  const split = await executor.herdrJson(["pane", "split", "--current", "--direction", "right", "--cwd", repoRoot, "--no-focus"]);
  workerPaneId = split.json?.result?.pane?.pane_id;
  assert.ok(workerPaneId, `pane split failed: ${split.stderr}`);
  const start = await executor.herdrJson([
    "agent", "start", workerName, "--kind", "pi", "--pane", workerPaneId, "--timeout", "60000", "--",
    ...cfg.worker.startArgs,
  ]);
  assert.ok(start.ok, `agent start failed: ${start.stderr}`);
  console.log(`PASS worker ${workerName} live in pane ${workerPaneId}`);

  // Seed state the way orch_launch_worker would.
  executor.saveState(statePath, {
    version: 1,
    maxWorkers: 1,
    workers: [
      {
        name: workerName,
        paneId: workerPaneId,
        ticket: 999,
        invocation: "Reply with exactly: OK",
        prNumber: null,
        prSource: null,
        ciRounds: 0,
        launchedAt: Date.now(),
        lastActivityAt: Date.now(),
        lastChangeSeq: start.json?.result?.agent?.state_change_seq ?? 0,
        lastStatus: "idle",
        phase: "working",
      },
    ],
    history: [],
    pendingEvents: [],
  });

  step("gate: second launch refused at cap", () => {
    const state = executor.loadState(statePath);
    const gate = launchGate(state);
    assert.equal(gate.allowed, false);
    assert.equal(gate.reason, "at_cap");
  });

  // 2. Ticks while the worker works: only canary may be targeted.
  let resumeNudge = null;
  await waitFor("worker turn settles to idle", async () => {
    const { actions, state } = await executor.runTick(statePath, cfg, repoRoot);
    for (const action of actions) {
      assert.equal(action.worker, workerName, `action targeted non-canary agent: ${JSON.stringify(action)}`);
    }
    assert.ok(state.pendingEvents.length === 0, `unexpected early events: ${JSON.stringify(state.pendingEvents)}`);
    const record = state.workers[0];
    if (record && record.lastStatus === "idle" && record.resumeNudged) {
      resumeNudge = actions.find((a) => a.type === "prompt");
      return true;
    }
    return false;
  });
  step("watcher: resume nudge fired after idle window", () => {
    assert.ok(resumeNudge, "expected a resume prompt action");
    assert.match(resumeNudge.text, /Continue the task/);
  });
  step("watcher: nudge text visible on worker screen", () => {
    const read = execFileSync("herdr", ["agent", "read", workerName, "--source", "recent-unwrapped", "--lines", "80"], { encoding: "utf8" });
    assert.ok(executor.stripAnsi(read).includes("Continue the task"), "nudge text not found on pane");
  });

  // Let the worker finish answering the resume nudge before the CI phase.
  await waitFor("settle idle after resume nudge", async () => {
    const { state } = await executor.runTick(statePath, cfg, repoRoot);
    return state.workers[0]?.lastStatus === "idle";
  });

  // 3. CI red: pin a PR, stub gh red, expect ciMaxRounds nudges then wake event.
  {
    const state = executor.loadState(statePath);
    state.workers[0].prNumber = 99;
    state.workers[0].prSource = "pinned";
    executor.saveState(statePath, state);
  }
  setGh("OPEN", [{ status: "COMPLETED", conclusion: "FAILURE" }]);
  let exhaustedEvent = null;
  await waitFor("CI rounds exhausted", async () => {
    const { actions, state } = await executor.runTick(statePath, cfg, repoRoot);
    for (const action of actions) {
      assert.equal(action.worker, workerName);
      if (action.type === "prompt") assert.match(action.text, /CI is red on PR #99/);
    }
    const event = state.pendingEvents.find((e) => e.code === "CI_ROUNDS_EXHAUSTED");
    if (event) {
      exhaustedEvent = event;
      return true;
    }
    return false;
  });
  step("ci: exhausted after ciMaxRounds with wake event", () => {
    assert.equal(exhaustedEvent.rounds, cfg.ciMaxRounds);
    assert.equal(exhaustedEvent.pr, 99);
  });

  // Settle the worker's final red-CI reply before flipping gh to green.
  await waitFor("settle idle after CI rounds", async () => {
    const { state } = await executor.runTick(statePath, cfg, repoRoot);
    return state.workers[0]?.lastStatus === "idle";
  });

  // 4. CI green -> merge nudge; then merged -> close_pane and archive.
  setGh("OPEN", [{ status: "COMPLETED", conclusion: "SUCCESS" }]);
  let mergeNudged = false;
  await waitFor("merge nudge", async () => {
    const { actions } = await executor.runTick(statePath, cfg, repoRoot);
    mergeNudged = actions.some((a) => a.type === "prompt" && /PR #99/.test(a.text));
    return mergeNudged;
  });
  step("merge: nudge sent when green", () => assert.ok(mergeNudged));

  setGh("MERGED", [{ status: "COMPLETED", conclusion: "SUCCESS" }]);
  let cycleDone = false;
  await waitFor("cycle done + pane closed", async () => {
    const { state } = await executor.runTick(statePath, cfg, repoRoot);
    const event = state.pendingEvents.find((e) => e.code === "CYCLE_DONE");
    if (!event) return false;
    cycleDone = event;
    return true;
  });
  step("close: CYCLE_DONE archived, worker removed", () => {
    assert.equal(cycleDone.pr, 99);
    const state = executor.loadState(statePath);
    assert.equal(state.workers.length, 0);
    assert.equal(state.history.length, 1);
    assert.equal(state.history[0].phase, "done");
  });

  // 5. index.ts loads in a real pi process (jiti compile gate) and exits cleanly.
  step("pi adapter: loads via --extension in print mode", () => {
    const smokeConfig = join(scratch, "smoke-config.json");
    writeFileSync(
      smokeConfig,
      JSON.stringify({ version: 1, pollSeconds: 5, statePath: join(scratch, "smoke-state.json") }),
    );
    const result = spawnSync(
      "pi",
      ["--extension", join(extDir, "index.ts"), "-p", "Reply with exactly: EXTOK"],
      {
        cwd: repoRoot,
        timeout: 180000,
        encoding: "utf8",
        env: { ...process.env, ORCH_CONFIG_PATH: smokeConfig, ORCH_STATE_PATH: join(scratch, "smoke-state.json"), AGENTS_ARTIFACTS_ROOT: scratch },
      },
    );
    assert.ok(result.status === 0, `pi exited ${result.status}: ${(result.stderr || "").slice(0, 400)}`);
    assert.match(result.stdout || "", /EXTOK/);
  });
} finally {
  // Cleanup: kill the canary pane if it still exists; never leave strays.
  try {
    const list = await executor.herdrJson(["agent", "list"]);
    const agents = list.ok ? (list.json?.result?.agents ?? []) : [];
    const stray = agents.find((a) => a.name === workerName);
    if (stray) await executor.herdrJson(["pane", "close", stray.pane_id]);
    const after = await executor.herdrJson(["agent", "list"]);
    const survivors = (after.json?.result?.agents ?? []).filter((a) => a.name === workerName);
    if (survivors.length) {
      failures.push("cleanup: canary pane survived");
      console.error("FAIL cleanup: canary pane survived");
    } else {
      console.log("PASS cleanup: no canary panes remain");
    }
  } catch (error) {
    failures.push(`cleanup error: ${error.message}`);
  }
  if (!process.env.AGENT_KIT_CANARY_KEEP) {
    rmSync(scratch, { recursive: true, force: true });
  } else {
    console.log(`kept scratch: ${scratch}`);
  }
}

if (failures.length) {
  console.error(`CANARY FAILED: ${failures.join("; ")}`);
  process.exit(1);
}
console.log("CANARY PASSED");
