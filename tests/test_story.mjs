import test from "node:test";
import assert from "node:assert/strict";
import { buildFrames, viewAtFrame } from "../web/release-story.mjs";
const behavior = { total: 24, passed: 24, failed: 0, complete: true };
const base = {
  status: "promoted",
  traffic: 100,
  created_at: "2026-11-26T10:00:00Z",
  updated_at: "2026-11-26T10:00:05Z",
  behavior,
  windows: [],
  events: [],
  summary: "Promoted",
  policy: { consecutive_breach_windows: 2 },
};
const window = (stage, candidate, stable, baseline = 0, verdict = "pass") => ({
  stage,
  verdict,
  breached_rules: verdict === "breach" ? ["p95 latency"] : [],
  candidate: { count: candidate, routed_count: candidate },
  stable: {
    count: stable,
    routed_count: stable - baseline,
    baseline_count: baseline,
  },
  unknown_count: 0,
});
test("replay never reveals future checks, exposure or recovery", () => {
  const r = {
    ...base,
    windows: [
      window(10, 6, 54),
      window(50, 30, 30),
      window(100, 60, 6, 6),
      window(100, 60, 6, 6),
    ],
  };
  const frames = buildFrames(r),
    start = viewAtFrame(r, frames[0]),
    first = viewAtFrame(r, frames[2]),
    end = viewAtFrame(r, frames.at(-1));
  assert.equal(start.behavior, null);
  assert.equal(start.candidateRequests, 0);
  assert.equal(start.windows.length, 0);
  assert.equal(first.candidateRequests, 6);
  assert.equal(first.stableRequests, 54);
  assert.equal(first.windows.length, 1);
  assert.equal(end.candidateRequests, 156);
  assert.equal(end.stableRequests, 84);
  assert.equal(end.recovery, undefined);
  assert.deepEqual(
    frames.filter((f) => f.kind === "window").map((f) => f.traffic),
    [10, 50, 100, 100],
  );
});
test("blocked releases have no invented traffic measurements", () => {
  const r = {
    ...base,
    status: "blocked",
    traffic: 0,
    behavior: { ...behavior, passed: 18, failed: 6 },
  };
  const frames = buildFrames(r);
  assert.deepEqual(
    frames.map((f) => f.kind),
    ["start", "behavior", "decision"],
  );
  assert.equal(frames[1].tone, "danger");
  assert.equal(viewAtFrame(r, frames.at(-1)).candidateRequests, 0);
});
test("recovery is only visible after restore and keeps direct probes out of exposure", () => {
  const r = {
    ...base,
    status: "recovered",
    traffic: 0,
    windows: [window(10, 6, 54, 0, "breach"), window(10, 6, 54, 0, "breach")],
    events: [
      {
        status: "rolling_back",
        message: "Restore stable",
        at: "2026-11-26T10:00:04Z",
      },
    ],
    recovery: { total: 6, passed: 6, failed: 0, complete: true },
  };
  const frames = buildFrames(r);
  assert.deepEqual(
    frames.map((f) => f.kind),
    [
      "start",
      "behavior",
      "window",
      "window",
      "rollback",
      "recovery",
      "decision",
    ],
  );
  assert.equal(frames[4].traffic, null);
  assert.equal(viewAtFrame(r, frames[4]).recovery, null);
  assert.equal(viewAtFrame(r, frames[5]).recovery.passed, 6);
  assert.equal(frames[5].traffic, 0);
});
test("incomplete measurements and failed recovery stay explicit", () => {
  const r = {
    ...base,
    status: "needs_attention",
    traffic: 0,
    windows: [{ ...window(10, 0, 6, 6, "insufficient"), unknown_count: 60 }],
    recovery: { total: 6, passed: 0, failed: 1, skipped: 5, complete: false },
  };
  const frames = buildFrames(r);
  assert.equal(frames[2].title, "More evidence is needed.");
  assert.equal(frames.find((f) => f.kind === "recovery").tone, "danger");
  assert.equal(viewAtFrame(r, frames.at(-1)).unknownRequests, 60);
  assert.equal(viewAtFrame(r, frames.at(-1)).stableRequests, 0);
});
test("an active release has no fabricated final decision", () => {
  const frames = buildFrames({
    ...base,
    status: "canary",
    windows: [window(10, 6, 54)],
  });
  assert.equal(
    frames.some((f) => f.kind === "decision"),
    false,
  );
  assert.equal(frames.at(-1).kind, "window");
});
test("zero and incomplete fixture sets are never presented as passing", () => {
  for (const result of [
    { total: 0, passed: 0, failed: 0, complete: true },
    { total: 24, passed: 24, failed: 0, complete: false },
  ]) {
    const frames = buildFrames({
      ...base,
      behavior: result,
      recovery: result,
      status: "needs_attention",
    });
    assert.equal(frames.find((f) => f.kind === "behavior").tone, "danger");
    assert.equal(frames.find((f) => f.kind === "recovery").tone, "danger");
  }
});
