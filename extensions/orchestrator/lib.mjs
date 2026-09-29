// orchestrator extension — pure decision logic.
// No I/O here: every function takes data in and returns data out, so the
// no-token unit tests and the live canary drive the exact same code paths
// that the pi adapter (index.ts) runs in a real session.

// ---------- config ----------

export const DEFAULT_CONFIG = {
  version: 1,
  maxWorkers: 1,
  pollSeconds: 30,
  stallSeconds: 900,
  idleNudgeSeconds: 900,
  mergeNudgeSeconds: 900,
  ciMaxRounds: 5,
  autoAnswerCap: 3,
  prDiscovery: true,
  ciCommand: "gh",
  statePath: "",
  worker: {
    kind: "pi",
    namePrefix: "orch-worker",
    startArgs: ["--provider", "ollama", "--model", "qwen3.8:27b-mlx-128k"],
    invocation: "/code ticket:{ticket} profile:local",
  },
  dialogAllowlist: [],
};

const CONFIG_NUMBER_KEYS = [
  "maxWorkers",
  "pollSeconds",
  "stallSeconds",
  "idleNudgeSeconds",
  "mergeNudgeSeconds",
  "ciMaxRounds",
  "autoAnswerCap",
];
const NAME_RE = /^[a-z][a-z0-9_-]{0,31}$/;

// Fail closed: anything malformed rejects the whole config and the adapter
// stays inert. Unknown keys are rejected so a typo can never silently
// disable a safety threshold.
export function validateConfig(raw, overrides = {}) {
  const errors = [];
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, errors: ["config must be a JSON object"] };
  }
  const known = new Set([
    "version",
    ...CONFIG_NUMBER_KEYS,
    "prDiscovery",
    "ciCommand",
    "statePath",
    "worker",
    "dialogAllowlist",
  ]);
  for (const key of Object.keys(raw)) {
    if (!known.has(key)) errors.push(`unknown config key: ${key}`);
  }
  // Trusted overrides (env-derived statePath etc.) merge BEFORE validation so
  // a required key can be supplied exclusively through an override.
  const config = {
    ...DEFAULT_CONFIG,
    ...raw,
    ...overrides,
    worker: { ...DEFAULT_CONFIG.worker, ...(raw.worker ?? {}) },
  };

  for (const key of CONFIG_NUMBER_KEYS) {
    const value = config[key];
    if (!Number.isFinite(value) || value <= 0) errors.push(`${key} must be a finite number > 0`);
  }
  if (typeof config.prDiscovery !== "boolean") errors.push("prDiscovery must be a boolean");
  if (typeof config.ciCommand !== "string" || !config.ciCommand.trim()) {
    errors.push("ciCommand must be a non-empty string");
  }
  if (typeof config.statePath !== "string" || !config.statePath.trim()) {
    errors.push("statePath must be a non-empty string");
  }
  if (config.version !== 1) errors.push("unsupported config version");
  if (!NAME_RE.test(config.worker.namePrefix)) {
    errors.push("worker.namePrefix must match ^[a-z][a-z0-9_-]{0,31}$");
  }
  if (config.worker.kind !== "pi") errors.push('worker.kind must be "pi"');
  if (
    !Array.isArray(config.worker.startArgs) ||
    config.worker.startArgs.some((a) => typeof a !== "string")
  ) {
    errors.push("worker.startArgs must be an array of strings");
  }
  if (typeof config.worker.invocation !== "string" || !config.worker.invocation.trim()) {
    errors.push("worker.invocation must be a non-empty string");
  }
  if (!Array.isArray(config.dialogAllowlist)) {
    errors.push("dialogAllowlist must be an array of regex-source strings");
  } else {
    for (const pattern of config.dialogAllowlist) {
      if (typeof pattern !== "string") {
        errors.push("dialogAllowlist entries must be strings");
        continue;
      }
      try {
        new RegExp(pattern, "im");
      } catch {
        errors.push(`dialogAllowlist entry is not a valid regex: ${pattern}`);
      }
    }
  }
  if (errors.length) return { ok: false, errors };
  return { ok: true, config };
}

// ---------- state ----------

export function initState(maxWorkers = 1) {
  return { version: 1, maxWorkers, workers: [], history: [], pendingEvents: [] };
}

export function isValidState(state) {
  return (
    state !== null &&
    typeof state === "object" &&
    state.version === 1 &&
    Array.isArray(state.workers) &&
    Array.isArray(state.history) &&
    Array.isArray(state.pendingEvents)
  );
}

// ---------- pure helpers ----------

// Only trust full PR URLs on the origin repo. Bare "#123", "PR #45" prose,
// issue URLs, and other repos' PRs are rejected.
export function extractPrNumber(text, repoSlug) {
  if (typeof text !== "string" || !text || typeof repoSlug !== "string" || !repoSlug) return null;
  const escaped = repoSlug.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`https://github\\.com/(${escaped})/pull/(\\d+)`, "g");
  let match;
  let last = null;
  while ((match = re.exec(text)) !== null) {
    if (match[1] === repoSlug) last = Number(match[2]);
  }
  return last;
}

export function matchesDialogAllowlist(screenText, patterns) {
  if (typeof screenText !== "string" || !screenText) return false;
  for (const pattern of patterns) {
    try {
      if (new RegExp(pattern, "im").test(screenText)) return true;
    } catch {
      return false; // invalid pattern -> fail closed
    }
  }
  return false;
}

export function coalesceEvents(events, cap = 5) {
  const seen = new Map();
  for (const event of events) {
    const key = `${event.code}:${event.worker ?? ""}`;
    if (!seen.has(key)) seen.set(key, event);
  }
  const merged = [...seen.values()];
  merged.sort((a, b) => (b.tier ?? 2) - (a.tier ?? 2));
  return merged.slice(0, cap);
}

// ---------- nudge templates ----------

export function ciNudgeText(ticket, pr, round, maxRounds) {
  return (
    `CI is red on PR #${pr} (ticket ${ticket}). Investigate the root cause of the ` +
    `failure with \`gh run view --log-failed\` or the check logs, fix the actual cause, ` +
    `push, and do not stop until every required check is green. ` +
    `Fix round ${round} of ${maxRounds}.`
  );
}

export function mergeNudgeText(pr) {
  return (
    `All required checks on PR #${pr} are green. Merge the PR now with the repo's ` +
    `standard merge flow, then report the merged state.`
  );
}

export function resumeNudgeText(ticket) {
  return (
    `Continue the task for ticket ${ticket}. If you are blocked or finished, say so ` +
    `explicitly instead of waiting silently.`
  );
}

// ---------- launch gate ----------

export function launchGate(state) {
  const live = state.workers.length;
  if (live >= state.maxWorkers) {
    return { allowed: false, reason: "at_cap", live, max: state.maxWorkers };
  }
  return { allowed: true, live, max: state.maxWorkers };
}

// ---------- tick ----------

// world = {
//   liveAgents: [{name, paneId, agentStatus, stateChangeSeq}],  // herdr truth, ALL agents
//   paneScreen: string|null,   // worker recent-unwrapped screen text
//   pr: {number, state: "OPEN"|"MERGED"|"CLOSED", anyFailed, allConcluded} | null,
//   repoSlug: "owner/repo"
// }
// Returns { state, actions, events }. tick never mutates its input state.
export function tick(prevState, world, config, now) {
  const state = structuredClone(prevState);
  const actions = [];
  const events = [];
  const liveByName = new Map((world.liveAgents ?? []).map((a) => [a.name, a]));

  for (const worker of state.workers) {
    const live = liveByName.get(worker.name);
    // Ownership rule: a worker record in state is the ONLY thing the watcher
    // may act on. Panes absent from state are never touched, even if their
    // name shares the prefix.
    if (!live) {
      // Pane already gone: if the PR it shipped has merged, the cycle still
      // completed cleanly. Anything else is a lost worker.
      const mergedWhileGone =
        worker.prNumber != null &&
        world.pr?.number === worker.prNumber &&
        world.pr.state === "MERGED";
      if (mergedWhileGone) {
        worker.phase = "done";
        events.push({ code: "CYCLE_DONE", tier: 2, worker: worker.name, ticket: worker.ticket, pr: worker.prNumber });
      } else if (worker.phase !== "done") {
        worker.phase = "lost";
        events.push({ code: "WORKER_LOST", tier: 2, worker: worker.name, ticket: worker.ticket });
      }
      continue;
    }

    // Reconcile herdr truth into the record.
    if (live.stateChangeSeq !== worker.lastChangeSeq) {
      worker.lastChangeSeq = live.stateChangeSeq;
      worker.lastActivityAt = now;
      worker.stallEscaped = false;
      worker.stallReported = false;
      worker.stallAt = null;
    }
    const status = live.agentStatus === "done" ? "idle" : live.agentStatus;
    if (status !== worker.lastStatus) {
      worker.lastStatus = status;
      worker.lastActivityAt = now;
      worker.blockedReported = false;
    }

    // One-time PR discovery from the pane screen, pinned to the origin repo.
    if (!worker.prNumber && config.prDiscovery && typeof world.paneScreen === "string" && world.paneScreen) {
      const found = extractPrNumber(world.paneScreen, world.repoSlug ?? "");
      if (found) {
        worker.prNumber = found;
        worker.prSource = "screen";
      }
    }

    if (status === "blocked") {
      const allowlisted =
        config.dialogAllowlist.length > 0 &&
        matchesDialogAllowlist(world.paneScreen ?? "", config.dialogAllowlist);
      const answered = worker.autoAnswers ?? 0;
      if (allowlisted && answered < config.autoAnswerCap) {
        actions.push({ type: "send_keys", worker: worker.name, keys: ["enter"] });
        worker.autoAnswers = answered + 1;
      } else if (!worker.blockedReported) {
        events.push({ code: "BLOCKED_DIALOG", tier: 2, worker: worker.name, ticket: worker.ticket });
        worker.blockedReported = true;
      }
      continue;
    }

    if (status === "working") {
      if (now - worker.lastActivityAt >= config.stallSeconds * 1000) {
        if (!worker.stallEscaped) {
          actions.push({ type: "send_keys", worker: worker.name, keys: ["escape"] });
          worker.stallEscaped = true;
          worker.stallAt = now;
        } else if (!worker.stallReported && now - worker.stallAt >= config.stallSeconds * 1000) {
          events.push({ code: "STALL_PERSISTENT", tier: 2, worker: worker.name, ticket: worker.ticket });
          worker.stallReported = true;
        }
      }
      continue;
    }

    if (status !== "idle") continue; // "unknown" or anything else: observe only

    worker.phase = worker.prNumber ? "awaiting_review" : "working";

    if (worker.prNumber && world.pr && world.pr.number === worker.prNumber) {
      if (world.pr.state === "MERGED") {
        actions.push({ type: "close_pane", worker: worker.name, paneId: live.paneId });
        worker.phase = "done";
        events.push({ code: "CYCLE_DONE", tier: 2, worker: worker.name, ticket: worker.ticket, pr: worker.prNumber });
        continue;
      }
      if (world.pr.state === "OPEN" && world.pr.anyFailed) {
        if (worker.ciRounds < config.ciMaxRounds) {
          worker.ciRounds += 1;
          actions.push({
            type: "prompt",
            worker: worker.name,
            text: ciNudgeText(worker.ticket, worker.prNumber, worker.ciRounds, config.ciMaxRounds),
          });
          worker.lastActivityAt = now;
        } else if (!worker.ciReported) {
          events.push({
            code: "CI_ROUNDS_EXHAUSTED",
            tier: 2,
            worker: worker.name,
            ticket: worker.ticket,
            pr: worker.prNumber,
            rounds: worker.ciRounds,
          });
          worker.ciReported = true;
        }
        continue;
      }
      if (world.pr.state === "OPEN" && world.pr.allConcluded && !world.pr.anyFailed) {
        if (!worker.mergeNudged) {
          actions.push({ type: "prompt", worker: worker.name, text: mergeNudgeText(worker.prNumber) });
          worker.mergeNudged = true;
          worker.mergeNudgedAt = now;
        } else if (!worker.mergeReported && now - worker.mergeNudgedAt >= config.mergeNudgeSeconds * 1000) {
          events.push({ code: "MERGE_TIMEOUT", tier: 2, worker: worker.name, ticket: worker.ticket, pr: worker.prNumber });
          worker.mergeReported = true;
        }
        continue;
      }
      // CI pending: observe only.
      continue;
    }

    if (!worker.prNumber) {
      if (!worker.resumeNudged && now - worker.lastActivityAt >= config.idleNudgeSeconds * 1000) {
        actions.push({ type: "prompt", worker: worker.name, text: resumeNudgeText(worker.ticket) });
        worker.resumeNudged = true;
        worker.resumeNudgedAt = now;
      } else if (
        worker.resumeNudged &&
        !worker.idleReported &&
        now - worker.resumeNudgedAt >= config.idleNudgeSeconds * 1000
      ) {
        events.push({ code: "WORKER_IDLE_UNFINISHED", tier: 2, worker: worker.name, ticket: worker.ticket });
        worker.idleReported = true;
      }
    }
  }

  state.workers = state.workers.filter((worker) => {
    if (worker.phase === "done" || worker.phase === "lost") {
      state.history.push({ ...worker, closedAt: now });
      return false;
    }
    return true;
  });

  state.pendingEvents = coalesceEvents([...(state.pendingEvents ?? []), ...events]);
  return { state, actions, events };
}

// Build the single wake message for the orchestrator session from pending events.
export function formatWakeMessage(events) {
  if (!events.length) return null;
  const lines = events.map((event) => {
    const bits = [`[orchestrator] ${event.code}`];
    if (event.ticket != null) bits.push(`ticket=${event.ticket}`);
    if (event.pr != null) bits.push(`pr=${event.pr}`);
    if (event.rounds != null) bits.push(`rounds=${event.rounds}`);
    bits.push(`worker=${event.worker}`);
    if (event.tier === 3) bits.push("ESCALATE_TO_FOUNDER");
    return bits.join(" ");
  });
  return lines.join("\n");
}
