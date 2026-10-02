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
  repos: {},
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
    "repos",
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
  if (config.repos === null || typeof config.repos !== "object" || Array.isArray(config.repos)) {
    errors.push("repos must be an object mapping repo keys to { path, ... }"
    );
  } else {
    for (const [key, entry] of Object.entries(config.repos)) {
      if (!NAME_RE.test(key)) {
        errors.push(`repos key must match ^[a-z][a-z0-9_-]{0,31}$: ${key}`);
        continue;
      }
      if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
        errors.push(`repos.${key} must be an object`);
        continue;
      }
      for (const sub of Object.keys(entry)) {
        if (!["path", "invocation", "startArgs", "ciCommand"].includes(sub)) {
          errors.push(`unknown key in repos.${key}: ${sub}`);
        }
      }
      if (typeof entry.path !== "string" || !entry.path.trim()) {
        errors.push(`repos.${key}.path must be a non-empty string`);
      }
      if (entry.invocation !== undefined && (typeof entry.invocation !== "string" || !entry.invocation.trim())) {
        errors.push(`repos.${key}.invocation must be a non-empty string`);
      }
      if (
        entry.startArgs !== undefined &&
        (!Array.isArray(entry.startArgs) || entry.startArgs.some((a) => typeof a !== "string"))
      ) {
        errors.push(`repos.${key}.startArgs must be an array of strings`);
      }
      if (entry.ciCommand !== undefined && (typeof entry.ciCommand !== "string" || !entry.ciCommand.trim())) {
        errors.push(`repos.${key}.ciCommand must be a non-empty string`);
      }
    }
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

// ---------- worker policy ----------

// Thresholds decide when the watcher wakes the orchestrator or nudges a worker.
// The state file is shared by every pi session on the host, but a config file is
// per repo. A session whose cwd has no `.pi/orchestrator.json` therefore used to
// apply its DEFAULT thresholds to another repo's worker.
//
// Incident 2026-09-30: a session with default stallSeconds=900 raised
// STALL_PERSISTENT for a worker whose owner had configured 3600, while the worker
// was mid-tool-call on a 40-minute CI poll. The policy published with the worker
// governs it, not whatever the observing session happens to default to.

export const POLICY_KEYS = [
  "stallSeconds",
  "idleNudgeSeconds",
  "mergeNudgeSeconds",
  "ciMaxRounds",
  "autoAnswerCap",
  "prDiscovery",
  "ciCommand",
  "dialogAllowlist",
];

// Keep only the decision keys this module can enforce, and only when each value
// is well formed. A hand-edited or truncated policy must never turn a threshold
// into NaN (which silently disables every stall check) or into a negative number.
// Anything rejected here falls back to the local config.
export function normalizePolicy(raw) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return {};
  const policy = {};
  for (const key of ["stallSeconds", "idleNudgeSeconds", "mergeNudgeSeconds", "ciMaxRounds", "autoAnswerCap"]) {
    const value = raw[key];
    if (Number.isFinite(value) && value > 0) policy[key] = value;
  }
  if (typeof raw.prDiscovery === "boolean") policy.prDiscovery = raw.prDiscovery;
  if (typeof raw.ciCommand === "string" && raw.ciCommand.trim()) policy.ciCommand = raw.ciCommand;
  if (Array.isArray(raw.dialogAllowlist) && raw.dialogAllowlist.every((p) => typeof p === "string")) {
    policy.dialogAllowlist = raw.dialogAllowlist;
  }
  return policy;
}

// The policy to publish for a worker this session launches.
export function policyFrom(config) {
  return normalizePolicy(config);
}

// The policy a tick must obey: the published worker policy wins over local config.
export function effectivePolicy(config, published) {
  return { ...config, ...normalizePolicy(published) };
}

// Resolve the policy for one tick. A session that was explicitly configured for
// this repo adopts the policy when nothing has published one yet, so a worker
// launched before this rule existed stops being judged by foreign defaults on the
// owner's next tick. Returns `publish` when state.policy must be written.
export function resolveTickPolicy(config, state, explicit) {
  const published = normalizePolicy(state?.policy);
  const adopt = explicit === true && state?.policy == null;
  const owned = adopt ? policyFrom(config) : published;
  return { policy: { ...config, ...owned }, publish: adopt ? owned : null };
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

// GitHub returns two different entry shapes in one statusCheckRollup array.
// A CheckRun carries `status` plus `conclusion`; a commit StatusContext (for
// example each Vercel deployment) carries `state` and has no `status` field at
// all. Reading only `status` made every StatusContext look permanently pending
// from the first one onward, which silently disabled the merge nudge and
// MERGE_TIMEOUT in every repository that has such a status.
export function isConcludedCheck(check) {
  if (check === null || typeof check !== "object") return false;
  if (typeof check.status === "string" && check.status) return check.status === "COMPLETED";
  if (typeof check.state === "string" && check.state) {
    return check.state === "SUCCESS" || check.state === "FAILURE" || check.state === "ERROR";
  }
  return false; // unknown shape: never claim the rollup finished
}

// The green set mirrors the repository's existing classifiers (the boss skill
// treats FAILURE, CANCELLED, TIMED_OUT, ACTION_REQUIRED and STARTUP_FAILURE as
// failed). Anything not green counts as failed, so an unrecognized conclusion
// can never be announced to the worker as a passing check.
const GREEN_CONCLUSIONS = new Set(["SUCCESS", "NEUTRAL", "SKIPPED"]);

export function isFailedCheck(check) {
  if (check === null || typeof check !== "object") return false;
  // Read the red signal from both fields before the green one, so a mixed
  // entry can never be reported green by short-circuiting past its failure.
  if (check.state === "FAILURE" || check.state === "ERROR") return true;
  if (typeof check.status === "string" && check.status) {
    if (check.status !== "COMPLETED") return false; // still running, not failed
    return !GREEN_CONCLUSIONS.has(check.conclusion);
  }
  return false;
}

// Pure summary of one rollup, so the decision is unit-testable without gh.
export function summarizeRollup(rollup) {
  const entries = Array.isArray(rollup) ? rollup : [];
  return {
    anyFailed: entries.some(isFailedCheck),
    allConcluded: entries.length > 0 && entries.every(isConcludedCheck),
  };
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

export const ABORTED_TURN_PATTERN = "^\\s*Operation aborted\\s*$";

// The abort marker lingers in scrollback long after recovery, so only the
// last few non-empty screen lines count as "currently aborted".
export function isAbortedTurnScreen(screenText, tailLines = 4) {
  if (typeof screenText !== "string" || !screenText) return false;
  const tail = screenText.split("\n").filter((line) => line.trim()).slice(-tailLines).join("\n");
  return matchesDialogAllowlist(tail, [ABORTED_TURN_PATTERN]);
}

export function abortedNudgeText(ticket) {
  return (
    `Your last turn was aborted mid-work. Continue ticket ${ticket} exactly ` +
    `where you left off.`
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

// Resume-first: find a prior session for this ticket that pi can resume.
// Returns the session file path or null.
export function findResumableSession(history, ticket) {
  if (!Array.isArray(history) || ticket == null) return null;
  for (let i = history.length - 1; i >= 0; i--) {
    const record = history[i];
    if (record && record.ticket === ticket && typeof record.sessionPath === "string" && record.sessionPath) {
      return record.sessionPath;
    }
  }
  return null;
}

// ---------- repo registry ----------

// Resolve a launch target. Omitting repoKey uses the orchestrator session's
// own cwd (v1 behavior). An unknown key or a nonexistent path refuses the
// launch — never guesses. `exists` is injected so tests stay pure.
export function resolveRepo(config, repoKey, fallbackCwd, exists) {
  if (repoKey == null || repoKey === "") {
    return { ok: true, path: fallbackCwd, invocation: config.worker.invocation, startArgs: config.worker.startArgs, ciCommand: config.ciCommand, repo: null };
  }
  const entry = (config.repos ?? {})[repoKey];
  if (!entry) {
    const known = Object.keys(config.repos ?? {});
    return { ok: false, error: `unknown repo '${repoKey}' (registered: ${known.length ? known.join(", ") : "none"})` };
  }
  if (!exists(entry.path)) {
    return { ok: false, error: `repos.${repoKey}.path does not exist: ${entry.path}` };
  }
  return {
    ok: true,
    path: entry.path,
    invocation: entry.invocation ?? config.worker.invocation,
    startArgs: entry.startArgs ?? config.worker.startArgs,
    ciCommand: entry.ciCommand ?? config.ciCommand,
    repo: repoKey,
  };
}

// Per-worker working directory with a v1 migration fallback:
// recorded repoPath wins; otherwise the live herdr agent's reported cwd;
// otherwise the orchestrator session's cwd.
export function resolveWorkerCwd(worker, liveCwd, fallbackCwd) {
  if (worker && worker.repoPath) return worker.repoPath;
  if (liveCwd) return liveCwd;
  return fallbackCwd;
}

// ---------- tick ----------

// world = {
//   liveAgents: [{name, paneId, agentStatus, stateChangeSeq}],  // herdr truth, ALL agents
//   paneScreen: string|null,   // worker recent-unwrapped screen text
//   sessionActivityAt: { [workerName]: number|null }, // worker JSONL mtime in ms
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
      worker.stallReported = false;
    }
    const status = live.agentStatus === "done" ? "idle" : live.agentStatus;
    if (status !== worker.lastStatus) {
      worker.lastStatus = status;
      worker.lastActivityAt = now;
      worker.blockedReported = false;
      worker.abortNudged = false;
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
      // Herdr's stateChangeSeq stays fixed throughout a turn. Transcript
      // growth is progress even when the worker has been "working" for hours.
      const activityAt = world.sessionActivityAt?.[worker.name];
      if (Number.isFinite(activityAt) && activityAt > worker.lastActivityAt && activityAt <= now) {
        worker.lastActivityAt = activityAt;
        worker.stallReported = false;
      }
      if (!worker.stallReported && now - worker.lastActivityAt >= config.stallSeconds * 1000) {
        // An old or unreadable transcript is ambiguous (e.g. a long model
        // call). Wake the supervisor; never cancel an active turn with Escape.
        events.push({ code: "STALL_PERSISTENT", tier: 2, worker: worker.name, ticket: worker.ticket });
        worker.stallReported = true;
      }
      continue;
    }

    if (status !== "idle") continue; // "unknown" or anything else: observe only

    worker.phase = worker.prNumber ? "awaiting_review" : "working";

    // Fast abort recovery: an aborted turn leaves the worker idle with
    // "Operation aborted" on screen. Nudge immediately (once per idle
    // episode, capped) instead of waiting for the generic idle window.
    if (isAbortedTurnScreen(world.paneScreen ?? "")) {
      const nudges = worker.abortNudges ?? 0;
      if (nudges < config.autoAnswerCap) {
        if (!worker.abortNudged) {
          actions.push({ type: "prompt", worker: worker.name, text: abortedNudgeText(worker.ticket) });
          worker.abortNudged = true;
          worker.abortNudges = nudges + 1;
          worker.lastActivityAt = now;
        }
      } else if (!worker.abortReported) {
        events.push({ code: "WORKER_ABORT_LOOP", tier: 2, worker: worker.name, ticket: worker.ticket });
        worker.abortReported = true;
      }
      continue;
    }

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
