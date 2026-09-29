// orchestrator extension — effectful executor.
// The ONLY module that runs herdr/gh commands or writes the state file.
// index.ts (pi adapter) and the live canary both drive tick() through
// runTick() here, so tests exercise the real command surface.

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir as osTmpdir } from "node:os";
import { tick, isValidState, resolveWorkerCwd } from "./lib.mjs";

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

  // Per-worker repo context: recorded repoPath wins, then the live herdr
  // agent's own reported cwd (covers records created before the registry).
  const liveCwdByName = new Map(allAgents.map((a) => [a.name, a.cwd]));
  const first = state.workers[0];
  const firstCwd = first
    ? resolveWorkerCwd(first, liveCwdByName.get(first.name), cwd)
    : cwd;
  const slug = await repoSlug(firstCwd);
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

// Resume-first task change: close the worker's pane and relaunch the SAME pi
// session with new provider/model (model may carry a ':thinking' level).
// Carries ticket, repo, invocation, and the CI-round counter; resets only
// transient watcher flags. Returns { ok, record? } or { ok: false, error }.
export async function changeWorker(statePath, config, change, cwd = process.cwd()) {
  const state = loadState(statePath);
  if (!state || !state.workers.length) return { ok: false, error: "no active worker" };
  const worker = state.workers[0];

  // Session path: recorded, else live from herdr (covers pre-v2 records).
  let sessionPath = worker.sessionPath ?? null;
  if (!sessionPath) {
    const got = await herdrJson(["agent", "get", worker.name]);
    sessionPath = got.ok ? (got.json?.result?.agent?.agent_session?.value ?? null) : null;
  }
  if (!sessionPath) {
    return { ok: false, error: `worker ${worker.name} has no resumable pi session; launch fresh with orch_launch_worker forceFresh:true` };
  }

  const args = ["--provider", change.provider, "--model", change.model, "--session", sessionPath];
  const oldName = worker.name;
  const close = await applyAction({ type: "close_pane", worker: oldName, paneId: worker.paneId });
  if (!close.ok) return { ok: false, error: `close failed: ${close.stderr}` };

  const seq = state.workers.length + state.history.length + 1;
  const name = `${config.worker.namePrefix}-${seq}`;
  const repoPath = resolveWorkerCwd(worker, null, cwd);
  const split = await herdrJson(["pane", "split", "--current", "--direction", "right", "--cwd", repoPath, "--no-focus"]);
  const paneId = split.ok ? split.json?.result?.pane?.pane_id : null;
  if (!paneId) return { ok: false, error: `pane split failed: ${split.stderr}` };
  const start = await herdrJson([
    "agent", "start", name, "--kind", config.worker.kind, "--pane", paneId, "--timeout", "90000", "--", ...args,
  ]);
  if (!start.ok) {
    await herdrJson(["pane", "close", paneId]);
    return { ok: false, error: `agent start failed: ${start.stderr}` };
  }

  state.workers = state.workers.filter((w) => w.name !== oldName);
  state.history.push({ ...worker, closedAt: Date.now(), closeReason: change.reason ?? "changed" });
  state.workers.push({
    ...worker,
    name,
    paneId,
    sessionPath,
    launchedAt: Date.now(),
    lastActivityAt: Date.now(),
    lastChangeSeq: start.json?.result?.agent?.state_change_seq ?? 0,
    lastStatus: "idle",
    phase: worker.prNumber ? "awaiting_review" : "working",
    stallEscaped: false,
    stallReported: false,
    blockedReported: false,
    resumeNudged: false,
    idleReported: false,
    mergeNudged: false,
    mergeReported: false,
    ciReported: false,
    changedAt: Date.now(),
    changeReason: change.reason ?? null,
  });
  saveState(statePath, state);
  await applyAction({
    type: "prompt",
    worker: name,
    text: `Resume ticket ${worker.ticket ?? ""} where you left off. Your worktree is ${repoPath}.`,
  });
  return { ok: true, record: state.workers[0] };
}
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
