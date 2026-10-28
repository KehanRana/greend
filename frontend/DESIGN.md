# GreenRoute dashboard direction

Style: Framed sheet · seed 1044720148. No redraws. Climate-derived green-grey frame replaces the neutral frame tint; the work area remains white. IBM Plex Sans is self-hosted from its npm package, with tabular figures. Monospace is confined to request/job IDs.

The screen asks: where is flexible inference going, and what estimated difference does that make against the default region?

Product-specific element: three simulated inference regions become a geographic routing comparison, with live source values beside the map. The comparison curve represents default versus selected endpoints; it does not assert a measured network path. Pending jobs show their own deadlines and thresholds beside the routing evidence.

Data inventory:

- Three registry endpoints: Northern Virginia, Frankfurt and Sydney, with explicit demo zone IDs.
- Carbon signal in gCO2e/kWh, source and observation/retrieval times, validity and endpoint health.
- Completed execution and compared-token counts derived from retained attempts.
- Available/excluded comparisons and signed estimated grams derived from persisted estimate records.
- Immutable coefficient, model/version and baseline captured at dispatch.
- Owner-scoped jobs, pending states, deadlines and cancellation eligibility.
- Shared metadata-only request decisions; results are not displayed in the overview.
- Persistent scheduled/high/clean scenarios; scenario controls require a separate operator key.
- Synthetic batch payloads only; no customer content, customers or impact statistics are invented.

Frame arithmetic at 1440px: 228px sidebar + 8px right inset + 1204px sheet. Inside the sheet, 24px padding and two 1px borders leave 1154px. Two main columns share the remaining width at 2:1 with a 24px gutter. At tablet width navigation folds; at phone width all panels stack and tables become two-line request lists.

Map data: Natural Earth through world-atlas 2.0.2, projected with d3-geo and converted to a local SVG path by scripts/map.mjs. Geographic data is public domain; world-atlas copyright/license is retained in public/map-license.txt. The map does not draw or claim grid-zone boundaries.

Interaction: connect with a client key held only in memory, submit synthetic batches, cancel pending owned work, inspect decision/source evidence, and apply operator-authorized reversible grid scenarios. Polling preserves last-good values and labels refresh failures. Dialogs use native focus containment and Escape handling. Reduced motion removes control feedback transitions.

No time-series chart is added because historical trend analysis is not an MVP requirement. Regional values, the map and paired estimate figures carry the evidence without inventing history.

## Final visual review

1. The dominant map and comparison panel answer location and estimated impact together. The signed comparison is the largest figure; counts remain secondary.
2. Sheet edges and 24px gutters line up at desktop width. Tablet/phone panels stack without horizontal page overflow. All requested block headers and initial rows appear on the desktop first screen.
3. Colour is limited to the mark, regional intensity/status data and signed differences. The green-grey frame gives the product its climate context without a saturated sidebar.
4. Figures come from the API. The final synthetic workspace shows 11.424000 g baseline minus 2.358720 g selected = 9.065280 g difference; 510 compared executions equal 510 completed executions with zero exclusions in this fixture run. Recent rows are a capped history, not an independently typed total.
5. Missing estimates use a dash, loaded zeros remain zeros, and the per-token ratio is unavailable for zero compared tokens. Job badges count the owner's pending states, not all workspace jobs.
6. Map dots and legend use the same intensity classes. Natural Earth projection places the endpoints geographically; labels and region magnitudes remain visible outside the map. No false history chart was added.
7. Mobile navigation folds into a menu; obscured main content is inert while open, Escape dismisses it, and dialogs restore focus. The full queue/evidence views avoid requiring users to navigate the long overview for a specific task.

Final production-build inspection: 0 failures and 0 warnings at 1440, 768 and 390 pixels; all screenshots reviewed. WCAG 2 A/AA and 2.1 A/AA axe tags: zero detected violations at those widths (25 passing checks each). This is an automated/manual spot check, not a full accessibility certification.

Static source lint has three defended warnings: zero-padding formats the deadline clock; shadows occur only on the floating menu/dialog; repeated em dashes denote missing data rather than prose punctuation.

Remaining visual limitation: the phone overview is long because it retains all required information; the dedicated queue/evidence views are the compact work surfaces. Sample data is clearly simulated. Replace the mock inference/grid adapters and calibrate the energy model only after the live integration gates are met.
