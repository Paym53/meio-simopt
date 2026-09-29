"""
Rolling re-optimisation (spec v5, Section 13).

At every review week:
    1. optimise the (s,S) schedule from the current state (warm start = last schedule, shifted)
    2. commit the week-1 decisions
    3. one real week happens (here: simulated with an independent "reality" seed)
    4. read the new state (stock by age, open orders) and shift the calendar by one week

Everything in this module is plain bookkeeping around the simulator.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .config import DemandForecast, InitialState, ModelInput, SearchSettings
from .policy import PolicySchedule, dc_order_weeks, initial_schedule, rm_order_weeks


def state_after_week_one(model: ModelInput, reality) -> InitialState:
    """State at the start of week 2 (after the receipts of week 2), taken from the
    detailed trace of seed 0 of a one-week 'reality' simulation.
    Order weeks are re-numbered so that week 2 becomes the new week 1."""
    dc_stock = {p.name: {} for p in model.products}
    rm_stock = {m.name: {} for m in model.materials}
    for row in reality.trace.dc_ages:
        if row["seed"] == 0 and row["week"] == 2 and row["stage"].startswith("start"):
            dc_stock[row["product"]] = {a: row[f"age_{a}"] for a in range(1, 100)
                                        if row.get(f"age_{a}", 0) > 0}
    for row in reality.trace.rm_ages:
        if row["seed"] == 0 and row["week"] == 2 and row["stage"].startswith("start"):
            rm_stock[row["material"]] = {a: row[f"age_{a}"] for a in range(1, 100)
                                         if row.get(f"age_{a}", 0) > 0}

    dc_pipe = {p.name: [] for p in model.products}
    rm_pipe = {m.name: [] for m in model.materials}
    for o in reality.trace.orders:
        placed_before_week_2 = o["order_week"] <= 1
        still_open = o["actual_arrival"] is not None and o["actual_arrival"] > 2
        if o["seed"] != 0 or not placed_before_week_2 or not still_open or o["released"] == 0:
            continue
        entry = (o["order_week"] - 1, o["released"])          # re-numbered: old week 1 -> new week 0
        if o["lane"].startswith("PF"):
            dc_pipe[o["item"]].append(entry)
        else:
            rm_pipe[o["item"]].append(entry)
    for lane in (dc_pipe, rm_pipe):
        for key in lane:
            lane[key] = sorted(lane[key])
    return InitialState(dc_stock, rm_stock, dc_pipe, rm_pipe)


def shift_model_one_week(model: ModelInput, new_state: InitialState) -> ModelInput:
    """Same horizon length, one week later: forecast, calendars and capacity overrides
    move one week forward, and the new state replaces the old one."""
    mean = {k: np.concatenate([[0.0], v[2:]]) for k, v in model.demand.mean.items()}
    sd = {k: np.concatenate([[0.0], v[2:]]) for k, v in model.demand.sd.items()}
    products = [replace(p, closed_production_weeks=[w - 1 for w in p.closed_production_weeks if w > 1])
                for p in model.products]
    materials = [replace(m, closed_order_weeks=[w - 1 for w in m.closed_order_weeks if w > 1])
                 for m in model.materials]
    overrides = {w - 1: c for w, c in model.capacity_overrides.items() if w > 1}
    return replace(model, products=products, materials=materials,
                   demand=DemandForecast(mean=mean, sd=sd), initial_state=new_state,
                   capacity_overrides=overrides)


def shift_schedule(old: PolicySchedule, old_model: ModelInput, new_model: ModelInput,
                   settings: SearchSettings) -> PolicySchedule:
    """Warm start for the next review: week t of the new schedule = week t+1 of the old
    one. Weeks that were not orderable before get the quantile-based start value."""
    fresh = initial_schedule(new_model, settings)
    new = fresh.copy()
    new.dc_cap = dict(old.dc_cap)                      # horizon-wide parameters carry over
    new.rm_floor = dict(old.rm_floor)
    for p in new_model.products:
        old_product = next(x for x in old_model.products if x.name == p.name)
        old_active = set(dc_order_weeks(old_model, old_product))
        for t in dc_order_weeks(new_model, p):
            if t + 1 in old_active:
                new.dc_s[p.name][t] = old.dc_s[p.name][t + 1]
                new.dc_S[p.name][t] = old.dc_S[p.name][t + 1]
    for m in new_model.materials:
        old_active = set(rm_order_weeks(old_model, old_model.material(m.name)))
        for t in rm_order_weeks(new_model, m):
            if t + 1 in old_active:
                new.rm_s[m.name][t] = old.rm_s[m.name][t + 1]
                new.rm_S[m.name][t] = old.rm_S[m.name][t + 1]
    return new
