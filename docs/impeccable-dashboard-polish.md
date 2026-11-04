# Fleet Dispatch interface refinement — Impeccable pass

This change is a **visual/UX refinement** of the existing Singapore fleet
operations dashboard. It preserves the existing teal visual identity, live
telemetry, map interactions, approved-only dispatch flow, warnings, API calls,
and light/dark preference. It does **not** change any AI decisions, backend
services, fleet state, rental state, route calculations, or Kubernetes settings.

## Interface improvements

- A more legible Fleet Dispatch header with a route-and-waypoint symbol,
  concise Singapore context, clear SIMULATION label, and consistent theme controls.
- A compact, dynamically populated **Operational snapshot** above the main
  workspace, showing reporting EVs, available EVs, open dispatches, and critical
  demand zones. All numbers come from existing endpoints and are **display only**.
  Data older than ten seconds, missing, errored, or partial is rendered as
  an em dash with an explanatory label, **not fabricated as zero**.
- An expanded, quieter map-first composition and more clearly differentiated
  zone/dispatch inspector panel.
- Consistent selection states, semantic fleet status badges, controlled panel
  spacing, stronger table legibility, and restrained operational tab navigation.
- Smaller-screen layout that stacks the map and inspector, reflows the summary
  into two columns, and keeps important actions usable on touch devices.
- Explicit keyboard focus, reduced-motion support, and coordinated light/dark
  surface and contrast tokens.

## Scope of this branch

Only `dashboard/src` styling/components and focused dashboard unit tests
change. `polish.css` loads **after** existing `styles.css` so the
incumbent styles are preserved and the refinement can be reviewed or
reverted without wholesale rewrites. No production claims or decorative
placeholder data have been introduced.

## Verify

```powershell
git switch design/impeccable-fleet-dashboard-polish
npm.cmd --prefix dashboard run build
npm.cmd --prefix dashboard test -- --run
```

Manually review light/dark views at approximately 1440, 1024, 768, and
390 pixels, with long and missing labels, a disconnected Fleet service,
stale telemetry, an active relocation, a critical hotspot, an empty fleet,
and a pending approval. Verify keyboard navigation through theme controls,
zone selection, tabs, map buttons, and approval confirmation. Ensure
`SIMULATION` remains prominent and screen readers announce current
summary states correctly.

**Verification gap:** The dashboard Vitest runner previously timed out
on Windows **before any tests executed**. A successful production build
does not constitute passing Vitest. The GitHub-connected editing environment
cannot run the user's local dashboard or capture its actual browser render.
A local device/browser review is required before merging.

This is a refinement, not a replacement design system. No existing
`PRODUCT.md` or `DESIGN.md` was found in the repository.
