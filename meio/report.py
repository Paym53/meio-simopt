"""
Console overview of a run.
"""
from __future__ import annotations

import pandas as pd

from .config import ModelInput, SearchSettings

LINE = "=" * 78


def _title(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


def print_inputs(model: ModelInput, settings: SearchSettings) -> None:
    _title("MEIO SIMULATION-OPTIMISATION  -  input overview")
    print(f"Horizon: {model.horizon} weeks | L_max = {model.lead_time_max_global} | "
          f"evaluation window: weeks {model.evaluation_weeks[0]}-{model.evaluation_weeks[-1]}")
    print(f"Seeds: search {settings.n_search_seeds}, hold-out {settings.n_holdout_seeds}, "
          f"test {settings.n_test_seeds} | Z = {settings.z}")
    for p in model.products:
        print(f"\nFinished good {p.name}: shelf life {p.shelf_life} wk, batch {p.batch_size}, MOQ {p.moq}, "
              f"RMW -> PF {model.rmw_to_pf_lead_time(p)} wk (slowest BOM material), "
              f"PF -> DC lead time {p.lead_time_min}-{p.lead_time_max} wk (release -> DC "
              f"{model.release_to_dc_min(p)}-{model.release_to_dc_max(p)} wk), closed weeks {p.closed_production_weeks}")
        for i in p.channel_priority():
            c = p.channels[i]
            print(f"   channel {c.name:<8} F = {c.target_fill_rate:.2f}, accepts age <= {p.max_age_for_channel(c)}")
        print(f"   BOM: " + ", ".join(f"{q} x {m}" for m, q in p.bom.items()))
    print("\nRaw materials:")
    for m in model.materials:
        print(f"   {m.name}: shelf life {m.shelf_life} wk, batch {m.batch_size}, MOQ {m.moq}, "
              f"supplier lead time {m.lead_time_min}-{m.lead_time_max} wk, RMW -> PF {m.rmw_to_pf_lead_time} wk")


def print_results(model: ModelInput, decisions: pd.DataFrame, schedule_df: pd.DataFrame,
                  cells: pd.DataFrame, costs: pd.DataFrame, kpis: pd.DataFrame,
                  checks: pd.DataFrame, run_info: dict, excel_path: str) -> None:
    _title("SEARCH")
    for k, v in run_info.items():
        print(f"{k:<38} {v}")

    _title("DECISIONS TO COMMIT NOW (week 1)")
    cols = ["Decision", "Item", "Position", "Expected waste", "Effective position", "s (week 1)",
            "S (week 1)", "Rule quantity", "Committed quantity"]
    table = decisions[cols].astype(object)
    print(table.where(table.notna(), "").to_string(index=False))      # blank instead of <NA>
    print("\nPosition = stock on hand + on order (RMW: echelon incl. RM inside FG). Effective = position -")
    print("expected waste. Rule quantity = what the (s,S) rule orders; committed = chosen by the lookahead.")

    _title("OPTIMISED (s,S) SCHEDULE - first 10 weeks (full schedule in Excel)")
    keep = ["week"] + [c for c in schedule_df.columns if "(optimised)" in c]
    short = schedule_df[keep].head(10).rename(columns=lambda c: c.replace(" (optimised)", ""))
    print(short.to_string(index=False, na_rep=""))

    alpha = cells["target_share"].iloc[0] if len(cells) else 0.0
    _title(f"SERVICE - final verdict on untouched test seeds: fill >= F in >= {alpha:.0%} of futures per cell")
    for (p, c), grp in cells.groupby(["product", "channel"], sort=False):
        passed = int(grp["test_pass"].sum())
        worst = grp.loc[grp["test_share_met"].idxmin()]
        print(f"{p} {c:<8} F {grp['target_F'].iloc[0]:.2f}: {passed}/{len(grp)} cells pass | worst week "
              f"{int(worst['week'])}: F met in {int(worst['test_futures_meeting_F'])}/"
              f"{int(worst['test_seeds_with_demand'])} futures ({worst['test_share_met']:.2%}), "
              f"mean fill {worst['test_mean_fill']:.4f}")
    failed = cells[~cells["test_pass"]]
    if len(failed):
        print("\nFailed cells:")
        print(failed[["product", "channel", "week", "target_F", "test_futures_meeting_F", "test_seeds_with_demand",
                      "test_share_met", "test_mean_fill", "declared_unfixable_in_search"]].to_string(index=False))
    else:
        print("\nAll cells pass.")

    _title("COST over the horizon - mean per seed (test seeds)")
    show = costs[["cost component", "mean", "share of total", "P5", "P95"]].copy()
    show["mean"] = show["mean"].map("{:,.0f}".format)
    show["share of total"] = show["share of total"].map("{:.1%}".format)
    show["P5"] = show["P5"].map("{:,.0f}".format)
    show["P95"] = show["P95"].map("{:,.0f}".format)
    print(show.to_string(index=False))

    _title("KPIs (test seeds)")
    show = kpis.copy()
    show["value"] = show["value"].map(lambda v: f"{v:,.4f}" if isinstance(v, float) and v < 1.5 else f"{v:,.1f}")
    print(show.to_string(index=False))

    _title("CONSISTENCY CHECKS")
    print(checks.to_string(index=False))
    print(f"\nResults written to: {excel_path}\n")
