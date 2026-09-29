"""
Week-1 lookahead (last part of the ordering policy).

The age-aware capped (s,S) rule is tuned first (meio/search.py). Then the decisions that are committed
now - the DC order of each product and the supplier order of each raw material
in week 1 - are chosen directly by simulation:

    for each candidate quantity:
        simulate the search seeds from the current state,
        week 1 uses the candidate, weeks 2..H use the tuned rule
    keep the cheapest candidate that fails no cell the rule's own quantity passes
    (service is never traded for cost; cells no week-1 decision can fix do not
    drive the choice)

The DC order is chosen first (with the rule's RM orders), then each RM order one
material after the other with everything chosen so far fixed. Because the
simulation starts from the exact current state (stock by age, open orders), the
choice uses the full age information even though the rule itself does not.

If only the rule's own quantity is offered, the lookahead returns exactly the
rule's decision (tested).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import service
from .config import ModelInput, SearchSettings
from .policy import PolicySchedule
from .scenarios import ScenarioSet
from .simulation import simulate


def _score(model, schedule, seeds, orders, settings, margins, unfixable):
    """Mean cost and the set of cells failing the search rule for one candidate."""
    result = simulate(model, schedule, seeds, week1_orders=orders)
    cells = service.apply_search_rule(service.cell_table(model, result), settings.z, margins)
    failing = {(p, c, int(w)) for p, c, w, ok in zip(cells["product"], cells["channel"], cells["week"],
                                                     cells["search_feasible"]) if not ok}
    return result.mean_total_cost(), failing - set(unfixable), result


def _choose(step, item, candidates, rule_quantity, orders, key, model, schedule,
            seeds, settings, margins, unfixable, log):
    """Evaluate all candidates for one decision and return the best quantity.
    The rule's own quantity is the incumbent; another candidate must be strictly better."""
    rows, failing_sets = [], []
    for q in candidates:
        trial = {"dc": dict(orders["dc"]), "rm": dict(orders["rm"])}
        trial[key][item] = int(q)
        cost, failing, _ = _score(model, schedule, seeds, trial, settings, margins, unfixable)
        rows.append({"step": step, "item": item, "candidate": int(q), "is_rule_quantity": int(q) == rule_quantity,
                     "mean_cost": round(cost, 2), "failing_cells": len(failing)})
        failing_sets.append(failing)
    rule_index = next(i for i, r in enumerate(rows) if r["is_rule_quantity"])
    best = rule_index
    for i, r in enumerate(rows):
        acceptable = failing_sets[i] <= failing_sets[rule_index]      # no cell worse than with the rule
        r["acceptable"] = acceptable
        if acceptable and r["mean_cost"] < rows[best]["mean_cost"] - 1e-6:
            best = i
    for i, r in enumerate(rows):
        r["chosen"] = i == best
    log.extend(rows)
    return rows[best]["candidate"]


def lookahead_week1(model: ModelInput, schedule: PolicySchedule,
                    seeds: ScenarioSet, settings: SearchSettings, margins: dict, unfixable: set):
    """Return (orders, rule_orders, log): the chosen week-1 orders
    {"dc": {product: Q}, "rm": {material: O}}, the rule's own week-1 orders, and a
    table of every evaluated candidate."""
    base = simulate(model, schedule, seeds)
    rule_orders = {"dc": {p.name: int(base.dc[p.name]["ordered_Q"][0, 1]) for p in model.products},
                   "rm": {m.name: int(base.rm[m.name]["ordered_O"][0, 1]) for m in model.materials}}
    orders = {"dc": dict(rule_orders["dc"]), "rm": dict(rule_orders["rm"])}
    log: list[dict] = []

    # --- DC orders: 0, MOQ, MOQ + batches ... up to what RM and capacity allow in week 1
    usable = {m.name: sum(model.initial_state.rm_stock.get(m.name, {}).values()) for m in model.materials}
    for p in model.products:
        feasible = min([usable[r] // a for r, a in p.bom.items()] + [model.capacity_in_week(1)])
        feasible = (feasible // p.batch_size) * p.batch_size
        candidates = {0, rule_orders["dc"][p.name]}
        if feasible >= p.moq and 1 not in p.closed_production_weeks:
            candidates.update(range(p.moq, feasible + 1, p.batch_size))
        orders["dc"][p.name] = _choose("1 DC order", p.name, sorted(candidates), rule_orders["dc"][p.name],
                                       orders, "dc", model, schedule, seeds, settings, margins,
                                       unfixable, log)

    # --- RM orders: 0, MOQ and a grid around the rule's quantity, one material at a time
    for m in model.materials:
        if 1 in m.closed_order_weeks:
            continue
        rule_q = rule_orders["rm"][m.name]
        step = max(m.batch_size, int(np.ceil(0.10 * max(rule_q, m.moq) / m.batch_size)) * m.batch_size)
        candidates = {0, m.moq, rule_q}
        for k in range(1, settings.lookahead_rm_steps + 1):
            for q in (rule_q + k * step, rule_q - k * step):
                if q >= m.moq:
                    candidates.add(q)
        if m.supplier_capacity is not None:
            candidates = {min(q, m.supplier_capacity) for q in candidates}
        orders["rm"][m.name] = _choose("2 RM order", m.name, sorted(candidates), rule_q, orders, "rm",
                                       model, schedule, seeds, settings, margins, unfixable, log)

    return orders, rule_orders, pd.DataFrame(log)
