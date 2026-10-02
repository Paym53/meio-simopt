"""
The weekly simulation of the supply chain (spec v5, Section 8).

All seeds are simulated at the same time: every stock array has one row per
seed. The weekly steps are:

  Step 1  ageing                  (all stock one week older)
  Step 2  receipts                (supplier -> RMW, production -> DC, enter at age 1)
  Step 3a DC ordering             (s,S rule on the effective DC position, order cap)
  Step 3b production release      (capped by usable RM, capacity of the production week,
                                   batch size and MOQ; the rest is cancelled)
  Step 3c RM transport            (leaves the RMW in the release week, oldest first; material r
                                   reaches production tau_r weeks later; production starts when
                                   all BOM materials are there, tau_p = max tau_r; FG at the DC
                                   after tau_p + L~)
  Step 3d RMW ordering            (s,S rule on the effective echelon position,
                                   minimum physical RM stock)
  Step 4  demand and allocation   (oldest age first; within an age the strictest
                                   channel first; unmet demand is lost)
  Step 5  service recording       (fill rate per seed, week and channel)
  Step 6  end of week             (scrap expired stock, charge holding cost)

Results for all seeds are stored per week; seeds listed in trace_seeds are
additionally recorded in full detail (stock by age, flows, orders).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import ModelInput, Product, Tier, max_fg_from, rm_units_for
from .policy import PolicySchedule, dc_order_weeks, rm_order_weeks
from .scenarios import ScenarioSet

# Names of the recorded weekly series (arrays of shape (n_seeds, H + 1))
DC_SERIES = ["on_hand_start", "receipts", "pipeline_before_order", "inventory_position",
             "expected_waste", "effective_position", "cut_by_policy_cap",
             "ordered_Q", "rm_limit", "released_P", "cancelled", "cut_by_rm", "cut_by_capacity",
             "demand", "sales", "lost_sales", "waste", "on_hand_end", "pipeline_end"]
RM_SERIES = ["on_hand_start", "receipts", "limited_release", "shipped_T", "pipeline_before_order",
             "echelon_position", "expected_waste", "effective_echelon_position", "installation_position",
             "floor_triggered", "ordered_O", "waste", "on_hand_end", "pipeline_end"]
CHANNEL_SERIES = ["demand", "sales", "lost_sales"]
COST_COMPONENTS = ["FG holding", "FG waste", "Production fixed", "Production variable",
                   "PF->DC transport", "RM purchase", "RM order fixed", "RM holding",
                   "RM waste", "RMW->PF transport"]


@dataclass
class Trace:
    """Full detail for a few seeds, stored as lists of table rows."""
    dc_ages: list[dict] = field(default_factory=list)
    rm_ages: list[dict] = field(default_factory=list)
    sales_by_age: list[dict] = field(default_factory=list)
    shipped_by_age: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)


@dataclass
class SimResult:
    scenario_name: str
    n_seeds: int
    horizon: int
    dc: dict[str, dict[str, np.ndarray]]
    rm: dict[str, dict[str, np.ndarray]]
    channel: dict[tuple[str, str], dict[str, np.ndarray]]
    fill: dict[tuple[str, str], np.ndarray]          # NaN where demand = 0
    cost_weekly: dict[str, np.ndarray]
    trace: Trace
    # units-weighted average remaining shelf life of the stock at the end of each week
    # (shelf life - age), per item; NaN where there is no stock. Float, so kept apart from dc/rm.
    dc_remaining_life_end: dict[str, np.ndarray] = field(default_factory=dict)
    rm_remaining_life_end: dict[str, np.ndarray] = field(default_factory=dict)

    def cost_per_seed(self) -> dict[str, np.ndarray]:
        return {name: arr.sum(axis=1) for name, arr in self.cost_weekly.items()}

    def total_cost_per_seed(self) -> np.ndarray:
        return sum(self.cost_per_seed().values())

    def mean_total_cost(self) -> float:
        return float(self.total_cost_per_seed().mean())


# ---------------------------------------------------------------------------
# Small building blocks (each one is unit-tested)
# ---------------------------------------------------------------------------
def round_up_to_order_rules(need: np.ndarray, batch: int, moq: int) -> np.ndarray:
    """Order quantity for a need: round up to a multiple of the batch size, at least the MOQ.
    A need <= 0 gives 0."""
    q = np.ceil(np.maximum(need, 0) / batch).astype(np.int64) * batch
    return np.where(q > 0, np.maximum(q, moq), 0)


def round_down_to_supply_rules(qty: np.ndarray, batch: int, moq: int) -> np.ndarray:
    """A quantity that was cut (by RM, capacity, supplier capacity) must still respect
    the supply rules: round DOWN to a batch multiple; below the MOQ nothing is released."""
    q = (np.maximum(qty, 0) // batch) * batch
    return np.where(q >= moq, q, 0)


def tier_unit_cost(qty: np.ndarray, tiers: list[Tier]) -> np.ndarray:
    """All-units discount: the unit cost of the band containing qty applies to ALL units.
    Bands are contiguous and sorted, so this is the last band with lower <= qty."""
    rate = np.full(np.shape(qty), tiers[0].unit_cost, dtype=float)
    for tier in tiers[1:]:
        rate = np.where(qty >= tier.lower, tier.unit_cost, rate)
    return rate


def _differences(cumulative: np.ndarray) -> np.ndarray:
    """Per-column amounts from a running total along axis 1 (first column stays as is)."""
    out = cumulative.copy()
    out[:, 1:] -= cumulative[:, :-1]
    return out


def withdraw_fifo(stock: np.ndarray, quantity: np.ndarray, max_age: int) -> np.ndarray:
    """Take `quantity` units per seed out of `stock` (n_seeds, ages + 1), oldest
    first, using only ages <= max_age. Changes stock in place and returns the
    taken units per age. Raises an error if there is not enough stock.

    Vectorised over ages (integer stock, so exact): going from the oldest usable age
    down, the cumulative stock C_k is taken up to the quantity: taken so far =
    min(C_k, quantity); the take of each age is the difference of that series."""
    taken = np.zeros_like(stock)
    top = min(max_age, stock.shape[1] - 1)
    quantity = quantity.astype(np.int64)
    if top < 1:
        if (quantity > 0).any():
            raise RuntimeError("withdraw_fifo: not enough stock (release was not capped correctly)")
        return taken
    usable = stock[:, top:0:-1]                       # ages top, top-1, ..., 1 (oldest first)
    cumulative = np.cumsum(usable, axis=1)
    if (cumulative[:, -1] < quantity).any():
        raise RuntimeError("withdraw_fifo: not enough stock (release was not capped correctly)")
    take = _differences(np.minimum(cumulative, quantity[:, None]))
    taken[:, top:0:-1] = take
    stock[:, top:0:-1] -= take
    return taken


def _allocate_demand_loop(stock, demand, max_age, priority):
    """Reference allocation, age by age (oldest first), channels in priority order within
    an age. Used for float stock (the expected-waste projection) and as a fallback."""
    open_demand = [np.asarray(d).astype(stock.dtype) for d in demand]   # a copy, same type as the stock
    sales = [np.zeros_like(stock) for _ in demand]
    for age in range(stock.shape[1] - 1, 0, -1):
        for c in priority:
            if age > max_age[c]:
                continue                     # too old for this channel (shelf-life gate)
            take = np.minimum(stock[:, age], open_demand[c])
            stock[:, age] -= take
            open_demand[c] -= take
            sales[c][:, age] += take
    return sales, open_demand


def allocate_demand(stock: np.ndarray, demand: list[np.ndarray], max_age: list[int],
                    priority: list[int]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Serve demand from stock (changed in place).
    Age buckets are used from oldest to youngest (FIFO). Within a bucket the channels
    that accept this age are served in priority order. Unmet demand is lost.
    Returns sales per channel by age (n_seeds, ages + 1) and lost sales per channel.

    Fast path for integer stock: "age by age, channels in priority order within an age"
    gives the same result as "channel by channel in priority order, each FIFO over its
    accepted ages". At an age, a channel's take is min(stock left there after the
    higher-priority channels, its own demand left after the older ages); neither depends
    on what lower-priority channels took at other ages, so the order of the two loops
    can be swapped. Each channel's FIFO take is then a cumulative sum over its ages
    (exact for integers). Float stock (the expected-waste projection) keeps the loop."""
    top = stock.shape[1] - 1
    if not np.issubdtype(stock.dtype, np.integer):
        return _allocate_demand_loop(stock, demand, max_age, priority)
    limits = [min(max_age[c], top) for c in priority]
    open_demand = [np.asarray(d).astype(stock.dtype) for d in demand]
    sales = [np.zeros_like(stock) for _ in demand]
    for c, limit in zip(priority, limits):
        if limit < 1:
            continue
        usable = stock[:, limit:0:-1]                  # accepted ages, oldest first
        served = np.minimum(np.cumsum(usable, axis=1), open_demand[c][:, None])
        take = _differences(served)
        sales[c][:, limit:0:-1] = take
        stock[:, limit:0:-1] -= take
        open_demand[c] -= served[:, -1]
    return sales, open_demand


def schedule_arrivals(arrivals: np.ndarray, last_arrival: np.ndarray, week: int,
                      quantity: np.ndarray, lead_time: np.ndarray) -> np.ndarray:
    """Order-preserving arrivals: an order arrives at max(week + lead time, arrival week
    of the previous order on the same lane). Only real orders (quantity > 0) block later
    ones. Adds the quantity to arrivals[seed, arrival week]; returns the arrival weeks
    (0 where no order was placed)."""
    has_order = quantity > 0
    arrival = np.maximum(week + lead_time, last_arrival)
    rows = np.nonzero(has_order)[0]
    arrivals[rows, arrival[rows]] += quantity[rows]
    last_arrival[rows] = arrival[rows]
    return np.where(has_order, arrival, 0)


def projected_fg_waste(stock: np.ndarray, weekly_channel_demand: list[list[float]],
                       max_age: list[int], priority: list[int]) -> np.ndarray:
    """Expected number of on-hand FG units that will expire before a new order can arrive.

    The on-hand stock (not changed) is used up week by week with the MEAN forecast
    demand of each channel, with exactly the same rules as the real allocation
    (oldest first, shelf-life gates, channel priority). Units that reach the oldest
    sellable age unsold count as expected waste. Only on-hand stock is projected:
    under FIFO new arrivals are used after the older stock, so they do not change
    whether an old unit expires (valid as long as the shelf life exceeds the window).
    """
    proj = stock.astype(float)
    n, oldest = proj.shape[0], proj.shape[1] - 1
    waste = np.zeros(n)
    for w, channel_means in enumerate(weekly_channel_demand):
        if w > 0:                                          # one week older
            proj[:, 2:] = proj[:, 1:-1].copy()
            proj[:, 1] = 0.0
        # the allocation of allocate_demand (same order, same float operations), without
        # recording sales. Skipped because they would take exactly 0: channels without
        # demand, and ages 1..w, which are empty in projection week w (no new stock enters
        # the projection and everything ages one week per week).
        open_demand = [np.full(n, float(d)) for d in channel_means]
        for age in range(oldest, w, -1):
            column = proj[:, age]
            for c in priority:
                if age > max_age[c] or channel_means[c] == 0:
                    continue
                take = np.minimum(column, open_demand[c])
                column -= take
                open_demand[c] -= take
        waste += proj[:, oldest]
        proj[:, oldest] = 0.0
    return waste


def projected_rm_waste(stock: np.ndarray, weekly_use: list[float], max_age: int) -> np.ndarray:
    """Expected number of on-hand RM units that will expire before a new supplier order
    can arrive, when the stock is used oldest first at the expected weekly use.

    Closed form of the FIFO projection: the oldest units still on hand at the end of
    window week j have age max_age - j and expire then. Everything at least that old
    was either used (cumulative use D_j), or already expired earlier (W_before). So
        waste_j = max(0, stock of ages >= max_age - j  -  W_before  -  D_j)."""
    n = stock.shape[0]
    waste_before = np.zeros(n)
    # stock of age >= a for every age a, from one cumulative sum (integers, so exact)
    at_least = np.cumsum(stock[:, ::-1], axis=1)[:, ::-1]      # at_least[:, a] = stock[:, a:].sum()
    cumulative_use = 0.0
    for j, use in enumerate(weekly_use):
        cumulative_use += use
        age = max_age - j
        if age < 1:
            break
        waste_before += np.maximum(0.0, at_least[:, age] - waste_before - cumulative_use)
    return waste_before


def _mean_channel_demand(model: ModelInput, product: Product, week: int) -> list[float]:
    """Forecast mean per channel for a week (weeks beyond the forecast use its last week)."""
    week = min(week, model.demand.last_week)
    return [float(model.demand.mean[(product.name, c.name)][week]) for c in product.channels]


def average_remaining_life(stock: np.ndarray, shelf_life: int) -> np.ndarray:
    """Units-weighted mean of (shelf life - age) per seed; NaN for seeds without stock.
    stock: (n_seeds, max_age + 1), column = age (column 0 unused)."""
    units = stock[:, 1:].astype(float)
    total = units.sum(axis=1)
    remaining = shelf_life - np.arange(1, stock.shape[1])
    weighted = (units * remaining).sum(axis=1)
    return np.where(total > 0, weighted / np.maximum(total, 1.0), np.nan)


def _age_row(prefix: dict, stock_row: np.ndarray) -> dict:
    row = dict(prefix)
    for age in range(1, len(stock_row)):
        row[f"age_{age}"] = int(stock_row[age])
    row["total"] = int(stock_row[1:].sum())
    return row


# ---------------------------------------------------------------------------
# The simulation
# ---------------------------------------------------------------------------
def simulate(model: ModelInput, schedule: PolicySchedule, scen: ScenarioSet,
             trace_seeds: list[int] | tuple = (), week1_orders: dict | None = None,
             report_details: bool = True) -> SimResult:
    """Simulate all seeds of `scen` under the age-aware capped (s,S) policy.

    week1_orders (optional): {"dc": {product: Q}, "rm": {material: O}} replaces the
    rule's orders in week 1 only. Used by the lookahead to test candidate quantities.
    report_details=False skips values that only the reports use (average remaining shelf
    life); the search and the lookahead simulate hundreds of candidates and never read them.
    Everything the decisions and the search use is identical either way."""
    H, n = model.horizon, scen.n_seeds
    week1_orders = week1_orders or {"dc": {}, "rm": {}}
    products, materials = model.products, model.materials
    trace = Trace()
    trace_seeds = [k for k in trace_seeds if k < n]

    # --- constants of this model, computed once instead of in every week ---
    last_forecast_week = model.demand.last_week
    mean_rows = {p.name: [[float(model.demand.mean[(p.name, c.name)][w]) for c in p.channels]
                          for w in range(last_forecast_week + 1)] for p in products}

    def mean_channel_demand(p, week):              # = _mean_channel_demand(model, p, week)
        return mean_rows[p.name][min(week, last_forecast_week)]

    tau = {p.name: model.rmw_to_pf_lead_time(p) for p in products}
    release_to_dc_median = {p.name: tau[p.name] + p.lead_time_median for p in products}
    channel_max_age = {p.name: [p.max_age_for_channel(c) for c in p.channels] for p in products}
    channel_priority = {p.name: p.channel_priority() for p in products}
    material_by_name = {m.name: m for m in materials}
    users_of = {m.name: model.products_using(m.name) for m in materials}

    # --- which weeks may order (calendars and horizon rule C19) ---
    dc_can_order = {p.name: set(dc_order_weeks(model, p)) for p in products}
    rm_can_order = {m.name: set(rm_order_weeks(model, m)) for m in materials}

    # --- result containers ---
    def zeros():
        return np.zeros((n, H + 1), dtype=np.int64)
    dc_rec = {p.name: {s: zeros() for s in DC_SERIES} for p in products}
    rm_rec = {m.name: {s: zeros() for s in RM_SERIES} for m in materials}
    ch_rec = {(p.name, c.name): {s: zeros() for s in CHANNEL_SERIES} for p in products for c in p.channels}
    fill = {(p.name, c.name): np.full((n, H + 1), np.nan) for p in products for c in p.channels}
    cost = {name: np.zeros((n, H + 1)) for name in COST_COMPONENTS}
    dc_life = {p.name: np.full((n, H + 1), np.nan) for p in products} if report_details else {}
    rm_life = {m.name: np.full((n, H + 1), np.nan) for m in materials} if report_details else {}

    # --- state: stock by age (column = age), future arrivals by week (column = week) ---
    dc_stock, dc_arrivals, dc_last_arrival = {}, {}, {}
    for p in products:
        dc_stock[p.name] = np.zeros((n, p.max_sellable_age + 1), dtype=np.int64)
        for age, qty in model.initial_state.dc_stock.get(p.name, {}).items():
            dc_stock[p.name][:, age] = qty
        dc_arrivals[p.name] = np.zeros((n, H + model.release_to_dc_max(p) + 2), dtype=np.int64)
        dc_last_arrival[p.name] = np.zeros(n, dtype=np.int64)
        # open releases from before week 1 (order-preserving among themselves)
        pipeline = model.initial_state.dc_pipeline.get(p.name, [])
        for k, (release_week, qty) in enumerate(pipeline):
            raw_arrival = scen.dc_pipeline_arrival[p.name][k]
            lead = raw_arrival - release_week
            arr = schedule_arrivals(dc_arrivals[p.name], dc_last_arrival[p.name], release_week,
                                    np.full(n, qty), lead)
            for s in trace_seeds:
                trace.orders.append({"seed": s, "lane": "PF -> DC (release)", "item": p.name,
                                     "order_week": release_week, "source": "initial pipeline",
                                     "ordered": qty, "released": qty, "cancelled": 0,
                                     "lead_time_draw": int(lead[s]), "planned_arrival": int(raw_arrival[s]),
                                     "actual_arrival": int(arr[s]),
                                     "held_up_by_earlier_order": bool(arr[s] > raw_arrival[s])})

    # production capacity used per production week (a release of week t is produced in week
    # t + tau_p; products with different tau_p can share a production week)
    capacity_used = np.zeros((n, H + max(tau.values()) + 2), dtype=np.int64)

    rm_stock, rm_arrivals, rm_last_arrival = {}, {}, {}
    for m in materials:
        rm_stock[m.name] = np.zeros((n, m.max_shippable_age + 1), dtype=np.int64)
        for age, qty in model.initial_state.rm_stock.get(m.name, {}).items():
            rm_stock[m.name][:, age] = qty
        rm_arrivals[m.name] = np.zeros((n, H + m.lead_time_max + 2), dtype=np.int64)
        rm_last_arrival[m.name] = np.zeros(n, dtype=np.int64)
        pipeline = model.initial_state.rm_pipeline.get(m.name, [])
        for k, (order_week, qty) in enumerate(pipeline):
            raw_arrival = scen.rm_pipeline_arrival[m.name][k]
            lead = raw_arrival - order_week
            arr = schedule_arrivals(rm_arrivals[m.name], rm_last_arrival[m.name], order_week,
                                    np.full(n, qty), lead)
            for s in trace_seeds:
                trace.orders.append({"seed": s, "lane": "Supplier -> RMW", "item": m.name,
                                     "order_week": order_week, "source": "initial pipeline",
                                     "ordered": qty, "released": qty, "cancelled": 0,
                                     "lead_time_draw": int(lead[s]), "planned_arrival": int(raw_arrival[s]),
                                     "actual_arrival": int(arr[s]),
                                     "held_up_by_earlier_order": bool(arr[s] > raw_arrival[s])})

    # Pipeline = everything scheduled to arrive after the current week. Kept as a running
    # total (integers, so exactly the sum over arrivals[:, t + 1:]): minus the receipts of
    # week t, plus new orders that arrive after week t.
    dc_pipeline = {p.name: dc_arrivals[p.name][:, 1:].sum(axis=1) for p in products}
    rm_pipeline = {m.name: rm_arrivals[m.name][:, 1:].sum(axis=1) for m in materials}

    def flow(seed, week, source, target, item, qty, note):
        if qty > 0:
            trace.flows.append({"seed": seed, "week": week, "from": source, "to": target,
                                "item": item, "quantity": int(qty), "note": note})

    # =======================================================================
    for t in range(1, H + 1):
        # ---------------- Step 1: ageing ----------------
        # (week 1 starts from the given state, which is already aged)
        if t > 1:
            for stock in list(dc_stock.values()) + list(rm_stock.values()):
                stock[:, 2:] = stock[:, 1:-1].copy()   # expired units were removed in Step 6
                stock[:, 1] = 0

        # ---------------- Step 2: receipts ----------------
        for m in materials:
            receipt = rm_arrivals[m.name][:, t].copy()
            rm_pipeline[m.name] -= receipt
            rm_stock[m.name][:, 1] += receipt
            rm_rec[m.name]["receipts"][:, t] = receipt
            rm_rec[m.name]["on_hand_start"][:, t] = rm_stock[m.name][:, 1:].sum(axis=1)
        for p in products:
            receipt = dc_arrivals[p.name][:, t].copy()
            dc_pipeline[p.name] -= receipt
            dc_stock[p.name][:, 1] += receipt
            dc_rec[p.name]["receipts"][:, t] = receipt
            dc_rec[p.name]["on_hand_start"][:, t] = dc_stock[p.name][:, 1:].sum(axis=1)

        for s in trace_seeds:
            for p in products:
                trace.dc_ages.append(_age_row({"seed": s, "product": p.name, "week": t,
                                               "stage": "start (after ageing + receipts)"},
                                              dc_stock[p.name][s]))
                flow(s, t, "In transit (PF -> DC)", "DC", p.name, dc_rec[p.name]["receipts"][s, t],
                     "production output arrives at the DC (age 1)")
            for m in materials:
                trace.rm_ages.append(_age_row({"seed": s, "material": m.name, "week": t,
                                               "stage": "start (after ageing + receipts)"},
                                              rm_stock[m.name][s]))
                flow(s, t, f"Supplier {m.name}", "RMW", m.name, rm_rec[m.name]["receipts"][s, t],
                     "supplier delivery arrives at the RMW (age 1)")

        # ---------------- Step 3a: DC ordering ----------------
        ordered_Q, dc_expected_waste = {}, {}
        for p in products:
            on_hand = dc_stock[p.name][:, 1:].sum(axis=1)
            pipeline = dc_pipeline[p.name].copy()
            position = on_hand + pipeline

            # subtract stock expected to expire before a new order arrives
            window = [mean_channel_demand(p, t + w) for w in range(release_to_dc_median[p.name])]
            expected_waste = projected_fg_waste(dc_stock[p.name], window, channel_max_age[p.name],
                                                 channel_priority[p.name])
            dc_expected_waste[p.name] = expected_waste
            effective = position - expected_waste

            capped = np.zeros(n, dtype=bool)
            if t in dc_can_order[p.name]:
                trigger = effective < schedule.dc_s[p.name][t]
                need = schedule.dc_S[p.name][t] - effective
                Q = np.where(trigger, round_up_to_order_rules(need, p.batch_size, p.moq), 0)
                # order cap (multiple of the batch size)
                cap = max(p.moq, (schedule.dc_cap[p.name] // p.batch_size) * p.batch_size)
                capped = Q > cap
                Q = np.minimum(Q, cap)
            else:
                Q = np.zeros(n, dtype=np.int64)
            if t == 1 and p.name in week1_orders["dc"]:    # lookahead candidate for week 1
                Q = np.full(n, week1_orders["dc"][p.name], dtype=np.int64)
                capped = np.zeros(n, dtype=bool)
            ordered_Q[p.name] = Q

            rec = dc_rec[p.name]
            rec["pipeline_before_order"][:, t] = pipeline
            rec["inventory_position"][:, t] = position
            rec["expected_waste"][:, t] = np.rint(expected_waste)
            rec["effective_position"][:, t] = np.rint(effective)
            rec["cut_by_policy_cap"][:, t] = capped
            rec["ordered_Q"][:, t] = Q

        # ---------------- Step 3b + 3c: release, cancellation, RM transport ----------------
        usable_rm = {m.name: rm_stock[m.name][:, 1:].sum(axis=1) for m in materials}
        shipped_total = {m.name: np.zeros(n, dtype=np.int64) for m in materials}
        shipped_by_age = {m.name: np.zeros_like(rm_stock[m.name]) for m in materials} if trace_seeds else {}

        for p in products:                            # products share RM and capacity (list order)
            Q = ordered_Q[p.name]
            rm_limit = np.full(n, np.iinfo(np.int64).max)
            for mat_name, per_unit in p.bom.items():
                rm_limit = np.minimum(rm_limit, max_fg_from(per_unit, usable_rm[mat_name]))
            production_week = t + tau[p.name]               # all BOM materials at production: t + tau_p
            capacity_before = model.capacity_in_week(production_week) - capacity_used[:, production_week]
            limit = np.minimum(np.minimum(Q, rm_limit), capacity_before)
            P = round_down_to_supply_rules(limit, p.batch_size, p.moq)
            cancelled = Q - P

            # Diagnostics: which constraint was binding? (the smallest of order, RM, capacity)
            cut = cancelled > 0
            rm_binding = cut & (rm_limit == limit) & (rm_limit < Q)
            capacity_binding = cut & (capacity_before == limit) & (capacity_before < Q)
            for mat_name, per_unit in p.bom.items():
                rm_rec[mat_name]["limited_release"][:, t] |= rm_binding & (max_fg_from(per_unit, usable_rm[mat_name]) == limit)

            capacity_used[:, production_week] += P
            for mat_name, per_unit in p.bom.items():   # materials leave the RMW in the release week
                m = material_by_name[mat_name]
                shipped = rm_units_for(per_unit, P)    # per_unit x P, rounded up to whole units
                taken = withdraw_fifo(rm_stock[mat_name], shipped, m.max_shippable_age)
                if trace_seeds:
                    shipped_by_age[mat_name] += taken
                shipped_total[mat_name] += shipped
                usable_rm[mat_name] -= shipped

            arrival = schedule_arrivals(dc_arrivals[p.name], dc_last_arrival[p.name], production_week, P,
                                        scen.dc_lead_time[p.name][:, t])
            dc_pipeline[p.name] += np.where(arrival > t, P, 0)

            rec = dc_rec[p.name]
            rec["rm_limit"][:, t] = np.minimum(rm_limit, 10**9)
            rec["released_P"][:, t] = P
            rec["cancelled"][:, t] = cancelled
            rec["cut_by_rm"][:, t] = rm_binding
            rec["cut_by_capacity"][:, t] = capacity_binding

            # production costs are booked in the release week (the decision week)
            cost["Production fixed"][:, t] += p.fixed_cost_per_release * (P > 0)
            cost["Production variable"][:, t] += tier_unit_cost(P, p.production_tiers) * P
            cost["PF->DC transport"][:, t] += tier_unit_cost(P, p.transport_tiers) * P

            for s in trace_seeds:
                if Q[s] > 0:
                    lead = int(scen.dc_lead_time[p.name][s, t])
                    trace.orders.append({"seed": s, "lane": "PF -> DC (release)", "item": p.name,
                                         "order_week": t, "source": "DC rule" if not (t == 1 and p.name in week1_orders["dc"]) else "lookahead",
                                         "ordered": int(Q[s]), "released": int(P[s]),
                                         "cancelled": int(cancelled[s]),
                                         "cut_reason": ("raw material" if rm_binding[s] else
                                                        "capacity" if capacity_binding[s] else ""),
                                         "lead_time_draw": lead,
                                         "planned_arrival": production_week + lead if P[s] > 0 else None,
                                         "actual_arrival": int(arrival[s]) if P[s] > 0 else None,
                                         "held_up_by_earlier_order": bool(P[s] > 0 and arrival[s] > production_week + lead)})
                for mat_name, per_unit in p.bom.items():
                    flow(s, t, "RMW", "PF", mat_name, rm_units_for(per_unit, P[s]),
                         f"shipped for release of {p.name}; reaches production in week "
                         f"{t + material_by_name[mat_name].rmw_to_pf_lead_time}, produced in week {production_week}")
                    flow(s, t, "PF", "consumed", mat_name, rm_units_for(per_unit, P[s]),
                         f"transformed into {p.name} in week {production_week}")
                flow(s, t, "PF", "In transit (PF -> DC)", p.name, P[s],
                     f"produced in week {production_week}; arrives in week {int(arrival[s])}" if P[s] > 0 else "")
                if cancelled[s] > 0:
                    reason = "raw material short" if rm_binding[s] else "capacity exhausted"
                    flow(s, t, "DC order", "cancelled", p.name, cancelled[s],
                         f"not released: {reason} (release rounded down to batch/MOQ)")

        for m in materials:
            rm_rec[m.name]["shipped_T"][:, t] = shipped_total[m.name]
            cost["RMW->PF transport"][:, t] += m.transport_cost * shipped_total[m.name]

        # ---------------- Step 3d: RMW ordering on the echelon position ----------------
        # FG position after this week's releases (on hand + pipeline), once per product
        fg_position = {p.name: dc_stock[p.name][:, 1:].sum(axis=1) + dc_pipeline[p.name] for p in products}
        for m in materials:
            on_hand = rm_stock[m.name][:, 1:].sum(axis=1)
            pipeline = rm_pipeline[m.name].copy()
            downstream = np.zeros(n)                       # RM already inside FG
            downstream_waste = np.zeros(n)                 # ... of which expected to expire at the DC
            users = users_of[m.name]
            for p in users:
                downstream += p.bom[m.name] * fg_position[p.name]
                downstream_waste += p.bom[m.name] * dc_expected_waste[p.name]
            echelon = on_hand + pipeline + downstream

            # RM expected to expire before a new supplier order arrives
            weekly_use = [sum(p.bom[m.name] * sum(mean_channel_demand(p, t + j + release_to_dc_median[p.name]))
                              for p in users) for j in range(m.lead_time_median)]
            rm_waste = projected_rm_waste(rm_stock[m.name], weekly_use, m.max_shippable_age)
            effective_echelon = echelon - rm_waste - downstream_waste
            installation = on_hand + pipeline - rm_waste   # physical RM at / on the way to the RMW

            floor_triggered = np.zeros(n, dtype=bool)
            if t in rm_can_order[m.name]:
                trigger = effective_echelon < schedule.rm_s[m.name][t]
                need = np.where(trigger, schedule.rm_S[m.name][t] - effective_echelon, 0.0)
                # minimum physical stock at the RMW
                floor_triggered = installation < schedule.rm_floor[m.name]
                need = np.maximum(need, np.where(floor_triggered, schedule.rm_floor[m.name] - installation, 0.0))
                O = round_up_to_order_rules(need, m.batch_size, m.moq)
                if m.supplier_capacity is not None:
                    O = round_down_to_supply_rules(np.minimum(O, m.supplier_capacity), m.batch_size, m.moq)
            else:
                O = np.zeros(n, dtype=np.int64)
            if t == 1 and m.name in week1_orders["rm"]:    # lookahead candidate for week 1
                O = np.full(n, week1_orders["rm"][m.name], dtype=np.int64)
            arrival = schedule_arrivals(rm_arrivals[m.name], rm_last_arrival[m.name], t, O,
                                        scen.rm_lead_time[m.name][:, t])
            rm_pipeline[m.name] += np.where(arrival > t, O, 0)

            rec = rm_rec[m.name]
            rec["pipeline_before_order"][:, t] = pipeline
            rec["echelon_position"][:, t] = np.rint(echelon)
            rec["expected_waste"][:, t] = np.rint(rm_waste)
            rec["effective_echelon_position"][:, t] = np.rint(effective_echelon)
            rec["installation_position"][:, t] = np.rint(installation)
            rec["floor_triggered"][:, t] = floor_triggered
            rec["ordered_O"][:, t] = O
            cost["RM purchase"][:, t] += m.unit_cost * O
            cost["RM order fixed"][:, t] += m.fixed_order_cost * (O > 0)

            for s in trace_seeds:
                if O[s] > 0:
                    lead = int(scen.rm_lead_time[m.name][s, t])
                    trace.orders.append({"seed": s, "lane": "Supplier -> RMW", "item": m.name,
                                         "order_week": t, "source": "RMW rule" if not (t == 1 and m.name in week1_orders["rm"]) else "lookahead",
                                         "ordered": int(O[s]), "released": int(O[s]), "cancelled": 0,
                                         "lead_time_draw": lead, "planned_arrival": t + lead,
                                         "actual_arrival": int(arrival[s]),
                                         "held_up_by_earlier_order": bool(arrival[s] > t + lead)})
                row = _age_row({"seed": s, "material": m.name, "week": t}, shipped_by_age[m.name][s])
                trace.shipped_by_age.append(row)

        # ---------------- Step 4 + 5: demand, allocation, service ----------------
        for p in products:
            demands = [scen.demand[(p.name, c.name)][:, t] for c in p.channels]
            sales, lost = allocate_demand(dc_stock[p.name], demands, channel_max_age[p.name],
                                          channel_priority[p.name])
            sold_by_channel = [sales[i][:, 1:].sum(axis=1) for i in range(len(demands))]

            for i, c in enumerate(p.channels):
                sold = sold_by_channel[i]
                rec = ch_rec[(p.name, c.name)]
                rec["demand"][:, t] = demands[i]
                rec["sales"][:, t] = sold
                rec["lost_sales"][:, t] = lost[i]
                with np.errstate(invalid="ignore", divide="ignore"):
                    fill[(p.name, c.name)][:, t] = np.where(demands[i] > 0, 1.0 - lost[i] / demands[i], np.nan)
                for s in trace_seeds:
                    trace.sales_by_age.append(_age_row({"seed": s, "product": p.name, "week": t,
                                                        "channel": c.name}, sales[i][s]))
                    flow(s, t, "DC", f"Channel {c.name}", p.name, sold[s], "sales")
                    flow(s, t, f"Channel {c.name}", "lost sales", p.name, lost[i][s],
                         "unmet demand (no physical flow)")
            dc_rec[p.name]["demand"][:, t] = sum(demands)
            dc_rec[p.name]["sales"][:, t] = sum(sold_by_channel)
            dc_rec[p.name]["lost_sales"][:, t] = sum(lost)

        # ---------------- Step 6: end of week (waste, holding) ----------------
        for p in products:
            stock = dc_stock[p.name]
            waste = stock[:, p.max_sellable_age].copy()      # oldest sellable age -> scrapped
            stock[:, p.max_sellable_age] = 0
            dc_rec[p.name]["waste"][:, t] = waste
            dc_rec[p.name]["on_hand_end"][:, t] = stock[:, 1:].sum(axis=1)
            if report_details:
                dc_life[p.name][:, t] = average_remaining_life(stock, p.shelf_life)
            dc_rec[p.name]["pipeline_end"][:, t] = dc_pipeline[p.name]
            cost["FG waste"][:, t] += p.waste_cost * waste
            cost["FG holding"][:, t] += p.holding_cost * stock[:, 1:].sum(axis=1)
            for s in trace_seeds:
                flow(s, t, "DC", "waste", p.name, waste[s], "expired: too old for every channel")
                trace.dc_ages.append(_age_row({"seed": s, "product": p.name, "week": t,
                                               "stage": "end (after sales + waste)"}, stock[s]))
        for m in materials:
            stock = rm_stock[m.name]
            waste = stock[:, m.max_shippable_age].copy()
            stock[:, m.max_shippable_age] = 0
            rm_rec[m.name]["waste"][:, t] = waste
            rm_rec[m.name]["on_hand_end"][:, t] = stock[:, 1:].sum(axis=1)
            if report_details:
                rm_life[m.name][:, t] = average_remaining_life(stock, m.shelf_life)
            rm_rec[m.name]["pipeline_end"][:, t] = rm_pipeline[m.name]
            cost["RM waste"][:, t] += m.waste_cost * waste
            cost["RM holding"][:, t] += m.holding_cost * stock[:, 1:].sum(axis=1)
            for s in trace_seeds:
                flow(s, t, "RMW", "waste", m.name, waste[s], "expired: can no longer be shipped")
                trace.rm_ages.append(_age_row({"seed": s, "material": m.name, "week": t,
                                               "stage": "end (after shipment + waste)"}, stock[s]))
    # =======================================================================

    return SimResult(scen.name, n, H, dc_rec, rm_rec, ch_rec, fill, cost, trace, dc_life, rm_life)
