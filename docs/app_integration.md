# Plan: from this engine to a Lovable app

*This document describes how the Python engine in this repository can later become the backend of an app whose interface is built with Lovable. Nothing here is implemented yet, except the JSON input/output contract.*

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

**Input.** A JSON document as in `examples/example_input.json`, described at the top of `meio/io_json.py`.
- `horizon`, `production_capacity`, `capacity_overrides`
- `products`: shelf life, channels (target fill rate, minimum remaining life), BOM, batch size / MOQ, costs, discount tiers, lead-time distribution, closed weeks
- `materials`: shelf life, batch size / MOQ, costs, lead-time distribution, supplier capacity, closed weeks
- `demand_forecast[product][channel] = {mean: [...], sd: [...]}`, one value per week starting at week 1
- `initial_state`: stock by age at the DC and RMW, open orders with their order week (≤ 0)

**Output.** `summary.json`, with snake_case keys.

| Key | Content |
|---|---|
| `run` | Run metadata: timestamp, policy, preset, seeds, runtime, mean cost, cells passing, order cap, RMW minimums |
| `decisions_to_commit` | One row per decision: `position`, `expected_waste`, `effective_position`, `reorder_level_s`, `order_up_to_level_S`, `rule_quantity`, `committed_quantity`, `note` |
| `service` | `by_channel` (cells passing, worst week), `failed_cells`, `all_cells_pass` |
| `costs` | Cost components (mean, share, P5, P95, SE) |
| `kpis` | Pooled fill rates, waste, cancellations, stock levels |
| `policy` | Optimised s and S per week for the DC and the RMW, order cap, RMW minimums |
| `weekly_means_test_seeds` | Mean flows, positions and costs per week |

The detailed `results.xlsx` stays available as a download.

## 3. Next steps (in this order)

1. **Web API in this repo.** For example FastAPI with an `api/` folder.
   - `POST /runs`: accepts an input JSON and a preset, starts a background job and returns a `run_id`. Runs take 1–6 minutes, so the request must not wait for the result.
   - `GET /runs/{run_id}`: returns the status and, when finished, `summary.json`.
   - `GET /runs/{run_id}/results.xlsx`: the Excel download.
2. **Hosting.** Any service that runs a Python container (for example Render, Railway, Fly.io or Google Cloud Run). Protect the API with a key.
3. **Lovable project.**
   - Create it with Lovable Cloud or your own Supabase.
   - Store the API URL and key as Supabase secrets.
   - Let an edge function call the API, and store runs and results in Supabase tables.
   - Screens: input editor (products, channels, materials, forecast upload), run list, decisions of the week, service heat map (week × channel), cost breakdown, policy chart (s/S over time).
4. **Later.** Scheduled weekly runs (rolling review), user accounts, comparison of scenarios.
