# Lovable prompt pack - MEIO planning app

This file holds the prompts that build the web app for this model in Lovable. It is kept in the
repository because the prompts depend on the API contract (`api.py`, `summary.json`); change
both together.

## How to use it

1. **Project Knowledge.** Paste **Part 1 (Master brief)** into the Lovable project's knowledge /
   custom-instructions field, so it applies to every later message. If your plan has no such
   field, send it as the first message.
2. **Build in phases.** Send the **Part 2** phase prompts one at a time. Check each phase against
   its acceptance list before sending the next. One large "build everything" message gives
   shallow, inconsistent screens.
3. **Secrets first.** Before Phase 1, add the Supabase secrets `MEIO_API_URL` and `MEIO_API_KEY`.
4. **Data availability.** Fields marked **[v2]** are not yet in the API output. The app must show
   a "requires API v2" placeholder for them, never invented numbers. They become available with
   the backend extension described in Part 3.

---

## Part 1 - Master brief (paste into Project Knowledge)

### 1. Product
Build **"MEIO Planner"**, a professional web app for weekly multi-echelon inventory planning of
perishable products. It is the front end of an existing Python simulation-optimisation engine
(REST API). The engine plans a two-echelon supply chain:

```
Suppliers (one per raw material) -> Raw-material warehouse (RMW) -> Production (PF, holds no stock) -> Distribution centre (DC) -> Sales channels
```

Every week the planner re-runs the model and gets:
- **Decisions to commit now (week 1):** FG order from production (Q), production release (P),
  RM transport RMW -> production (T), RM supplier orders (O).
- **The plan for weeks 2..H:** an optimised age-aware (s,S) policy per item and week, with the
  expected orders, stock, service and cost it produces. These are not commitments; they are
  re-optimised next week.
- **Uncertainty:** every result comes from thousands of simulated futures ("test seeds"). The app
  must always show the spread of outcomes, not only averages.

Users are supply chain planners and managers. They want to see the answer first (what to do
now, is service safe, what does it cost), then drill down into why.

### 2. Non-negotiable principles
1. **Decision first.** Every screen answers a planner's question. Start with the conclusion
   (KPI, recommendation, status), then the chart, then the detail table.
2. **Overview -> drill-down.** Portfolio view by default. A global item filter narrows every tab
   to one or more finished goods (FG); raw materials (RM) follow automatically via the BOM.
   Clicking an item, week or cell anywhere opens its detail.
3. **Weekly buckets.** The time axis is always "week 1 ... week H". Week 1 = current review week
   (label it "W1 - commit now"). Weeks before the evaluation window are greyed with the label
   "warm-up (not evaluated)".
4. **Uncertainty always visible.** Every result chart over weeks shows the median line, a
   P25-P75 band and a P5-P95 band from the test seeds. Every cost or KPI number shows its
   spread (P5-P95) or standard error in a tooltip.
5. **Never fabricate data.** Show only numbers from the API or computed from the input JSON with
   the formulas in this brief. If a field is missing, show a neutral placeholder ("Requires API
   v2" or "Run the model to see results"). No demo or mock numbers, except in a clearly labelled
   "Example data" state loaded from the example input.
6. **Edit anywhere, run once.** Parameters can be edited in the tab where they are shown (e.g.
   fill-rate targets in Demand, lead times in Lead times). All edits change the **scenario draft**
   (one input JSON). A top-bar badge "Inputs changed - results out of date" appears and results
   are dimmed until the scenario is run again.
7. **Explain on hover.** Every KPI, column header and chart has an info tooltip with a
   one-sentence definition in planner language (definitions in section 8).

### 3. Architecture (Supabase + edge functions)
- The browser never calls the engine directly and never sees the API key.
- Edge functions (use the secrets `MEIO_API_URL`, `MEIO_API_KEY`; header `X-API-Key`):
  - `meio-run-start`: `POST {MEIO_API_URL}/runs?preset=<quick|standard>`, body = scenario input
    JSON **[v2: or `{"input": {...}, "settings": {...}}`]**. 422 -> return the `detail` text to the
    UI as a validation error.
  - `meio-run-status`: `GET {MEIO_API_URL}/runs/{run_id}` -> `{status, preset, summary, error}`,
    status is `queued | running | completed | failed`. When `completed`, store the summary in
    Supabase in the same call.
  - `meio-run-xlsx`: proxies `GET {MEIO_API_URL}/runs/{run_id}/results.xlsx` for download.
- Poll the status every 20 s while a run is active (a run takes 3-20 minutes depending on the
  server plan). Show a run progress card: queued -> running (elapsed time) -> completed/failed.
  One run at a time: the API queues further runs.
- The API server forgets results when it restarts. Supabase is the system of record.
- Tables:
  - `scenarios` (id, name, description, parent_scenario_id, input_json jsonb, settings_json jsonb,
    created_at, updated_at, is_baseline bool)
  - `runs` (id, scenario_id, api_run_id, preset, status, started_at, finished_at, error,
    input_snapshot jsonb, settings_snapshot jsonb)
  - `run_results` (run_id, summary jsonb)
  - `comparisons` (id, name, run_ids uuid[])
- Store the exact input snapshot with every run, so a result can always be traced back to its
  data.
- Libraries: shadcn/ui, Tailwind, Recharts (range areas for bands), @xyflow/react (network map),
  TanStack Table (dense tables with sorting, filtering, sticky headers), PapaParse (CSV),
  date-fns (week labels).

### 4. Data contract

#### 4.1 Input JSON (scenario draft), exactly this format
```
horizon (weeks H), production_capacity (units/week), capacity_overrides {"week": capacity}
products[]: name, shelf_life (weeks), bom {material: units per FG}, batch_size, moq,
  holding_cost (per unit-week), waste_cost (per unit), fixed_cost_per_release,
  production_tiers [{lower, upper, unit_cost}], transport_tiers [{lower, upper, unit_cost}]
  (all-units discounts: the rate of the band applies to all units),
  lead_time_dist {"weeks": probability}, closed_production_weeks [week],
  channels [{name, min_remaining_life, target_fill_rate}]
materials[]: name, shelf_life, min_life_at_shipment, batch_size, moq, unit_cost,
  fixed_order_cost, holding_cost, waste_cost, transport_cost, lead_time_dist {"weeks": probability},
  supplier_capacity (units/order or null), closed_order_weeks [week]
demand_forecast {product: {channel: {"mean": [week1..], "sd": [week1..]}}}
initial_state: dc_stock {product: {"age": units}}, rm_stock {material: {"age": units}},
  dc_pipeline {product: [[release_week <= 0, units]]}, rm_pipeline {material: [[order_week <= 0, units]]}
```
Load `examples/example_input.json` from the engine repository as the "Example data" scenario.

Derived quantities (compute in the app, show in Master data):
- Max accepted age for channel c = `shelf_life - min_remaining_life` ("sellable shelf life").
- Max shippable RM age = `shelf_life - min_life_at_shipment`.
- L_max = largest lead time with probability > 0 over all FG. **Evaluation window** = weeks
  `L_max + 1 ... H` (earlier weeks are already determined by orders in the pipeline).
- Demand distribution per product/channel/week: negative binomial with the given mean and sd
  (`n = mean^2 / (sd^2 - mean)`, `p = n / (n + mean)`); if `sd^2 <= mean` Poisson(mean); if
  `mean = 0` zero. Use it for forecast fan charts (P5/P25/P50/P75/P95) before a run.

Client-side validation (the engine's rules plus the fill-rate range; block "Run" and show the list):
lead-time probabilities sum to 1 per item; MOQ is a multiple of the batch size; required
remaining life < shelf life; every BOM material exists; cost tiers are contiguous
(next.lower = previous.upper + 1); initial stock ages within 1..max sellable/shippable age;
forecast covers at least H + 1 weeks; target fill rates within 0..1.

#### 4.2 Output `summary` (from `GET /runs/{id}`)
- `run`: timestamp, policy, preset, seeds, runtime, `mean_cost_over_horizon_test_seeds`,
  `mean_cost_start_schedule_search_seeds`, `mean_cost_final_schedule_search_seeds`,
  `test_cells_passing` ("84 / 84"), order caps and RMW minimums.
- `decisions_to_commit[]`: decision, item, location, position, expected_waste,
  effective_position, reorder_level_s, order_up_to_level_S, rule_quantity (what the (s,S) rule
  alone would order), committed_quantity (after lookahead), note.
- `service.by_channel[]`: product, channel, target_fill_rate, cells_evaluated, cells_passing,
  worst_week, worst_week_mean_fill. `service.failed_cells[]`, `service.all_cells_pass`.
- `costs[]`: cost_component, mean, share_of_total, sd, p5, p95, se_of_mean (per horizon).
- `kpis[]`: kpi (text), value columns (pooled fill rates, waste shares, stock levels, orders).
- `policy`: `weeks`, `dc.{FG}.{s[], S[], order_cap}`, `rmw.{RM}.{s[], S[], minimum_physical_stock}`
  (array index 0 = week 1; empty levels = no ordering possible that week).
- `weekly_means_test_seeds[]`: one row per week, columns `dc_<fg>_<metric>`,
  `<fg>_<channel>_<demand|sales|lost_sales>`, `rmw_<rm>_<metric>`, `cost_<component>` (lower-case
  item names).
- **[v2]** `meta`: products, materials, channels per product, horizon,
  `evaluation_weeks {first, last}`, `commit_week: 1`.
- **[v2]** `weekly_bands[]`: long format `{location: DC|RMW|channel|cost, item, channel, metric,
  week, mean, p5, p25, p50, p75, p95}` for every weekly metric, plus DC
  `avg_remaining_life_end`. Use it for all bands.
- **[v2]** `service.cells[]`: `{product, channel, week, target_fill_rate, mean_fill, se, pass}`.
- **[v2]** `baseline`: the heuristic start schedule, evaluated on the same test seeds:
  `{policy (same shape as policy), mean_cost_over_horizon_test_seeds, costs[], service_by_channel[], kpis[]}`.
- **[v2]** `settings`: the calculation settings used.

### 5. Layout and navigation
- **Desktop first** (1280-1920 px), usable at 1024 px. Dense but calm.
- **Top bar (always visible):** app name - **scenario selector** (dropdown with search; shows
  scenario name + last run time + status dot) - **global item filter** (multi-select: "All
  finished goods" default, or one/several FG) - week range slider - run status pill - primary
  button "Run scenario" (preset picker in a split button) - download results.xlsx.
- **Left sidebar (collapsible to icons), grouped:**
  - *Scenarios*: Scenario comparison (top level: works across scenarios)
  - *Plan*: Overview, Decisions
  - *Analyse*: Inventory dynamics, Demand forecast, Lead times, BOM navigator, Supply
    restrictions, Baseline vs optimised
  - *Configure*: Data input, Calculation settings
  - *Understand*: Explainability
- **Page pattern (every tab, top to bottom):** title + one-line purpose -> KPI strip (4-6 tiles)
  -> main visual -> secondary visuals in a 2-column grid -> detail table (collapsible) -> "Edit
  parameters" side sheet (right drawer) for the parameters this tab owns.
- **Drill-down:** clicking an item name opens an **item side panel** (right drawer, 480 px) with
  the item's master data, network position, lead-time distribution, key charts and a link to the
  full tab filtered to it. Breadcrumbs: `Portfolio > FG1 > Retail > W14`.
- URL holds state (scenario, filter, tab, selected item, week range) so views can be shared.

### 6. Visual design system
Slim, technical, quiet. Numbers are the heroes; chrome is minimal.
(Placeholder palette: replace the hex values with the reference image's colours.)
- **Typography:** Inter (UI), JetBrains Mono or tabular-nums Inter for all numbers. Sizes 12/13
  (tables), 14 (body), 16/20/24 (headings). Right-align numbers, thousands separators.
- **Colour tokens (light):** background #F7F8FA, surface #FFFFFF, border #E4E7EC,
  text #101828, muted #667085, sidebar #0B1220 with #C8D0DC text, accent #2563EB.
  **Dark mode** with the same roles (background #0B1220, surface #111A2E, border #22304A,
  text #E6EAF2).
- **Status:** pass/ok #12B76A, watch #F79009, fail/risk #F04438; always paired with an icon or
  text, never colour alone.
- **Series colours (colour-blind safe), fixed per entity across the whole app:** channels and
  items in order #2563EB, #0EA5E9, #7C3AED, #D97706, #059669, #DB2777. Locations: Supplier
  slate, RMW amber, PF violet, DC blue, Channels teal.
- **Bands:** P5-P95 fill at 12 % opacity, P25-P75 at 24 %, median solid 2 px, mean dashed 1 px,
  targets/levels as thin reference lines (s dotted, S dashed, target fill rate red dotted).
  Legend chip "Median · 50 % band · 90 % band". Tooltip lists all five values.
- **Components:** 8 px grid, radius 8, 1 px borders instead of shadows, KPI tiles with value,
  unit, delta vs baseline or vs previous run, and a sparkline. Tables: 32 px rows, sticky
  header, zebra off, hover highlight, inline status chips. Heatmaps: sequential single-hue scale
  with a diverging variant for "distance to target".
- Loading skeletons, empty states with the next action ("Run the scenario to see results"),
  toast for run finished/failed.

### 7. Tabs

**7.1 Scenario comparison (top level).** Purpose: see what changes when data or parameters change.
- Save any completed run as a named snapshot ("Save to comparison").
- Duplicate scenario -> edit -> run -> compare. Scenario tree: children show their parent.
- Compare 2-4 runs side by side: total cost with P5-P95, cost by category (grouped bars),
  pooled fill rate per channel vs target, cells passing, average DC/RMW stock, waste share,
  week-1 decisions. Delta column coloured by direction; for costs, a delta counts as real only
  if |delta| > 2 x sqrt(se1^2 + se2^2) (else show "within noise").
- "What changed" list: a diff of the two input snapshots (field, old, new), grouped by tab.
- Overlay chart: one weekly metric (chosen by the user) for all compared runs, with bands.

**7.2 Overview (Plan).** Purpose: is the plan healthy, what does it cost, where is the risk?
- KPI strip: expected total cost over the horizon (P5-P95), service cells passing
  ("84 / 84"), worst channel fill vs target, FG waste share, average DC stock, average remaining
  shelf life at the DC **[v2]**.
- **Supply chain map** (@xyflow/react, left to right): supplier nodes (one per RM) -> RMW -> PF ->
  DC -> channel nodes. It follows the item filter: selecting an FG highlights its RMs, suppliers
  and channels and dims the rest. Node badges: stock (median), waste risk, service status.
  Edge labels: median lead time and range (e.g. "6 wk (5-8)"), BOM quantity on RM -> PF edges.
  Click a node -> its detail drawer.
- Cost by category: horizontal bars with P5-P95 whiskers (from `costs`), share of total.
- Fill rate per channel: bullet chart pooled fill vs target; click -> service heatmap.
- Service heatmap **[v2 cells]**: rows = product x channel, columns = weeks, colour = mean fill
  minus target; failing cells outlined red; warm-up weeks greyed.
- Inventory dynamics per location: DC on-hand and RMW on-hand over weeks with bands.
- **Run-offs at risk:** weekly expected waste (units expected to expire before a new order can
  arrive) and realised waste, with bands; table of items with the highest at-risk units.
- Decisions teaser: the week-1 decisions card (links to Decisions).

**7.3 Decisions (Plan).** Purpose: what to do now, and what is planned next.
- **Commit now (W1)** card list, one card per decision: item, location, committed quantity (big
  number), rule quantity vs committed (if different: "Lookahead adjusted: -40 units vs the (s,S)
  rule"; show `note` as context, e.g. the order cap), position -> expected waste -> effective position vs s and S as a
  small bar. Buttons "Mark as executed" (stores status and user in Supabase) and "Export".
  - Order FG from production (Q), Release to production (P, may be cut by RM/capacity/MOQ),
    Transport RM to production (T), Order RM from supplier (O).
- **Allocation rule** (read-only explanation card): FIFO (oldest stock first); within an age, the
  channel with the strictest freshness requirement first, ties by higher target fill rate;
  unmet demand is lost. Show expected sales per channel for W1 with bands.
- **Planned, not committed (W2..H):** table by week x item: s, S, expected orders (median,
  P5-P95), expected releases, transports, supplier orders. Label: "Indicative - re-optimised
  every week". Toggle "only weeks with ordering possible".
- Download: CSV of W1 decisions; full results.xlsx.

**7.4 Inventory dynamics (Analyse).** Purpose: how stock, positions and service evolve.
- Item selector (single item focus, FG or RM) + location toggle DC / RMW.
- Main chart: on-hand end of week with bands, overlaid with the policy's s and S (and order cap
  / RMW minimum as reference lines).
- Position chart: inventory position, expected waste, effective position (the rule compares the
  effective position with s).
- Flow chart (stacked bars, medians): receipts, sales, waste, lost sales per week.
- Fill rate per channel per week with bands and target line; service heatmap.
- Cut releases: share of seeds where releases were cut by RM, capacity or the order cap.
- Editable here: s/S are optimised (read-only, with a note), order cap and RMW minimum shown.

**7.5 Demand forecast (Analyse).** Purpose: what demand the plan protects against.
- Multi-select products and channels. Fan chart per product/channel: median + P25-P75 + P5-P95
  from the negative-binomial formula (before a run) and, after a run **[v2]**, the simulated
  demand bands. Aggregated view: total per product (sum of channels).
- Table: week x channel with mean, sd, coefficient of variation; heat-coloured CV.
- Editable here: mean and sd per week (grid editor with fill-down, scale by %, paste from Excel),
  target fill rate and minimum remaining life per channel. Upload CSV.

**7.6 Lead times (Analyse).** Purpose: how long and how uncertain each link is.
- Per FG: path view Supplier -> RMW -> PF -> DC with the lead-time distribution of every link as
  a small bar chart (probability per week), median and range. The critical path (longest RM
  supplier lead time + FG lead time) is highlighted.
- Cumulative lead time distribution per FG (supplier + production), computed by convolution.
- Explain: lead times are random per order and order-preserving (a later order never arrives
  before an earlier one).
- Editable here: lead-time distributions (week/probability rows, must sum to 1, live chart).

**7.7 BOM navigator (Analyse).** Purpose: trace raw materials to finished goods.
- Two-way view: FG -> RMs (quantity per FG) and RM -> FGs (where used). Sankey or tree.
- Per RM: shared by how many FG, RM demand implied by the FG forecast (BOM x FG demand), shelf
  life vs supplier lead time (risk flag if shelf life is short relative to lead time).
- Editable here: BOM quantities; add/remove materials.

**7.8 Supply restrictions (Analyse).** Purpose: what the supply side can deliver.
- Production capacity per week (line) with overrides and closed production weeks marked;
  expected production load (released P, median and P95) against capacity **[v2 bands]**.
- Supplier capacity per order and closed order weeks per RM (calendar strip per item).
- Batch sizes and MOQs per FG and RM (table), with the effect: the smallest order possible.
- Editable here: capacity, capacity overrides by week, closed weeks, supplier capacity.

**7.9 Baseline vs optimised (Analyse).** Purpose: what the optimisation adds over a simple rule.
- Baseline = the heuristic (s,S) schedule the search starts from: s = quantile of demand over
  the lead time (the quantile equals the highest target fill rate of the product), S = the same
  plus one extra week of cover, rounded to batches; for RM the same on the echelon with supplier
  + production lead time. This safety stock is built into the quantile.
- Side by side on the same test seeds **[v2 baseline]**: total cost and cost by category,
  fill rate per channel vs target, cells passing, average stock, waste. s/S over weeks for
  baseline vs optimised per item (step lines).
- Until v2: show the search-seed costs `mean_cost_start_schedule_search_seeds` vs
  `mean_cost_final_schedule_search_seeds` with the note "search seeds; test-seed comparison
  requires API v2".

**7.10 Data input (Configure).** Purpose: maintain all planning data of the scenario.
- Sections (sub-tabs): Planning horizon, Finished goods, Channels, Raw materials, Bill of
  materials, Lead times, Cost tiers, Demand forecast, Initial stock, Open orders, Capacity.
- **Planning horizon:** horizon H (weeks). Shown read-only: commit week = W1, evaluation window
  = W(L_max+1)..W(H) with the explanation.
- **Master data per FG:** shelf life, sellable shelf life per channel (derived), batch, MOQ,
  holding/waste/fixed release cost, production and transport tiers, lead time, channels with
  target fill rates. **Per RM:** shelf life, min life at shipment, batch, MOQ, unit cost, fixed
  order cost, holding, waste and transport cost, lead time, supplier capacity, closed weeks.
  Each item row opens a detail sheet with its stochastic lead-time chart.
- **Uploads:** CSV templates (download buttons) and import with preview, row-level validation
  errors and "replace / merge" choice. Templates:
  `products.csv` (name, shelf_life, batch_size, moq, holding_cost, waste_cost,
  fixed_cost_per_release), `channels.csv` (product, channel, min_remaining_life,
  target_fill_rate), `materials.csv` (name, shelf_life, min_life_at_shipment, batch_size, moq,
  unit_cost, fixed_order_cost, holding_cost, waste_cost, transport_cost, supplier_capacity),
  `bom.csv` (product, material, quantity), `lead_times.csv` (item, weeks, probability),
  `cost_tiers.csv` (product, tier_type production|transport, lower, upper, unit_cost),
  `demand_forecast.csv` (product, channel, week, mean, sd), `initial_stock.csv` (location DC|RMW,
  item, age, units), `open_orders.csv` (lane DC|RMW, item, order_week, units),
  `capacity.csv` (week, capacity), `closed_weeks.csv` (item, week).
- Import / export the whole scenario as input JSON.

**7.11 Calculation settings (Configure).** Purpose: accuracy vs runtime.
- Preset cards: quick (200 / 400 / 2,000 seeds), standard (300 / 600 / 5,000); full (500 /
  1,000 / 10,000) shown disabled with "not available on this server". Show expected runtime
  and an accuracy note.
- **[v2]** Advanced (collapsed): search / hold-out / test seed counts, Z (safety multiplier on
  the standard error, default 2.0), minimum margin bump (0.005), improve passes, outer rounds,
  step fraction, lookahead RM steps, base seed. Each with a tooltip and default reset.

**7.12 Explainability (Understand).** Purpose: trust. Static, well-designed content pages with
diagrams (use exactly this content; do not add claims):
- *The weekly simulation*, in this order: 1 ageing (all stock one week older) -> 2 receipts
  (arrivals enter at age 1) -> 3a DC order ((s,S) on the effective DC position, order cap)
  -> 3b production release (limited by usable RM, capacity, batch and MOQ; the rest is
  cancelled) -> 3c RM transport to production (oldest first) -> 3d RM order ((s,S) on the
  effective echelon position, plus a minimum physical stock) -> 4 demand and allocation (oldest
  first, strictest channel first, lost sales) -> 5 service recording -> 6 scrap expired stock,
  holding cost.
- *The policy:* age-aware (s,S): the inventory position minus the stock expected to expire
  before a new order arrives is compared with s; if below, order up to S (rounded to batch size,
  at least MOQ, at most the order cap). The RMW also orders when physical stock falls below its
  minimum. The week-1 orders are then chosen by a lookahead: candidate quantities are simulated
  from today's exact state; the cheapest candidate that fails no service cell the rule's own
  quantity passes is committed.
- *The optimisation:* start from the heuristic schedule -> repair (raise levels feeding failing
  cells) -> improve (small moves that lower cost while all cells stay feasible) -> hold-out check
  (fresh seeds; raise a safety margin where the search over-fitted) -> lookahead -> final verdict
  on untouched test seeds.
- *Service rules:* a cell = product x channel x week in the evaluation window. Search:
  mean - Z x SE - margin >= target. Hold-out: weak if mean < target. Final verdict: mean >=
  target. Seeds without demand in a cell are excluded.
- *Uncertainty:* seeds = complete random futures (demand and lead times); all candidates are
  compared on the same seeds (common random numbers).
- *Assumptions and limitations:* (s,S)-type policy with lookahead, no global optimality claim;
  demand and lead times independent over weeks, no disruption regimes; FG shelf life independent
  of RM age; capacity applies in the release week; stock left at the end of the horizon has no
  value.
- *Glossary* with all KPI definitions (section 8).

### 8. Definitions for tooltips
- Fill rate = sales / demand in a week and channel (lost sales, no backorders).
- Pooled fill rate = total sales / total demand over the evaluation window.
- Service cell = one product x channel x week; passes if the mean fill over test seeds >= target.
- Inventory position = on hand + in the pipeline. Effective position = inventory position -
  stock expected to expire before a new order arrives.
- Echelon position (RMW) = RM on hand + RM on order + BOM x (FG on hand + FG in the pipeline).
- Expected waste / run-off at risk = units projected to expire before replenishment can arrive.
- Remaining shelf life = shelf life - age; a channel accepts a unit only if its remaining life
  >= the channel's minimum.
- P5 / P95 = 5 % / 95 % of simulated futures are below this value.

### 9. Quality bar (acceptance for every phase)
- No invented numbers; every number traces to the summary, the input JSON or a documented
  formula.
- The item filter changes every tab consistently; URL state survives reload.
- Every chart over weeks has bands where the data provides them, week-1 marker, warm-up shading.
- Edits mark the scenario as changed and dim results until the next run.
- Validation errors are shown next to the field and block "Run".
- Keyboard accessible, WCAG AA contrast in light and dark mode, colour never the only signal.

---

## Part 2 - Phase prompts (send one at a time)

**Phase 1 - Foundation.**
> Using the Master brief, build the app shell: top bar (scenario selector, global item filter,
> week range, run status, Run button with preset split), grouped left sidebar with all tabs as
> empty pages that show their title and purpose line, light/dark design tokens, and the Supabase
> tables. Implement the edge functions `meio-run-start`, `meio-run-status`, `meio-run-xlsx` and
> the run flow: create scenario from the example input JSON (paste it into a seed file), run it,
> poll every 20 s, store the summary, show toast and status. Build **Data input** completely
> (sub-tabs, grid editors, CSV templates and import with validation, JSON import/export) and the
> client-side validation. Acceptance: I can load the example, run it with preset quick, reload
> the page and still see the completed run.

**Phase 2 - Overview and Decisions.**
> Build **Overview** and **Decisions** exactly as specified in the Master brief (sections 7.2 and
> 7.3), including the supply chain map that follows the item filter, the band chart component
> (reusable: median, P25-P75, P5-P95, mean, reference lines, week-1 marker, warm-up shading) and
> the item side panel. Where a [v2] field is missing, show the "Requires API v2" placeholder.

**Phase 3 - Inventory dynamics, Demand forecast, Lead times.**
> Build sections 7.4, 7.5 and 7.6 with the reusable band chart, the negative-binomial forecast
> fan chart, the lead-time distribution charts, the convolution for cumulative lead time and the
> in-tab parameter editors (right drawer) that update the scenario draft.

**Phase 4 - BOM navigator, Supply restrictions, Baseline vs optimised.**
> Build sections 7.7, 7.8 and 7.9, including the where-used view, the capacity/calendar views and
> the baseline comparison (with the search-seed fallback until v2).

**Phase 5 - Scenario comparison, Calculation settings, Explainability.**
> Build sections 7.1, 7.11 and 7.12: scenario tree, save to comparison, 2-4 run comparison with
> noise-aware deltas and input diff, overlay chart; settings presets; the explainability pages
> with diagrams of the weekly steps and the optimisation loop.

**Phase 6 - Polish.**
> Review all tabs against the quality bar in section 9. Fix inconsistencies in spacing, number
> formats, colours per entity, tooltips, empty and loading states, dark mode and keyboard
> navigation. List anything you could not implement.

---

## Part 3 - Backend extension needed for [v2] fields (engine repository)

Additive to `summary.json` (no existing key changes), plus an optional request shape:
1. `meta` - products, materials, channels per product, horizon, evaluation window, commit week.
2. `weekly_bands` - mean and P5/P25/P50/P75/P95 per weekly metric over the test seeds, long
   format; add DC average remaining shelf life of stock at the end of each week.
3. `service.cells` - every cell with mean fill, SE and pass.
4. `baseline` - the start schedule simulated on the same test seeds (rule orders, no
   lookahead): policy, cost, costs by component, service by channel, KPIs.
5. `settings` - the calculation settings used.
6. `POST /runs` accepts `{"input": {...}, "settings": {...}}` besides the plain input JSON, with
   a whitelist of overridable settings and limits (seed counts capped for the server's memory).
