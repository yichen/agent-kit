#!/usr/bin/env bash
# Print a deterministic daily health and diverted-work report from Ollama events.
# The default mode is read-only. --notify adds a macOS alert only when a day
# with at least one attempt has zero successes or a success rate below 80%.

set -u

LOG_FILE="${OLLAMA_DELEGATE_LOG_FILE:-$HOME/.local/state/sharedanchor-ollama-delegate/events.log}"
REPORT_DATE="$(date '+%Y-%m-%d')"
NOTIFY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --date)
      [ $# -ge 2 ] || { echo "ollama-delegate-report: --date requires YYYY-MM-DD or yesterday" >&2; exit 1; }
      REPORT_DATE="$2"
      shift 2
      ;;
    --log-file)
      [ $# -ge 2 ] || { echo "ollama-delegate-report: --log-file requires a path" >&2; exit 1; }
      LOG_FILE="$2"
      shift 2
      ;;
    --notify)
      NOTIFY=1
      shift
      ;;
    -h|--help)
      sed -n '2,/^set -u$/p' "$0" | sed '$d'
      exit 0
      ;;
    *)
      echo "ollama-delegate-report: unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

NODE_BIN=""
for candidate in "${OLLAMA_DELEGATE_NODE:-}" "$(command -v node 2>/dev/null || true)" /opt/homebrew/bin/node /usr/local/bin/node; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    NODE_BIN="$candidate"
    break
  fi
done
if [ -z "$NODE_BIN" ]; then
  echo "ollama-delegate-report: Node.js is required" >&2
  exit 2
fi
if [ "$REPORT_DATE" = "yesterday" ]; then
  # shellcheck disable=SC2016 # JavaScript template literals are intentionally single-quoted for the shell.
  REPORT_DATE="$("$NODE_BIN" -e 'const d=new Date(); d.setDate(d.getDate()-1); const p=(n)=>String(n).padStart(2,"0"); process.stdout.write(`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`);')"
fi
if ! [[ "$REPORT_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "ollama-delegate-report: --date must be YYYY-MM-DD or yesterday" >&2
  exit 1
fi

REPORT_LOG_FILE="$LOG_FILE" REPORT_DATE="$REPORT_DATE" REPORT_NOTIFY="$NOTIFY" "$NODE_BIN" <<'NODE'
const fs = require("fs");
const {spawnSync} = require("child_process");

const pricingDate = "2026-09-13";
const inputPrice = 2.00;
const outputPrice = 10.00;
const outcomes = ["success", "busy", "timeout", "unavailable", "prompt_too_large", "invalid_output", "lock_identity_error", "error"];
const reportDate = process.env.REPORT_DATE;
const logPath = process.env.REPORT_LOG_FILE;
const notify = process.env.REPORT_NOTIFY === "1";
const testCaller = (caller) => caller === "smoke-test" || caller.endsWith("/test") || caller.startsWith("test/");
const localDate = (value) => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};
const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(reportDate);
const validDate = (() => {
  if (!dateMatch) return false;
  const year = Number(dateMatch[1]);
  const month = Number(dateMatch[2]);
  const day = Number(dateMatch[3]);
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= days[month - 1];
})();
if (!validDate) {
  process.stderr.write("ollama-delegate-report: invalid calendar date\n");
  process.exit(1);
}

const counts = Object.fromEntries(outcomes.map((outcome) => [outcome, 0]));
const attempts = [];
const consumptions = new Set();
const resultStates = {
  delivered: new Set(),
  verified_used: new Set(),
  verified_rejected: new Set(),
  superseded: new Set(),
};
let malformed = 0;
if (fs.existsSync(logPath) && fs.statSync(logPath).isFile()) {
  for (const rawLine of fs.readFileSync(logPath, "utf8").split("\n")) {
    if (!rawLine) continue;
    try {
      const event = JSON.parse(rawLine);
      if (!event || typeof event !== "object" || typeof event.timestamp !== "string" || typeof event.caller !== "string") throw new Error("shape");
      const eventDate = localDate(event.timestamp);
      if (!eventDate) throw new Error("timestamp");
      const type = event.event_type || "attempt";
      if (type === "consumption") {
        if (event.outcome !== "consumed" || !/^[0-9a-f]{32}$/.test(event.event_id || "")) throw new Error("consumption");
        consumptions.add(`${event.event_id}\0${event.caller}`);
        continue;
      }
      if (type === "result") {
        if (!Object.hasOwn(resultStates, event.outcome) || !/^[0-9a-f]{32}$/.test(event.event_id || "")) throw new Error("result");
        resultStates[event.outcome].add(`${event.event_id}\0${event.caller}`);
        continue;
      }
      if (type === "maintenance") continue;
      if (type !== "attempt" || !outcomes.includes(event.outcome) || !Number.isInteger(event.prompt_tokens) || event.prompt_tokens < 0 || !Number.isInteger(event.completion_tokens) || event.completion_tokens < 0 || typeof event.reason !== "string") throw new Error("attempt");
      if (eventDate === reportDate) {
        attempts.push(event);
        counts[event.outcome] += 1;
      }
    } catch (_) {
      malformed += 1;
    }
  }
}

const production = attempts.filter((event) => !testCaller(event.caller));
const tests = attempts.filter((event) => testCaller(event.caller));
const successes = production.filter((event) => event.outcome === "success");
const wasConsumed = (event) => typeof event.event_id === "string" && consumptions.has(`${event.event_id}\0${event.caller}`);
const consumed = successes.filter(wasConsumed);
const consumedTests = tests.filter((event) => event.outcome === "success" && wasConsumed(event));
const hasResult = (state, event) => typeof event.event_id === "string" && resultStates[state].has(`${event.event_id}\0${event.caller}`);
const delivered = successes.filter((event) => hasResult("delivered", event));
const verifiedUsed = successes.filter((event) => hasResult("verified_used", event));
const verifiedRejected = successes.filter((event) => hasResult("verified_rejected", event));
const superseded = successes.filter((event) => hasResult("superseded", event));
const useful = verifiedUsed;
const successRate = production.length ? successes.length * 100 / production.length : 0;
const reasonCounts = new Map();
for (const event of production.filter((item) => item.outcome !== "success" && item.reason)) reasonCounts.set(event.reason, (reasonCounts.get(event.reason) || 0) + 1);
const topReason = [...reasonCounts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
const sessions = new Set(production.map((event) => event.session_hash).filter((value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value)));

console.log("Attempts and health");
console.log(`Date: ${reportDate}`);
console.log(`Total attempts: ${attempts.length}`);
console.log(`Production attempts: ${production.length}`);
console.log(`Test attempts: ${attempts.length - production.length}`);
for (const outcome of outcomes) console.log(`${outcome}: ${counts[outcome]}`);
console.log(production.length ? `Production success rate: ${successRate.toFixed(1)}%` : "Production success rate: n/a (no attempts)");
console.log(`Validated production generations: ${successes.length}`);
console.log(`Consumed production turns: ${consumed.length}`);
console.log(`Consumed test turns: ${consumedTests.length}`);
console.log(`V2 delivered results: ${delivered.length}`);
console.log(`V2 verified-used results: ${verifiedUsed.length}`);
console.log(`V2 verified-rejected results: ${verifiedRejected.length}`);
console.log(`V2 superseded results: ${superseded.length}`);
console.log(delivered.length ? `V2 verification yield: ${(verifiedUsed.length * 100 / delivered.length).toFixed(1)}%` : "V2 verification yield: n/a (no delivered results)");
console.log(`Known production sessions: ${sessions.size}`);
if (production.length && successes.length === 0) console.log("Zero production successes: yes");
if (production.length && successRate < 80) console.log(`Most common failure reason: ${topReason ? `${topReason[0]} (${topReason[1]})` : "none recorded"}`);
if (malformed) console.log(`Malformed event lines ignored: ${malformed}`);

const sum = (items, field) => items.reduce((total, event) => total + event[field], 0);
const allPrompt = sum(successes, "prompt_tokens");
const allCompletion = sum(successes, "completion_tokens");
const consumedPrompt = sum(consumed, "prompt_tokens");
const consumedCompletion = sum(consumed, "completion_tokens");
const usefulPrompt = sum(useful, "prompt_tokens");
const usefulCompletion = sum(useful, "completion_tokens");
const consumedValue = (consumedPrompt * inputPrice + consumedCompletion * outputPrice) / 1_000_000;
const value = (usefulPrompt * inputPrice + usefulCompletion * outputPrice) / 1_000_000;

const percentile = (values, fraction) => {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
};
const wallDurations = successes.map((event) => event.duration_ms).filter((value) => Number.isFinite(value) && value >= 0);
const generationRates = successes.flatMap((event) => Number.isInteger(event.eval_duration_ns) && event.eval_duration_ns > 0 && event.completion_tokens > 0
  ? [event.completion_tokens * 1e9 / event.eval_duration_ns]
  : []);

console.log();
console.log("Delegated work");
console.log(`Validated production tokens: ${allPrompt + allCompletion} (${allPrompt} prompt, ${allCompletion} completion)`);
console.log(`Consumed production tokens: ${consumedPrompt + consumedCompletion} (${consumedPrompt} prompt, ${consumedCompletion} completion)`);
console.log(`Sonnet 5 API-equivalent value of consumed work: $${consumedValue.toFixed(4)}`);
console.log(`Verified-use production tokens: ${usefulPrompt + usefulCompletion} (${usefulPrompt} prompt, ${usefulCompletion} completion)`);
console.log(`Sonnet 5 API-equivalent value of verified-use work: $${value.toFixed(4)}`);
if (wallDurations.length) console.log(`Successful wall latency: p50 ${(percentile(wallDurations, 0.5) / 1000).toFixed(2)}s, p95 ${(percentile(wallDurations, 0.95) / 1000).toFixed(2)}s`);
if (generationRates.length) console.log(`Generation throughput: p50 ${percentile(generationRates, 0.5).toFixed(1)} tok/s, p95 ${percentile(generationRates, 0.95).toFixed(1)} tok/s`);
const taskTypes = [...new Set(successes.map((event) => event.task_type).filter((value) => typeof value === "string"))].sort();
for (const taskType of taskTypes) {
  const items = successes.filter((event) => event.task_type === taskType);
  const durations = items.map((event) => event.duration_ms).filter((value) => Number.isFinite(value) && value >= 0);
  console.log(`Task ${taskType}: ${items.length} success, p50 ${durations.length ? (percentile(durations, 0.5) / 1000).toFixed(2) + "s" : "n/a"}`);
}
console.log();
console.log(`Pricing constant: Sonnet 5, ${pricingDate}, $${inputPrice.toFixed(2)}/million input tokens and $${outputPrice.toFixed(2)}/million output tokens.`);
console.log("Caveat: legacy success events have no consumption receipt, so they count as validated generations but not confirmed consumed turns.");
console.log("Caveat: legacy consumption receipts mean delivered-to-caller, while V2 verified-used events require evidence and source verification.");
console.log("Caveat: consumed work is attributed to the generation's local calendar date, including receipts published just after midnight.");
console.log("Caveat: the Max plan is a flat subscription, so this value is not cash returned to the account.");
console.log("Caveat: local and Claude tokenizers and verbosity differ, so this estimates equivalent work rather than Claude tokens actually emitted.");

if (notify && production.length && (successes.length === 0 || successRate < 80)) {
  const message = `${reportDate}: ${successes.length}/${production.length} Ollama delegate attempts succeeded (${successRate.toFixed(0)}%). Run /ollama-report.`;
  const script = `display notification ${JSON.stringify(message)} with title "SharedAnchor Ollama delegate" sound name "Basso"`;
  spawnSync("osascript", ["-e", script], {stdio: "ignore"});
}
NODE
