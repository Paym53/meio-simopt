"""
Policy comparison: variants A, B and C across several supply-chain configurations.

    python compare_policies.py                 all configurations + rolling check (~25 min on 2 cores)
    python compare_policies.py --no-rolling    single-review comparison only

Fair comparison rules
---------------------
* Every variant is tuned with the same search settings and the SAME search, hold-out
  and test seeds (common random numbers). Cost differences are therefore paired per seed.
* A is tuned from the quantile start.
* B is tuned warm-started from A's result, with a non-binding cap (= capacity) and RMW
  minimums of 0. B's policy class contains A, so this tests whether B's extra logic adds
  value on top of a tuned (s,S). "B0" is B tuned from the quantile start (shows how
  sensitive the result is to the search start).
* C uses B's tuned rule and chooses the week-1 orders by lookahead (so C differs from B
  only in the decisions committed at this review).
* Single review: performance over the 36-week horizon on 5,000 untouched test seeds.
* Rolling check (base configuration): the week-1 decisions are executed in a simulated
  "reality" for 20 consecutive review weeks on 16 reality paths. The tuned rules are
  shifted weekly (not re-tuned), so the check isolates the effect of the policy logic.

Output: output/policy_comparison.xlsx and a printed summary.
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import replace
from multiprocessing import Pool

import numpy as np
import pandas as pd

from meio import service
from meio.config import (VARIANT_A, VARIANT_B, VARIANT_C, DemandForecast, ModelInput, SearchSettings,
                         build_example_input, validate_input)
from meio.excel_export import write_workbook
from meio.lookahead import lookahead_week1
from meio.rolling import shift_model_one_week, shift_schedule, state_after_week_one
from meio.scenarios import build_scenarios
from meio.search import Searcher
from meio.simulation import simulate

SETTINGS = SearchSettings(n_search_seeds=300, n_holdout_seeds=600, n_test_seeds=5000,
                          max_improve_passes=3, max_outer_rounds=2, trace_seeds=[])


# ---------------------------------------------------------------------------
# Configurations
# ---------------------------------------------------------------------------
def base(m: ModelInput) -> ModelInput:
    return m


def large_lots(m: ModelInput) -> ModelInput:
    """FG batch 20 -> 60 and MOQ 60 -> 180; RM batch sizes and MOQs doubled."""
    products = [replace(p, batch_size=60, moq=180) for p in m.products]
    materials = [replace(x, batch_size=2 * x.batch_size, moq=2 * x.moq) for x in m.materials]
    return replace(m, products=products, materials=materials)


def long_variable_lead_times(m: ModelInput) -> ModelInput:
    """Longer and more variable lead times on both lanes."""
    dc = {5: 0.10, 6: 0.25, 7: 0.25, 8: 0.20, 9: 0.12, 10: 0.08}
    rm = {"RM_A": {10: 0.15, 11: 0.25, 12: 0.25, 13: 0.20, 14: 0.15},
          "RM_B": {7: 0.20, 8: 0.30, 9: 0.25, 10: 0.15, 11: 0.10},
          "RM_C": {11: 0.20, 13: 0.30, 15: 0.30, 17: 0.20},
          "RM_D": {5: 0.30, 6: 0.30, 7: 0.20, 8: 0.20}}
    products = [replace(p, lead_time_dist=dc) for p in m.products]
    materials = [replace(x, lead_time_dist=rm[x.name]) for x in m.materials]
    return replace(m, products=products, materials=materials)


def short_shelf_life(m: ModelInput) -> ModelInput:
    """FG shelf life 12 -> 10 weeks (Retail accepts age <= 3, Online <= 5, Outlet <= 8)."""
    return replace(m, products=[replace(p, shelf_life=10) for p in m.products])


def high_demand_uncertainty(m: ModelInput) -> ModelInput:
    """Forecast standard deviation x 1.6."""
    sd = {k: v * 1.6 for k, v in m.demand.sd.items()}
    return replace(m, demand=DemandForecast(mean=m.demand.mean, sd=sd))


def cheap_rm_holding(m: ModelInput) -> ModelInput:
    """RM holding cost x 0.25 (raw material much cheaper to hold than finished goods)."""
    return replace(m, materials=[replace(x, holding_cost=x.holding_cost * 0.25) for x in m.materials])


CONFIGS = {
    "1 Base": (base, "Example as specified"),
    "2 Large lots": (large_lots, "FG batch 60 / MOQ 180; RM batch and MOQ doubled"),
    "3 Long variable lead times": (long_variable_lead_times, "DC lead time 5-10 wk; supplier lead times +1-3 wk and wider"),
    "4 Short FG shelf life": (short_shelf_life, "FG shelf life 10 instead of 12 weeks"),
    "5 High demand uncertainty": (high_demand_uncertainty, "forecast sd x 1.6"),
    "6 Cheap RM holding": (cheap_rm_holding, "RM holding cost x 0.25"),
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def metrics(model: ModelInput, result, schedule, variant_key: str, extra: dict) -> dict:
    weeks = model.evaluation_weeks
    p = model.products[0]
    dc = result.dc[p.name]
    cells = service.final_verdict(service.cell_table(model, result))
    total = result.total_cost_per_seed()
    comp = result.cost_per_seed()
    row = {
        "variant": {"A": "A plain", "B0": "B0 (cold start)", "B": "B age-aware capped",
                    "C": "C = B + lookahead"}[variant_key],
        "mean cost": total.mean(),
        "SE cost": total.std(ddof=1) / np.sqrt(len(total)),
        "cells pass": f"{int(cells['test_pass'].sum())}/{len(cells)}",
        "cells failing": int((~cells["test_pass"]).sum()),
        "worst cell: mean fill - F": float((cells["mean_fill"] - cells["target_F"]).min()),
        "FG holding": comp["FG holding"].mean(),
        "FG waste cost": comp["FG waste"].mean(),
        "production + PF->DC transport": (comp["Production fixed"] + comp["Production variable"]
                                          + comp["PF->DC transport"]).mean(),
        "RM purchase + order cost": (comp["RM purchase"] + comp["RM order fixed"]).mean(),
        "RM holding": comp["RM holding"].mean(),
        "RM waste cost": comp["RM waste"].mean(),
        "RMW->PF transport": comp["RMW->PF transport"].mean(),
        "FG waste % of receipts": 100 * dc["waste"][:, 1:].sum() / max(dc["receipts"][:, 1:].sum(), 1),
        "RM waste % of receipts": 100 * sum(result.rm[m.name]["waste"][:, 1:].sum() for m in model.materials)
                                  / max(sum(result.rm[m.name]["receipts"][:, 1:].sum() for m in model.materials), 1),
        "avg DC stock": dc["on_hand_end"][:, 1:].mean(),
        "avg RMW stock (all RM)": sum(result.rm[m.name]["on_hand_end"][:, 1:].mean() for m in model.materials),
        "order weeks cut by RM %": 100 * dc["cut_by_rm"][:, 1:][dc["ordered_Q"][:, 1:] > 0].mean(),
        "order weeks cut by capacity %": 100 * dc["cut_by_capacity"][:, 1:][dc["ordered_Q"][:, 1:] > 0].mean(),
        "order weeks capped by policy %": 100 * dc["cut_by_policy_cap"][:, 1:][dc["ordered_Q"][:, 1:] > 0].mean(),
        "releases per seed": (dc["released_P"][:, 1:] > 0).sum(axis=1).mean(),
        "supplier orders per seed": sum((result.rm[m.name]["ordered_O"][:, 1:] > 0).sum(axis=1).mean()
                                        for m in model.materials),
    }
    for c in p.channels:
        ch = result.channel[(p.name, c.name)]
        row[f"pooled fill {c.name}"] = ch["sales"][:, weeks].sum() / max(ch["demand"][:, weeks].sum(), 1)
    row["DC order cap"] = schedule.dc_cap.get(p.name) if variant_key != "A" else None
    row["RMW minimums"] = str(schedule.rm_floor) if variant_key != "A" else None
    row["week-1 DC order"] = int(dc["ordered_Q"][0, 1])
    row["week-1 RM orders"] = str({m.name: int(result.rm[m.name]["ordered_O"][0, 1]) for m in model.materials})
    row.update(extra)
    return row, total


def paired(diff: np.ndarray) -> tuple[float, float]:
    return float(diff.mean()), float(diff.std(ddof=1) / np.sqrt(len(diff)))


# ---------------------------------------------------------------------------
# One configuration: tune A and B, lookahead C, test all on the same seeds
# ---------------------------------------------------------------------------
def run_config(name: str) -> dict:
    modifier, _ = CONFIGS[name]
    st = SETTINGS
    model = modifier(build_example_input())
    validate_input(model)
    search = build_scenarios(model, st.n_search_seeds, st.base_seed + 1, "search")
    holdout = build_scenarios(model, st.n_holdout_seeds, st.base_seed + 2, "hold-out")
    test = build_scenarios(model, st.n_test_seeds, st.base_seed + 3, "test")

    rows, totals, schedules = [], {}, {}
    for key, variant in (("A", VARIANT_A), ("B0", VARIANT_B), ("B", VARIANT_B)):
        start = None
        if key == "B":                                   # warm start from A: B contains A
            start = schedules["A"].schedule.copy()
            start.dc_cap = {p.name: model.production_capacity for p in model.products}
            start.rm_floor = {m.name: 0 for m in model.materials}
        t0 = time.time()
        outcome = Searcher(model, st, search, holdout, verbose=False, variant=variant,
                           start_schedule=start).run()
        runtime = time.time() - t0
        if key == "B":
            runtime += rows[0]["runtime [s]"]            # B includes tuning A first
        result = simulate(model, outcome.schedule, test, variant=variant)
        row, total = metrics(model, result, outcome.schedule, key,
                             {"search evaluations": outcome.n_evaluations, "runtime [s]": round(runtime),
                              "unfixable cells in search": len(outcome.unfixable)})
        rows.append(row)
        totals[key] = total
        schedules[key] = outcome

    t0 = time.time()
    outB = schedules["B"]
    orders, rule_orders, la_log = lookahead_week1(model, outB.schedule, VARIANT_C, search, st,
                                                  outB.margins, outB.unfixable)
    runtime = time.time() - t0
    result = simulate(model, outB.schedule, test, variant=VARIANT_C, week1_orders=orders)
    row, total = metrics(model, result, outB.schedule, "C",
                         {"search evaluations": outB.n_evaluations + len(la_log),
                          "runtime [s]": round(runtime) + rows[2]["runtime [s]"],
                          "unfixable cells in search": len(outB.unfixable)})
    rows.append(row)
    totals["C"] = total

    for r in rows:
        r["configuration"] = name
    diffs = []
    for a, b in (("B0", "A"), ("B", "A"), ("C", "B"), ("C", "A")):
        mean, se = paired(totals[a] - totals[b])
        diffs.append({"configuration": name, "comparison": f"{a} - {b}", "mean cost difference": mean,
                      "SE (paired)": se, "relative %": 100 * mean / totals[b].mean(),
                      "significant (|diff| > 2 SE)": abs(mean) > 2 * se})
    la_log.insert(0, "configuration", name)
    print(f"  finished {name}", flush=True)
    return {"rows": rows, "diffs": diffs, "lookahead": la_log,
            "schedules": {k: v.schedule for k, v in schedules.items()},
            "margins": outB.margins, "unfixable": outB.unfixable}


# ---------------------------------------------------------------------------
# Rolling check (base configuration)
# ---------------------------------------------------------------------------
def rolling_path(args) -> list[dict]:
    """One reality path for one variant: execute 20 review weeks, re-deciding every week."""
    variant_key, schedule, path, n_weeks = args
    variant = {"A": VARIANT_A, "B": VARIANT_B, "C": VARIANT_C}[variant_key]
    st = SETTINGS
    model = build_example_input()
    rows = []
    for week in range(1, n_weeks + 1):
        reality = build_scenarios(model, 1, 900_000 + 1_000 * path + week, "reality")   # same for all variants
        orders = None
        if variant.lookahead:
            seeds = build_scenarios(model, 200, 500_000 + 1_000 * path + week, "lookahead")
            orders, _, _ = lookahead_week1(model, schedule, variant, seeds, st, {}, set())
        res = simulate(model, schedule, reality, trace_seeds=[0], variant=variant, week1_orders=orders)
        p = model.products[0]
        row = {"variant": variant_key, "path": path, "week": week,
               "cost": sum(v[0, 1] for v in res.cost_weekly.values()),
               "FG waste": int(res.dc[p.name]["waste"][0, 1]),
               "RM waste": int(sum(res.rm[m.name]["waste"][0, 1] for m in model.materials)),
               "DC stock end": int(res.dc[p.name]["on_hand_end"][0, 1]),
               "RMW stock end": int(sum(res.rm[m.name]["on_hand_end"][0, 1] for m in model.materials))}
        for c in p.channels:
            ch = res.channel[(p.name, c.name)]
            row[f"{c.name} demand"] = int(ch["demand"][0, 1])
            row[f"{c.name} lost"] = int(ch["lost_sales"][0, 1])
        rows.append(row)
        new_state = state_after_week_one(model, res)
        previous = model
        model = shift_model_one_week(model, new_state)
        schedule = shift_schedule(schedule, previous, model, st)
    return rows


def rolling_differences(df: pd.DataFrame, first_week: int) -> pd.DataFrame:
    """Paired differences of the mean weekly cost per path (weeks first_week+)."""
    per_path = df[df["week"] >= first_week].groupby(["variant", "path"])["cost"].mean().unstack(0)
    rows = []
    for a, b in (("B", "A"), ("C", "B"), ("C", "A")):
        d = per_path[a] - per_path[b]
        rows.append({"comparison": f"{a} - {b}", "mean weekly cost difference": d.mean(),
                     "SE (paired over paths)": d.std(ddof=1) / np.sqrt(len(d)),
                     "relative %": 100 * d.mean() / per_path[b].mean(),
                     "significant (|diff| > 2 SE)": abs(d.mean()) > 2 * d.std(ddof=1) / np.sqrt(len(d))})
    return pd.DataFrame(rows)


def summarise_rolling(df: pd.DataFrame, first_week: int) -> pd.DataFrame:
    base = build_example_input()
    out = []
    for key, g in df.groupby("variant"):
        later = g[g["week"] >= first_week]
        row = {"variant": key, "paths": g["path"].nunique(), "weeks": g["week"].max(),
               "mean cost per week (all weeks)": g["cost"].mean(),
               f"mean cost per week (weeks {first_week}+)": later["cost"].mean(),
               "FG waste per week": g["FG waste"].mean(), "RM waste per week": g["RM waste"].mean(),
               "avg DC stock": g["DC stock end"].mean(), "avg RMW stock": g["RMW stock end"].mean()}
        for c in base.products[0].channels:
            d, lost = later[f"{c.name} demand"], later[f"{c.name} lost"]
            row[f"{c.name} pooled fill (weeks {first_week}+)"] = 1 - lost.sum() / max(d.sum(), 1)
            fills = np.where(d > 0, 1 - lost / d.clip(lower=1), 1.0)
            row[f"{c.name} share of weeks >= F"] = float((fills >= c.target_fill_rate).mean())
        out.append(row)
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Holding-cost check
# ---------------------------------------------------------------------------
def holding_cost_check() -> pd.DataFrame:
    m = build_example_input()
    p = m.products[0]
    rm_value = sum(q * m.material(r).unit_cost for r, q in p.bom.items())
    fg_value = rm_value + p.production_tiers[1].unit_cost + p.transport_tiers[1].unit_cost
    rows = [{"item": p.name, "unit value (approx.)": round(fg_value, 2), "holding / unit / week": p.holding_cost,
             "holding % of value / week": round(100 * p.holding_cost / fg_value, 2),
             "holding % of value / year": round(5200 * p.holding_cost / fg_value, 1),
             "note": "value = BOM material cost + middle-tier production and transport cost"}]
    rm_per_fg = 0.0
    for r, q in p.bom.items():
        x = m.material(r)
        rm_per_fg += q * x.holding_cost
        rows.append({"item": r, "unit value (approx.)": x.unit_cost, "holding / unit / week": x.holding_cost,
                     "holding % of value / week": round(100 * x.holding_cost / x.unit_cost, 2),
                     "holding % of value / year": round(5200 * x.holding_cost / x.unit_cost, 1), "note": ""})
    rows.append({"item": "all RM for one FG unit", "unit value (approx.)": round(rm_value, 2),
                 "holding / unit / week": round(rm_per_fg, 3),
                 "holding % of value / week": round(100 * rm_per_fg / rm_value, 2),
                 "holding % of value / year": round(5200 * rm_per_fg / rm_value, 1),
                 "note": f"= {100 * rm_per_fg / p.holding_cost:.0f}% of the FG holding cost per unit"})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-rolling", action="store_true")
    parser.add_argument("--paths", type=int, default=16)
    parser.add_argument("--weeks", type=int, default=20)
    parser.add_argument("--out", default="output/policy_comparison.xlsx")
    args = parser.parse_args()
    started = time.time()

    print(f"Comparing variants A/B/C on {len(CONFIGS)} configurations "
          f"({SETTINGS.n_search_seeds} search / {SETTINGS.n_holdout_seeds} hold-out / {SETTINGS.n_test_seeds} test seeds)")
    with Pool(2) as pool:
        outputs = pool.map(run_config, list(CONFIGS))
    summary = pd.DataFrame([r for o in outputs for r in o["rows"]])
    diffs = pd.DataFrame([d for o in outputs for d in o["diffs"]])
    lookahead_log = pd.concat([o["lookahead"] for o in outputs], ignore_index=True)

    rolling_rows, rolling_summary, rolling_diffs = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    if not args.no_rolling:
        print(f"Rolling check on the base configuration: {args.paths} paths x {args.weeks} review weeks")
        base_out = outputs[0]
        jobs = [(key, base_out["schedules"]["A" if key == "A" else "B"], path, args.weeks)
                for key in ("A", "B", "C") for path in range(args.paths)]
        with Pool(2) as pool:
            parts = pool.map(rolling_path, jobs)
        rolling_rows = pd.DataFrame([r for part in parts for r in part])
        first = build_example_input().lead_time_max_global + 1
        rolling_summary = summarise_rolling(rolling_rows, first_week=first)
        rolling_diffs = rolling_differences(rolling_rows, first_week=first)

    configs = pd.DataFrame([{"configuration": k, "change vs base": v[1]} for k, v in CONFIGS.items()])
    cols = ["configuration", "variant"] + [c for c in summary.columns if c not in ("configuration", "variant")]
    summary = summary[cols]

    # ---- printed overview
    pd.set_option("display.width", 220)
    print("\n=== Mean horizon cost on test seeds (lower is better) and cells passing ===")
    view = summary.pivot(index="configuration", columns="variant", values="mean cost").round(0)
    view_pass = summary.pivot(index="configuration", columns="variant", values="cells pass")
    print(pd.concat({"mean cost": view, "cells pass": view_pass}, axis=1).to_string())
    print("\n=== Paired cost differences (same test seeds) ===")
    print(diffs.round({"mean cost difference": 1, "SE (paired)": 1, "relative %": 2}).to_string(index=False))
    if len(rolling_summary):
        print("\n=== Rolling check (base configuration) ===")
        print(rolling_summary.round(4).to_string(index=False))
        print(rolling_diffs.round(2).to_string(index=False))
    print("\n=== Holding-cost check ===")
    print(holding_cost_check().to_string(index=False))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    sheets = [
        ("01_Configurations", "Configurations compared (all other inputs as in the example)", configs),
        ("02_Summary", f"Test-seed results ({SETTINGS.n_test_seeds} seeds) per configuration and variant", summary),
        ("03_Paired_Differences", "Cost differences on the same seeds; significant if |difference| > 2 SE", diffs),
        ("04_Lookahead", "Variant C: every week-1 candidate evaluated per configuration", lookahead_log),
        ("05_Holding_Cost_Check", "Holding cost per unit and relative to unit value", holding_cost_check()),
    ]
    if len(rolling_summary):
        sheets += [("06_Rolling_Summary", "Rolling check: realised results when decisions are executed weekly",
                    rolling_summary),
                   ("07_Rolling_Differences", "Rolling check: paired weekly cost differences over reality paths",
                    rolling_diffs),
                   ("08_Rolling_Weeks", "Rolling check: one row per variant, path and review week", rolling_rows)]
    write_workbook(args.out, sheets, include_readme=False)
    print(f"\nWritten to {args.out}  (total runtime {time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
