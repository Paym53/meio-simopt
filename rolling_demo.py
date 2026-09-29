"""
Rolling-horizon demonstration: re-optimise at every review week.

    python rolling_demo.py              3 review weeks with small seed sets
    python rolling_demo.py --weeks 5

Policy: variant C (age-aware capped (s,S) + week-1 lookahead), the main policy of the model.

Each review:  tune the rule -> choose week-1 orders by lookahead -> commit -> one "real" week happens
(simulated with an independent reality seed) -> read the new state -> shift one week.
Output: a printed table and output/rolling_demo.xlsx.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from meio.config import VARIANT_C, build_example_input, settings_for_preset, validate_input
from meio.excel_export import write_workbook
from meio.lookahead import lookahead_week1
from meio.policy import committed_decisions
from meio.rolling import shift_model_one_week, shift_schedule, state_after_week_one
from meio.scenarios import build_scenarios
from meio.search import Searcher
from meio.simulation import simulate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=3, help="number of review weeks")
    parser.add_argument("--out", default="output/rolling_demo.xlsx")
    args = parser.parse_args()

    settings = settings_for_preset("quick")
    model = build_example_input()
    warm_start, previous_model = None, None
    review_rows, decision_rows, state_rows = [], [], []

    for review in range(1, args.weeks + 1):
        validate_input(model)
        print(f"\n===== Review week {review} =====")
        if warm_start is not None:
            warm_start = shift_schedule(warm_start, previous_model, model, settings)

        search = build_scenarios(model, settings.n_search_seeds, settings.base_seed + 1 + 100 * review, "search")
        holdout = build_scenarios(model, settings.n_holdout_seeds, settings.base_seed + 2 + 100 * review, "hold-out")
        outcome = Searcher(model, settings, search, holdout, start_schedule=warm_start, variant=VARIANT_C).run()
        orders, rule_orders, _ = lookahead_week1(model, outcome.schedule, VARIANT_C, search, settings,
                                                 outcome.margins, outcome.unfixable)

        # the "real" week: one independent seed, detailed trace for the state update
        reality_seed = build_scenarios(model, 1, 777_000 + review, "reality")
        reality = simulate(model, outcome.schedule, reality_seed, trace_seeds=[0], variant=VARIANT_C,
                           week1_orders=orders)

        for row in committed_decisions(model, outcome.schedule, reality, rule_orders):
            decision_rows.append({"review_week": review, **row})
        p = model.products[0]
        dc = reality.dc[p.name]
        realised = {"review_week": review,
                    "search mean cost (horizon)": round(outcome.last_search_eval.cost),
                    "FG ordered Q": int(dc["ordered_Q"][0, 1]), "FG released P": int(dc["released_P"][0, 1]),
                    "real demand": int(dc["demand"][0, 1]), "real sales": int(dc["sales"][0, 1]),
                    "real lost sales": int(dc["lost_sales"][0, 1]), "FG waste": int(dc["waste"][0, 1]),
                    "DC stock end of week": int(dc["on_hand_end"][0, 1]),
                    "real cost of the week": round(sum(v[0, 1] for v in reality.cost_weekly.values()), 2)}
        for m in model.materials:
            realised[f"{m.name} ordered O"] = int(reality.rm[m.name]["ordered_O"][0, 1])
            realised[f"{m.name} stock end of week"] = int(reality.rm[m.name]["on_hand_end"][0, 1])
        review_rows.append(realised)

        new_state = state_after_week_one(model, reality)
        for name, ages in new_state.dc_stock.items():
            state_rows.append({"review_week": review + 1, "location": "DC", "item": name,
                               "on hand by age": str(ages), "total": sum(ages.values()),
                               "open orders (order week, qty)": str(new_state.dc_pipeline[name])})
        for name, ages in new_state.rm_stock.items():
            state_rows.append({"review_week": review + 1, "location": "RMW", "item": name,
                               "on hand by age": str(ages), "total": sum(ages.values()),
                               "open orders (order week, qty)": str(new_state.rm_pipeline[name])})

        previous_model = model
        model = shift_model_one_week(model, new_state)
        warm_start = outcome.schedule

    summary = pd.DataFrame(review_rows)
    print("\n===== Rolling summary =====")
    print(summary.to_string(index=False))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    write_workbook(args.out, [
        ("01_Reviews", "One row per review week: committed decisions and what really happened", summary),
        ("02_Decisions", "Committed week-1 decisions per review", pd.DataFrame(decision_rows)),
        ("03_States", "State read at the start of each following review", pd.DataFrame(state_rows)),
    ], include_readme=False)
    print(f"\nWritten to {args.out}")


if __name__ == "__main__":
    main()
