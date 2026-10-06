# Plan: from this engine to a Lovable app

*This document describes how the Python engine in this repository becomes the backend of an app whose interface is built with Lovable. Implemented: the JSON input/output contract and the web API (`api.py`, step 1 below).*

## 1. Why two repositories

- **Lovable builds the frontend.** Its projects are web apps, and its built-in backend is Supabase: database, auth, storage and edge functions. Edge functions can call external HTTP APIs, with API keys kept as secrets.
- **Lovable can't import this repository.** It only creates and syncs its own GitHub repository, one branch at a time. See the [Lovable GitHub docs](https://docs.lovable.dev/integrations/github).
- **This engine is Python**, with numpy, pandas and multi-minute runs. It has to run as its own web service.

So:

```
repo 1: meio-simopt (this repo)            repo 2: created by Lovable
Python engine  +  (later) small web API    React frontend + Supabase (tables, auth, edge functions)
        ^                                          |
        |   HTTPS: POST /runs, GET /runs/{id}      |
        +------------------------------------------+
```

## 2. The contract (already implemented)

**Input.** A JSON document as in `examples/example_input.json`, described at the top of `meio/io_json.py`. Optional `target_share_of_futures` (0–1, default 0.98): the share of futures in which every cell must reach its fill-rate target; lowering it (or a channel's F) lowers cost.
- `horizon`
- `sites` *(optional, multi-product)*: production sites, each `{name, capacity, capacity_overrides, closed_weeks}` (capacity in FG units per production week, shared by every product made there; a closed week stops all its products). Without `sites`, one default site `PF` is built from the old `production_capacity` / `capacity_overrides` (results unchanged)
- `products[].site` *(optional)*: the site that makes the product (must be one of `sites`; leave it out without `sites`). Products may share raw materials (the same material name in several BOMs); the RMW stock of a shared material serves the products in the order of the `products` list
- `products`: shelf life, channels (target fill rate, minimum remaining life), BOM (units of each material per FG unit, whole or fractional such as 0.2; the RM shipped is rounded up to whole units), batch size / MOQ, costs, discount tiers, lead-time distribution, closed weeks
- `materials`: shelf life, batch size / MOQ, costs, lead-time distribution, supplier capacity, closed weeks, `rmw_to_pf_lead_time` (weeks from the RMW to production, whole number ≥ 0, optional, default 0). A product is produced when its slowest BOM material has arrived (τ_p = max over the BOM) and reaches the DC after τ_p plus the PF → DC lead time; capacity and closed production weeks refer to that production week
- `demand_forecast[product][channel] = {mean: [...], sd: [...]}`, one value per week starting at week 1
- `initial_state`: stock by age at the DC and RMW, open orders with their order week (≤ 0)

**Output.** `summary.json`, with snake_case keys.

| Key | Content |
|---|---|
| `run` | Run metadata: timestamp, policy, preset, seeds, runtime, mean cost, cells passing, `independent_groups` (e.g. "FG1, FG2, FG3 \| FG4"), order cap, RMW minimums |
| `decisions_to_commit` | One row per decision: `position`, `expected_waste`, `effective_position`, `reorder_level_s`, `order_up_to_level_S`, `rule_quantity`, `committed_quantity`, `note` |
| `service` | `by_channel` (cells passing, worst week = smallest share of futures meeting F, with `worst_week_futures_meeting_target` / `worst_week_futures` / `worst_week_share_meeting_target`, `target_share_of_futures`), `failed_cells`, `all_cells_pass` |
| `costs` | Cost components (mean, share, P5, P95, SE) |
| `kpis` | Pooled fill rates, waste, cancellations, stock levels |
| `policy` | Optimised s and S per week for the DC and the RMW, order cap, RMW minimums; `parameters` *(v2)*: the tuned search variables per item, `products[p]` = `safety_factor_z, lot_cover_weeks, minimum_lot, order_cap_weeks, end_of_horizon_z_shift`, `materials[m]` = `safety_factor_z, lot_cover_weeks, minimum_stock_weeks, end_of_horizon_z_shift`, and `n_parameters` (the levels follow from these and the forecast; see `docs/search_algorithm.md`) |
| `weekly_means_test_seeds` | Mean flows, positions and costs per week |
| `meta` *(v2)* | `summary_version` (2), `products`, `materials`, `channels` per product, `bom` per product, `sites` (`name, capacity, products`), `product_site`, `independent_groups` (products optimised together because they share a site or material), `horizon`, `weeks`, `evaluation_weeks {first, last}`, `commit_week` (1) |
| `weekly_bands` *(v2)* | One entry per weekly series: `{location: DC\|RMW\|channel\|cost, item, channel, metric, mean[], p5[], p25[], p50[], p75[], p95[]}` over the test seeds, index 0 = week 1 (`null` where no seed has a value). Same series as `weekly_means_test_seeds`, plus `avg_remaining_life_end` (units-weighted shelf life − age of the stock at the end of the week) for every DC and RMW item |
| `service.cells` *(v2)* | Every cell: `product, channel, week, target_fill_rate, target_share_of_futures, futures_meeting_target, seeds_with_demand, share_of_futures_meeting_target, mean_fill, se, pass`. **Service target (chance constraint):** a cell passes if its fill rate reaches `target_fill_rate` in at least `target_share_of_futures` (default 0.98) of the simulated futures, shown e.g. as "F met in 9,995 / 10,000 futures"; `mean_fill` is information only |
| `baseline` *(v2)* | The heuristic start schedule of the search (same rule, quantile-based levels, initial cap and minimum), simulated on the same test seeds with the rule's own week-1 orders: `description, policy, mean_cost_over_horizon_test_seeds, test_cells_passing, costs, service_by_channel, kpis` |
| `settings` *(v2)* | All calculation settings of the run (seed counts, `z`, margins, rounds, ...) |

The detailed `results.xlsx` stays available as a download.

## 3. Next steps (in this order)

1. **Web API in this repo (done: `api.py`, tests in `tests/test_api.py`).**
   - `POST /runs?preset=quick|standard|full`: accepts an input JSON, or `{"input": <input JSON>, "settings": {...}}` to override calculation settings of the preset, checks both (422 with a message if invalid), queues a background job and returns `{run_id, status: "queued", preset, policy, settings_overridden}`. Overridable settings and limits: `n_search_seeds` ≥ 50, `n_holdout_seeds` ≥ 100, `n_test_seeds` ≥ 500 (each at most the largest value among the allowed presets), `z` 0–5, `min_margin_bump` 0–0.1, `max_outer_rounds` 1–5, `max_improve_passes` 1–6, `step_fraction` 0.02–0.5, `lookahead_rm_steps` 0–8, `base_seed`. Runs take 1–10 minutes (the search uses several CPU cores), so the request does not wait for the result. Runs are executed one at a time.
   - `GET /runs/{run_id}`: returns `{run_id, status, preset, summary, error}`; `status` is `queued`, `running`, `completed` or `failed`, and `summary` is `summary.json` once completed.
   - `GET /runs/{run_id}/results.xlsx`: the Excel download (404 until the run is completed).
   - `GET /health`: liveness probe.
   - Key: set `MEIO_API_KEY` on the host; every endpoint except `/health` then needs the header `X-API-Key`. `MEIO_CORS_ORIGINS` limits browser origins (default `*`).
   - The job registry is in memory and run folders are on the instance disk: after a restart, finished runs are still found if their folder survived; on a free Render instance it does not.
2. **Hosting.** Render web service defined in `render.yaml`; runbook: [`deployment_render.md`](deployment_render.md). Set `MEIO_API_KEY` as an environment variable and store the same key as a Supabase secret, so only the edge function (not the browser) sends it.
3. **Lovable project.**
   - Create it with Lovable Cloud or your own Supabase.
   - Store the API URL and key as Supabase secrets.
   - Let an edge function call the API, and store runs and results in Supabase tables.
   - Prompts for building the app: [`lovable_app_prompt.md`](lovable_app_prompt.md).
   - Screens: input editor (products, channels, materials, forecast upload), run list, decisions of the week, service heat map (week × channel), cost breakdown, policy chart (s/S over time).
4. **Later.** Scheduled weekly runs (rolling review), user accounts, comparison of scenarios.
