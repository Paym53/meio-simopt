"""
Policy parameters: the week-specific (s, S) schedule, which weeks may order,
the distribution-based starting schedule, and the week-1 decisions that are
committed at a review.

DC rule   (per product f, week t):
    if IP_DC < s_DC[t]: order Q = S_DC[t] - IP_DC, rounded up to the batch size, at least MOQ
RMW rule  (per material r, week t):
    if EIP_r < s_RM[t]: order O = S_RM[t] - EIP_r, rounded up to the batch size, at least MOQ
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Material, ModelInput, Product, SearchSettings
from .scenarios import sample_demand, sample_lead_time


@dataclass
class PolicySchedule:
    """(s, S) levels per product / material and week (arrays indexed by week), plus a
    DC order cap per product and a minimum physical stock (installation position) per
    raw material. All six are required: the policy always applies the cap and minimum."""
    dc_s: dict[str, np.ndarray]
    dc_S: dict[str, np.ndarray]
    rm_s: dict[str, np.ndarray]
    rm_S: dict[str, np.ndarray]
    dc_cap: dict[str, int]
    rm_floor: dict[str, int]

    def copy(self) -> "PolicySchedule":
        return PolicySchedule({k: v.copy() for k, v in self.dc_s.items()},
                              {k: v.copy() for k, v in self.dc_S.items()},
                              {k: v.copy() for k, v in self.rm_s.items()},
                              {k: v.copy() for k, v in self.rm_S.items()},
                              dict(self.dc_cap), dict(self.rm_floor))


# ---------------------------------------------------------------------------
# Weeks in which ordering is possible (rule C19 + calendars)
# ---------------------------------------------------------------------------
def dc_order_weeks(model: ModelInput, product: Product) -> list[int]:
    """Weeks in which the DC may order product f: production open and the
    earliest possible arrival (t + L_min) still inside the horizon."""
    last = model.horizon - product.lead_time_min
    return [t for t in range(1, last + 1) if t not in product.closed_production_weeks]


def rm_order_weeks(model: ModelInput, material: Material) -> list[int]:
    """Weeks in which the RMW may order material r: supplier accepts orders and
    the material can still reach the DC as FG inside the horizon (t + G_min + L_min <= H)."""
    l_min = min(p.lead_time_min for p in model.products_using(material.name))
    last = model.horizon - material.lead_time_min - l_min
    return [t for t in range(1, last + 1) if t not in material.closed_order_weeks]


# ---------------------------------------------------------------------------
# Distribution-based starting schedule (spec 12.2)
# ---------------------------------------------------------------------------
def _sample_total_demand_paths(model: ModelInput, product: Product, n: int,
                               rng: np.random.Generator) -> np.ndarray:
    """Sampled total demand (all channels) per week, shape (n, last_forecast_week + 1)."""
    last = model.demand.last_week
    paths = np.zeros((n, last + 1))
    for c in product.channels:
        for t in range(1, last + 1):
            paths[:, t] += sample_demand(model.demand.mean[(product.name, c.name)][t],
                                         model.demand.sd[(product.name, c.name)][t], n, rng)
    return paths


def _window_sum(cumulative: np.ndarray, start: int, end: np.ndarray) -> np.ndarray:
    """Sum of weekly demand over weeks start..end (end per sample), using a cumulative sum.
    Weeks beyond the last forecast week are cut off at the last forecast week."""
    end = np.minimum(end, cumulative.shape[1] - 1)
    rows = np.arange(len(end))
    return cumulative[rows, end] - cumulative[rows, start - 1]


def _round_up(x: float, multiple: int) -> int:
    return int(np.ceil(x / multiple) * multiple)


def initial_schedule(model: ModelInput, settings: SearchSettings) -> PolicySchedule:
    """Quantile-based start: s = q0-quantile of demand over the protection interval,
    S = q0-quantile over the interval plus m0 extra weeks. The intervals use random
    lead times, so the start reflects both forecast and lead-time uncertainty.

    DC  interval for an order in week t: weeks t .. t + L~
    RMW interval for an order in week t: weeks t .. t + G~ + L~   (times BOM quantity)
    """
    rng = np.random.default_rng(settings.base_seed + 999)   # own stream, never reused
    n, H, m0 = settings.n_quantile_samples, model.horizon, settings.initial_extra_cover_weeks

    paths, cumulative, lead = {}, {}, {}
    for p in model.products:
        paths[p.name] = _sample_total_demand_paths(model, p, n, rng)
        cumulative[p.name] = np.cumsum(paths[p.name], axis=1)

    dc_s, dc_S = {}, {}
    for p in model.products:
        q0 = settings.initial_quantile or max(c.target_fill_rate for c in p.channels)
        s_arr = np.zeros(H + 1, dtype=np.int64)
        S_arr = np.zeros(H + 1, dtype=np.int64)
        for t in dc_order_weeks(model, p):
            L = sample_lead_time(p.lead_time_dist, n, rng)
            s_val = np.quantile(_window_sum(cumulative[p.name], t, t + L), q0)
            S_val = np.quantile(_window_sum(cumulative[p.name], t, t + L + m0), q0)
            s_arr[t] = _round_up(s_val, p.batch_size)
            S_arr[t] = max(_round_up(S_val, p.batch_size), s_arr[t] + p.batch_size)
        dc_s[p.name], dc_S[p.name] = s_arr, S_arr

    rm_s, rm_S = {}, {}
    for m in model.materials:
        users = model.products_using(m.name)
        q0 = settings.initial_quantile or max(c.target_fill_rate for p in users for c in p.channels)
        s_arr = np.zeros(H + 1, dtype=np.int64)
        S_arr = np.zeros(H + 1, dtype=np.int64)
        for t in rm_order_weeks(model, m):
            G = sample_lead_time(m.lead_time_dist, n, rng)
            need_s, need_S = np.zeros(n), np.zeros(n)
            for p in users:
                L = sample_lead_time(p.lead_time_dist, n, rng)
                need_s += p.bom[m.name] * _window_sum(cumulative[p.name], t, t + G + L)
                need_S += p.bom[m.name] * _window_sum(cumulative[p.name], t, t + G + L + m0)
            s_arr[t] = _round_up(np.quantile(need_s, q0), m.batch_size)
            S_arr[t] = max(_round_up(np.quantile(need_S, q0), m.batch_size), s_arr[t] + m.batch_size)
        rm_s[m.name], rm_S[m.name] = s_arr, S_arr

    # Starting values of the order cap and RMW minimum (the search tunes them):
    #   DC cap   = initial_cap_weeks x mean weekly demand, rounded up to batches, at least the MOQ
    #   RMW floor = initial_floor_share x mean weekly RM use x median supplier lead time
    dc_cap, rm_floor = {}, {}
    mean_weekly = {p.name: _mean_weekly_demand(model, p) for p in model.products}
    for p in model.products:
        dc_cap[p.name] = max(p.moq, _round_up(settings.initial_cap_weeks * mean_weekly[p.name], p.batch_size))
    for m in model.materials:
        use = sum(p.bom[m.name] * mean_weekly[p.name] for p in model.products_using(m.name))
        rm_floor[m.name] = _round_up(settings.initial_floor_share * use * m.lead_time_median, m.batch_size)

    return PolicySchedule(dc_s, dc_S, rm_s, rm_S, dc_cap, rm_floor)


def _mean_weekly_demand(model: ModelInput, product: Product) -> float:
    """Average forecast mean of total demand per week over the horizon."""
    total = sum(model.demand.mean[(product.name, c.name)][1:model.horizon + 1] for c in product.channels)
    return float(np.mean(total))


# ---------------------------------------------------------------------------
# Decisions committed at this review (week 1)
# ---------------------------------------------------------------------------
def committed_decisions(model: ModelInput, schedule: PolicySchedule, result,
                        rule_orders: dict | None = None) -> list[dict]:
    """Week-1 decisions to execute now. The state at the start of week 1 is known, so
    these decisions are identical in every seed; this is checked here.

    rule_orders: the quantities the (s,S) rule alone would have ordered, shown next to
    the committed (lookahead) quantities. Without it the rule quantity is the committed one."""
    def same_in_all_seeds(arr, label):
        if len(np.unique(arr[:, 1])) != 1:
            raise RuntimeError(f"week-1 value '{label}' differs between seeds - state not deterministic")
        return int(arr[0, 1])

    rows = []
    for p in model.products:
        dc = result.dc[p.name]
        committed = same_in_all_seeds(dc["ordered_Q"], "DC order")
        rows.append({
            "Decision": "Order FG from production (Q)", "Item": p.name, "Location": "DC",
            "Position": same_in_all_seeds(dc["inventory_position"], "DC position"),
            "Expected waste": same_in_all_seeds(dc["expected_waste"], "DC expected waste"),
            "Effective position": same_in_all_seeds(dc["effective_position"], "DC effective position"),
            "s (week 1)": int(schedule.dc_s[p.name][1]), "S (week 1)": int(schedule.dc_S[p.name][1]),
            "Rule quantity": rule_orders["dc"][p.name] if rule_orders else committed,
            "Committed quantity": committed,
            "Note": f"order cap {schedule.dc_cap[p.name]}",
        })
        released = same_in_all_seeds(dc["released_P"], "release")
        rows.append({
            "Decision": "Release to production = produce (P)", "Item": p.name, "Location": "PF",
            "Committed quantity": released,
            "Note": f"cancelled {committed - released} (raw material, capacity or batch/MOQ rules)"
                    if committed > released else "released in full",
        })
    for m in model.materials:
        rows.append({
            "Decision": "Transport RM to production (T)", "Item": m.name, "Location": "RMW -> PF",
            "Committed quantity": same_in_all_seeds(result.rm[m.name]["shipped_T"], "RM shipment"),
            "Note": "BOM x released production, oldest RM first",
        })
    for m in model.materials:
        rm = result.rm[m.name]
        committed = same_in_all_seeds(rm["ordered_O"], "RM order")
        installation = same_in_all_seeds(rm["installation_position"], "RM installation position")
        floor = schedule.rm_floor[m.name]
        rows.append({
            "Decision": "Order RM from supplier (O)", "Item": m.name, "Location": "RMW",
            "Position": same_in_all_seeds(rm["echelon_position"], "RM echelon position"),
            "Expected waste": same_in_all_seeds(rm["echelon_position"], "x") -
                              same_in_all_seeds(rm["effective_echelon_position"], "RM effective position"),
            "Effective position": same_in_all_seeds(rm["effective_echelon_position"], "RM effective position"),
            "s (week 1)": int(schedule.rm_s[m.name][1]), "S (week 1)": int(schedule.rm_S[m.name][1]),
            "Rule quantity": rule_orders["rm"][m.name] if rule_orders else committed,
            "Committed quantity": committed,
            "Note": f"physical RM position {installation} vs minimum {floor}",
        })
    return rows
