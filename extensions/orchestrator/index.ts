// orchestrator — host-scoped pi extension that babysits worker coding agents
// running in herdr panes. The pi session that loads this extension is the
// Orchestrator: it triages tickets, launches one local-model worker at a time
// (maxWorkers), and is woken ONLY on judgment events. Mechanical nudges
// (CI rounds, resume, allowlisted dialogs, stall reporting, close-after-merge)
// run here without any model tokens.
//
// Pure decisions live in lib.mjs; all herdr/gh/fs effects live in
// executor.mjs. This file only wires pi APIs to them.

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as fs from "node:fs";
import { join } from "node:path";
import {
  DEFAULT_CONFIG,
  findResumableSession,
  formatWakeMessage,
  launchGate,
  resolveRepo,
  validateConfig,
} from "./lib.mjs";
import {
  applyAction,
  changeWorker,
  gatherWorld,
  herdrJson,
  loadState,
  runTick,
  saveState,
} from "./executor.mjs";

interface OrchestratorConfig {
  [key: string]: unknown;
  statePath: string;
}

function defaultStatePath(): string {
  const root = process.env.AGENTS_ARTIFACTS_ROOT || join(process.env.HOME || "/tmp", "agents-artifacts");
  return join(root, "orchestrator", "state.json");
}

function loadOrchestratorConfig(): { ok: true; config: OrchestratorConfig } | { ok: false; errors: string[] } {
  const configPath = process.env.ORCH_CONFIG_PATH || join(process.cwd(), ".pi", "orchestrator.json");
  let raw: unknown = {};
  try {
    if (fs.existsSync(configPath)) {
      raw = JSON.parse(fs.readFileSync(configPath, "utf8"));
    }
  } catch (error) {
    return { ok: false, errors: [`config at ${configPath} is not valid JSON: ${String(error)}`] };
  }
  const overrides: Record<string, unknown> = {
    statePath: process.env.ORCH_STATE_PATH || defaultStatePath(),
  };
  const result = validateConfig(raw as Record<string, unknown>, overrides);
  if (!result.ok) return { ok: false, errors: result.errors };
  return { ok: true, config: result.config as OrchestratorConfig };
}

function workerRecord(state: ReturnType<typeof loadState>) {
  return state?.workers[0] ?? null;
}

function textResult(text: string, details?: unknown) {
  return { content: [{ type: "text" as const, text }], details };
}

export default function orchestratorExtension(pi: ExtensionAPI) {
  let configResult: ReturnType<typeof loadOrchestratorConfig> | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;
  let ticking = false;

  function config() {
    if (!configResult) configResult = loadOrchestratorConfig();
    return configResult;
  }

  function statePath(): string {
    const result = config();
    return result.ok ? result.config.statePath : "";
  }

  // Flush pending judgment events into the conversation as one user message.
  // No-op while a turn is active (the agent_settled handler retries).
  async function wakeIfPending(ctx: ExtensionContext) {
    const result = config();
    if (!result.ok || !ctx.isIdle()) return;
    const path = statePath();
    const state = loadState(path);
    if (!state || !state.pendingEvents.length) return;
    const message = formatWakeMessage(state.pendingEvents);
    state.pendingEvents = [];
    saveState(path, state);
    if (message) pi.sendUserMessage(message);
  }

  async function pollTick(ctx: ExtensionContext) {
    const result = config();
    if (!result.ok || ticking) return;
    ticking = true;
    try {
      await runTick(statePath(), result.config, process.cwd());
      await wakeIfPending(ctx);
    } catch {
      // Never let one bad poll kill the watcher; the next tick reconciles.
    } finally {
      ticking = false;
    }
  }

  pi.registerTool({
    name: "orch_status",
    label: "Orchestrator status",
    description:
      "Read-only snapshot of the orchestrator: config validity, worker capacity, current worker record, and live herdr state for managed workers only. Writes nothing.",
    parameters: Type.Object({}),
    async execute() {
      const result = config();
      if (!result.ok) return textResult(`orchestrator config invalid: ${result.errors.join("; ")}`);
      const state = loadState(statePath());
      const gate = state ? launchGate(state) : { allowed: true, live: 0, max: result.config.maxWorkers };
      const worker = workerRecord(state);
      let live: unknown = null;
      if (worker) {
        const list = await herdrJson(["agent", "list"]);
        const agents = list.ok ? (list.json?.result?.agents ?? []) : [];
        live = agents.find((a: { name: string }) => a.name === worker.name) ?? null;
      }
      return textResult(
        JSON.stringify(
          {
            configOk: true,
            statePath: result.config.statePath,
            registeredRepos: Object.fromEntries(
              Object.entries(result.config.repos ?? {}).map(([key, value]) => [key, value.path]),
            ),
            capacity: { live: gate.live, max: gate.max, canLaunch: gate.allowed },
            worker,
            liveHerdrState: live,
            pendingEvents: state?.pendingEvents ?? [],
            historyCount: state?.history.length ?? 0,
          },
          null,
          2,
        ),
        { worker, gate },
      );
    },
  });

  pi.registerTool({
    name: "orch_launch_worker",
    label: "Orchestrator launch worker",
    description:
      "Launch one worker coding agent in a new herdr pane (pi on the configured local model) and start its task. Refused when maxWorkers is already reached or the repo is not registered. The caller triages the ticket; this tool only creates and starts the worker.",
    parameters: Type.Object({
      ticket: Type.Optional(Type.Number({ description: "Ticket number the worker will implement" })),
      repo: Type.Optional(
        Type.String({ description: "Registered repo key from the orchestrator config; omit for the orchestrator's own repo" }),
      ),
      invocation: Type.Optional(
        Type.String({ description: "Override the configured worker invocation; {ticket} is substituted" }),
      ),
      forceFresh: Type.Optional(
        Type.Boolean({ description: "Set true to start a brand-new session even when a resumable session exists for this ticket (resume-first policy)" }),
      ),
    }),
    async execute(_id, params) {
      const result = config();
      if (!result.ok) return textResult(`orchestrator config invalid: ${result.errors.join("; ")}`);
      const cfg = result.config;
      const repo = resolveRepo(cfg, params.repo ?? null, process.cwd(), (p) => fs.existsSync(p));
      if (!repo.ok) {
        return textResult(`REFUSED: ${repo.error}`, { refused: true });
      }
      const path = statePath();
      const state = loadState(path) ?? {
        version: 1,
        maxWorkers: cfg.maxWorkers,
        workers: [],
        history: [],
        pendingEvents: [],
      };
      const gate = launchGate(state);
      if (!gate.allowed) {
        return textResult(
          `REFUSED: ${gate.live}/${gate.max} worker(s) already running (reason: ${gate.reason}). Close the current worker first.`,
          { refused: true },
        );
      }
      if (params.ticket != null && !params.forceFresh) {
        const resumable = findResumableSession(state.history, params.ticket);
        if (resumable) {
          return textResult(
            `REFUSED (resume-first): ticket ${params.ticket} has a resumable session (${resumable}). Use orch_change_worker to change model/effort on that session, or pass forceFresh:true to deliberately start over.`,
            { refused: true, resumable },
          );
        }
      }
      const seq = state.workers.length + state.history.length + 1;
      const name = `${cfg.worker.namePrefix}-${seq}`;
      const split = await herdrJson(["pane", "split", "--current", "--direction", "right", "--cwd", repo.path, "--no-focus"]);
      const paneId = split.ok ? split.json?.result?.pane?.pane_id : null;
      if (!paneId) {
        return textResult(`pane split failed: ${split.stderr || "no pane_id in response"}`, { failed: true });
      }
      const startArgs = ["agent", "start", name, "--kind", cfg.worker.kind, "--pane", paneId, "--timeout", "60000", "--", ...repo.startArgs];
      const start = await herdrJson(startArgs);
      if (!start.ok) {
        await herdrJson(["pane", "close", paneId]); // never leave a dead pane behind
        return textResult(`agent start failed: ${start.stderr || "unknown error"} (pane ${paneId} closed)`, { failed: true });
      }
      const now = Date.now();
      const invocation = (params.invocation ?? repo.invocation).replaceAll(
        "{ticket}",
        params.ticket != null ? String(params.ticket) : "",
      );
      state.workers.push({
        name,
        paneId,
        repo: repo.repo,
        repoPath: repo.path,
        ticket: params.ticket ?? null,
        sessionPath: start.json?.result?.agent?.agent_session?.value ?? null,
        invocation,
        prNumber: null,
        prSource: null,
        ciRounds: 0,
        launchedAt: now,
        lastActivityAt: now,
        lastChangeSeq: start.json?.result?.agent?.state_change_seq ?? 0,
        lastStatus: "idle",
        phase: "working",
      });
      saveState(path, state);
      await applyAction({ type: "prompt", worker: name, text: invocation });
      return textResult(
        `worker ${name} launched in pane ${paneId} (repo ${repo.repo ?? "session-cwd"}, capacity now ${state.workers.length}/${cfg.maxWorkers}); invocation sent: ${invocation}`,
        { name, paneId, ticket: params.ticket ?? null, repo: repo.repo },
      );
    },
  });

  pi.registerTool({
    name: "orch_change_worker",
    label: "Orchestrator change worker",
    description:
      "Resume-first task change: closes the current worker's pane and relaunches the SAME pi session on a new provider/model (model may carry a ':thinking' level, e.g. deepseek-flash:high). Preserves ticket, repo, worktree, and CI-round count. Refused when no resumable session exists.",
    parameters: Type.Object({
      provider: Type.String({ description: "pi provider name, e.g. ollama or deepseek" }),
      model: Type.String({ description: "Model id or pattern, optionally ':thinking' e.g. deepseek-flash:high" }),
      reason: Type.Optional(Type.String({ description: "Why the worker is being changed" })),
    }),
    async execute(_id, params) {
      const result = config();
      if (!result.ok) return textResult(`orchestrator config invalid: ${result.errors.join(";")}`);
      const changed = await changeWorker(statePath(), result.config, params, process.cwd());
      if (!changed.ok) return textResult(`REFUSED: ${changed.error}`, { refused: true });
      const record = changed.record;
      return textResult(
        `worker ${record.name} relaunched in pane ${record.paneId} on ${params.provider}/${params.model}, resuming session for ticket ${record.ticket}`,
        { name: record.name, paneId: record.paneId, ticket: record.ticket },
      );
    },
  });

  pi.registerTool({
    name: "orch_prompt_worker",
    label: "Orchestrator prompt worker",
    description: "Send a instruction message to the current worker agent (e.g. a root-cause direction after CI rounds were exhausted).",
    parameters: Type.Object({ text: Type.String({ description: "Message to send to the worker" }) }),
    async execute(_id, params) {
      const result = config();
      if (!result.ok) return textResult(`orchestrator config invalid: ${result.errors.join("; ")}`);
      const worker = workerRecord(loadState(statePath()));
      if (!worker) return textResult("no active worker", { failed: true });
      const applied = await applyAction({ type: "prompt", worker: worker.name, text: params.text });
      return textResult(applied.ok ? `sent to ${worker.name}` : `send failed: ${applied.stderr}`, {
        worker: worker.name,
      });
    },
  });

  pi.registerTool({
    name: "orch_note_pr",
    label: "Orchestrator note PR",
    description: "Pin the PR number for the current worker so the watcher can track CI and merge state without relying on screen parsing.",
    parameters: Type.Object({ pr: Type.Number({ description: "PR number" }) }),
    async execute(_id, params) {
      const result = config();
      if (!result.ok) return textResult(`orchestrator config invalid: ${result.errors.join("; ")}`);
      const path = statePath();
      const state = loadState(path);
      const worker = workerRecord(state);
      if (!state || !worker) return textResult("no active worker", { failed: true });
      worker.prNumber = params.pr;
      worker.prSource = "pinned";
      saveState(path, state);
      return textResult(`PR #${params.pr} pinned to worker ${worker.name}`, { worker: worker.name, pr: params.pr });
    },
  });

  pi.registerTool({
    name: "orch_close_worker",
    label: "Orchestrator close worker",
    description: "Close the current worker's herdr pane, kill its session, and archive the cycle in the orchestrator state.",
    parameters: Type.Object({ reason: Type.String({ description: "Why the worker is being closed" }) }),
    async execute(_id, params) {
      const result = config();
      if (!result.ok) return textResult(`orchestrator config invalid: ${result.errors.join("; ")}`);
      const path = statePath();
      const state = loadState(path);
      const worker = workerRecord(state);
      if (!state || !worker) return textResult("no active worker", { failed: true });
      await applyAction({ type: "close_pane", worker: worker.name, paneId: worker.paneId });
      worker.phase = "done";
      state.workers = state.workers.filter((w: { name: string }) => w.name !== worker.name);
      state.history.push({ ...worker, closedAt: Date.now(), closeReason: params.reason });
      saveState(path, state);
      return textResult(`worker ${worker.name} closed (${params.reason})`, { closed: worker.name });
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    const result = config();
    if (!result.ok) {
      if (ctx.hasUI) ctx.ui.notify(`orchestrator disabled: ${result.errors.join("; ")}`, "warning");
      return;
    }
    const interval = setInterval(() => {
      void pollTick(ctx);
    }, Math.max(5, result.config.pollSeconds) * 1000);
    interval.unref?.(); // never block process exit in --print mode
    timer = interval;
    void pollTick(ctx);
  });

  pi.on("agent_settled", async (_event, ctx) => {
    await wakeIfPending(ctx);
  });

  pi.on("session_shutdown", async () => {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  });
}
