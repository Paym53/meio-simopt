"""
Run one review of the MEIO simulation-optimisation model.

    python main.py                                 example input, preset "standard", policy C
    python main.py --preset quick                  fast run (~1-2 min)
    python main.py --preset full                   more seeds (~5-6 min)
    python main.py --input my_case.json            your own input (format: meio/io_json.py)

Policy (the only one in the model):
    age-aware (s,S) rule with DC order cap and RMW minimum, tuned by simulation-optimisation,
    plus a week-1 lookahead that chooses the orders committed now by simulating
    candidate quantities from the current state.

Steps:
    1. read and check the input
    2. draw three disjoint seed sets (search, hold-out, test)
    3. tune the week-specific (s,S) schedule (repair -> improve -> hold-out check)
    4. choose the week-1 orders by lookahead
    5. final verdict on the untouched test seeds
    6. print the overview and write  output/<run>/results.xlsx, summary.json, input.json
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import asdict
from datetime import datetime

import pandas as pd

from meio import report, service, tables
from meio.config import (POLICY_NAME, PRESETS, ModelInput, SearchSettings, build_example_input, settings_for_preset,
                         validate_input)
from meio.excel_export import write_workbook
from meio.io_json import (build_summary, load_model, model_to_dict, policy_dict, records, save_json,
                          service_by_channel)
from meio.lookahead import lookahead_week1
from meio.policy import PolicySchedule, committed_decisions
from meio.scenarios import ScenarioSet, build_scenarios
from meio.search import Searcher
from meio.simulation import simulate


def evaluate_baseline(model: ModelInput, schedule: PolicySchedule, test_seeds: ScenarioSet) -> dict:
    """The heuristic start schedule of the search (quantile-based (s,S), initial order cap and
    RMW minimum), simulated on the same test seeds as the optimised schedule, with the rule's
    own week-1 orders (no lookahead). Same policy rule, heuristic parameters: this shows what
    the optimisation adds. Returns plain data for summary.json."""
    result = simulate(model, schedule, test_seeds)
    cells = service.final_verdict(service.cell_table(model, result)).rename(columns={"mean_fill": "test_mean_fill"})
    return {
        "description": "Heuristic start schedule of the search (demand quantiles over the lead time incl. "
                       "safety via the quantile, one extra week of cover for S), same test seeds, "
                       "week-1 orders from the rule (no lookahead)",
        "policy": policy_dict(model, schedule),
        "mean_cost_over_horizon_test_seeds": result.mean_total_cost(),
        "test_cells_passing": f"{int(cells['test_pass'].sum())} / {len(cells)}",
        "costs": records(tables.cost_table(result)),
        "service_by_channel": service_by_channel(cells),
        "kpis": records(tables.kpi_table(model, result)),
    }


def run(model: ModelInput, settings: SearchSettings, run_dir: str, preset_name: str = "") -> dict:
    """Run one review and write all outputs to run_dir. Returns the summary dict."""
    started = time.time()
    validate_input(model)
    os.makedirs(run_dir, exist_ok=True)
    save_json(model_to_dict(model), os.path.join(run_dir, "input.json"))
    report.print_inputs(model, settings)

    # 2. seed sets (different random streams -> disjoint random futures)
    search_seeds = build_scenarios(model, settings.n_search_seeds, settings.base_seed + 1, "search")
    holdout_seeds = build_scenarios(model, settings.n_holdout_seeds, settings.base_seed + 2, "hold-out")
    test_seeds = build_scenarios(model, settings.n_test_seeds, settings.base_seed + 3, "test")

    # 3. tune the (s,S) schedule
    print(f"\nPolicy: {POLICY_NAME}")
    print("Tuning the (s,S) schedule ...")
    outcome = Searcher(model, settings, search_seeds, holdout_seeds).run()

    # 4. choose the committed week-1 orders by simulated lookahead
    print("Week-1 lookahead ...")
    week1_orders, rule_orders, lookahead_log = lookahead_week1(
        model, outcome.schedule, search_seeds, settings, outcome.margins, outcome.unfixable)
    search_time = time.time() - started

    # 5. final verdict on the test seeds (with full trace for a few seeds)
    test_result = simulate(model, outcome.schedule, test_seeds, trace_seeds=settings.trace_seeds,
                           week1_orders=week1_orders)
    test_cells = service.final_verdict(service.cell_table(model, test_result))
    cells = tables.service_cells_table(outcome.last_search_eval.cells, outcome.last_holdout_cells,
                                       test_cells, outcome.unfixable)

    # 6. tables, overview, outputs
    decisions = pd.DataFrame(committed_decisions(model, outcome.schedule, test_result, rule_orders))
    for col in ("Position", "Expected waste", "Effective position", "s (week 1)", "S (week 1)",
                "Rule quantity", "Committed quantity"):
        decisions[col] = decisions[col].astype("Int64")
    schedule_df = tables.schedule_table(model, outcome.start_schedule, outcome.schedule)
    costs = tables.cost_table(test_result)
    kpis = tables.kpi_table(model, test_result)
    checks = tables.conservation_checks(model, test_result)
    weekly = tables.weekly_means_table(model, test_result)
    bands = tables.weekly_band_series(model, test_result)
    margins = pd.DataFrame([{"product": p, "channel": c, "week": w, "final_cell_margin": v}
                            for (p, c, w), v in sorted(outcome.margins.items())])
    holdout = pd.concat(outcome.holdout_rounds, ignore_index=True) if outcome.holdout_rounds else pd.DataFrame()
    if len(margins) and len(holdout):
        holdout = holdout.merge(margins, on=["product", "channel", "week"], how="left")

    run_info = {
        "run folder": run_dir,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "policy": POLICY_NAME,
        "preset": preset_name or "custom",
        "seeds (search / hold-out / test)": f"{settings.n_search_seeds} / {settings.n_holdout_seeds} / "
                                            f"{settings.n_test_seeds}",
        "simulated schedules (search evaluations)": outcome.n_evaluations + len(lookahead_log),
        "runtime search + lookahead [s]": round(search_time, 1),
        "search feasible at the end": outcome.last_search_eval.feasible,
        "cells declared unfixable during search": len(outcome.unfixable),
        "mean cost start schedule (search seeds)": round(outcome.search_log[0]["mean_cost"]),
        "mean cost final schedule (search seeds)": round(outcome.last_search_eval.cost),
        "mean cost over horizon (test seeds)": round(test_result.mean_total_cost()),
        "test cells passing": f"{int(cells['test_pass'].sum())} / {len(cells)}",
    }
    for name, cap in outcome.schedule.dc_cap.items():
        run_info[f"DC order cap {name}"] = cap
    for name, floor in outcome.schedule.rm_floor.items():
        run_info[f"RMW minimum physical stock {name}"] = floor

    excel_path = os.path.join(run_dir, "results.xlsx")
    report.print_results(model, decisions, schedule_df, cells, costs, kpis, checks, run_info, excel_path)

    trace_seeds = [k for k in settings.trace_seeds if k < test_seeds.n_seeds]
    tr = test_result.trace
    products_df, channels_df = tables.products_table(model)
    sheets = [
        ("01_Settings", "Model and search settings, run information",
         tables.settings_table(model, settings, run_info)),
        ("02_Products", "Finished goods", products_df),
        ("03_Channels", "Sales channels per finished good", channels_df),
        ("04_Materials", "Raw materials", tables.materials_table(model)),
        ("05_Cost_Tiers", "All-units discount tiers (rate of the band applies to all units)", tables.tiers_table(model)),
        ("06_Lead_Times", "Lead-time distributions (random, order-preserving)", tables.lead_time_table(model)),
        ("07_Demand_Forecast", "Forecast distribution per week (negative binomial with this mean and sd)",
         tables.demand_table(model)),
        ("08_Initial_State", "State at the start of week 1 (after this week's receipts)",
         tables.initial_state_table(model)),
        ("09_Policy_Schedule", "Week-specific (s,S) levels; empty = no ordering possible in that week",
         schedule_df),
        ("10_Committed_Week1", "Decisions to execute now (identical in all seeds, because the current state is known)",
         decisions),
        ("11_Service_Cells", "Fill-rate cells: search rule, hold-out mean, final test verdict", cells),
        ("12_Holdout_Margins", "Weak cells per round (hold-out mean fill < F) and the margins applied", holdout),
        ("13_Costs", f"Cost per component over {test_seeds.n_seeds} test seeds", costs),
        ("14_KPIs", "Key performance indicators on the test seeds", kpis),
        ("15_Weekly_Means", "Mean over all test seeds per week", weekly),
        ("16_Trace_Weekly", f"Test seeds {trace_seeds}: all flows and positions per week",
         tables.trace_weekly_table(model, outcome.schedule, test_result, trace_seeds)),
        ("17_Trace_DC_Ages", "DC stock by age: start = after ageing and receipts, end = after sales and waste",
         pd.DataFrame(tr.dc_ages)),
        ("18_Trace_Sales_Age", "Units sold per channel and age", pd.DataFrame(tr.sales_by_age)),
        ("19_Trace_RM_Ages", "RMW stock by age: start = after ageing and receipts, end = after shipment and waste",
         pd.DataFrame(tr.rm_ages)),
        ("20_Trace_RM_Shipped", "RM shipped to production by age", pd.DataFrame(tr.shipped_by_age)),
        ("21_Trace_Flows", "Material flows between locations (filter by seed, week, item)",
         pd.DataFrame(tr.flows)),
        ("22_Trace_Orders", "Order log with lead-time draws and order-preserving arrivals",
         pd.DataFrame(tr.orders).sort_values(["seed", "lane", "item", "order_week"])),
        ("23_Checks", "Unit balances over all test seeds", checks),
        ("24_Search_Log", "Accepted search steps", pd.DataFrame(outcome.search_log)),
        ("25_Lookahead", "Week-1 lookahead: every candidate quantity, evaluated on the search seeds", lookahead_log),
    ]
    write_workbook(excel_path, sheets)

    # baseline on the same test seeds; the optimised result is released first to save memory
    del test_result, tr, sheets
    baseline = evaluate_baseline(model, outcome.start_schedule, test_seeds)
    print(f"Baseline (heuristic start schedule, same test seeds): mean cost "
          f"{baseline['mean_cost_over_horizon_test_seeds']:,.0f}, cells passing {baseline['test_cells_passing']}"
          f"  |  optimised: {run_info['mean cost over horizon (test seeds)']:,.0f}, {run_info['test cells passing']}")

    summary = build_summary(model, run_info, decisions, cells, costs, kpis, outcome.schedule, weekly,
                            weekly_bands=bands, baseline=baseline, settings=asdict(settings))
    save_json(summary, os.path.join(run_dir, "summary.json"))
    print(f"Also written: {os.path.join(run_dir, 'summary.json')} and input.json")
    print(f"Total runtime: {time.time() - started:.1f} s")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="MEIO simulation-optimisation - one review")
    parser.add_argument("--input", help="input JSON file (default: built-in example)")
    parser.add_argument("--preset", default="standard", choices=list(PRESETS), help="seed counts and search effort")
    parser.add_argument("--out-dir", default="output", help="folder for the run folders")
    parser.add_argument("--name", help="name of the run folder (default: run_<timestamp>)")
    args = parser.parse_args()

    model = load_model(args.input) if args.input else build_example_input()
    settings = settings_for_preset(args.preset)
    run_name = args.name or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run(model, settings, os.path.join(args.out_dir, run_name), args.preset)


if __name__ == "__main__":
    main()
