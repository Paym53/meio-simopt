"""
JSON interface of the model.

* Input:  a ModelInput can be written to and read from a JSON file. This is the
          format a future app or web API sends to the model.
* Output: every run writes summary.json - the committed decisions, service
          verdict, costs, KPIs and the optimised policy - in plain JSON.
          Version 2 adds meta, weekly_bands (spread over the test seeds per week),
          service.cells, baseline (heuristic start schedule on the same seeds) and
          settings. Version-1 keys are unchanged (docs/app_integration.md).

examples/example_input.json is the app's default dataset (FG1, about 4,000 units per week, maintained
by hand). Run   python -m meio.io_json <file>   to write the small built-in reference instance.

Input format (all weeks are numbered from 1 = current review week):
{
  "horizon": 36,
  "production_capacity": 450,
  "capacity_overrides": {"18": 0},                           # optional: week -> capacity
  "sites": [{name, capacity, capacity_overrides: {...}, closed_weeks: [...]}],   # optional: production
                                       # sites; then every product names its "site" and production_capacity
                                       # / capacity_overrides above are not used
  "target_share_of_futures": 0.98,                           # optional: every cell must meet its fill
                                                             # rate F in at least this share of futures
  "products": [ {name, shelf_life, channels: [{name, min_remaining_life, target_fill_rate}],
                 bom: {material: units per FG, whole or fractional e.g. 0.2}, batch_size, moq, holding_cost, waste_cost,
                 fixed_cost_per_release, production_tiers: [{lower, upper, unit_cost}],
                 transport_tiers: [...], lead_time_dist: {"weeks": probability},
                 closed_production_weeks: [...], site: "S1" (with "sites")} ],
  "materials": [ {name, shelf_life, min_life_at_shipment, batch_size, moq, unit_cost,
                  fixed_order_cost, holding_cost, waste_cost, transport_cost,
                  lead_time_dist: {"weeks": probability}, supplier_capacity, closed_order_weeks,
                  rmw_to_pf_lead_time} ],        # optional (default 0): weeks from the RMW to production;
                                                 # a product is produced when its slowest material arrives
  "demand_forecast": {product: {channel: {"mean": [week 1, week 2, ...], "sd": [...]}}},
  "initial_state": {"dc_stock": {product: {"age": units}}, "rm_stock": {material: {"age": units}},
                    "dc_pipeline": {product: [[release_week, units], ...]},
                    "rm_pipeline": {material: [[order_week, units], ...]}}
}
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict

import numpy as np
import pandas as pd

from .config import (Channel, DemandForecast, InitialState, Material, ModelInput, Product, ProductionSite, Tier,
                     build_example_input)


# ---------------------------------------------------------------------------
# Model input  <->  plain dict
# ---------------------------------------------------------------------------
def model_to_dict(model: ModelInput) -> dict:
    def dist(d):
        return {str(k): v for k, v in sorted(d.items())}

    products = []
    for p in model.products:
        d = asdict(p)
        d["lead_time_dist"] = dist(p.lead_time_dist)
        products.append(d)
    materials = []
    for m in model.materials:
        d = asdict(m)
        d["lead_time_dist"] = dist(m.lead_time_dist)
        materials.append(d)

    forecast = {}
    for (p, c), mean in model.demand.mean.items():
        forecast.setdefault(p, {})[c] = {"mean": [round(float(x), 3) for x in mean[1:]],
                                         "sd": [round(float(x), 3) for x in model.demand.sd[(p, c)][1:]]}
    s = model.initial_state
    return {
        "horizon": model.horizon,
        "production_capacity": model.production_capacity,
        "capacity_overrides": {str(k): v for k, v in model.capacity_overrides.items()},
        "target_share_of_futures": model.target_share_of_futures,
        **({"sites": [{"name": s.name, "capacity": s.capacity,
                       "capacity_overrides": {str(k): v for k, v in s.capacity_overrides.items()},
                       "closed_weeks": list(s.closed_weeks)} for s in model.sites]} if model.sites else {}),
        "products": products,
        "materials": materials,
        "demand_forecast": forecast,
        "initial_state": {
            "dc_stock": {k: {str(a): q for a, q in sorted(v.items())} for k, v in s.dc_stock.items()},
            "rm_stock": {k: {str(a): q for a, q in sorted(v.items())} for k, v in s.rm_stock.items()},
            "dc_pipeline": {k: [list(x) for x in v] for k, v in s.dc_pipeline.items()},
            "rm_pipeline": {k: [list(x) for x in v] for k, v in s.rm_pipeline.items()},
        },
    }


def _bom_quantity(value) -> int | float:
    """BOM quantity from JSON: whole numbers stay int (1.0 -> 1), fractions stay float (0.2)."""
    value = float(value)
    return int(value) if value.is_integer() else value


def model_from_dict(d: dict) -> ModelInput:
    def dist(x):
        return {int(k): float(v) for k, v in x.items()}

    products = []
    for p in d["products"]:
        products.append(Product(
            name=p["name"], shelf_life=p["shelf_life"],
            channels=[Channel(**c) for c in p["channels"]],
            bom={k: _bom_quantity(v) for k, v in p["bom"].items()},
            batch_size=p["batch_size"], moq=p["moq"], holding_cost=p["holding_cost"],
            waste_cost=p["waste_cost"], fixed_cost_per_release=p["fixed_cost_per_release"],
            production_tiers=[Tier(**t) for t in p["production_tiers"]],
            transport_tiers=[Tier(**t) for t in p["transport_tiers"]],
            lead_time_dist=dist(p["lead_time_dist"]),
            closed_production_weeks=list(p.get("closed_production_weeks", [])),
            site=p.get("site", ""),
        ))
    materials = []
    for m in d["materials"]:
        materials.append(Material(
            name=m["name"], shelf_life=m["shelf_life"], min_life_at_shipment=m.get("min_life_at_shipment", 1),
            batch_size=m["batch_size"], moq=m["moq"], unit_cost=m["unit_cost"],
            fixed_order_cost=m["fixed_order_cost"], holding_cost=m["holding_cost"],
            waste_cost=m["waste_cost"], transport_cost=m["transport_cost"],
            lead_time_dist=dist(m["lead_time_dist"]), supplier_capacity=m.get("supplier_capacity"),
            closed_order_weeks=list(m.get("closed_order_weeks", [])),
            rmw_to_pf_lead_time=m.get("rmw_to_pf_lead_time", 0),      # missing = 0 (same week)
        ))

    mean, sd = {}, {}
    for p, channels in d["demand_forecast"].items():
        for c, f in channels.items():
            mean[(p, c)] = np.array([0.0] + [float(x) for x in f["mean"]])   # index 0 = unused week 0
            sd[(p, c)] = np.array([0.0] + [float(x) for x in f["sd"]])

    s = d["initial_state"]
    state = InitialState(
        dc_stock={k: {int(a): int(q) for a, q in v.items()} for k, v in s["dc_stock"].items()},
        rm_stock={k: {int(a): int(q) for a, q in v.items()} for k, v in s["rm_stock"].items()},
        dc_pipeline={k: sorted((int(w), int(q)) for w, q in v) for k, v in s["dc_pipeline"].items()},
        rm_pipeline={k: sorted((int(w), int(q)) for w, q in v) for k, v in s["rm_pipeline"].items()},
    )
    return ModelInput(horizon=d["horizon"], products=products, materials=materials,
                      demand=DemandForecast(mean=mean, sd=sd), initial_state=state,
                      production_capacity=d.get("production_capacity", 0),
                      capacity_overrides={int(k): int(v) for k, v in d.get("capacity_overrides", {}).items()},
                      target_share_of_futures=float(d.get("target_share_of_futures", 0.98)),
                      sites=[ProductionSite(name=s["name"], capacity=int(s["capacity"]),
                                            capacity_overrides={int(k): int(v) for k, v in
                                                                s.get("capacity_overrides", {}).items()},
                                            closed_weeks=[int(w) for w in s.get("closed_weeks", [])])
                             for s in d.get("sites", [])])


def save_json(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_jsonable(data), f, indent=2)


def load_model(path: str) -> ModelInput:
    with open(path, encoding="utf-8") as f:
        return model_from_dict(json.load(f))


# ---------------------------------------------------------------------------
# Results  ->  summary.json
# ---------------------------------------------------------------------------
def to_jsonable(x):
    """Convert numpy / pandas values (also inside dicts and lists) to plain JSON types."""
    if isinstance(x, dict):
        return {str(k): to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_jsonable(v) for v in x]
    if isinstance(x, pd.DataFrame):
        return to_jsonable(x.to_dict(orient="records"))
    if isinstance(x, np.ndarray):
        return to_jsonable(x.tolist())
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if x is pd.NA or x is pd.NaT:
        return None
    return x


DECISION_KEYS = {"Decision": "decision", "Item": "item", "Location": "location", "Position": "position",
                 "Expected waste": "expected_waste", "Effective position": "effective_position",
                 "s (week 1)": "reorder_level_s", "S (week 1)": "order_up_to_level_S",
                 "Rule quantity": "rule_quantity", "Committed quantity": "committed_quantity", "Note": "note"}


def snake(text: str) -> str:
    """'mean cost (test seeds)' -> 'mean_cost_test_seeds' (keys that are easy to use in code)."""
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(text))
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def records(df: pd.DataFrame, keys: dict | None = None) -> list[dict]:
    renamed = df.rename(columns=keys) if keys else df.rename(columns=snake)
    return to_jsonable(renamed.to_dict(orient="records"))


def service_by_channel(cells: pd.DataFrame) -> list[dict]:
    """Per product and channel: cells passing on the test seeds and the worst week (the week
    with the smallest share of futures meeting the fill-rate target). cells needs the columns
    product, channel, week, target_F, target_share, test_share_met, test_futures_meeting_F,
    test_seeds_with_demand, test_mean_fill, test_pass."""
    rows = []
    for (p, c), g in cells.groupby(["product", "channel"], sort=False):
        worst = g.loc[g["test_share_met"].fillna(1.0).idxmin()]
        rows.append({"product": p, "channel": c, "target_fill_rate": float(g["target_F"].iloc[0]),
                     "target_share_of_futures": float(g["target_share"].iloc[0]),
                     "cells_evaluated": int(len(g)), "cells_passing": int(g["test_pass"].sum()),
                     "worst_week": int(worst["week"]), "worst_week_mean_fill": float(worst["test_mean_fill"]),
                     "worst_week_futures_meeting_target": int(worst["test_futures_meeting_F"]),
                     "worst_week_futures": int(worst["test_seeds_with_demand"]),
                     "worst_week_share_meeting_target": float(worst["test_share_met"])})
    return rows


def policy_dict(model: ModelInput, schedule) -> dict:
    """s and S per week (index 0 = week 1), DC order cap and RMW minimum of a schedule."""
    return {
        "weeks": list(range(1, model.horizon + 1)),
        "dc": {p.name: {"s": schedule.dc_s[p.name][1:], "S": schedule.dc_S[p.name][1:],
                        "order_cap": schedule.dc_cap.get(p.name)} for p in model.products},
        "rmw": {m.name: {"s": schedule.rm_s[m.name][1:], "S": schedule.rm_S[m.name][1:],
                         "minimum_physical_stock": schedule.rm_floor.get(m.name)} for m in model.materials},
    }


def meta_dict(model: ModelInput) -> dict:
    """What the summary covers: items, channels, weeks, evaluation window, commit week."""
    weeks = model.evaluation_weeks
    return {
        "summary_version": 2,
        "products": [p.name for p in model.products],
        "materials": [m.name for m in model.materials],
        "channels": {p.name: [c.name for c in p.channels] for p in model.products},
        "horizon": model.horizon,
        "weeks": list(range(1, model.horizon + 1)),
        "evaluation_weeks": {"first": weeks[0], "last": weeks[-1]} if weeks else None,
        "commit_week": 1,
    }


def build_summary(model: ModelInput, run_info: dict, decisions: pd.DataFrame, cells: pd.DataFrame,
                  costs: pd.DataFrame, kpis: pd.DataFrame, schedule, weekly_means: pd.DataFrame,
                  weekly_bands: list[dict] | None = None, baseline: dict | None = None,
                  settings: dict | None = None) -> dict:
    """The run result as plain data: what to do now, how good the plan is, and the policy.
    All keys are snake_case so that an app or API can use them directly.

    Version 2 adds (only when given): meta, weekly_bands, service.cells, baseline, settings.
    The keys of version 1 are unchanged."""
    failed = cells[~cells["test_pass"]][["product", "channel", "week", "target_F", "target_share",
                                         "test_futures_meeting_F", "test_seeds_with_demand", "test_share_met",
                                         "test_mean_fill", "test_se"]]
    service = {"by_channel": service_by_channel(cells), "failed_cells": records(failed),
               "all_cells_pass": bool(cells["test_pass"].all())}
    summary = {
        "run": {snake(k): v for k, v in run_info.items()},
        "decisions_to_commit": records(decisions, DECISION_KEYS),
        "service": service,
        "costs": records(costs),
        "kpis": records(kpis),
        "policy": policy_dict(model, schedule),
        "weekly_means_test_seeds": records(weekly_means),
    }
    if weekly_bands is not None:
        summary["meta"] = meta_dict(model)
        summary["weekly_bands"] = weekly_bands
        service["cells"] = records(cells[["product", "channel", "week", "target_F", "target_share",
                                           "test_futures_meeting_F", "test_seeds_with_demand", "test_share_met",
                                           "test_mean_fill", "test_se", "test_pass"]].rename(columns={
            "target_F": "target_fill_rate", "target_share": "target_share_of_futures",
            "test_futures_meeting_F": "futures_meeting_target", "test_seeds_with_demand": "seeds_with_demand",
            "test_share_met": "share_of_futures_meeting_target",
            "test_mean_fill": "mean_fill", "test_se": "se", "test_pass": "pass"}))
    if baseline is not None:
        summary["baseline"] = baseline
    if settings is not None:
        summary["settings"] = settings
    return to_jsonable(summary)


if __name__ == "__main__":
    # Writes the small built-in reference instance (the one the unit tests use). The app's default
    # dataset examples/example_input.json is maintained separately, so this never overwrites it.
    if len(sys.argv) < 2:
        sys.exit("usage: python -m meio.io_json <target.json>   (writes the built-in reference instance)")
    save_json(model_to_dict(build_example_input()), sys.argv[1])
    print(f"built-in reference instance written to {sys.argv[1]}")
