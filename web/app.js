import {
  ACTIVE,
  STATUS,
  SCENARIOS,
  tone,
  buildFrames,
  viewAtFrame,
  checksPassed,
} from "./release-story.mjs";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const symbols = {
  play: '<path d="m8 5 11 7-11 7z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  back: '<path d="m14 6-6 6 6 6"/>',
  next: '<path d="m10 6 6 6-6 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  stop: '<path d="m7 7 10 10M17 7 7 17"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M5 17v4h14v-4"/>',
  replay: '<path d="M4 5v5h5M4 10a8 8 0 1 1 0 5"/>',
};
const icon = (name) =>
  `<svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${symbols[name] || ""}</svg>`;
const ms = (v) =>
  typeof v !== "number"
    ? "—"
    : v >= 1000
      ? `${(v / 1000).toFixed(2)} s`
      : `${Number(v.toFixed(1))} ms`;
const pct = (v) =>
  typeof v !== "number" ? "—" : `${Number((v * 100).toFixed(1))}%`;
const cash = (v) =>
  typeof v === "number"
    ? `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`
    : "—";
const stamp = (v) =>
  v
    ? new Date(v).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const clock = (v) =>
  v ? new Date(v).toLocaleTimeString(undefined, { hour12: false }) : "—";
const badge = (status) =>
  `<span class="badge ${tone(status)}"><span class="dot"></span>${esc(STATUS[status] || status)}</span>`;
let model = null,
  selectedId = "",
  busy = false,
  connected = false,
  snapshot = "",
  historySnapshot = "",
  pollTimer,
  serial = 0;
let replay = false,
  frameIndex = 0,
  playing = false,
  playTimer,
  speed = 3500,
  historyFilter = "all";
let announcement = "";
const pendingIds = new Map();
function selected() {
  return (
    model?.releases.find((r) => r.id === selectedId) ||
    model?.releases[0] ||
    null
  );
}
function report(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
function pause() {
  clearTimeout(playTimer);
  playing = false;
}
function resetPlayback() {
  pause();
  replay = false;
  frameIndex = 0;
}
function announce(text) {
  if (announcement !== text) {
    $("announcement").textContent = text;
    announcement = text;
  }
}
function renderHistory(r) {
  const items = model.releases.filter(
    (x) =>
      historyFilter === "all" ||
      (historyFilter === "attention"
        ? ["needs_attention", "inconclusive", "paused"].includes(x.status)
        : x.status === historyFilter),
  );
  const key = JSON.stringify([items, r?.id]);
  if (key === historySnapshot) return;
  const scroll = $("history").scrollTop,
    horizontal = $("history").scrollLeft;
  const focus = document.activeElement?.dataset?.release;
  $("history").innerHTML =
    items
      .map(
        (x) =>
          `<button class="history-row ${x.id === r?.id ? "chosen" : ""}" data-release="${esc(x.id)}" aria-pressed="${x.id === r?.id}" aria-label="${esc(SCENARIOS[x.scenario]?.action || x.scenario)} · ${esc(STATUS[x.status])} · ${esc(x.id)}"><span class="history-top"><span class="dot ${tone(x.status)}"></span><strong>${esc(SCENARIOS[x.scenario]?.action || x.scenario)}</strong></span><span style="font-size:11px">${esc(STATUS[x.status] || x.status)}</span><small>${esc(x.id.slice(0, 7))} · ${esc(stamp(x.created_at))}</small></button>`,
      )
      .join("") || '<p class="sidebar-empty">No releases in this view.</p>';
  $("history").scrollTop = scroll;
  $("history").scrollLeft = horizontal;
  if (focus)
    [...$("history").querySelectorAll("button")]
      .find((b) => b.dataset.release === focus)
      ?.focus({ preventScroll: true });
  $("releaseCount").textContent = model.releases.length;
  historySnapshot = key;
}
function summaryCopy(r) {
  const sample = r.behavior?.failures?.[0];
  if (
    r.status === "blocked" &&
    sample?.expected?.total !== undefined &&
    sample?.candidate?.values?.total !== sample?.expected?.total
  )
    return `Expected ${cash(sample.expected.total)}; the candidate returned ${cash(sample.candidate?.values?.total)} with HTTP ${sample.candidate?.http_status ?? "—"}. The behavior gate stopped rollout before routed traffic.`;
  if (r.status === "recovered")
    return `The candidate was stopped and stable routing was restored. ${r.recovery?.passed ?? 0} critical checkout checks verified recovery.`;
  if (r.status === "promoted")
    return "Behavior checks and every traffic stage passed. The candidate was accepted as stable at the end of this run.";
  return r.summary;
}
function stats(r, frame, view) {
  const terminal = frame.kind === "decision";
  const fields = [
    [
      "Behavior checks",
      view.behavior
        ? `${view.behavior.passed}<small> / ${view.behavior.total}</small>`
        : "—",
      view.behavior?.skipped
        ? `${view.behavior.skipped} skipped`
        : "Baskets matching the contract",
    ],
    [
      "Candidate exposure",
      String(view.candidateRequests),
      `${view.stableRequests} stable · ${view.unknownRequests} unattributed`,
    ],
    [
      "Detection interval",
      terminal ? ms(r.detection_ms) : "—",
      "First breach → rollback decision",
    ],
    [
      "Recovery checks",
      view.recovery
        ? `${view.recovery.passed}<small> / ${view.recovery.total}</small>`
        : "—",
      view.recovery
        ? `Verification took ${ms(r.recovery_ms)}`
        : "Verified through the router",
    ],
  ];
  return `<div class="stats" aria-label="${replay ? "Evidence through this replay step" : "Release measurements"}">${fields.map(([label, value, note]) => `<div class="stat"><div class="stat-label">${label}</div><strong>${value}</strong><small>${esc(note)}</small></div>`).join("")}</div>`;
}
function priceChart(b, recovering = false) {
  const s = b.failures?.[0] || b.sample;
  if (!s)
    return noChart(
      "No comparison recorded",
      "Open the complete evidence for the behavior gate result.",
    );
  const rows = [
    ["expected", "Expected", s.expected?.total],
    ["stable", "Stable", s.stable?.values?.total],
    [
      "candidate",
      recovering ? "Routed" : "Candidate",
      s.candidate?.values?.total,
    ],
  ];
  const max = Math.max(
    1,
    ...rows.map((x) => (typeof x[2] === "number" ? x[2] : 0)),
  );
  return `<div class="chart-heading"><div><h3>${recovering ? "Recovery check" : "Checkout comparison"}</h3><p>${esc(s.name)} · total in INR</p></div><span class="badge ${checksPassed(b) ? "success" : "danger"}">${b.passed} / ${b.total} passed</span></div>
    <div class="price-chart" role="img" aria-label="${esc(rows.map((x) => `${x[1]} ${cash(x[2])}`).join(", "))}">${rows.map(([cls, label, value]) => `<div class="price-row ${cls}"><span>${label}</span><div class="price-track"><div class="price-fill" style="width:${typeof value === "number" ? (Math.max(0, value) / max) * 100 : 0}%"></div></div><strong>${cash(value)}</strong></div>`).join("")}</div>
    <div class="price-input">${esc(s.input?.discount_percent ?? 0)}% discount · Stable HTTP ${esc(s.stable?.http_status ?? "—")} · ${recovering ? "Routed" : "Candidate"} HTTP ${esc(s.candidate?.http_status ?? "—")}</div>
    <p class="chart-caption">${b.failed ? "A successful HTTP response alone does not prove correct behavior." : "The displayed basket is one of the recorded contract checks."}</p>`;
}
function noChart(title, copy) {
  return `<div class="no-chart"><div class="large-number" aria-hidden="true">01 /</div><h3>${esc(title)}</h3><p>${esc(copy)}</p></div>`;
}
function latencyChart(r, frame, windows) {
  const limit = r.policy?.candidate_p95_limit_ms ?? 200;
  const all = r.windows || [],
    total = Math.max(1, all.length);
  const max =
    Math.ceil(
      Math.max(
        limit * 1.3,
        ...all.flatMap((w) => [w.candidate.p95_ms || 0, w.stable.p95_ms || 0]),
      ) / 100,
    ) * 100;
  const width = innerWidth <= 640 ? 340 : 600,
    left = 42,
    right = width - 18,
    top = 24,
    bottom = 185;
  const x = (i) => left + ((right - left) * (i + 0.5)) / total,
    y = (v) => bottom - (v / max) * (bottom - top);
  const bars = total < 4;
  let plot = "";
  for (let i = 0; i <= 4; i++) {
    const v = (max * i) / 4;
    plot += `<line class="grid" x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}"/><text x="${left - 10}" y="${y(v) + 4}" text-anchor="end">${Math.round(v)}</text>`;
  }
  plot += `<line class="threshold" x1="${left}" x2="${right}" y1="${y(limit)}" y2="${y(limit)}"/><text x="${right}" y="${y(limit) - 7}" text-anchor="end" style="fill:var(--danger)">${limit} ms limit</text>`;
  if (frame.kind === "window")
    plot += `<rect x="${x(frame.windowIndex) - 22}" y="${top}" width="44" height="${bottom - top}" fill="var(--accent)" opacity=".05"/>`;
  for (const side of ["stable", "candidate"]) {
    if (!bars) {
      let path = "",
        pen = false;
      windows.forEach((w, i) => {
        const v = w[side].p95_ms;
        if (typeof v !== "number") {
          pen = false;
          return;
        }
        path += `${pen ? "L" : "M"}${x(i)} ${y(v)} `;
        pen = true;
      });
      plot += `<path class="${side}" d="${path}"/>`;
    }
    windows.forEach((w, i) => {
      const v = w[side].p95_ms;
      if (typeof v !== "number") return;
      const label = `Window ${i + 1}, ${side}: ${ms(v)}, ${w[side].count} requests`;
      if (bars)
        plot += `<rect x="${x(i) + (side === "candidate" ? 3 : -22)}" y="${y(v)}" width="19" height="${Math.max(1, bottom - y(v))}" fill="${side === "candidate" ? "var(--accent)" : "var(--blue)"}"><title>${esc(label)}</title></rect><text x="${x(i) + (side === "candidate" ? 13 : -13)}" y="${Math.max(top - 8, y(v) - 8)}" text-anchor="middle">${Math.round(v)}</text>`;
      else
        plot += `<circle class="${side === "candidate" ? "point" : "stable-point"}" cx="${x(i)}" cy="${y(v)}" r="4"><title>${esc(label)}</title></circle>`;
    });
  }
  for (let i = 0; i < total; i++) {
    if (total <= 8 || i % Math.ceil(total / 8) === 0 || i === total - 1)
      plot += `<text x="${x(i)}" y="207" text-anchor="middle">W${i + 1}</text>`;
  }
  const w = windows.at(-1);
  return `<div class="chart-heading"><div><h3>Response latency</h3><p>p95 in milliseconds · lower is better</p></div><span class="badge ${w?.verdict === "pass" ? "success" : "warning"}">${esc(w?.verdict === "pass" ? "Within policy" : w?.verdict === "breach" ? "Policy breach" : "Insufficient data")}</span></div>
    <div class="legend"><span><i></i>Candidate</span><span><i class="stable"></i>Stable</span><span><i class="limit"></i>Policy limit</span></div>
    <svg class="chart" viewBox="0 0 ${width} 220" role="img" aria-label="${esc(`${windows.length} measured windows. Latest candidate p95 ${ms(w?.candidate.p95_ms)}, stable ${ms(w?.stable.p95_ms)}. Limit ${limit} milliseconds. Exact values are available in the complete evidence table.`)}">${plot}</svg>
    <div class="window-focus"><strong>W${windows.length} · ${w?.stage}% traffic</strong> &nbsp; Candidate ${ms(w?.candidate.p95_ms)} · Stable ${ms(w?.stable.p95_ms)} · Candidate errors ${pct(w?.candidate.error_rate)}</div>
    <p class="chart-caption">Observation windows in sequence, not elapsed time. ${all.some((w) => w.stable.baseline_count) ? "Stable includes direct comparison probes." : ""} Select a step below to inspect it.</p>`;
}
function chart(r, frame, view) {
  if (view.recovery && frame.kind === "recovery")
    return priceChart(view.recovery, true);
  if (view.windows.length) return latencyChart(r, frame, view.windows);
  if (view.behavior) return priceChart(view.behavior);
  return noChart(
    "Stable stays in service.",
    "The candidate starts with zero routed traffic. First, ReleaseGuard checks the same baskets against both versions.",
  );
}
function review(r, frames, frame, view) {
  const live = ACTIVE.has(r.status),
    idx = replay ? frameIndex : frames.length - 1;
  return `<div class="section-head"><div><h2>The release, explained</h2><p>${replay ? "Recorded evidence · playback does not change traffic." : live ? "Measurements appear as the controller records them." : "Walk through the evidence at your own pace."}</p></div><span class="tag">${replay ? "REPLAY" : live ? "LIVE" : "RUN SUMMARY"}</span></div>
    <section class="review-panel" aria-label="Release walkthrough"><div class="playback-bar"><div class="playback-label">${icon(replay ? "replay" : "check")}<strong>${replay ? `Step ${idx + 1} of ${frames.length}` : live ? "Release in progress" : "Run complete"}</strong><span>${replay ? (playing ? "Playing" : "Paused") : live ? "Replay available when finished" : `${frames.length} recorded steps`}</span></div>
    <div class="playback-controls">${replay ? `<button data-action="prev" data-focus="prev" ${idx === 0 ? "disabled" : ""} aria-label="Previous step">${icon("back")} Back</button><button class="play" data-action="play" data-focus="play">${icon(playing ? "pause" : "play")}${playing ? "Pause" : idx === frames.length - 1 ? "Restart" : "Play"}</button><button data-action="next" data-focus="next" ${idx === frames.length - 1 ? "disabled" : ""} aria-label="Next step">Next ${icon("next")}</button><label class="sr-only" for="speed">Playback pace</label><select id="speed" data-focus="speed"><option value="6000" ${speed === 6000 ? "selected" : ""}>Slow · 6s</option><option value="3500" ${speed === 3500 ? "selected" : ""}>Normal · 3.5s</option><option value="1800" ${speed === 1800 ? "selected" : ""}>Fast · 1.8s</option></select><button data-action="summary" data-focus="summary">Summary</button>` : `<button class="play" data-action="replay" data-focus="start-replay" ${live ? "disabled" : ""}>${icon("replay")} Replay walkthrough</button>`}</div></div>
    <div class="story-layout"><div class="chart-panel">${chart(r, frame, view)}</div><aside class="story-aside"><div class="step-index">${replay ? `${String(idx + 1).padStart(2, "0")} / ${esc(frame.label).toUpperCase()}` : live ? "CURRENT STEP" : "THE OUTCOME"}</div><h3>${esc(frame.title)}</h3><p>${esc(frame.description)}</p>${frame.kind === "window" ? `<p style="margin-top:12px"><strong>${view.windows.at(-1).candidate.count}</strong> candidate requests · ${view.windows.at(-1).stable.count} stable samples</p>` : ""}<div class="route-block"><div class="route-label"><span>Candidate traffic ${replay ? "at this step" : "at decision"}</span><strong>${frame.traffic === null ? "Restoring" : `${frame.traffic}%`}</strong></div>${frame.traffic !== null ? `<div class="route-track" role="img" aria-label="Candidate ${frame.traffic} percent, stable ${100 - frame.traffic} percent"><span style="width:${frame.traffic}%"></span></div>` : ""}<small>${frame.at ? `Recorded ${esc(clock(frame.at))}` : "No separate timestamp recorded"}${replay ? " · replay time is illustrative" : ""}</small></div></aside></div>
    <div class="step-strip" aria-label="Recorded release steps">${frames.map((f, i) => `<button class="step-button ${i === idx ? "current" : i < idx ? "past" : ""}" data-frame="${i}" data-focus="frame-${i}" aria-pressed="${i === idx}" ${live ? "disabled" : ""}><span>${String(i + 1).padStart(2, "0")}</span>${esc(f.label)}</button>`).join("")}</div>
    ${replay ? `<div class="scrub"><label for="scrubber">Step ${idx + 1} / ${frames.length}</label><input id="scrubber" data-focus="scrubber" type="range" min="0" max="${frames.length - 1}" step="1" value="${idx}" aria-label="Walkthrough step" aria-valuetext="${esc(frame.label)}"></div>` : ""}</section>`;
}
function fixtureDetails(b, title, key) {
  if (!b) return "";
  const s = b.failures?.[0] || b.sample;
  return `<h3>${title}</h3><p class="note">${b.passed} / ${b.total} passed · ${b.failed} failed · ${b.skipped || 0} skipped · ${b.rules_checked} rules checked</p>${s ? `<div class="table-scroll"><table><caption class="sr-only">${title}: ${esc(s.name)}</caption><thead><tr><th>Example</th><th>Expected total</th><th>Stable total</th><th>${key === "recovery" ? "Routed" : "Candidate"} total</th></tr></thead><tbody><tr><td>${esc(s.name)}</td><td>${cash(s.expected?.total)}</td><td>${cash(s.stable?.values?.total)}</td><td>${cash(s.candidate?.values?.total)}</td></tr></tbody></table></div>` : ""}${(b.failures || []).map((f, i) => `<details class="raw-failure" data-detail="${key}-${i}"><summary>${esc(f.name)} · ${esc(f.rule)}</summary><pre>${esc(JSON.stringify(f, null, 2))}</pre></details>`).join("")}`;
}
function evidence(r) {
  return `<details class="evidence" data-detail="evidence"><summary>Complete release evidence <span>Measurements, fixtures & event log</span></summary><div class="evidence-content"><p class="note">Full recorded run. This section includes all steps, even during replay.</p>${fixtureDetails(r.behavior, "Behavior gate", "behavior")}
    ${r.windows.length ? `<h3>Observation windows</h3><div class="table-scroll"><table><caption class="sr-only">Exact latency, traffic and error measurements for every window</caption><thead><tr><th>Window</th><th>Traffic</th><th>Candidate p95</th><th>Stable p95</th><th>Requests C / S</th><th>Errors C / S</th><th>Verdict</th></tr></thead><tbody>${r.windows.map((w, i) => `<tr><td><button data-window="${i}" ${ACTIVE.has(r.status) ? "disabled" : ""} aria-label="Inspect window ${i + 1}">W${i + 1}</button></td><td>${w.stage}%</td><td>${ms(w.candidate.p95_ms)}</td><td>${ms(w.stable.p95_ms)}</td><td>${w.candidate.count} / ${w.stable.count}</td><td>${pct(w.candidate.error_rate)} / ${pct(w.stable.error_rate)}</td><td>${esc(w.verdict)}</td></tr>`).join("")}</tbody></table></div><p class="note">C / S = candidate / stable. Stable includes ${r.windows.reduce((n, w) => n + (w.stable.baseline_count || 0), 0)} direct probes, excluded from routed exposure. Missing or unattributed planned requests: ${r.windows.reduce((n, w) => n + (w.unknown_count || 0), 0)}. Candidate limits: ${ms(r.policy?.candidate_p95_limit_ms)} p95 and ${pct(r.policy?.candidate_error_rate_limit)} errors.</p>` : ""}
    ${fixtureDetails(r.recovery, "Recovery verification", "recovery")}<h3>Decision timeline</h3><ol class="event-log">${r.events.map((e) => `<li><time datetime="${esc(e.at)}">${esc(clock(e.at))}</time><span>${esc(e.message)}</span></li>`).join("")}</ol><h3>Version identity</h3>${[
      ["Stable before run", r.stable_version],
      ["Candidate", r.candidate_version],
      ["Commit SHA", r.commit_sha],
      ["Stable artifact", r.stable_digest],
      ["Candidate artifact", r.candidate_digest],
    ]
      .filter((x) => x[1])
      .map(
        ([k, v]) =>
          `<div class="identity"><span>${k}</span><code>${esc(v)}</code></div>`,
      )
      .join("")}</div></details>`;
}
function render() {
  if (!model) return;
  const r = selected();
  renderHistory(r);
  const key = JSON.stringify([
    r,
    model.stable,
    replay,
    frameIndex,
    playing,
    speed,
  ]);
  if (snapshot !== key) {
    const open = [...$("screen").querySelectorAll("details[open]")].map(
      (d) => d.dataset.detail,
    );
    const focus = document.activeElement?.dataset?.focus;
    const focusedDetail =
      document.activeElement?.closest("details")?.dataset.detail;
    const stripScroll =
      $("screen").querySelector(".step-strip")?.scrollLeft || 0;
    if (!r)
      $("screen").innerHTML =
        `<section class="empty-state"><div class="eyebrow">YOUR FIRST RELEASE</div><h2>Make the next release a confident one.</h2><p>Choose a candidate with New release. Watch the checks run, then use the walkthrough to explain every decision.</p><div class="empty-steps">${Object.values(
          SCENARIOS,
        )
          .map(
            (s, i) =>
              `<div><span>0${i + 1}</span><h3>${s.action}</h3><p>${s.description}</p></div>`,
          )
          .join("")}</div></section>`;
    else {
      const frames = buildFrames(r);
      frameIndex = Math.min(frameIndex, frames.length - 1);
      const frame = frames[replay ? frameIndex : frames.length - 1],
        view = viewAtFrame(r, frame),
        live = ACTIVE.has(r.status);
      $("screen").innerHTML =
        `<section class="release-head"><div><h2>${esc(SCENARIOS[r.scenario]?.name || r.scenario)}</h2><div class="release-meta"><code>${esc(r.id)}</code><span>${esc(stamp(r.created_at))}</span>${badge(r.status)}</div></div><div class="head-actions"><button class="secondary" data-action="replay" data-focus="replay" ${live ? "disabled" : ""}>${icon("replay")} Replay walkthrough</button><button class="secondary" data-action="export" data-focus="export">${icon("download")} Export evidence</button></div></section>
      <section class="result-banner ${tone(r.status)}"><div class="result-icon">${icon(["blocked", "needs_attention"].includes(r.status) ? "stop" : live ? "arrow" : "check")}</div><div><h3>${live ? "Live status" : replay ? "Recorded outcome" : "Release outcome"} · ${esc(STATUS[r.status])}</h3><p>${esc(summaryCopy(r))}</p></div>${r.status === "needs_attention" && r.id === model.releases[0]?.id && model.mode === "local" ? '<button data-action="retry" data-focus="retry">Retry recovery</button>' : ""}</section>
      ${stats(r, frame, view)}${review(r, frames, frame, view)}${evidence(r)}<div class="stable-foot"><span class="dot"></span> Current stable <code>${esc(model.stable?.version)}</code><span>· ${replay ? "Replay is a recorded view." : "History is saved automatically."}</span></div>`;
      announce(
        replay
          ? `Replay step ${frameIndex + 1} of ${frames.length}. ${frame.title}`
          : `${SCENARIOS[r.scenario]?.name}. ${STATUS[r.status]}`,
      );
    }
    for (const d of $("screen").querySelectorAll("details"))
      if (open.includes(d.dataset.detail)) d.open = true;
    const focusTarget = focus
      ? [...$("screen").querySelectorAll("[data-focus]")].find(
          (e) => e.dataset.focus === focus,
        )
      : null;
    if (focusTarget && !focusTarget.disabled)
      focusTarget.focus({ preventScroll: true });
    else if (focusTarget?.disabled)
      $("screen")
        .querySelector('[data-focus="play"]')
        ?.focus({ preventScroll: true });
    else if (focusedDetail)
      [...$("screen").querySelectorAll("details")]
        .find((d) => d.dataset.detail === focusedDetail)
        ?.querySelector("summary")
        .focus({ preventScroll: true });
    const strip = $("screen").querySelector(".step-strip");
    if (strip) strip.scrollLeft = stripScroll;
    snapshot = key;
  }
  $("environment").textContent = connected
    ? model.mode === "compose"
      ? "Cloud · Compose"
      : "Local controller connected"
    : "Controller disconnected";
  $("linkLight").classList.toggle("online", connected);
  $("screen").setAttribute("aria-busy", "false");
  $("newRelease").hidden = model.mode !== "local";
  $("newRelease").disabled =
    !connected ||
    busy ||
    !!model.active_id ||
    model.releases[0]?.status === "needs_attention";
  document
    .querySelectorAll('[data-scenario],[data-action="retry"]')
    .forEach((b) => (b.disabled = !connected || busy || !!model.active_id));
}
async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const data = await response.json();
  if (!response.ok)
    throw Error(data.error || `Controller returned ${response.status}`);
  return data;
}
async function refresh() {
  clearTimeout(pollTimer);
  const current = ++serial;
  try {
    const data = await request("/api/state");
    if (current !== serial) return;
    model = data;
    connected = true;
    if ($("notice").textContent.startsWith("Controller connection")) report("");
    render();
  } catch (e) {
    if (current !== serial) return;
    connected = false;
    report(`Controller connection lost. Retrying… ${e.message}`);
    $("environment").textContent = "Controller disconnected";
    $("linkLight").classList.remove("online");
    render();
  } finally {
    if (current === serial)
      pollTimer = setTimeout(refresh, model?.active_id ? 450 : 1800);
  }
}
async function launch(scenario, id) {
  if (busy || !connected) return;
  if (scenario && !pendingIds.has(scenario))
    pendingIds.set(scenario, crypto.randomUUID());
  busy = true;
  $("launchError").textContent = "";
  report("");
  render();
  try {
    const r = await request(
      id ? `/api/releases/${encodeURIComponent(id)}/recover` : "/api/releases",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-ReleaseGuard-UI": "1",
        },
        body: JSON.stringify(
          id ? {} : { scenario, request_id: pendingIds.get(scenario) },
        ),
      },
    );
    pendingIds.delete(scenario);
    resetPlayback();
    selectedId = r.id;
    $("launcher").close();
    await refresh();
  } catch (e) {
    if ($("launcher").open) $("launchError").textContent = e.message;
    else report(e.message);
  } finally {
    busy = false;
    render();
  }
}
function stepTo(index) {
  const r = selected();
  if (!r || ACTIVE.has(r.status)) return;
  pause();
  replay = true;
  frameIndex = Math.max(0, Math.min(index, buildFrames(r).length - 1));
  render();
}
function schedule() {
  clearTimeout(playTimer);
  if (!playing) return;
  playTimer = setTimeout(() => {
    const frames = buildFrames(selected());
    if (frameIndex < frames.length - 1) frameIndex++;
    if (frameIndex === frames.length - 1) pause();
    render();
    schedule();
  }, speed);
}
function exportEvidence() {
  const r = selected();
  if (!r) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = `releaseguard-${r.id}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$("scenarios").innerHTML = Object.entries(SCENARIOS)
  .map(
    ([id, s], i) =>
      `<button class="scenario" data-scenario="${id}"><span class="scenario-number">0${i + 1}</span><span><b>${s.action}</b><small>${s.name}</small><em>${s.description}</em></span>${icon("arrow")}</button>`,
  )
  .join("");
$("newRelease").addEventListener("click", () => {
  pause();
  render();
  $("launchError").textContent = "";
  $("launcher").showModal();
});
$("historyFilter").addEventListener("change", (e) => {
  historyFilter = e.target.value;
  renderHistory(selected());
});
document.addEventListener("change", (e) => {
  if (e.target.id === "speed") {
    speed = Number(e.target.value);
    schedule();
  }
  if (e.target.id === "scrubber") stepTo(Number(e.target.value));
});
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled) return;
  if (b.dataset.scenario) return launch(b.dataset.scenario);
  if (b.dataset.release) {
    resetPlayback();
    selectedId = b.dataset.release;
    return render();
  }
  if (b.dataset.frame !== undefined) return stepTo(Number(b.dataset.frame));
  if (b.dataset.window !== undefined) {
    const index = buildFrames(selected()).findIndex(
      (f) => f.windowIndex === Number(b.dataset.window),
    );
    stepTo(index);
    $("screen")
      .querySelector(".review-panel")
      .scrollIntoView({ block: "start" });
    return;
  }
  switch (b.dataset.action) {
    case "replay":
      stepTo(0);
      $("screen")
        .querySelector(".section-head")
        ?.scrollIntoView({ block: "start" });
      break;
    case "prev":
      stepTo(frameIndex - 1);
      break;
    case "next":
      stepTo(frameIndex + 1);
      break;
    case "summary":
      resetPlayback();
      render();
      break;
    case "play":
      if (playing) pause();
      else {
        if (frameIndex === buildFrames(selected()).length - 1) frameIndex = 0;
        playing = true;
        schedule();
      }
      render();
      break;
    case "export":
      exportEvidence();
      break;
    case "retry":
      launch(null, selected()?.id);
      break;
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden && playing) {
    pause();
    render();
  }
});
matchMedia("(prefers-reduced-motion: reduce)").addEventListener(
  "change",
  (e) => {
    if (e.matches) {
      pause();
      render();
    }
  },
);
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    snapshot = "";
    render();
  }, 150);
});
refresh();
