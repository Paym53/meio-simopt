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
python -m pytest -q                         # must pass before every commit (27+ tests)
python main.py --preset quick               # end-to-end check, ~1-2 min, writes output/run_*/
python main.py --input examples/example_input.json --preset quick
```

## Conventions (do not break)
- **Week indexing.** Arrays over weeks use the week number as index (`x[:, t]` = week t). Index 0 is unused. Week 1 is the current review week.
- **Age indexing.** Stock arrays use age as index (`stock[:, b]` = age b). Age 1 = arrived this week.
- **Initial state.** It is the state at the start of week 1, after that week's receipts. Open orders arrive in week ≥ 2.
- **Weekly order of steps** in `simulation.simulate`: 1 ageing, 2 receipts, 3a DC order, 3b release, 3c RM transport, 3d RM order, 4 demand, 5 fill, 6 waste and holding. Keep the step comments.
- **Allocation.** Oldest age first; within an age, the channel with the tightest shelf-life requirement first (ties: higher target fill rate). Lost sales.
- **Lead times.** Random and order-preserving. Lead times are drawn per order week, which keeps common random numbers across candidate policies.
- **Fill-rate rules** (`meio/service.py`):
  - search: `mean - Z*SE - margin >= F`;
  - hold-out: weak if `mean < F`, then `margin += max(bump, F - mean)`;
  - final verdict: `mean >= F`, with no Z and no margin.
  - Seeds with zero demand in a cell are excluded. The evaluation window starts after the global L_max.
- **Cut releases** still respect batch size and MOQ (round down; below the MOQ nothing is released).
- **Lookahead.** A candidate may not fail any cell the rule's own quantity passes; among those, the cheapest wins.
- **JSON contract.** `examples/example_input.json` (input) and `summary.json` (output, snake_case keys) are used by a future app. Change them only on purpose, and update `meio/io_json.py`, the examples and `docs/app_integration.md` together.

## Style
- Readable, master-student-level Python: plain functions and dataclasses, clear names, docstrings that explain the logic, numpy vectorised over seeds.
- No new dependencies without a good reason; add any to `requirements.txt` and `pyproject.toml`.
- Every mechanic change needs a unit test in `tests/test_mechanics.py`. Conservation-of-units checks must stay at 0.

## Workflow
- Work on a branch and open a pull request; do not push directly to `main`.
- Run the tests and a `--preset quick` run before proposing changes.
- Update `README.md` and the docs when behaviour or outputs change.
- `output/` is git-ignored; never commit run results.

## Planned next work
1. Search algorithm: multi-start (quantile start + further start schedules), fewer parameters, a better optimiser.
2. Several finished goods sharing raw materials; realistic holding costs (unit value × rate + storage).
3. Web API (FastAPI, asynchronous runs) for a Lovable frontend (see `docs/app_integration.md`).
