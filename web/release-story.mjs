// The walkthrough projects recorded evidence. It never controls traffic.
export const ACTIVE = new Set([
  "queued",
  "validating",
  "canary",
  "paused",
  "rolling_back",
]);
export const STATUS = {
  queued: "Queued",
  validating: "Checking behavior",
  canary: "Observing traffic",
  paused: "Waiting for evidence",
  rolling_back: "Restoring stable",
  blocked: "Release blocked",
  recovered: "Recovery verified",
  promoted: "Release promoted",
  inconclusive: "Inconclusive",
  needs_attention: "Needs attention",
};
export const SCENARIOS = {
  wrong_total: {
    name: "Incorrect checkout total",
    action: "Prevent",
    description:
      "A successful response with the wrong price. Catch it before rollout.",
  },
  slow: {
    name: "Slow candidate",
    action: "Recover",
    description:
      "Correct prices, slow responses. Detect the breach and verify recovery.",
  },
  healthy: {
    name: "Healthy candidate",
    action: "Release",
    description:
      "Correct behavior and healthy performance. Follow the full rollout.",
  },
};
export function tone(status) {
  return (
    {
      blocked: "danger",
      needs_attention: "danger",
      recovered: "success",
      promoted: "success",
      paused: "warning",
      rolling_back: "warning",
      inconclusive: "warning",
    }[status] || "neutral"
  );
}
export function checksPassed(result) {
  return (
    !!result &&
    result.total > 0 &&
    result.passed === result.total &&
    result.failed === 0 &&
    result.complete !== false
  );
}
export function buildFrames(r) {
  const frames = [
    {
      kind: "start",
      label: "Candidate",
      title: "A new candidate enters the gate.",
      description:
        "The recorded stable version keeps serving traffic while the candidate is prepared for checks.",
      traffic: 0,
      windowCount: 0,
      at: r.created_at,
      tone: "neutral",
    },
  ];
  if (r.behavior) {
    const b = r.behavior,
      passed = checksPassed(b);
    frames.push({
      kind: "behavior",
      label: "Behavior",
      title: passed
        ? "The checkout contract passes."
        : "The behavior gate did not pass.",
      description: passed
        ? `${b.passed} of ${b.total} baskets matched the expected contract on both versions. The candidate can enter the canary.`
        : `${b.passed} of ${b.total} baskets passed${b.skipped ? `; ${b.skipped} were skipped` : ""}. Inspect the expected and observed results below.`,
      traffic: 0,
      windowCount: 0,
      tone: passed ? "success" : "danger",
      at:
        r.events?.find((e) => e.status === "blocked" || e.status === "canary")
          ?.at || null,
    });
  }
  (r.windows || []).forEach((w, i) =>
    frames.push({
      kind: "window",
      label: `${w.stage}% · W${i + 1}`,
      windowIndex: i,
      windowCount: i + 1,
      traffic: w.stage,
      title:
        w.verdict === "insufficient"
          ? "More evidence is needed."
          : w.verdict === "breach"
            ? "The candidate exceeds policy."
            : "This observation window passes.",
      description:
        w.verdict === "insufficient"
          ? "Missing attribution or too few samples prevents a reliable decision. The controller holds this stage."
          : w.verdict === "breach"
            ? `${(w.breached_rules || []).join(" and ") || "Performance"} exceeded the configured limit. Rollback requires ${r.policy?.consecutive_breach_windows ?? 2} consecutive breach windows.`
            : `${w.candidate.count} candidate requests were measured at ${w.stage}% traffic. Latency and errors stayed within policy.`,
      at: w.at || w.finished_at,
      tone: w.verdict === "pass" ? "success" : "warning",
    }),
  );
  const rollback = r.events?.find((e) => e.status === "rolling_back");
  if (rollback)
    frames.push({
      kind: "rollback",
      label: "Restore",
      title: "Restore the recorded stable version.",
      description: rollback.message,
      traffic: null,
      windowCount: r.windows?.length || 0,
      at: rollback.at,
      tone: "warning",
    });
  if (r.recovery)
    frames.push({
      kind: "recovery",
      label: "Verify",
      title: checksPassed(r.recovery)
        ? "Stable behavior is verified again."
        : "Recovery needs a closer look.",
      description: `${r.recovery.passed} of ${r.recovery.total} critical baskets passed. These checks exercise the routed checkout and verify the expected stable version.`,
      traffic: r.traffic,
      windowCount: r.windows?.length || 0,
      at: r.updated_at,
      tone: checksPassed(r.recovery) ? "success" : "danger",
    });
  if (!ACTIVE.has(r.status))
    frames.push({
      kind: "decision",
      label: "Decision",
      title: STATUS[r.status] || r.status,
      description: r.summary,
      traffic: r.traffic,
      windowCount: r.windows?.length || 0,
      at: r.updated_at,
      tone: tone(r.status),
    });
  return frames;
}
export function viewAtFrame(r, frame) {
  const windows = (r.windows || []).slice(0, frame.windowCount);
  return {
    windows,
    behavior: frame.kind === "start" ? null : r.behavior,
    recovery: ["recovery", "decision"].includes(frame.kind) ? r.recovery : null,
    candidateRequests: windows.reduce(
      (n, w) => n + (w.candidate.routed_count ?? w.candidate.count ?? 0),
      0,
    ),
    stableRequests: windows.reduce(
      (n, w) => n + (w.stable.routed_count ?? 0),
      0,
    ),
    unknownRequests: windows.reduce((n, w) => n + (w.unknown_count || 0), 0),
  };
}
