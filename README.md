# meio-simopt

Simulation-optimisation model for a **perishable two-echelon supply chain with production**:

```
Supplier r --(random lead time)--> Raw-material warehouse (RMW) --(fixed lead time tau_r)--> Production (PF, no stock) --(random lead time)--> DC --> sales channels
                                   stock by age, perishable                                                          stock by age, perishable
```

Every week (review period 1 week) the model decides:
- how much finished good (FG) the DC orders from production (**Q**);
- how much is released and produced (**P**), and the raw material transported to production for it (**T**);
- how much of each raw material the RMW orders from its supplier (**O**).

Demand is given as non-stationary forecast distributions per week and channel. Lead times are random and order-preserving. Unmet demand is lost. Each channel has a minimum remaining shelf life and a per-week fill-rate target.

**Ordering policy.** The model uses one policy: an age-aware (s,S) rule with a DC order cap and a minimum physical RM stock, tuned by simulation-optimisation, plus a **week-1 lookahead** that chooses the orders committed now by simulating candidate quantities from the current state. Why this policy: see [`docs/policy_choice.md`](docs/policy_choice.md).

## Quick start

Requires Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python main.py --preset quick      # ~1-2 min, example input
python main.py                     # preset "standard", ~3 min
python -m pytest -q                # 89 tests
```

Each run creates a folder `output/run_<timestamp>/` containing:
- `results.xlsx`: 25 sheets with inputs, policy, decisions, service per cell, costs, KPIs, and full traces (stock by age, flows, orders) for 3 seeds;
- `summary.json`: the same results as plain JSON, meant for apps and APIs, including percentile bands per week over the test seeds and the heuristic baseline evaluated on the same seeds (format: [`docs/app_integration.md`](docs/app_integration.md));
- `input.json`: the exact input used.

## Command-line options

| Option | Meaning |
|---|---|
| `--input file.json` | Your own input (format: `examples/example_input.json`, described in `meio/io_json.py`) |
| `--preset quick\|standard\|full` | Seed counts and search effort (200/400/2,000 … 500/1,000/10,000 seeds) |
| `--out-dir`, `--name` | Where the run folder is written |

Other scripts:
- `python rolling_demo.py`: 3 consecutive weekly reviews with state updates.
- `python -m meio.io_json <file>`: writes the small built-in reference instance (used by the tests and by `main.py` without `--input`).

## Web API

`api.py` wraps a run in a small FastAPI service for the Lovable app (plan: [`docs/app_integration.md`](docs/app_integration.md)).

```bash
python api.py                      # http://localhost:8000/docs (interactive)
```

| Endpoint | Meaning |
|---|---|
| `GET /health` | Liveness probe |
| `POST /runs?preset=quick` | Body = input JSON as in `examples/example_input.json`, or `{"input": ..., "settings": {...}}` to override calculation settings (limits in `docs/app_integration.md`). Checks both (422 with a message if invalid), queues the run and returns `run_id` |
| `GET /runs/{run_id}` | `status`: queued, running, completed or failed; when completed also `summary` (= `summary.json`) |
| `GET /runs/{run_id}/results.xlsx` | The Excel workbook of a completed run |

Runs are executed one at a time. Optional environment variables: `MEIO_API_KEY` (then every endpoint except `/health` needs the header `X-API-Key`), `MEIO_ALLOWED_PRESETS` (comma-separated, default all), `MEIO_CORS_ORIGINS` (comma-separated, default `*`), `MEIO_OUTPUT_DIR` (default `output`).

**Hosting on Render.** Defined as code in [`render.yaml`](render.yaml); step-by-step runbook in [`docs/deployment_render.md`](docs/deployment_render.md). After deploying, check it with `python scripts/smoke_test.py https://<service>.onrender.com --key <key>`.

## How a run works

1. **Seeds.** Three disjoint sets of random futures (search, hold-out, test), each with demand and lead-time draws.
2. **Tune the rule.** Week-specific s and S at the DC and RMW, plus the order cap and RMW minimums:
   - start: quantiles of demand over random protection intervals;
   - **repair**: raise the levels that feed failing cells;
   - **improve**: coordinate moves that lower the mean cost while every cell stays feasible;
   - **hold-out check**: raise the safety margin of cells the search over-fitted.
3. **Lookahead.** Choose this week's orders by simulating candidates. The DC order goes first, then each RM order. A candidate may not fail any cell the rule's own quantity passes.
4. **Final verdict.** Mean fill ≥ F for every (week × channel) cell after L_max, on the untouched test seeds, with no safety margin.
5. **Baseline.** The heuristic start schedule is simulated on the same test seeds, so the report shows what the optimisation adds (cost and service).

**Fill-rate rules.**
- Search: `mean_fill − Z·SE − cell_margin ≥ F`, with Z = 2.
- Hold-out: a cell is weak if `mean_fill < F`; then `margin += max(0.005, F − mean_fill)`.
- Test: `mean_fill ≥ F`.

## Weekly mechanics (`meio/simulation.py`)

1. **Ageing.**
2. **Receipts** (enter at age 1).
3. **Ordering and release:**
   - **3a DC order:** the effective position (on hand + pipeline − expected waste) is compared with s; the order goes up to S, rounded to batch/MOQ, capped.
   - **3b Release:** the order is capped by usable RM, the capacity left in the production week (release week + τ_p, shared by all products produced that week), batch and MOQ; the rest is cancelled.
   - **3c RM transport:** oldest first; all BOM materials leave the RMW now. Material r needs τ_r weeks to production (`materials[].rmw_to_pf_lead_time`, default 0; example: 1 for every material). Production starts when the slowest has arrived (τ_p = max τ_r of the BOM); the FG reaches the DC after τ_p + the random PF → DC lead time.
   - **3d RM order:** based on the effective echelon position, or on the physical RM position falling below its minimum.
4. **Demand:** oldest age first; within an age, the tightest channel first (ties: higher F); unmet demand is lost.
5. **Fill recording.**
6. **Waste and holding cost.**

The full specification is in [`docs/model_specification_v5.md`](docs/model_specification_v5.md). The code follows its step numbers.

## Project structure

```
CLAUDE.md                  instructions for Claude Code (commands, conventions, workflow)
main.py                    one review (default: preset standard)
api.py                     web API (FastAPI) around main.run
render.yaml                Render deployment (Blueprint)
scripts/smoke_test.py      checks a deployed API end to end
rolling_demo.py            consecutive weekly reviews
meio/
  config.py                input dataclasses, policy name, presets, example instance
  io_json.py               JSON input/output (the app contract)
  scenarios.py             random futures: demand and lead times
  simulation.py            weekly simulation (steps 1-6), expected-waste projections
  policy.py                (s,S) schedule, order weeks, start schedule, committed decisions
  search.py                repair -> improve -> hold-out search
  lookahead.py             week-1 lookahead
  service.py               fill-rate cells and the three rules
  rolling.py               state update between reviews
  tables.py, report.py, excel_export.py   output
tests/test_mechanics.py    unit tests (conservation, FIFO, arrivals, rules, JSON)
tests/test_api.py          web API tests (input checks, run lifecycle, API key)
tests/test_deployment.py   render.yaml consistent with api.py and CI
examples/                  example_input.json (the app's default dataset: FG1, ~4,000 units/week) and example_summary.json (its quick run)
docs/                      specification, policy choice, app integration plan
```

## Status and roadmap

- ✅ Running model with the age-aware capped (s,S) + lookahead policy, Excel and JSON output, tests and CI.
- ⏭ Search algorithm: multi-start, parameter reduction, better optimiser. Currently the start point changes results by up to 18 %.
- ⏭ Several finished goods sharing raw materials; realistic holding costs (value × rate + storage).
- ✅ Web API (`api.py`) with Render Blueprint (`render.yaml`).
- ⏭ Lovable frontend: see [`docs/app_integration.md`](docs/app_integration.md).

## Main limitations

- (s,S)-type policy with lookahead only; no global optimality claim.
- Demand and lead times are independent over weeks; there are no disruption regimes.
- FG shelf life is independent of RM age.
- Capacity applies in the production week (release week + τ_p). RMW → PF lead times are deterministic; faster materials wait at production for the slowest one without holding cost.
- No value is given to stock left at the end of the horizon.
