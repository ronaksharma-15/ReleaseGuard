# ReleaseGuard dashboard

Audience: developers and hackathon judges inspecting a stateless checkout rollout.

The UI/UX Pro Max analytics-dashboard search informed the use of neutral surfaces, restrained accents, compact measurements and progressive disclosure. Its unrelated landing-page recommendations are not applicable. This dashboard uses a warm light canvas, charcoal navigation, rust candidate data and blue stable data; no external fonts or chart packages are required.

## Visual rules

- Use semantic tokens in `web/style.css` for surfaces, text, borders, status and chart series.
- Use system sans-serif typography, with monospace reserved for identities, timestamps and chart labels.
- Keep borders subtle, corners modest and status color localized. Avoid decorative glow, gradients and animated ornaments.
- Put the outcome first, then measurements and the walkthrough. Keep raw fixtures, identities and logs in a disclosure.
- Below 900px, history becomes a horizontal strip; below 640px, the chart and explanation stack.

## Evidence and interaction

- Replay is explicitly recorded evidence, starts paused and never changes controller execution.
- Reveal measurements only through the selected frame. Label the final outcome separately.
- Compare prices with horizontal bars. Use grouped bars for one to three latency windows and lines for four or more.
- Label window order, units and thresholds. Use dashed stable lines and textual verdicts as alternatives to color.
- Do not interpolate missing samples, simulate timestamps or count direct comparison probes as routed traffic.
- Retain an exact-values table and JSON export for every recorded run.
- Support keyboard navigation, visible focus, a labeled slider, play/pause and manual stepping. Pause playback when the page becomes hidden. Respect reduced-motion preferences.

## Verification

Run `node --test tests/test_story.mjs` for evidence projection. Check all three scenarios, replay boundaries, history filters, export and the 375px/1440px layouts in a browser. Backend checks remain `python -m unittest discover -s tests -v`.
