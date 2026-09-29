// orchestrator extension — effectful executor.
// The ONLY module that runs herdr/gh commands or writes the state file.
// index.ts (pi adapter) and the live canary both drive tick() through
// runTick() here, so tests exercise the real command surface.

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir as osTmpdir } from "node:os";
import { tick, isValidState } from "./lib.mjs";

export function run(cmd, args, timeoutMs = 30000) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
      resolve({ ok: !error, stdout: String(stdout ?? ""), stderr: String(stderr ?? ""), error });
    });
  });
}

export async function herdrJson(args) {
  const result = await run("herdr", args);
  if (!result.ok) return { ok: false, error: result.error, stderr: result.stderr };
  try {
    return { ok: true, json: JSON.parse(result.stdout) };
  } catch {
    return { ok: false, error: new Error("herdr returned non-JSON output"), stderr: result.stdout };
  }
}

// Strip ANSI escapes so screen parsing (PR URLs, dialog patterns) is stable.
export function stripAnsi(text) {
  // eslint-disable-next-line no-control-regex
  return String(text).replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07/g, "");
}

export function loadState(statePath) {
  if (!existsSync(statePath)) return null;
  try {
    const state = JSON.parse(readFileSync(statePath, "utf8"));
    return isValidState(state) ? state : null;
  } catch {
    return null;
  }
}

export function saveState(statePath, state) {
  mkdirSync(dirname(statePath), { recursive: true });
  const tmp = join(osTmpdir(), `orchestrator-state-${process.pid}-${Date.now()}.json`);
  writeFileSync(tmp, JSON.stringify(state, null, 2));
  renameSync(tmp, statePath); // atomic on the same filesystem
}

async function gatherPrStatus(config, prNumber) {
  const result = await run(config.ciCommand, [
    "pr",
    "view",
    String(prNumber),
    "--json",
    "state,statusCheckRollup",
  ]);
  if (!result.ok) return null;
  try {
    const json = JSON.parse(result.stdout);
    const rollup = Array.isArray(json.statusCheckRollup) ? json.statusCheckRollup : [];
    const anyFailed = rollup.some((c) => c.conclusion === "FAILURE");
    const allConcluded = rollup.length > 0 && rollup.every((c) => c.status === "COMPLETED");
    return { number: prNumber, state: json.state ?? "UNKNOWN", anyFailed, allConcluded };
  } catch {
    return null;
  }
}

export async function repoSlug(cwd = process.cwd()) {
  const result = await run("git", ["-C", cwd, "remote", "get-url", "origin"]);
  const url = result.stdout.trim();
  const ssh = url.match(/^git@github\.com:([^/]+\/[^/]+?)(\.git)?$/);
  if (ssh) return ssh[1];
  const https = url.match(/^https:\/\/github\.com\/([^/]+\/[^/]+?)(\.git)?$/);
  if (https) return https[1];
  return null;
}

// Build the world snapshot for one tick from live herdr + gh state.
export async function gatherWorld(config, state, cwd = process.cwd()) {
  const list = await herdrJson(["agent", "list"]);
  const allAgents = list.ok ? (list.json?.result?.agents ?? []) : [];
  const wanted = new Map(state.workers.map((w) => [w.name, w]));
  const liveAgents = allAgents
    .filter((a) => wanted.has(a.name))
    .map((a) => ({
      name: a.name,
      paneId: a.pane_id,
      agentStatus: a.agent_status,
      stateChangeSeq: a.state_change_seq,
    }));

  let paneScreen = null;
  if (state.workers.length) {
    const read = await run("herdr", [
      "agent",
      "read",
      state.workers[0].name,
      "--source",
      "recent-unwrapped",
      "--lines",
      "150",
    ]);
    if (read.ok) paneScreen = stripAnsi(read.stdout);
  }

  const slug = await repoSlug(cwd);
  const first = state.workers[0];
  const pr = first?.prNumber ? await gatherPrStatus(config, first.prNumber) : null;
  return { liveAgents, paneScreen, pr, repoSlug: slug };
}

// Apply one action decided by tick(). Only acts on names present in state.
export async function applyAction(action) {
  switch (action.type) {
    case "send_keys":
      return run("herdr", ["agent", "send-keys", action.worker, ...action.keys]);
    case "prompt":
      return run("herdr", ["agent", "prompt", action.worker, action.text]);
    case "close_pane":
      return run("herdr", ["pane", "close", action.paneId]);
    default:
      return { ok: false, stderr: `unknown action type: ${action.type}` };
  }
}

// One full poll: gather world -> decide -> apply -> persist.
// Returns { actions, newEvents } for the caller (pi wake logic / canary assertions).
export async function runTick(statePath, config, cwd = process.cwd()) {
  const state = loadState(statePath) ?? { version: 1, maxWorkers: config.maxWorkers, workers: [], history: [], pendingEvents: [] };
  const world = await gatherWorld(config, state, cwd);
  const { state: nextState, actions, events } = tick(state, world, config, Date.now());
  for (const action of actions) {
    await applyAction(action);
  }
  saveState(statePath, nextState);
  return { actions, newEvents: events, state: nextState };
}
