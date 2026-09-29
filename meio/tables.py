"""
Tables for the report and the Excel export.

Every function returns a pandas DataFrame, so the same tables can be printed,
checked in tests, or written to Excel.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from .config import ModelInput, SearchSettings
from .policy import PolicySchedule, dc_order_weeks, rm_order_weeks
from .simulation import CHANNEL_SERIES, DC_SERIES, RM_SERIES, SimResult


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def settings_table(model: ModelInput, settings: SearchSettings, extra: dict) -> pd.DataFrame:
    rows = [
        ("Horizon H [weeks]", model.horizon, "simulated weeks, week 1 = current review week"),
        ("L_max (global) [weeks]", model.lead_time_max_global, "largest possible DC lead time over all products"),
        ("Evaluation window", f"weeks {model.evaluation_weeks[0]}-{model.evaluation_weeks[-1]}",
         "service is only evaluated after L_max weeks"),
        ("Review period R [weeks]", 1, "every week is a review week"),
        ("Production capacity [FG/week]", model.production_capacity, "shared by all products"),
        ("Capacity overrides", str(model.capacity_overrides or "none"), "week -> capacity"),
        ("Search seeds", settings.n_search_seeds, "used to accept or reject moves"),
        ("Hold-out seeds", settings.n_holdout_seeds, "used to find weak cells"),
        ("Test seeds", settings.n_test_seeds, "untouched, used once for the final verdict"),
        ("Z (safety multiplier)", settings.z, "search rule: mean - Z*SE - margin >= F"),
        ("Minimum margin bump", settings.min_margin_bump, "weak cell: margin += max(bump, F - hold-out mean)"),
        ("Max outer rounds", settings.max_outer_rounds, "repair -> improve -> hold-out check"),
        ("Max improve passes", settings.max_improve_passes, ""),
        ("Step fraction (start / min)", f"{settings.step_fraction} / {settings.min_step_fraction}",
         "move size as fraction of the level, at least one batch"),
        ("Cut-share threshold", settings.cut_share_threshold,
         "share of seeds with RM-limited releases that makes the repair raise RMW levels"),
        ("Initial quantile q0", settings.initial_quantile or "max target F of the item", "start schedule"),
        ("Initial extra cover m0 [weeks]", settings.initial_extra_cover_weeks, "start schedule, S only"),
        ("Base random seed", settings.base_seed, "search = base+1, hold-out = base+2, test = base+3"),
        ("Trace seeds (test set)", str(settings.trace_seeds), "seeds shown in full detail"),
    ]
    rows += [(k, v, "") for k, v in extra.items()]
    return pd.DataFrame(rows, columns=["Setting", "Value", "Explanation"])


def products_table(model: ModelInput) -> tuple[pd.DataFrame, pd.DataFrame]:
    prod_rows, ch_rows = [], []
    for p in model.products:
        prod_rows.append({
            "product": p.name, "shelf_life_A": p.shelf_life, "max_sellable_age": p.max_sellable_age,
            "BOM": ", ".join(f"{q} x {m}" for m, q in p.bom.items()),
            "batch_size": p.batch_size, "MOQ": p.moq, "holding_cost/unit/week": p.holding_cost,
            "waste_cost/unit": p.waste_cost, "fixed_cost/release": p.fixed_cost_per_release,
            "lead_time_min": p.lead_time_min, "lead_time_max": p.lead_time_max,
            "closed_production_weeks": str(p.closed_production_weeks),
        })
        priority = p.channel_priority()
        for i, c in enumerate(p.channels):
            ch_rows.append({
                "product": p.name, "channel": c.name, "target_fill_rate_F": c.target_fill_rate,
                "min_remaining_life_rho": c.min_remaining_life,
                "max_accepted_age (A - rho)": p.max_age_for_channel(c),
                "service_priority (1 = first)": priority.index(i) + 1,
            })
    return pd.DataFrame(prod_rows), pd.DataFrame(ch_rows)


def materials_table(model: ModelInput) -> pd.DataFrame:
    rows = []
    for m in model.materials:
        rows.append({
            "material": m.name, "used_in": ", ".join(f"{p.name} ({p.bom[m.name]}/unit)" for p in model.products_using(m.name)),
            "shelf_life_A_RM": m.shelf_life, "min_life_at_shipment": m.min_life_at_shipment,
            "max_shippable_age": m.max_shippable_age, "batch_size": m.batch_size, "MOQ": m.moq,
            "unit_cost": m.unit_cost, "fixed_order_cost": m.fixed_order_cost,
            "holding_cost/unit/week": m.holding_cost, "waste_cost/unit": m.waste_cost,
            "transport_cost RMW->PF/unit": m.transport_cost,
            "supplier_capacity/order": m.supplier_capacity if m.supplier_capacity else "unlimited",
            "lead_time_min": m.lead_time_min, "lead_time_max": m.lead_time_max,
            "closed_order_weeks": str(m.closed_order_weeks),
        })
    return pd.DataFrame(rows)


def tiers_table(model: ModelInput) -> pd.DataFrame:
    rows = []
    for p in model.products:
        for label, tiers in (("Production (conversion)", p.production_tiers), ("Transport PF->DC", p.transport_tiers)):
            for g, tier in enumerate(tiers, start=1):
                rows.append({"product": p.name, "cost_type": label, "tier": g, "lower_qty": tier.lower,
                             "upper_qty": tier.upper, "unit_cost (all units)": tier.unit_cost})
    return pd.DataFrame(rows)


def lead_time_table(model: ModelInput) -> pd.DataFrame:
    rows = []
    for p in model.products:
        for weeks, prob in sorted(p.lead_time_dist.items()):
            rows.append({"lane": "PF -> DC (release to usable at DC)", "item": p.name,
                         "lead_time_weeks": weeks, "probability": prob})
    for m in model.materials:
        for weeks, prob in sorted(m.lead_time_dist.items()):
            rows.append({"lane": "Supplier -> RMW (order to usable at RMW)", "item": m.name,
                         "lead_time_weeks": weeks, "probability": prob})
    return pd.DataFrame(rows)


def demand_table(model: ModelInput) -> pd.DataFrame:
    rows = []
    for p in model.products:
        for c in p.channels:
            mean = model.demand.mean[(p.name, c.name)]
            sd = model.demand.sd[(p.name, c.name)]
            for t in range(1, model.demand.last_week + 1):
                rows.append({"product": p.name, "channel": c.name, "week": t,
                             "in_horizon": t <= model.horizon, "mean": mean[t], "sd": sd[t],
                             "cv": round(sd[t] / mean[t], 3) if mean[t] > 0 else None})
    return pd.DataFrame(rows)


def initial_state_table(model: ModelInput) -> pd.DataFrame:
    s = model.initial_state
    rows = []
    for name, by_age in s.dc_stock.items():
        for age, qty in sorted(by_age.items()):
            rows.append({"location": "DC", "type": "on hand", "item": name, "age": age,
                         "order_week": None, "quantity": qty})
    for name, by_age in s.rm_stock.items():
        for age, qty in sorted(by_age.items()):
            rows.append({"location": "RMW", "type": "on hand", "item": name, "age": age,
                         "order_week": None, "quantity": qty})
    for name, orders in s.dc_pipeline.items():
        for week, qty in orders:
            rows.append({"location": "PF -> DC", "type": "open release", "item": name, "age": None,
                         "order_week": week, "quantity": qty})
    for name, orders in s.rm_pipeline.items():
        for week, qty in orders:
            rows.append({"location": "Supplier -> RMW", "type": "open order", "item": name, "age": None,
                         "order_week": week, "quantity": qty})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Policy and decisions
# ---------------------------------------------------------------------------
def schedule_table(model: ModelInput, start: PolicySchedule, final: PolicySchedule) -> pd.DataFrame:
    rows = []
    for t in range(1, model.horizon + 1):
        row = {"week": t}
        for p in model.products:
            active = t in dc_order_weeks(model, p)
            row[f"DC {p.name} may order"] = active
            row[f"DC {p.name} s (start)"] = int(start.dc_s[p.name][t]) if active else None
            row[f"DC {p.name} S (start)"] = int(start.dc_S[p.name][t]) if active else None
            row[f"DC {p.name} s (optimised)"] = int(final.dc_s[p.name][t]) if active else None
            row[f"DC {p.name} S (optimised)"] = int(final.dc_S[p.name][t]) if active else None
        for m in model.materials:
            active = t in rm_order_weeks(model, m)
            row[f"RMW {m.name} may order"] = active
            row[f"RMW {m.name} s (start)"] = int(start.rm_s[m.name][t]) if active else None
            row[f"RMW {m.name} S (start)"] = int(start.rm_S[m.name][t]) if active else None
            row[f"RMW {m.name} s (optimised)"] = int(final.rm_s[m.name][t]) if active else None
            row[f"RMW {m.name} S (optimised)"] = int(final.rm_S[m.name][t]) if active else None
        rows.append(row)
    df = pd.DataFrame(rows)
    level_cols = [c for c in df.columns if c.endswith(")")]
    df[level_cols] = df[level_cols].astype("Int64")        # integers, empty where no ordering
    return df


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def service_cells_table(search_cells: pd.DataFrame, holdout_cells: pd.DataFrame,
                        test_cells: pd.DataFrame, unfixable: set) -> pd.DataFrame:
    keys = ["product", "channel", "week"]
    s = search_cells[keys + ["target_F", "mean_fill", "se", "cell_margin", "search_lower_bound",
                             "search_feasible"]].rename(columns={"mean_fill": "search_mean_fill",
                                                                 "se": "search_se"})
    h = holdout_cells[keys + ["mean_fill"]].rename(columns={"mean_fill": "holdout_mean_fill"})
    t = test_cells[keys + ["n_seeds_with_demand", "mean_fill", "se", "test_pass"]].rename(
        columns={"mean_fill": "test_mean_fill", "se": "test_se", "n_seeds_with_demand": "test_seeds_with_demand"})
    out = s.merge(h, on=keys).merge(t, on=keys)
    out["declared_unfixable_in_search"] = [(p, c, w) in unfixable for p, c, w in
                                           zip(out["product"], out["channel"], out["week"])]
    return out


def cost_table(result: SimResult) -> pd.DataFrame:
    per_seed = result.cost_per_seed()
    total = result.total_cost_per_seed()
    rows = []
    for name, values in per_seed.items():
        rows.append({"cost component": name, "mean": values.mean(), "share of total": values.mean() / total.mean(),
                     "sd": values.std(ddof=1), "P5": np.percentile(values, 5), "P95": np.percentile(values, 95)})
    rows.append({"cost component": "TOTAL", "mean": total.mean(), "share of total": 1.0,
                 "sd": total.std(ddof=1), "P5": np.percentile(total, 5), "P95": np.percentile(total, 95)})
    df = pd.DataFrame(rows)
    df["SE of mean"] = df["sd"] / np.sqrt(result.n_seeds)
    return df


def kpi_table(model: ModelInput, result: SimResult) -> pd.DataFrame:
    weeks = model.evaluation_weeks
    rows = []
    for p in model.products:
        dc = result.dc[p.name]
        for c in p.channels:
            ch = result.channel[(p.name, c.name)]
            demand = ch["demand"][:, weeks].sum()
            rows.append((f"{p.name} {c.name}: pooled fill rate (evaluation window)",
                         ch["sales"][:, weeks].sum() / demand if demand else None, f"target {c.target_fill_rate}"))
        receipts = dc["receipts"][:, 1:].sum()
        rows.append((f"{p.name}: FG waste as share of DC receipts", dc["waste"][:, 1:].sum() / receipts if receipts else None, ""))
        order_weeks = dc["ordered_Q"][:, 1:] > 0
        n_orders = order_weeks.sum()
        rows.append((f"{p.name}: order weeks with a cut release - RM binding",
                     dc["cut_by_rm"][:, 1:][order_weeks].sum() / n_orders if n_orders else None,
                     "share of weeks with Q > 0; the cancelled part is re-ordered next week if still needed"))
        rows.append((f"{p.name}: order weeks with a cut release - capacity binding",
                     dc["cut_by_capacity"][:, 1:][order_weeks].sum() / n_orders if n_orders else None, ""))
        rows.append((f"{p.name}: average DC stock end of week", dc["on_hand_end"][:, 1:].mean(), "units"))
        rows.append((f"{p.name}: releases per seed", (dc["released_P"][:, 1:] > 0).sum(axis=1).mean(), ""))
    for m in model.materials:
        rm = result.rm[m.name]
        receipts = rm["receipts"][:, 1:].sum()
        rows.append((f"{m.name}: average RMW stock end of week", rm["on_hand_end"][:, 1:].mean(), "units"))
        rows.append((f"{m.name}: RM waste as share of receipts", rm["waste"][:, 1:].sum() / receipts if receipts else None, ""))
        rows.append((f"{m.name}: supplier orders per seed", (rm["ordered_O"][:, 1:] > 0).sum(axis=1).mean(), ""))
    return pd.DataFrame(rows, columns=["KPI", "value", "note"])


def weekly_means_table(model: ModelInput, result: SimResult) -> pd.DataFrame:
    """Mean over all seeds of every recorded series, per week."""
    data = {"week": np.arange(1, model.horizon + 1)}
    for p in model.products:
        for s in DC_SERIES:
            data[f"DC {p.name} {s}"] = result.dc[p.name][s][:, 1:].mean(axis=0)
        for c in p.channels:
            for s in CHANNEL_SERIES:
                data[f"{p.name} {c.name} {s}"] = result.channel[(p.name, c.name)][s][:, 1:].mean(axis=0)
    for m in model.materials:
        for s in RM_SERIES:
            data[f"RMW {m.name} {s}"] = result.rm[m.name][s][:, 1:].mean(axis=0)
    for name, arr in result.cost_weekly.items():
        data[f"cost {name}"] = arr[:, 1:].mean(axis=0)
    return pd.DataFrame(data).round(3)


BAND_PERCENTILES = (5, 25, 50, 75, 95)


def _band(values: np.ndarray) -> dict:
    """Mean and percentiles over the seeds (axis 0) per week, for weeks 1..H.
    NaN entries (e.g. no stock) are ignored; a week with no value at all gives None."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)   # all-NaN weeks
        mean = np.nanmean(values, axis=0)
        pct = np.nanpercentile(values, BAND_PERCENTILES, axis=0)
    band = {"mean": np.round(mean, 3)}
    for q, row in zip(BAND_PERCENTILES, pct):
        band[f"p{q}"] = np.round(row, 3)
    return band


def weekly_band_series(model: ModelInput, result: SimResult) -> list[dict]:
    """Spread of every weekly series over the seeds: one entry per series with arrays
    mean, p5, p25, p50, p75, p95 (index 0 = week 1). Same series as weekly_means_table,
    plus the average remaining shelf life of the stock at the end of the week."""
    def entry(location, item, channel, metric, arr):
        return {"location": location, "item": item, "channel": channel, "metric": metric,
                **_band(arr[:, 1:].astype(float))}

    series = []
    for p in model.products:
        for s in DC_SERIES:
            series.append(entry("DC", p.name, None, s.lower(), result.dc[p.name][s]))
        if p.name in result.dc_remaining_life_end:
            series.append(entry("DC", p.name, None, "avg_remaining_life_end", result.dc_remaining_life_end[p.name]))
        for c in p.channels:
            for s in CHANNEL_SERIES:
                series.append(entry("channel", p.name, c.name, s, result.channel[(p.name, c.name)][s]))
    for m in model.materials:
        for s in RM_SERIES:
            series.append(entry("RMW", m.name, None, s.lower(), result.rm[m.name][s]))
        if m.name in result.rm_remaining_life_end:
            series.append(entry("RMW", m.name, None, "avg_remaining_life_end", result.rm_remaining_life_end[m.name]))
    for name, arr in result.cost_weekly.items():
        series.append(entry("cost", None, None, name.lower().replace(" ", "_").replace("->", "_"), arr))
    return series


def trace_weekly_table(model: ModelInput, schedule: PolicySchedule, result: SimResult,
                       seeds: list[int]) -> pd.DataFrame:
    """One row per (trace seed, week) with every flow and position of that seed."""
    rows = []
    for k in seeds:
        for t in range(1, model.horizon + 1):
            row = {"seed": k, "week": t}
            for p in model.products:
                row[f"DC {p.name} s"] = int(schedule.dc_s[p.name][t])
                row[f"DC {p.name} S"] = int(schedule.dc_S[p.name][t])
                for s in DC_SERIES:
                    row[f"DC {p.name} {s}"] = int(result.dc[p.name][s][k, t])
                for c in p.channels:
                    for s in CHANNEL_SERIES:
                        row[f"{p.name} {c.name} {s}"] = int(result.channel[(p.name, c.name)][s][k, t])
                    fill = result.fill[(p.name, c.name)][k, t]
                    row[f"{p.name} {c.name} fill"] = None if np.isnan(fill) else round(float(fill), 4)
            for m in model.materials:
                row[f"RMW {m.name} s"] = int(schedule.rm_s[m.name][t])
                row[f"RMW {m.name} S"] = int(schedule.rm_S[m.name][t])
                for s in RM_SERIES:
                    row[f"RMW {m.name} {s}"] = int(result.rm[m.name][s][k, t])
            for name, arr in result.cost_weekly.items():
                row[f"cost {name}"] = round(float(arr[k, t]), 2)
            rows.append(row)
    return pd.DataFrame(rows)


def conservation_checks(model: ModelInput, result: SimResult) -> pd.DataFrame:
    """Unit balance per item over all seeds:
    initial stock + receipts = outflows (sales or shipments) + waste + final stock."""
    rows = []
    for p in model.products:
        dc = result.dc[p.name]
        initial = sum(model.initial_state.dc_stock.get(p.name, {}).values())
        err = initial + dc["receipts"][:, 1:].sum(1) - dc["sales"][:, 1:].sum(1) - dc["waste"][:, 1:].sum(1) - dc["on_hand_end"][:, -1]
        rows.append({"check": f"DC {p.name}: initial + receipts - sales - waste - final stock = 0",
                     "max abs error over all seeds": int(np.abs(err).max()), "ok": bool(np.abs(err).max() == 0)})
        err = dc["ordered_Q"][:, 1:].sum(1) - dc["released_P"][:, 1:].sum(1) - dc["cancelled"][:, 1:].sum(1)
        rows.append({"check": f"DC {p.name}: ordered - released - cancelled = 0",
                     "max abs error over all seeds": int(np.abs(err).max()), "ok": bool(np.abs(err).max() == 0)})
        sales_ch = sum(result.channel[(p.name, c.name)]["sales"] for c in p.channels)
        err = sales_ch - dc["sales"]
        rows.append({"check": f"DC {p.name}: sum of channel sales = DC sales",
                     "max abs error over all seeds": int(np.abs(err).max()), "ok": bool(np.abs(err).max() == 0)})
    for m in model.materials:
        rm = result.rm[m.name]
        initial = sum(model.initial_state.rm_stock.get(m.name, {}).values())
        err = initial + rm["receipts"][:, 1:].sum(1) - rm["shipped_T"][:, 1:].sum(1) - rm["waste"][:, 1:].sum(1) - rm["on_hand_end"][:, -1]
        rows.append({"check": f"RMW {m.name}: initial + receipts - shipped - waste - final stock = 0",
                     "max abs error over all seeds": int(np.abs(err).max()), "ok": bool(np.abs(err).max() == 0)})
        needed = sum(p.bom[m.name] * result.dc[p.name]["released_P"] for p in model.products_using(m.name))
        err = rm["shipped_T"] - needed
        rows.append({"check": f"{m.name}: shipped to PF = BOM x released production",
                     "max abs error over all seeds": int(np.abs(err).max()), "ok": bool(np.abs(err).max() == 0)})
    return pd.DataFrame(rows)
