# meio-simopt

Simulation-optimisation model for a **perishable two-echelon supply chain with production**:

```
Supplier r --(random lead time)--> Raw-material warehouse (RMW) --> Production (PF, no stock) --(random lead time)--> DC --> sales channels
                                   stock by age, perishable                                                          stock by age, perishable
```

Every week (review period 1 week) the model decides:
- how much finished good (FG) the DC orders from production (**Q**);
- how much is released and produced (**P**), and the raw material transported to production for it (**T**);
- how much of each raw material the RMW orders from its supplier (**O**).

Demand is given as non-stationary forecast distributions per week and channel. Lead times are random and order-preserving. Unmet demand is lost. Each channel has a minimum remaining shelf life and a per-week fill-rate target.

**Main policy (variant C).** An age-aware (s,S) rule with a DC order cap and a minimum physical RM stock, tuned by simulation-optimisation, plus a **week-1 lookahead** that chooses the orders committed now by simulating candidate quantities from the current state. Why this policy: see [`docs/policy_choice.md`](docs/policy_choice.md).

## Quick start

Requires Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python main.py --preset quick      # ~1-2 min, example input
python main.py                     # preset "standard", ~3 min
python -m pytest -q                # 23 tests
```

Each run creates a folder `output/run_<timestamp>/` containing:
- `results.xlsx`: 25 sheets with inputs, policy, decisions, service per cell, costs, KPIs, and full traces (stock by age, flows, orders) for 3 seeds;
- `summary.json`: the same results as plain JSON, meant for apps and APIs;
- `input.json`: the exact input used.

## Command-line options

| Option | Meaning |
|---|---|
| `--input file.json` | Your own input (format: `examples/example_input.json`, described in `meio/io_json.py`) |
| `--preset quick\|standard\|full` | Seed counts and search effort (200/400/2,000 … 500/1,000/10,000 seeds) |
| `--variant C\|B\|A` | C = main policy; B without lookahead; A = plain (s,S) (benchmarks) |
| `--out-dir`, `--name` | Where the run folder is written |

Other scripts:
- `python compare_policies.py`: compares A/B/C on 6 configurations (about 25 min on 2 cores).
- `python rolling_demo.py`: 3 consecutive weekly reviews with state updates.
- `python -m meio.io_json examples/example_input.json`: writes the example input.

## How a run works

1. **Seeds.** Three disjoint sets of random futures (search, hold-out, test), each with demand and lead-time draws.
2. **Tune the rule.** Week-specific s and S at the DC and RMW, plus the order cap and RMW minimums:
   - start: quantiles of demand over random protection intervals;
   - **repair**: raise the levels that feed failing cells;
   - **improve**: coordinate moves that lower the mean cost while every cell stays feasible;
   - **hold-out check**: raise the safety margin of cells the search over-fitted.
3. **Lookahead.** Choose this week's orders by simulating candidates. The DC order goes first, then each RM order. A candidate may not fail any cell the rule's own quantity passes.
4. **Final verdict.** Mean fill ≥ F for every (week × channel) cell after L_max, on the untouched test seeds, with no safety margin.

**Fill-rate rules.**
- Search: `mean_fill − Z·SE − cell_margin ≥ F`, with Z = 2.
- Hold-out: a cell is weak if `mean_fill < F`; then `margin += max(0.005, F − mean_fill)`.
- Test: `mean_fill ≥ F`.

## Weekly mechanics (`meio/simulation.py`)

1. **Ageing.**
2. **Receipts** (enter at age 1).
3. **Ordering and release:**
   - **3a DC order:** the effective position (on hand + pipeline − expected waste) is compared with s; the order goes up to S, rounded to batch/MOQ, capped.
   - **3b Release:** the order is capped by usable RM, capacity, batch and MOQ; the rest is cancelled.
   - **3c RM transport:** oldest first, consumed at the PF.
   - **3d RM order:** based on the effective echelon position, or on the physical RM position falling below its minimum.
4. **Demand:** oldest age first; within an age, the tightest channel first (ties: higher F); unmet demand is lost.
5. **Fill recording.**
6. **Waste and holding cost.**

The full specification is in [`docs/model_specification_v5.md`](docs/model_specification_v5.md). The code follows its step numbers.

## Project structure

```
CLAUDE.md                  instructions for Claude Code (commands, conventions, workflow)
main.py                    one review (default: policy C, preset standard)
compare_policies.py        policy comparison A/B/C
rolling_demo.py            consecutive weekly reviews
meio/
  config.py                input dataclasses, policy variants, presets, example instance
  io_json.py               JSON input/output (the app contract)
  scenarios.py             random futures: demand and lead times
  simulation.py            weekly simulation (steps 1-6), expected-waste projections
  policy.py                (s,S) schedule, order weeks, start schedule, committed decisions
  search.py                repair -> improve -> hold-out search
  lookahead.py             week-1 lookahead (policy C)
  service.py               fill-rate cells and the three rules
  rolling.py               state update between reviews
  tables.py, report.py, excel_export.py   output
tests/test_mechanics.py    unit tests (conservation, FIFO, arrivals, rules, JSON)
examples/                  example_input.json (input format) and example_summary.json (output of a quick run)
docs/                      specification, policy choice, app integration plan
```

## Status and roadmap

- ✅ Running model with policy C, Excel and JSON output, tests and CI.
- ⏭ Search algorithm: multi-start, parameter reduction, better optimiser. Currently the start point changes results by up to 18 %.
- ⏭ Several finished goods sharing raw materials; realistic holding costs (value × rate + storage).
- ⏭ Web API and Lovable frontend: see [`docs/app_integration.md`](docs/app_integration.md).

## Main limitations

- (s,S)-type policy with lookahead only; no global optimality claim.
- Demand and lead times are independent over weeks; there are no disruption regimes.
- FG shelf life is independent of RM age.
- Capacity applies in the release week.
- No value is given to stock left at the end of the horizon.
