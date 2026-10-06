# CLAUDE.md - instructions for Claude Code in this repository

## Engineering protocol (binding for every coding task)
Follow @docs/engineering_protocol.md: formulate first (objective, hard/soft constraints,
Gherkin acceptance criteria, verification tier), enforce quality through deterministic
gates, run the role pipeline, and report a verification report. The conventions below win
on any conflict.

## What this is
Simulation-optimisation model for a perishable two-echelon supply chain: suppliers → raw-material warehouse (RMW) → production (PF, no stock) → DC → sales channels.
- **Policy (the only one):** an age-aware (s,S) rule with a DC order cap and RMW minimums, tuned by simulation, plus a week-1 lookahead. No policy variants or benchmark switches; `test_only_one_policy_exists_in_the_code` enforces this.
- **Design documents:** `docs/model_specification_v5.md` and `docs/policy_choice.md`.

## Commands
```bash
pip install -r requirements.txt
python -m pytest -q                         # must pass before every commit (100+ tests)
python main.py --preset quick               # end-to-end check, ~1-2 min, writes output/run_*/
python main.py --input examples/example_input.json --preset quick
python api.py                               # web API on http://localhost:8000/docs (tests: tests/test_api.py)
python scripts/smoke_test.py <url> --key K  # end-to-end check of a deployed API (see docs/deployment_render.md)
```

## Conventions (do not break)
- **Week indexing.** Arrays over weeks use the week number as index (`x[:, t]` = week t). Index 0 is unused. Week 1 is the current review week.
- **Age indexing.** Stock arrays use age as index (`stock[:, b]` = age b). Age 1 = arrived this week.
- **Initial state.** It is the state at the start of week 1, after that week's receipts. Open orders arrive in week ≥ 2.
- **Weekly order of steps** in `simulation.simulate`: 1 ageing, 2 receipts, 3a DC order, 3b release, 3c RM transport, 3d RM order, 4 demand, 5 fill, 6 waste and holding. Keep the step comments.
- **Allocation.** Oldest age first; within an age, the channel with the tightest shelf-life requirement first (ties: higher target fill rate). Lost sales.
- **Lead times.** Random and order-preserving. Lead times are drawn per order week, which keeps common random numbers across candidate policies.
- **RMW -> PF lead time τ_r per material** (`materials[].rmw_to_pf_lead_time`, deterministic, default 0). All BOM materials leave the RMW in the release week t; production starts when the slowest has arrived, in week t+τ_p with τ_p = max τ_r over the BOM (`ModelInput.rmw_to_pf_lead_time(p)`); its capacity (booked per production week, shared across products) and closed weeks apply; FG arrives at t+τ_p+L~. Every lead-time-based quantity uses τ_p+L (`ModelInput.release_to_dc_*`, `production_week`, `policy.feeding_release_weeks`); all τ_r = 0 must reproduce the original model exactly.
- **Fill-rate rules** (`meio/service.py`), a chance constraint per cell (product × channel × week): a future (seed) *meets* the target if its fill `1 - lost/demand >= F`; `share` = futures meeting F / futures with demand; `alpha` = `target_share_of_futures` (input, default 0.98):
  - search: `share - Z*SE_share - margin >= alpha`, with `SE_share = sqrt(p(1-p)/n)`, `p = (met+1)/(n+2)`;
  - hold-out: weak if `share < alpha`, then `margin += max(bump, alpha - share)`;
  - final verdict: `share >= alpha`, with no Z and no margin; reported as "F met in x / n futures".
  - The mean fill is reported as information only. Seeds with zero demand in a cell are excluded. The evaluation window starts after the global L_max.
- **Speed-ups never change results.** Fast paths (vectorised FIFO withdrawal/allocation over ages, running pipeline totals, constants precomputed per `simulate` call, `report_details=False` in the search and lookahead) must stay bit-identical to the step-by-step versions; `tests/test_mechanics.py` compares them against reference loops. The float expected-waste projection keeps its sequential arithmetic (order of float operations matters).
- **Several products and sites.** `products[].site` names a `sites[]` entry (capacity, overrides, closed weeks per production week, shared by the site's products); without `sites` one default site `PF` is built from `production_capacity` / `capacity_overrides` and results are unchanged. Shared RM and site capacity go to products in list order. `meio/decompose.py` splits products linked by a shared site or material into independent groups and searches each on its own sub-model (`evaluation_start` keeps the global evaluation window); one group = exactly the single-model search. Future multi-item constraints (joint MOQ, shared budgets) must add a link in `independent_groups`.
- **Cut releases** still respect batch size and MOQ (round down; below the MOQ nothing is released).
- **BOM quantities** may be fractional (e.g. 0.2). RM shipped for a release = quantity × P rounded **up** to whole units, in exact integer arithmetic (`config.rm_units_for`, `config.max_fg_from`); whole-number BOMs give exactly the old results.
- **Lookahead.** A candidate may not fail any cell the rule's own quantity passes; among those, the cheapest wins.
- **Price-break round-up.** The DC order is raised to a price-break quantity (never above the cap) when that is cheaper in total (all-units production + transport tiers); `simulation.round_up_to_price_break`.
- **Search v3** (`meio/search.py`, `meio/parametric.py`, `docs/search_algorithm.md`): the levels follow the forecast from a few parameters per item (DC: z, cover, min_lot, cap, end; RM: z, cover, floor, end); s = Φ(z)-quantile of the protection-interval demand, S − s = forecast over `cover` weeks, S never above the Φ(z+end)-quantile of the demand left until H. Week-specific exceptions are sparse offsets on top (`Searcher.offsets`). Multi-start (the classic start = the reported baseline), phase A (global repair by marginal analysis, line search, repaired large moves), phase B (local repair, trim, block-move polish), hold-out rounds (margins; repair on the hold-out seeds for cells the search seeds cannot see). Confirmation seeds (an accepted move may not make a cell weak there); deferred and unfixable cells may not get worse within a phase. Parallel evaluation (`n_workers`) must give results identical to sequential (`test_parallel_search_gives_exactly_the_sequential_result`).
- **JSON contract.** `examples/example_input.json` (input; the app's default dataset, maintained by hand: FG1-FG3 at site S1 sharing materials, FG4 at site S2; the tests and `main.py` without `--input` use the smaller `build_example_input()`) and `summary.json` (output, snake_case keys) are used by a future app. Change them only on purpose, and update `meio/io_json.py`, the examples and `docs/app_integration.md` together.

## Style
- Readable, master-student-level Python: plain functions and dataclasses, clear names, docstrings that explain the logic, numpy vectorised over seeds.
- No new dependencies without a good reason; add any to `requirements.txt` and `pyproject.toml`.
- Every mechanic change needs a unit test in `tests/test_mechanics.py`. Conservation-of-units checks must stay at 0.

## Workflow
- Work on a branch and open a pull request; do not push directly to `main`.
- Run the tests and a `--preset quick` run before proposing changes.
- Update `README.md` and the docs when behaviour or outputs change.
- `output/` is git-ignored; never commit run results.
- Deployment settings live in `render.yaml` (not the Render dashboard); `tests/test_deployment.py` keeps it consistent with `api.py` and CI. Never commit API keys.

## Planned next work
1. Parallel lookahead; more parallel batches in the search.
2. Multi-item constraints (joint MOQ over several products, as a link in `decompose.independent_groups`); realistic holding costs (unit value × rate + storage).
3. Lovable frontend on top of the web API (`api.py`, deployed on Render; see `docs/app_integration.md`).
