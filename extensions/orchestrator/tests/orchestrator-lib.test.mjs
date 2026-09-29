// No-token table-driven tests for the orchestrator decision library.
// Run: node --test extensions/orchestrator/tests/orchestrator-lib.test.mjs
// These cases are the contract: any behavior change must update a case here.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_CONFIG,
  ciNudgeText,
  coalesceEvents,
  extractPrNumber,
  findResumableSession,
  formatWakeMessage,
  initState,
  launchGate,
  matchesDialogAllowlist,
  mergeNudgeText,
  resolveRepo,
  resolveWorkerCwd,
  resumeNudgeText,
  isAbortedTurnScreen,
  ABORTED_TURN_PATTERN,
  tick,
  validateConfig,
} from "../lib.mjs";

const T0 = 1_000_000;

function baseConfig(overrides = {}) {
  return validateConfig({}, { statePath: "/tmp/orchestrator-test-state.json", ...overrides }).config;
}

function makeWorker(overrides = {}) {
  return {
    name: "orch-worker-1",
    paneId: "w1:p2",
    ticket: 101,
    invocation: "/code ticket:101",
    prNumber: null,
    prSource: null,
    ciRounds: 0,
    launchedAt: T0,
    lastActivityAt: T0,
    lastChangeSeq: 1,
    lastStatus: "idle",
    phase: "working",
    ...overrides,
  };
}

function live(name = "orch-worker-1", status = "idle", paneId = "w1:p2", seq = 1) {
  return { name, paneId, agentStatus: status, stateChangeSeq: seq };
}

// ---------- validateConfig ----------

test("validateConfig: empty object yields defaults with statePath override", () => {
  const result = validateConfig({}, { statePath: "/tmp/s.json" });
  assert.equal(result.ok, true);
  assert.equal(result.config.maxWorkers, 1);
  assert.equal(result.config.ciMaxRounds, 5);
  assert.equal(result.config.worker.kind, "pi");
  assert.equal(result.config.statePath, "/tmp/s.json");
});

test("validateConfig: fail-closed table", () => {
  const cases = [
    ["non-object", null],
    ["array", []],
    ["string", "config"],
    ["unknown key", { pollSeconds: 5 }],
    ["zero number", { maxWorkers: 0 }],
    ["negative number", { stallSeconds: -3 }],
    ["non-finite", { ciMaxRounds: Number.NaN }],
    ["bad version", { version: 2 }],
    ["bad prefix", { worker: { namePrefix: "Bad Prefix" } }],
    ["bad kind", { worker: { kind: "codex" } }],
    ["startArgs not strings", { worker: { startArgs: ["--ok", 7] } }],
    ["invalid regex allowlist", { dialogAllowlist: ["([unclosed]"] }],
    ["non-string allowlist", { dialogAllowlist: [42] }],
    ["empty statePath", { statePath: "  " }],
    ["empty ciCommand", { ciCommand: "" }],
    ["boolean prDiscovery as string", { prDiscovery: "yes" }],
  ];
  for (const [label, raw] of cases) {
    const result = validateConfig(raw);
    assert.equal(result.ok, false, `expected failure: ${label}`);
    assert.ok(result.errors.length >= 1, `expected errors: ${label}`);
  }
});

test("validateConfig: namePrefix accepts herdr legal names", () => {
  for (const prefix of ["orch-worker", "a", "canary_1-x"]) {
    const result = validateConfig({ worker: { namePrefix: prefix } }, { statePath: "/tmp/s.json" });
    assert.equal(result.ok, true, `${prefix}: ${result.ok ? "" : result.errors.join(";")}`);
  }
});

// ---------- launchGate ----------

test("launchGate: empty state allows, at cap refuses", () => {
  const state = initState(1);
  assert.deepEqual(launchGate(state), { allowed: true, live: 0, max: 1 });
  state.workers.push(makeWorker());
  assert.deepEqual(launchGate(state), { allowed: false, reason: "at_cap", live: 1, max: 1 });
});

test("launchGate: cap 2 allows one then refuses at two", () => {
  const state = initState(2);
  state.workers.push(makeWorker({ name: "w1" }));
  assert.equal(launchGate(state).allowed, true);
  state.workers.push(makeWorker({ name: "w2" }));
  assert.equal(launchGate(state).allowed, false);
});

// ---------- extractPrNumber ----------

test("extractPrNumber: positive and negative table", () => {
  const slug = "yichen/agent-kit";
  const cases = [
    ["https://github.com/yichen/agent-kit/pull/42 opened", 42],
    ["see https://github.com/yichen/agent-kit/pull/7 and later https://github.com/yichen/agent-kit/pull/9", 9],
    ["merged https://github.com/yichen/agent-kit/pull/123.", 123],
    ["fixes https://github.com/yichen/agent-kit/issues/42", null],
    ["https://github.com/other/repo/pull/42", null],
    ["PR #42 is ready", null],
    ["#42 done", null],
    ["", null],
    [null, null],
  ];
  for (const [text, expected] of cases) {
    assert.equal(extractPrNumber(text, slug), expected, JSON.stringify(text));
  }
});

test("extractPrNumber: repo slug regex metacharacters are escaped", () => {
  assert.equal(extractPrNumber("https://github.com/o.rg/repo/pull/5", "o.rg/repo"), 5);
});

// ---------- matchesDialogAllowlist ----------

test("dialog allowlist: anchored match and decoy rejection", () => {
  const patterns = ["^\\[Y\\]es to continue", "^Allow this command\\?"];
  assert.equal(matchesDialogAllowlist("Some output\n[Y]es to continue? (y/n)", patterns), true);
  assert.equal(matchesDialogAllowlist("The task says: Allow this command? as quoted text", patterns), false);
  assert.equal(matchesDialogAllowlist("plain screen", patterns), false);
  assert.equal(matchesDialogAllowlist("", patterns), false);
});

// ---------- coalesceEvents ----------

test("coalesceEvents: dedupes by code+worker, sorts tier3 first, caps", () => {
  const events = [
    { code: "CI_ROUNDS_EXHAUSTED", tier: 2, worker: "w1", ticket: 1 },
    { code: "CI_ROUNDS_EXHAUSTED", tier: 2, worker: "w1", ticket: 1 },
    { code: "BLOCKED_DIALOG", tier: 2, worker: "w1" },
    { code: "FOUNDER_ONLY", tier: 3, worker: "w1" },
    { code: "MERGE_TIMEOUT", tier: 2, worker: "w1" },
    { code: "WORKER_LOST", tier: 2, worker: "w1" },
    { code: "EXTRA", tier: 2, worker: "w1" },
  ];
  const merged = coalesceEvents(events);
  assert.equal(merged[0].code, "FOUNDER_ONLY");
  assert.ok(merged.length <= 5);
  assert.equal(merged.filter((e) => e.code === "CI_ROUNDS_EXHAUSTED").length, 1);
});

// ---------- tick: lifecycle table ----------

test("tick: pane gone without merge -> WORKER_LOST, archived", () => {
  const state = initState(1);
  state.workers.push(makeWorker());
  const { state: next, events } = tick(state, { liveAgents: [], paneScreen: null, pr: null, repoSlug: "o/r" }, baseConfig(), T0 + 10);
  assert.equal(next.workers.length, 0);
  assert.equal(next.history[0].phase, "lost");
  assert.equal(events[0].code, "WORKER_LOST");
});

test("tick: pane gone after merge -> close action + CYCLE_DONE (defensive)", () => {
  const state = initState(1);
  state.workers.push(makeWorker({ prNumber: 9, phase: "awaiting_review" }));
  const { actions, events } = tick(state, { liveAgents: [], paneScreen: null, pr: { number: 9, state: "MERGED", anyFailed: false, allConcluded: true }, repoSlug: "o/r" }, baseConfig(), T0 + 10);
  assert.equal(events[0].code, "CYCLE_DONE");
  void actions;
});

test("tick: blocked with empty allowlist -> wake event, NO actions (fail closed)", () => {
  const state = initState(1);
  state.workers.push(makeWorker());
  const { state: next, actions, events } = tick(state, { liveAgents: [live("orch-worker-1", "blocked")], paneScreen: "Approve tool? [y/n]", pr: null, repoSlug: "o/r" }, baseConfig(), T0 + 10);
  assert.deepEqual(actions, []);
  assert.equal(events[0].code, "BLOCKED_DIALOG");
  assert.equal(next.workers[0].blockedReported, true);
});

test("tick: blocked reported once until status changes", () => {
  const state = initState(1);
  state.workers.push(makeWorker());
  const world = { liveAgents: [live("orch-worker-1", "blocked")], paneScreen: null, pr: null, repoSlug: "o/r" };
  const first = tick(state, world, baseConfig(), T0 + 10);
  assert.equal(first.events.length, 1);
  const second = tick(first.state, world, baseConfig(), T0 + 20);
  assert.equal(second.events.length, 0);
  const afterUnblock = tick(second.state, { ...world, liveAgents: [live("orch-worker-1", "idle", "w1:p2", 2)] }, baseConfig(), T0 + 30);
  const third = tick(afterUnblock.state, world, baseConfig(), T0 + 40);
  assert.equal(third.events.length, 1); // blocked again -> reports again
});

test("tick: blocked with allowlisted dialog -> mechanical enter, capped", () => {
  const config = baseConfig({ dialogAllowlist: ["^Continue\\? \\[Y/n\\]"] });
  const state = initState(1);
  state.workers.push(makeWorker());
  const screen = "output\nContinue? [Y/n] ";
  const world = { liveAgents: [live("orch-worker-1", "blocked")], paneScreen: screen, pr: null, repoSlug: "o/r" };
  const first = tick(state, world, config, T0 + 10);
  assert.deepEqual(first.actions, [{ type: "send_keys", worker: "orch-worker-1", keys: ["enter"] }]);
  assert.equal(first.events.length, 0);
  // drive to the cap, then fail closed to a wake
  let current = first.state;
  let sawBlockedEvent = false;
  for (let i = 0; i < 5; i++) {
    const next = tick(current, world, config, T0 + 20 + i);
    current = next.state;
    if (next.events.some((e) => e.code === "BLOCKED_DIALOG")) sawBlockedEvent = true;
  }
  assert.equal(sawBlockedEvent, true);
});

test("tick: fresh working state -> no actions", () => {
  const state = initState(1);
  state.workers.push(makeWorker({ lastStatus: "working" }));
  const { actions, events } = tick(state, { liveAgents: [live("orch-worker-1", "working", "w1:p2", 1)], paneScreen: null, pr: null, repoSlug: "o/r" }, baseConfig(), T0 + 10);
  assert.deepEqual(actions, []);
  assert.deepEqual(events, []);
});

test("tick: stall -> escape once, then STALL_PERSISTENT once", () => {
  const S = 1000;
  const config = baseConfig({ stallSeconds: 100 });
  const state = initState(1);
  state.workers.push(makeWorker({ lastStatus: "working" }));
  const world = { liveAgents: [live("orch-worker-1", "working", "w1:p2", 1)], paneScreen: null, pr: null, repoSlug: "o/r" };
  const first = tick(state, world, config, T0 + 150 * S);
  assert.deepEqual(first.actions, [{ type: "send_keys", worker: "orch-worker-1", keys: ["escape"] }]);
  const second = tick(first.state, world, config, T0 + 300 * S);
  assert.deepEqual(second.actions, []);
  assert.equal(second.events[0].code, "STALL_PERSISTENT");
  const third = tick(second.state, world, config, T0 + 450 * S);
  assert.deepEqual(third.actions, []);
  assert.deepEqual(third.events, []);
});

test("tick: state change resets stall bookkeeping", () => {
  const S = 1000;
  const config = baseConfig({ stallSeconds: 100 });
  const state = initState(1);
  state.workers.push(makeWorker({ lastStatus: "working" }));
  const stalled = tick(state, { liveAgents: [live("orch-worker-1", "working", "w1:p2", 1)], paneScreen: null, pr: null, repoSlug: "o/r" }, config, T0 + 150 * S);
  const progressed = tick(stalled.state, { liveAgents: [live("orch-worker-1", "working", "w1:p2", 2)], paneScreen: null, pr: null, repoSlug: "o/r" }, config, T0 + 300 * S);
  assert.deepEqual(progressed.actions, []);
  const stalledAgain = tick(progressed.state, { liveAgents: [live("orch-worker-1", "working", "w1:p2", 2)], paneScreen: null, pr: null, repoSlug: "o/r" }, config, T0 + 500 * S);
  assert.deepEqual(stalledAgain.actions, [{ type: "send_keys", worker: "orch-worker-1", keys: ["escape"] }]);
});

test("tick: CI red nudge rounds then CI_ROUNDS_EXHAUSTED once", () => {
  const config = baseConfig({ ciMaxRounds: 3 });
  const state = initState(1);
  state.workers.push(makeWorker({ prNumber: 7, phase: "awaiting_review" }));
  const world = { liveAgents: [live("orch-worker-1", "idle")], paneScreen: null, pr: { number: 7, state: "OPEN", anyFailed: true, allConcluded: true }, repoSlug: "o/r" };
  let current = state;
  const nudgeTexts = [];
  for (let round = 1; round <= 3; round++) {
    const next = tick(current, world, config, T0 + round * 10);
    assert.equal(next.actions.length, 1);
    assert.equal(next.actions[0].type, "prompt");
    nudgeTexts.push(next.actions[0].text);
    current = next.state;
  }
  assert.match(nudgeTexts[0], /round 1 of 3/);
  assert.match(nudgeTexts[2], /round 3 of 3/);
  const exhausted = tick(current, world, config, T0 + 50);
  assert.deepEqual(exhausted.actions, []);
  assert.equal(exhausted.events[0].code, "CI_ROUNDS_EXHAUSTED");
  const settled = tick(exhausted.state, world, config, T0 + 60);
  assert.deepEqual(settled.events, []);
});

test("tick: CI green -> merge nudge once, then MERGE_TIMEOUT after window", () => {
  const S = 1000;
  const config = baseConfig({ mergeNudgeSeconds: 100 });
  const state = initState(1);
  state.workers.push(makeWorker({ prNumber: 7, phase: "awaiting_review" }));
  const world = { liveAgents: [live("orch-worker-1", "idle")], paneScreen: null, pr: { number: 7, state: "OPEN", anyFailed: false, allConcluded: true }, repoSlug: "o/r" };
  const first = tick(state, world, config, T0 + 10);
  assert.equal(first.actions[0].type, "prompt");
  assert.match(first.actions[0].text, /PR #7/);
  const second = tick(first.state, world, config, T0 + 50 * S);
  assert.deepEqual(second.actions, []);
  assert.deepEqual(second.events, []);
  const third = tick(second.state, world, config, T0 + 200 * S);
  assert.deepEqual(third.actions, []);
  assert.equal(third.events[0].code, "MERGE_TIMEOUT");
});

test("tick: merged -> close_pane, CYCLE_DONE, archived", () => {
  const state = initState(1);
  state.workers.push(makeWorker({ prNumber: 7, phase: "awaiting_review" }));
  const world = { liveAgents: [live("orch-worker-1", "idle")], paneScreen: null, pr: { number: 7, state: "MERGED", anyFailed: false, allConcluded: true }, repoSlug: "o/r" };
  const { state: next, actions, events } = tick(state, world, baseConfig(), T0 + 10);
  assert.deepEqual(actions, [{ type: "close_pane", worker: "orch-worker-1", paneId: "w1:p2" }]);
  assert.equal(events[0].code, "CYCLE_DONE");
  assert.equal(next.workers.length, 0);
  assert.equal(next.history[0].phase, "done");
});

test("tick: idle without PR -> resume nudge once, then WORKER_IDLE_UNFINISHED", () => {
  const S = 1000;
  const config = baseConfig({ idleNudgeSeconds: 100 });
  const state = initState(1);
  state.workers.push(makeWorker());
  const world = { liveAgents: [live("orch-worker-1", "idle")], paneScreen: null, pr: null, repoSlug: "o/r" };
  const first = tick(state, world, config, T0 + 200 * S);
  assert.equal(first.actions[0].type, "prompt");
  assert.match(first.actions[0].text, /ticket 101/);
  const second = tick(first.state, world, config, T0 + 250 * S);
  assert.deepEqual(second.actions, []);
  const third = tick(second.state, world, config, T0 + 400 * S);
  assert.equal(third.events[0].code, "WORKER_IDLE_UNFINISHED");
});

test("tick: PR discovered from screen only for origin repo", () => {
  const state = initState(1);
  state.workers.push(makeWorker());
  const world = {
    liveAgents: [live("orch-worker-1", "idle", "w1:p2", 2)],
    paneScreen: "Opened https://github.com/o/r/pull/55 for review",
    pr: null,
    repoSlug: "o/r",
  };
  const { state: next } = tick(state, world, baseConfig(), T0 + 10);
  assert.equal(next.workers[0].prNumber, 55);
  assert.equal(next.workers[0].prSource, "screen");
});

test("tick: never acts on foreign herdr agents", () => {
  const state = initState(1);
  state.workers.push(makeWorker());
  const world = {
    liveAgents: [
      live("orch-worker-1", "idle"),
      live("pi", "working", "w8:pZ", 99),
      live("pi-sharedanchor", "blocked", "wB:p1", 5),
    ],
    paneScreen: null,
    pr: null,
    repoSlug: "o/r",
  };
  const { actions } = tick(state, world, baseConfig(), T0 + 99999);
  for (const action of actions) {
    assert.equal(action.worker, "orch-worker-1");
  }
});

test("tick: does not mutate input state", () => {
  const state = initState(1);
  state.workers.push(makeWorker());
  const before = JSON.stringify(state);
  tick(state, { liveAgents: [live("orch-worker-1", "idle")], paneScreen: null, pr: null, repoSlug: "o/r" }, baseConfig(), T0 + 10);
  assert.equal(JSON.stringify(state), before);
});

// ---------- wake message ----------

test("formatWakeMessage: batches codes with ids and tier3 marker", () => {
  const message = formatWakeMessage([
    { code: "CI_ROUNDS_EXHAUSTED", tier: 2, worker: "w1", ticket: 5, pr: 9, rounds: 5 },
    { code: "FOO", tier: 3, worker: "w1" },
  ]);
  assert.match(message, /CI_ROUNDS_EXHAUSTED ticket=5 pr=9 rounds=5 worker=w1/);
  assert.match(message, /FOO worker=w1 ESCALATE_TO_FOUNDER/);
  assert.equal(formatWakeMessage([]), null);
});

// ---------- nudge text determinism ----------

// ---------- repo registry ----------

const REGISTRY = {
  repos: {
    learnrise: { path: "/Users/x/work/LearnRise" },
    sharedanchor: { path: "/Users/x/work/SharedAnchor", invocation: "/code ticket:{ticket} profile:local" },
  },
};
const allExist = () => true;

test("validateConfig: repo registry accepts valid entries and rejects bad ones", () => {
  assert.equal(validateConfig({ ...REGISTRY }, { statePath: "/tmp/s.json" }).ok, true);
  const cases = [
    ["repos not object", { repos: [] }],
    ["repos null", { repos: null }],
    ["bad repo key", { repos: { "Bad Key": { path: "/x" } } }],
    ["repo entry not object", { repos: { repo1: "/x" } }],
    ["missing path", { repos: { repo1: {} } }],
    ["empty path", { repos: { repo1: { path: "  " } } }],
    ["unknown repo subkey", { repos: { repo1: { path: "/x", model: "m" } } }],
    ["bad invocation", { repos: { repo1: { path: "/x", invocation: "" } } }],
    ["bad startArgs", { repos: { repo1: { path: "/x", startArgs: [1] } } }],
    ["bad ciCommand", { repos: { repo1: { path: "/x", ciCommand: "" } } }],
  ];
  for (const [label, raw] of cases) {
    const result = validateConfig(raw, { statePath: "/tmp/s.json" });
    assert.equal(result.ok, false, `expected failure: ${label}`);
  }
});


test("resolveRepo: omitted key falls back to session cwd and default invocation", () => {
  const cfg = baseConfig();
  const resolved = resolveRepo(cfg, null, "/session/cwd", allExist);
  assert.deepEqual(resolved, {
    ok: true,
    path: "/session/cwd",
    invocation: cfg.worker.invocation,
    startArgs: cfg.worker.startArgs,
    ciCommand: cfg.ciCommand,
    repo: null,
  });
});

test("resolveRepo: registered key resolves path and per-repo overrides", () => {
  const cfg = baseConfig({ ...REGISTRY });
  const resolved = resolveRepo(cfg, "sharedanchor", "/session/cwd", allExist);
  assert.equal(resolved.ok, true);
  assert.equal(resolved.path, "/Users/x/work/SharedAnchor");
  assert.equal(resolved.invocation, "/code ticket:{ticket} profile:local");
  assert.equal(resolved.repo, "sharedanchor");
  assert.equal(resolved.ciCommand, cfg.ciCommand);
  const plain = resolveRepo(cfg, "learnrise", "/session/cwd", allExist);
  assert.equal(plain.invocation, cfg.worker.invocation); // inherits default
});

test("resolveRepo: unknown key refuses with registered list; missing path refuses", () => {
  const cfg = baseConfig({ ...REGISTRY });
  const unknown = resolveRepo(cfg, "nope", "/session/cwd", allExist);
  assert.equal(unknown.ok, false);
  assert.match(unknown.error, /unknown repo 'nope'/);
  assert.match(unknown.error, /learnrise, sharedanchor/);
  const missing = resolveRepo(cfg, "learnrise", "/session/cwd", () => false);
  assert.equal(missing.ok, false);
  assert.match(missing.error, /does not exist/);
});


test("resolveWorkerCwd: recorded path wins, then live cwd, then fallback", () => {
  assert.equal(resolveWorkerCwd({ repoPath: "/recorded" }, "/live", "/fallback"), "/recorded");
  assert.equal(resolveWorkerCwd({}, "/live", "/fallback"), "/live");
  assert.equal(resolveWorkerCwd(null, null, "/fallback"), "/fallback");
  assert.equal(resolveWorkerCwd({ repoPath: "/recorded" }, null, "/fallback"), "/recorded");
});

// ---------- resume-first ----------

test("findResumableSession: most recent matching ticket wins", () => {
  const history = [
    { name: "w1", ticket: 7, sessionPath: "/old.jsonl" },
    { name: "w2", ticket: 9, sessionPath: "/other.jsonl" },
    { name: "w3", ticket: 7, sessionPath: "/new.jsonl" },
  ];
  assert.equal(findResumableSession(history, 7), "/new.jsonl");
  assert.equal(findResumableSession(history, 9), "/other.jsonl");
});

test("findResumableSession: fail-closed table", () => {
  assert.equal(findResumableSession([{ ticket: 7 }], 7), null, "record without sessionPath");
  assert.equal(findResumableSession([{ ticket: 8, sessionPath: "/x" }], 7), null, "ticket mismatch");
  assert.equal(findResumableSession([], 7), null, "empty history");
  assert.equal(findResumableSession(null, 7), null, "null history");
  assert.equal(findResumableSession([{ ticket: 7, sessionPath: "/x" }], null), null, "null ticket");
});

test("tick: aborted turn gets fast nudge, once per episode, capped", () => {
  const config = baseConfig({ autoAnswerCap: 2 });
  const state = initState(1);
  state.workers.push(makeWorker());
  const aborted = { liveAgents: [live("orch-worker-1", "idle", "w1:p2", 1)], paneScreen: " Operation aborted", pr: null, repoSlug: "o/r" };
  const healthy = { ...aborted, paneScreen: "all good" };

  const first = tick(state, aborted, config, T0 + 10);
  assert.equal(first.actions.length, 1);
  assert.match(first.actions[0].text, /aborted mid-work/);
  const second = tick(first.state, aborted, config, T0 + 20);
  assert.deepEqual(second.actions, [], "no duplicate nudge within the same idle episode");

  // Status change starts a new episode; second nudge consumed.
  const working = tick(second.state, { ...aborted, liveAgents: [live("orch-worker-1", "working", "w1:p2", 2)] }, config, T0 + 30);
  const third = tick(working.state, aborted, config, T0 + 40);
  assert.equal(third.actions.length, 1, "second episode nudges again");

  // Cap reached: further aborts wake the orchestrator instead of nudging.
  const working2 = tick(third.state, { ...aborted, liveAgents: [live("orch-worker-1", "working", "w1:p2", 3)] }, config, T0 + 50);
  const fourth = tick(working2.state, aborted, config, T0 + 60);
  assert.deepEqual(fourth.actions, []);
  assert.equal(fourth.events[0].code, "WORKER_ABORT_LOOP");
  const fifth = tick(fourth.state, aborted, config, T0 + 70);
  assert.deepEqual(fifth.events, [], "abort loop reported once");

  // Healthy screen never triggers the abort path.
  const fine = tick(fourth.state, healthy, config, T0 + 80);
  assert.deepEqual(fine.actions, []);
});

test("nudge templates are deterministic and self-describing", () => {
  assert.equal(ciNudgeText(101, 7, 2, 5), ciNudgeText(101, 7, 2, 5));
  assert.match(ciNudgeText(101, 7, 2, 5), /root cause/);
  assert.match(ciNudgeText(101, 7, 2, 5), /round 2 of 5/);
  assert.match(mergeNudgeText(7), /PR #7/);
  assert.match(resumeNudgeText(101), /ticket 101/);
  assert.match(" Operation aborted", new RegExp(ABORTED_TURN_PATTERN, "m"));
  assert.doesNotMatch("Operation abortedly fine", new RegExp(ABORTED_TURN_PATTERN, "m"));
});

test("isAbortedTurnScreen: only the visible tail counts", () => {
  assert.equal(isAbortedTurnScreen("working...\n Operation aborted"), true);
  assert.equal(
    isAbortedTurnScreen(" Operation aborted\nnow doing other things\nmade edits\nran tests\ntests pass"),
    false,
    "stale abort buried in scrollback",
  );
  assert.equal(isAbortedTurnScreen(""), false);
  assert.equal(isAbortedTurnScreen(null), false);
});

// ---------- config default sanity ----------

test("DEFAULT_CONFIG pins the agreed orchestration policy", () => {
  assert.equal(DEFAULT_CONFIG.maxWorkers, 1);
  assert.equal(DEFAULT_CONFIG.ciMaxRounds, 5);
  assert.equal(DEFAULT_CONFIG.dialogAllowlist.length, 0); // fail closed: every dialog wakes the orchestrator
  assert.deepEqual(DEFAULT_CONFIG.worker.startArgs, ["--provider", "ollama", "--model", "qwen3.8:27b-mlx-128k"]);
});
