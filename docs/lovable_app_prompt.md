# Lovable prompt pack - MEIO planning app

This file holds the prompts that build the web app for this model in Lovable. It is kept in the
repository because the prompts depend on the API contract (`api.py`, `summary.json` version 2,
`docs/app_integration.md`); change both together.

## How to use it

1. **Secrets first.** In the Lovable project (Supabase), add the secrets `MEIO_API_URL` (the Render
   URL) and `MEIO_API_KEY`.
2. **Onboard, then build.** Paste **Part 1 (Master brief)** into the project's knowledge /
   custom-instructions field, so every later message builds on it. It is the onboarding of a new
   colleague: domain, rules, data, design. If there is no such field, send it as the first message.
3. **Build in phases.** Send the **Part 2** phase prompts one at a time. After each phase, click
   through it, check its acceptance list, and ask for corrections in plain words before moving on.
   One "build everything" message gives shallow, inconsistent screens.
4. **Iterate on what you see.** Improvements you only notice once a screen exists (a table too
   long, a missing drill-down) are normal. Describe them in the next message.

---

## Part 1 - Master brief (paste into Project Knowledge)

### 0. How we work together
- Treat this brief as your onboarding. If something about the planning logic is unclear, **ask me
  instead of guessing**. Wrong planning logic is worse than a missing feature.
- **Exact rules vs. your judgement.** The model logic, the data contract, the formulas and the
  definitions in this brief are exact: implement them literally and do not add planning logic of
  your own. Visual layout details, micro-interactions, wording of empty states and chart polish are
  your judgement: propose good solutions.
- Build in small steps that I can review. When a table or list becomes long, propose grouping,
  collapsing or filtering.
- This app is a planning cockpit on top of a simulation-optimisation engine, not a full APS. Do
  not imply features the engine does not have (no ERP integration, no forecast algorithm, no
  historical accuracy unless I upload actuals).

### 1. Product
Build **"MEIO Planner"**, a web app for weekly multi-echelon inventory planning of perishable
products. It is the front end of an existing Python engine (REST API) that plans this network:

```
Suppliers (one per raw material) -> Raw-material warehouse (RMW) -> Production (PF, holds no stock) -> Distribution centre (DC) -> Sales channels
```

Every week the planner runs the model and gets:
- **Decisions to commit now (week 1):** FG order from production (Q), production release (P),
  RM transport RMW -> production (T), RM supplier orders (O).
- **The plan for weeks 2..H:** an optimised age-aware (s,S) policy per item and week and the
  orders, stock, service and cost it is expected to produce. Not commitments: re-optimised every
  week.
- **Uncertainty:** every result comes from thousands of simulated futures ("test seeds"). The app
  always shows the spread of outcomes, not only averages.
- **A baseline:** the same rule with simple heuristic levels, evaluated on the same futures, so
  the value of the optimisation is visible.

Users are supply chain planners and their managers. They want the answer first (what to do now,
is service safe, what does it cost, what are the options), then the reasons.

### 2. Non-negotiable principles
1. **Decision first.** Every screen starts with the conclusion (KPIs, recommendation, status),
   then the chart, then the detail table.
2. **Overview -> drill-down.** Portfolio view by default. A global item filter narrows every tab
   to one or more finished goods (FG); raw materials (RM) follow automatically via the BOM.
   Clicking an item, week or cell opens its detail.
3. **Weekly buckets.** Time axis "W1 ... WH". W1 = current review week, labelled "W1 - commit
   now". Weeks before the evaluation window are shaded "warm-up (not evaluated)". Wide grids get
   an optional 4-week aggregation toggle.
4. **Uncertainty always visible.** Every result chart over weeks shows the median line, a P25-P75
   band and a P5-P95 band from `weekly_bands`. Cost and KPI numbers show P5-P95 or the standard
   error on hover.
5. **Never fabricate data.** Only numbers from the API or computed from the input JSON with the
   formulas in this brief. Missing data -> neutral placeholder ("Run the scenario to see
   results", "Not in this run's output"). No mock numbers, except the clearly labelled "Example
   data" scenario loaded from the engine's example input.
6. **Edit where it is shown, run once.** Parameters are edited in the tab that shows them. All
   edits change the **scenario draft** (one input JSON). A top-bar badge "Inputs changed - results
   out of date" appears and results are dimmed until the next run.
7. **Every change is traceable.** Forecast overrides and parameter changes are logged (who, when,
   what, old -> new, reason). This supports review and coaching.
8. **Explain on hover.** Every KPI, column header and chart has an info tooltip with a one-sentence
   definition (section 8).

### 3. Architecture (Supabase + edge functions)
- The browser never calls the engine directly and never sees the API key.
- Edge functions (secrets `MEIO_API_URL`, `MEIO_API_KEY`; header `X-API-Key`):
  - `meio-run-start`: `POST {MEIO_API_URL}/runs?preset=<quick|standard>` with body
    `{"input": <scenario input JSON>, "settings": {<overrides>}}` (settings may be `{}`).
    Response `{run_id, status: "queued", preset, policy, settings_overridden}`. A 422 means
    invalid input or settings: show its `detail` text to the user.
  - `meio-run-status`: `GET {MEIO_API_URL}/runs/{run_id}` -> `{status, preset, summary, error}`,
    status `queued | running | completed | failed`. When `completed`, store the summary in Supabase
    in the same call.
  - `meio-run-xlsx`: proxies `GET {MEIO_API_URL}/runs/{run_id}/results.xlsx`.
  - `meio-assistant`: the planning assistant (section 7.14), using Lovable's built-in AI.
- Poll every 20 s while a run is active (a run takes about 3-20 minutes depending on the server
  plan). Run progress card: queued -> running (elapsed time) -> completed / failed. The API runs one
  job at a time and queues the rest.
- The API server forgets results when it restarts: Supabase is the system of record.
- Tables: `scenarios` (id, name, description, parent_scenario_id, input_json jsonb, settings_json
  jsonb, created_by, created_at, updated_at, is_example bool), `runs` (id, scenario_id, api_run_id,
  preset, status, started_at, finished_at, error, input_snapshot jsonb, settings_snapshot jsonb),
  `run_results` (run_id, summary jsonb), `change_log` (id, scenario_id, user, at, area, item,
  channel, week, field, old_value, new_value, reason, source: manual|assistant|upload),
  `decision_log` (run_id, decision, item, quantity, status: open|executed, user, at),
  `option_sets` (id, name, run_ids uuid[], extra_costs jsonb), `history` (product, channel, week
  date, units: optional actuals for display only, never sent to the engine).
- Libraries: shadcn/ui, Tailwind, Recharts (range areas for bands), @xyflow/react (network map),
  TanStack Table (dense grids, sticky header and first column), PapaParse (CSV), date-fns.

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
  supplier_capacity (units per order or null), closed_order_weeks [week],
  rmw_to_pf_lead_time (tau_r: whole weeks RMW -> production, >= 0, default 0)
  Per FG: tau_p = max tau_r over its BOM (production starts when the slowest material has arrived);
  production week = release week + tau_p (its capacity and closed weeks apply); FG arrival =
  release week + tau_p + PF->DC lead time.
demand_forecast {product: {channel: {"mean": [week1..], "sd": [week1..]}}}
initial_state: dc_stock {product: {"age": units}}, rm_stock {material: {"age": units}},
  dc_pipeline {product: [[release_week <= 0, units]]}, rm_pipeline {material: [[order_week <= 0, units]]}
```
Seed the "Example data" scenario with the engine's `examples/example_input.json` (I will paste it).

Derived quantities (compute in the app):
- Sellable shelf life per channel = `shelf_life - min_remaining_life` (oldest age the channel
  accepts). Max shippable RM age = `shelf_life - min_life_at_shipment`.
- L_max = largest tau_p + FG lead time with probability > 0. **Evaluation window** = W(L_max+1)..WH
  (earlier weeks are fixed by what is already in the pipeline).
- Forecast distribution per product/channel/week: negative binomial with the given mean and sd
  (`n = mean^2 / (sd^2 - mean)`, `p = n / (n + mean)`); Poisson(mean) if `sd^2 <= mean`; 0 if
  `mean = 0`. Use it for forecast fan charts before a run.

Client-side validation (block "Run", list the problems next to the fields): lead-time
probabilities sum to 1 per item; MOQ is a multiple of the batch size; required remaining life <
shelf life; every BOM material exists; cost tiers are contiguous (next.lower = previous.upper + 1);
initial stock ages within 1..max sellable/shippable age; forecast covers at least H + 1 weeks;
target fill rates within 0..1.

#### 4.2 Output `summary` (from `GET /runs/{id}`)
Arrays over weeks have index 0 = W1.
- `meta`: `summary_version` (2), `products`, `materials`, `channels` {product: [channel]},
  `horizon`, `weeks`, `evaluation_weeks {first, last}`, `commit_week` (1).
- `run`: timestamp, policy, preset, `mean_cost_over_horizon_test_seeds`, `test_cells_passing`
  ("84 / 84"), runtime, order cap and RMW minimums.
- `decisions_to_commit[]`: decision, item, location, position, expected_waste,
  effective_position, reorder_level_s, order_up_to_level_S, rule_quantity (the (s,S) rule alone),
  committed_quantity (after the lookahead), note.
- `service`: `by_channel[]` (product, channel, target_fill_rate, cells_evaluated, cells_passing,
  worst_week, worst_week_mean_fill), `failed_cells[]`, `all_cells_pass`, and `cells[]` (product,
  channel, week, target_fill_rate, mean_fill, se, seeds_with_demand, pass) for every cell.
- `costs[]`: cost_component (10 components + TOTAL), mean, share_of_total, sd, p5, p95,
  se_of_mean, over the horizon.
- `kpis[]`: kpi (text), value, note.
- `policy`: `weeks`, `dc.{FG}.{s[], S[], order_cap}`, `rmw.{RM}.{s[], S[],
  minimum_physical_stock}` (0 or empty = no ordering possible that week).
- `weekly_bands[]`: one entry per weekly series `{location: DC|RMW|channel|cost, item, channel,
  metric, mean[], p5[], p25[], p50[], p75[], p95[]}`; `null` where no simulated future has a
  value. Metrics:
  - DC (per FG): on_hand_start, receipts, pipeline_before_order, inventory_position,
    expected_waste, effective_position, cut_by_policy_cap, ordered_q, rm_limit, released_p,
    cancelled, cut_by_rm, cut_by_capacity, demand, sales, lost_sales, waste, on_hand_end,
    pipeline_end, avg_remaining_life_end.
  - RMW (per RM): on_hand_start, receipts, limited_release, shipped_t, pipeline_before_order,
    echelon_position, expected_waste, effective_echelon_position, installation_position,
    floor_triggered, ordered_o, waste, on_hand_end, pipeline_end, avg_remaining_life_end.
  - channel (per FG and channel): demand, sales, lost_sales.
  - cost: fg_holding, fg_waste, production_fixed, production_variable, pf_dc_transport,
    rm_purchase, rm_order_fixed, rm_holding, rm_waste, rmw_pf_transport.
  - Flags (cut_by_*, floor_triggered, limited_release) are 0/1: use `mean` = share of futures.
- `weekly_means_test_seeds[]`: the same means as flat rows (use `weekly_bands` instead).
- `baseline`: `description`, `policy` (same shape as `policy`),
  `mean_cost_over_horizon_test_seeds`, `test_cells_passing`, `costs[]`, `service_by_channel[]`,
  `kpis[]`: the heuristic schedule on the same simulated futures.
- `settings`: the calculation settings used (n_search_seeds, n_holdout_seeds, n_test_seeds, z,
  min_margin_bump, max_outer_rounds, max_improve_passes, step_fraction, lookahead_rm_steps,
  base_seed, ...).

### 5. Layout and navigation
- Desktop first (1280-1920 px), usable at 1024 px. Dense, calm, technical.
- **Top bar:** app name - **scenario selector** (search; name, last run, status dot) - **global
  item filter** (multi-select, default "All finished goods") - week range - run status pill -
  primary green button "Run scenario" (split: quick / standard) - download results.xlsx.
- **Left sidebar** (collapsible to icons), grouped:
  - *Scenarios & options*: Scenario comparison
  - *Plan*: Overview, Decisions
  - *Analyse*: Demand forecast, Inventory dynamics, Lead times, BOM navigator, Supply
    restrictions, Baseline vs optimised
  - *Configure*: Data input, Calculation settings
  - *Understand*: Explainability
- **Page pattern:** header row (title + one-line purpose on the left; tab-level selectors such as
  Product / Channel and the tab's primary action on the right, as in the reference screenshot)
  -> KPI strip -> main visual -> secondary visuals (2-column grid) -> detail table (collapsible)
  -> "Edit parameters" right drawer.
- **Item side panel** (right drawer, 480 px) from any item name: master data, network position,
  lead-time distribution, key charts, link to the filtered tab. Breadcrumbs `Portfolio > FG1 >
  Retail > W14`.
- **Planning assistant** available on every page as a floating button bottom-right, and inline at
  the top of the Demand forecast tab (section 7.14).
- URL holds state (scenario, filter, tab, item, week range).

### 6. Visual design system (from the reference screenshot)
Dark, slim, technical. Numbers are the heroes; chrome is minimal.
- **Theme:** dark by default (light mode optional with the same roles).
  - page background #0B1120; panels #0F172A; raised cards / table header #111827;
  - input fields and editable cells #1E293B with 1 px border #334155, focus ring #3B82F6;
  - dividers and grid lines #1E293B; text #E2E8F0; secondary text #94A3B8; disabled #64748B.
- **Accents:** green #22C55E = primary action (Save, Run, Apply plan), the optimised / plan
  series and positive deltas; blue #3B82F6 = secondary action (Apply, links), the baseline /
  statistical series (dashed); light grey #CBD5E1 = history / actuals; amber #F59E0B = watch;
  red #EF4444 = fail / negative deltas. Always pair colour with an icon or sign.
- **Typography:** Inter; tabular numerals for every number, right-aligned, thousands separators.
  Section labels in small caps style: 11 px, uppercase, letter-spacing 0.08em, #94A3B8, with an
  icon (like "+ ASK THE PLANNING ASSISTANT"). Page titles 18-20 px semibold, subtitles 13 px muted.
- **Tables / grids** (like the manual forecast grid in the screenshot): compact rows, sticky first
  column (product / channel with a small pill badge for the channel, e.g. blue pill "Retail"),
  sticky header with period labels in caps, a highlighted total row per product, a sticky total
  column on the right, a notes column with placeholder "reason...". Editable cells: the reference
  value small and muted above, the editable value in an input below, the % delta underneath in
  green or red.
- **Charts:** dark background, thin grid lines, legend on top, lines 2 px with small points, area
  fill under the main series at ~15 % opacity. Bands: P5-P95 at 12 %, P25-P75 at 24 % of the
  series colour, median solid, mean dashed 1 px. Reference lines: s dotted, S dashed (grey),
  target fill rate red dotted, W1 marker, warm-up shading.
- **Series colours fixed across the app:** optimised / plan green, baseline blue dashed, history
  grey; channels in order #22C55E, #3B82F6, #A855F7, #F59E0B, #14B8A6, #EC4899; locations:
  Supplier slate, RMW amber, PF violet, DC blue, Channels teal.
- Radius 8, 1 px borders instead of shadows, 8 px grid. Loading skeletons, empty states with the
  next action, toasts for run finished / failed.

### 7. Tabs

**7.1 Scenario comparison and decision options.** Purpose: never present a single answer; show
options and their trade-offs.
- Scenario tree: duplicate a scenario, change data or parameters, run, compare. Children show
  their parent.
- **Option templates** that create a child scenario in one click (then run it): service targets
  -/+ x points per channel; production capacity +/- x % in weeks a-b; demand upside / downside
  +/- x % for selected products and weeks; RM lead time +1 week for a supplier; shelf life
  change. Each template writes normal input edits (logged in `change_log`).
- **Options board** (2-4 runs side by side): total cost (P5-P95), cost by category, pooled fill
  per channel vs target, cells passing, average DC and RMW stock, waste share, average remaining
  shelf life, week-1 decisions. Optional field "extra cost of this option" (e.g. an extra shift)
  entered by the user and clearly labelled "user input", added to a separate "cost incl. option
  cost" row. **Recommendation line:** the cheapest option in which all service cells pass; if
  none passes, the one with the fewest failing cells, stated as such.
- Deltas: coloured by direction; a cost delta counts as real only if
  |delta| > 2 x sqrt(se1^2 + se2^2), else "within noise".
- "What changed" list: diff of the input snapshots (field, old, new, reason from `change_log`).
- Overlay chart: any weekly metric for all compared runs, with bands.

**7.2 Overview (Plan)** - the executive view. Purpose: is the plan healthy, what does it cost,
where is the risk?
- KPI strip: expected total cost (P5-P95) with delta vs baseline, service cells passing, worst
  channel fill vs target, FG waste share, average DC stock, average remaining shelf life at the DC.
- **Supply chain map** (@xyflow/react, left to right): suppliers (one per RM) -> RMW -> PF -> DC ->
  channels. Follows the item filter: the selected FG, its RMs, suppliers and channels are
  highlighted, the rest dimmed. Node badges: median stock, remaining shelf life, waste risk,
  service status. Edge labels: median lead time and range ("6 wk (5-8)"), BOM quantity on
  RM -> PF edges. Click a node -> detail drawer.
- Cost by category: horizontal bars with P5-P95 whiskers, share of total, baseline as a marker.
- Fill rate per channel: bullet chart (pooled fill vs target). Service heatmap from
  `service.cells`: rows product x channel, columns weeks, colour = mean fill - target, failing
  cells outlined red, warm-up greyed.
- Inventory dynamics per location: DC and RMW on-hand end of week with bands.
- **Run-offs at risk:** expected waste (units expected to expire before a new order can arrive)
  and realised waste per week with bands; table of items with the most units at risk; average
  remaining shelf life trend.
- Week-1 decisions card (links to Decisions).

**7.3 Decisions (Plan).** Purpose: what to do now, what is planned next.
- **Commit now (W1)**, one card per decision: item, location, committed quantity (big number),
  rule quantity vs committed ("Lookahead adjusted: -40 units vs the (s,S) rule"; `note` as
  context, e.g. the order cap), position -> expected waste -> effective position vs s and S as a
  small bar. "Mark as executed" (stored in `decision_log`), export CSV.
  Types: Order FG from production (Q); Release to production (P, may be cut by RM, capacity or
  MOQ); Transport RM to production (T); Order RM from supplier (O).
- **Allocation rule** card: FIFO (oldest stock first); within an age the channel with the strictest
  freshness requirement first, ties by higher target fill rate; unmet demand is lost. Expected W1
  sales per channel with bands.
- **Planned, not committed (W2..WH):** week x item grid: s, S, expected orders Q / releases P /
  transports T / supplier orders O (median, P5-P95), labelled "Indicative - re-optimised every
  week". Toggle "only weeks with ordering possible".

**7.4 Demand forecast (Analyse).** Purpose: see and adjust the demand the plan protects against.
- Header: title "Demand forecast", subtitle "Forecast distribution per product, channel and week -
  adjust the plan, every change is logged"; right side: Product selector, Channel selector
  ("All channels"), green "Save plan" button.
- **Planning assistant box** at the top (as in the screenshot): multi-line input with examples
  ("increase FG1 Retail in W10-W14 by 15 %", "set FG1 Online W20 to 150", "reduce all AMZ
  demand in W30-W36 by 10 %"), buttons "Apply" (blue) and "Reset to input forecast".
- **Manual override grid** (as in the screenshot): rows = product x channel with a channel pill,
  plus a highlighted TOTAL row per product; columns = weeks (toggle: 4-week buckets), a total
  column for the horizon, a notes column. Each cell: input forecast mean small and muted, the
  editable plan value below, % delta vs input underneath (green up / red down). Editing a total
  cell allocates proportionally to the channels' shares. Editing a 4-week bucket spreads
  proportionally over its weeks. Legend line under the grid: "Grey = input forecast | White =
  plan | % = change vs input | TOTAL row edits allocate by channel share".
- **Uncertainty rule for overrides:** when a mean is changed, the sd is scaled by the same factor
  (coefficient of variation stays constant), with a toggle "keep sd unchanged".
- A reason is required for every saved change (`change_log`, source manual / assistant / upload).
  "Change log" drawer with filters; "Reset to input forecast" per cell, row or all.
- **Chart below the grid:** per selected product (or channel): optional history (grey line with
  area, only if actuals were uploaded to `history`), input forecast (blue dashed), plan (green
  line with area), and the P5-P95 / P25-P75 bands of the plan distribution (negative binomial);
  after a run also the simulated demand bands from `weekly_bands`. Title "<Product> - demand
  history and forecast", subtitle with the distribution used. No accuracy or backtest numbers
  unless computed from uploaded actuals.
- Also editable here: target fill rate and minimum remaining life per channel (drawer).

**7.5 Inventory dynamics (Analyse).** Purpose: how stock, positions and service evolve.
- Item selector (FG or RM) + location toggle DC / RMW.
- Main chart: on-hand end of week with bands, with s and S of the optimised policy (step lines)
  and the order cap or RMW minimum as reference lines.
- Position chart: inventory (or echelon) position, expected waste, effective position (the rule
  compares the effective position with s).
- Flows (stacked bars, medians): receipts, sales, waste, lost sales.
- Fill rate per channel per week with bands and target line.
- Remaining shelf life of stock (bands) against the channels' minimum remaining life.
- Cut releases: share of futures in which releases were cut by RM, capacity or the order cap.

**7.6 Lead times (Analyse).** Purpose: how long and how uncertain each link is.
- Per FG: path Supplier -> RMW -> PF -> DC with a small probability bar chart per link, median
  and range (RMW -> PF = the fixed tau_r of each material, one bar with probability 1; the slowest
  material sets tau_p); the critical path (longest supplier lead time + tau_p + FG lead time)
  highlighted.
- Total lead time distribution per FG and RM (supplier + tau_p + production), computed by convolution.
- Note: lead times are random per order and order-preserving (a later order never arrives before
  an earlier one on the same link).
- Editable here: lead-time distributions (week / probability rows, must sum to 1, live chart) and
  tau_r per material (RMW -> PF, whole weeks >= 0).

**7.7 BOM navigator (Analyse).** Purpose: trace raw materials to finished goods and back.
- Expandable tree FG -> RMs and reverse "where used" RM -> FGs, each row with KPIs: quantity per
  FG, supplier lead time (median, range), RM stock on hand and its remaining shelf life, RM
  shelf life vs lead time (risk flag when the shelf life is short relative to the lead time),
  number of FGs sharing the RM. Colour-coded risk pills.
- **Requirement explosion (pegging):** enter an FG quantity and a target week -> per RM: required
  quantity (BOM x quantity), latest order week using the median and the P90 of (supplier lead time
  + FG lead time), current RM stock that can cover it, and a red flag when the latest order week
  is before W1 ("too late with normal lead times").
- Editable here: BOM quantities.

**7.8 Supply restrictions (Analyse).** Purpose: what the supply side can deliver.
- Production capacity per week (line) with overrides and closed production weeks marked; expected
  production (released P, median and P95 from `weekly_bands`) against capacity; weeks where
  releases were cut by capacity.
- Supplier capacity per order and closed order weeks per RM (calendar strip per item).
- Batch sizes and MOQs per FG and RM with the smallest possible order.
- Editable here: capacity, capacity overrides, closed weeks, supplier capacity.

**7.9 Baseline vs optimised (Analyse).** Purpose: what the optimisation adds over a simple rule.
- Explain the baseline in one card: same rule; s = quantile of demand over the lead time (the
  quantile = the product's highest target fill rate, which acts as the safety stock), S = the
  same plus one extra week of cover, rounded to batches; RM on the echelon with supplier +
  production lead time; initial order cap and RMW minimum; W1 orders from the rule (no lookahead).
  It is also the starting point of the optimisation.
- Side by side from `baseline` vs the optimised run: total cost and cost by category, fill rate per
  channel vs target, cells passing, average stock, waste, KPIs. s/S over weeks, baseline (blue
  dashed) vs optimised (green), per item.

**7.10 Data input (Configure).** Purpose: maintain all planning data of the scenario.
- Sub-tabs: Planning horizon, Finished goods, Channels, Raw materials, Bill of materials, Lead
  times, Cost tiers, Demand forecast, Initial stock, Open orders, Capacity, History (optional).
- Planning horizon: H (weeks) editable (tau_r is edited per material). Read-only: commit week = W1, evaluation window W(L_max+1)..WH with
  the explanation.
- Master data per FG and per RM as in 4.1; each row opens a detail sheet with its stochastic
  lead-time chart and derived values (sellable shelf life per channel, max shippable age).
- **Uploads:** CSV templates (download) and import with preview, row-level errors and
  replace / merge: `products.csv` (name, shelf_life, batch_size, moq, holding_cost, waste_cost,
  fixed_cost_per_release), `channels.csv` (product, channel, min_remaining_life,
  target_fill_rate), `materials.csv` (name, shelf_life, min_life_at_shipment, batch_size, moq,
  unit_cost, fixed_order_cost, holding_cost, waste_cost, transport_cost, supplier_capacity, rmw_to_pf_lead_time),
  `bom.csv` (product, material, quantity), `lead_times.csv` (item, weeks, probability),
  `cost_tiers.csv` (product, tier_type production|transport, lower, upper, unit_cost),
  `demand_forecast.csv` (product, channel, week, mean, sd), `initial_stock.csv` (location
  DC|RMW, item, age, units), `open_orders.csv` (lane DC|RMW, item, order_week, units),
  `capacity.csv` (week, capacity), `closed_weeks.csv` (item, week), `history.csv` (product,
  channel, week_date, units; display only).
- Import / export the whole scenario as input JSON.

**7.11 Calculation settings (Configure).** Purpose: accuracy vs runtime.
- Preset cards: quick (200 / 400 / 2,000 simulated futures for search / hold-out / test),
  standard (300 / 600 / 5,000); full (500 / 1,000 / 10,000) shown disabled "not available on
  this server". Expected runtime and an accuracy note.
- Advanced (collapsed), sent as `settings`: n_search_seeds (>= 50), n_holdout_seeds (>= 100),
  n_test_seeds (>= 500) - each at most the standard preset's value; z 0-5 (default 2.0);
  min_margin_bump 0-0.1 (0.005); max_outer_rounds 1-5; max_improve_passes 1-6; step_fraction
  0.02-0.5 (0.10); lookahead_rm_steps 0-8 (4); base_seed. Tooltips and "reset to preset".
  Show the API's 422 message if a value is refused.

**7.12 Explainability (Understand).** Static content pages with diagrams. Use exactly this
content; do not add claims.
- *The weekly simulation:* 1 ageing (all stock one week older) -> 2 receipts (arrivals enter at
  age 1) -> 3a DC order ((s,S) on the effective DC position, order cap) -> 3b production release
  (limited by usable RM, capacity of the production week, batch and MOQ; the rest is cancelled)
  -> 3c RM leaves the RMW (oldest first) and material r reaches production tau_r weeks later, production starts when the slowest has arrived -> 3d RM order ((s,S) on the effective echelon position, plus a
  minimum physical stock) -> 4 demand and allocation (oldest first, strictest channel first, lost
  sales) -> 5 service recording -> 6 scrap expired stock, holding cost.
- *The policy:* age-aware (s,S): the inventory position minus the stock expected to expire before
  a new order arrives is compared with s; below s, order up to S (rounded to the batch size, at
  least the MOQ, at most the order cap). The RMW also orders when its physical stock falls below
  its minimum. The W1 orders are then chosen by a lookahead: candidate quantities are simulated from
  today's exact state; the cheapest candidate that fails no service cell the rule's own quantity
  passes is committed.
- *The optimisation:* heuristic start (the baseline) -> repair (raise levels feeding failing cells)
  -> improve (small moves that lower cost while all cells stay feasible) -> hold-out check (fresh
  futures; raise a safety margin where the search over-fitted) -> lookahead -> final verdict on
  untouched test futures.
- *Service rules:* a cell = product x channel x week in the evaluation window. Search: mean - Z x
  SE - margin >= target. Hold-out: weak if mean < target. Final verdict: mean >= target. Futures
  without demand in a cell are excluded.
- *Uncertainty:* a seed is one complete random future (demand and lead times); all candidates
  are compared on the same futures (common random numbers).
- *Assumptions and limitations:* (s,S)-type policy with lookahead, no global optimality claim;
  demand and lead times independent over weeks, no disruption regimes; FG shelf life independent
  of RM age; capacity applies in the production week (release week + tau_p); tau_r is fixed and faster materials wait without holding cost; stock left at the end of the horizon has no
  value. Scope: a planning cockpit on a simulation-optimisation engine, not a full APS.
- *Glossary* (section 8).

**7.13 Change log & coaching (inside Scenario comparison and Demand forecast).** All overrides and
parameter changes with user, time, old -> new, reason and source; filter by user, item, area;
compare the plan (overrides) with the input forecast per product as a simple "value added of
overrides" view once actuals are uploaded.

**7.14 Planning assistant.** A natural-language helper, never an autopilot.
- **Commands** become a list of structured edits (JSON with a fixed schema: area, product,
  channel, weeks, field, operation set|scale|add, value). Supported areas: demand forecast
  (mean, optionally sd), channel targets and minimum remaining life, lead-time distributions,
  capacity and overrides, closed weeks. The edit list is validated against the scenario, shown as
  a **preview diff** (cells highlighted in the grid), and applied only after the user clicks
  "Apply"; each applied edit is logged with source "assistant". Nothing runs the model by itself.
- **Questions** ("why is Retail fill lowest in W14?", "what does the 95 % option cost vs 98 %?")
  are answered only from the current scenario's input, run summary and saved runs, citing the
  numbers used. If the data cannot answer it, say so.
- Implemented in the edge function `meio-assistant` with Lovable's built-in AI; the browser never
  holds model keys.

### 8. Definitions for tooltips
- Fill rate = sales / demand in a week and channel (lost sales, no backorders).
- Pooled fill rate = total sales / total demand over the evaluation window.
- Service cell = one product x channel x week; passes if the mean fill over the test futures >=
  target.
- Inventory position = on hand + in the pipeline. Effective position = inventory position - stock
  expected to expire before a new order arrives.
- Echelon position (RMW) = RM on hand + RM on order + BOM x (FG on hand + FG in the pipeline).
- Expected waste / run-off at risk = units projected to expire before replenishment can arrive.
- Remaining shelf life = shelf life - age; a channel accepts a unit only if its remaining life >=
  the channel's minimum. Average remaining shelf life = units-weighted over the stock at the end
  of the week.
- P5 / P95 = 5 % / 95 % of the simulated futures are below this value.
- Baseline = the same rule with heuristic levels (the starting point of the optimisation).

### 9. Quality bar (acceptance for every phase)
- No invented numbers; every number traces to the summary, the input JSON or a formula here.
- The item filter changes every tab consistently; URL state survives reload.
- Every chart over weeks has bands where the data provides them, the W1 marker and warm-up shading.
- Edits mark the scenario as changed, are logged with a reason and dim results until the next run.
- Validation errors appear next to the field and block "Run".
- Keyboard accessible, WCAG AA contrast, colour never the only signal.

---

## Part 2 - Phase prompts (send one at a time)

**Phase 1 - Foundation and data.**
> Using the Master brief, build the app shell in the dark design system (section 6): top bar,
> grouped sidebar with all tabs as pages showing title and purpose, and the Supabase tables. Build
> the edge functions `meio-run-start`, `meio-run-status`, `meio-run-xlsx` and the run flow: create
> the "Example data" scenario from the example input JSON below, run it with preset quick, poll
> every 20 s, store the summary, toast on finish. Build **Data input** completely (sub-tabs, grid
> editors, CSV templates and import with validation, JSON import / export) and the client-side
> validation. Acceptance: I load the example, run it, reload the page and still see the completed
> run with its status and timestamp.
> [paste examples/example_input.json here]

**Phase 2 - Overview and Decisions.**
> Build **Overview** (7.2) and **Decisions** (7.3). First build one reusable band chart component
> (median, P25-P75, P5-P95, mean, reference lines, W1 marker, warm-up shading) on `weekly_bands`,
> and the item side panel. Include the supply chain map that follows the item filter.

**Phase 3 - Demand forecast with overrides and assistant.**
> Build **Demand forecast** (7.4) exactly like the reference screenshot: header selectors and
> "Save plan", the planning assistant box (7.14, commands only for now) with preview and Apply /
> Reset, the manual override grid with input vs plan vs % delta, total rows, 4-week buckets,
> notes and required reasons, the change log, and the forecast chart with bands.

**Phase 4 - Inventory dynamics, Lead times, BOM navigator.**
> Build 7.5, 7.6 and 7.7, including the lead-time convolution, the BOM tree with KPIs and the
> requirement explosion (pegging), with in-tab parameter editors that update the scenario draft.

**Phase 5 - Supply restrictions, Baseline vs optimised, Settings.**
> Build 7.8, 7.9 and 7.11 (settings sent as `{"input", "settings"}`; show 422 messages).

**Phase 6 - Scenarios, options and explainability.**
> Build 7.1 (scenario tree, option templates, options board with recommendation, noise-aware
> deltas, input diff, overlay chart), 7.13, the question mode of the planning assistant (7.14,
> floating button on every page) and 7.12.

**Phase 7 - Polish.**
> Review every tab against the quality bar (section 9) and the design system (section 6): spacing,
> number formats, fixed colours per entity, tooltips, empty and loading states, keyboard use. List
> anything you could not implement.
