"""
Unit tests of the supply-chain mechanics (spec v5, Section 17).

Run with:   python -m pytest -q tests
      or:   python tests/test_mechanics.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meio import service
from meio.config import (VARIANT_A, VARIANT_B, Channel, InitialState, Material, ModelInput, Product,
                         SearchSettings, Tier, build_example_input, median_of)
from meio.policy import PolicySchedule, initial_schedule
from meio.scenarios import build_scenarios
from meio.simulation import (allocate_demand, projected_fg_waste, projected_rm_waste,
                             round_down_to_supply_rules, round_up_to_order_rules, schedule_arrivals,
                             simulate, tier_unit_cost, withdraw_fifo)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def test_order_rounding():
    need = np.array([0, 1, 20, 21, 75, -5])
    assert list(round_up_to_order_rules(need, batch=20, moq=60)) == [0, 60, 60, 60, 80, 0]


def test_cut_release_respects_supply_rules():
    qty = np.array([0, 59, 60, 79, 80, 135])
    assert list(round_down_to_supply_rules(qty, batch=20, moq=60)) == [0, 0, 60, 60, 80, 120]


def test_all_units_tiers_at_boundaries():
    tiers = [Tier(0, 199, 3.0), Tier(200, 399, 2.7), Tier(400, 10**9, 2.5)]
    qty = np.array([0, 199, 200, 399, 400, 1000])
    assert list(tier_unit_cost(qty, tiers)) == [3.0, 3.0, 2.7, 2.7, 2.5, 2.5]


def test_withdraw_fifo_takes_oldest_first():
    stock = np.array([[0, 10, 10, 10, 10]])          # ages 1..4, 10 units each
    taken = withdraw_fifo(stock, np.array([25]), max_age=4)
    assert list(taken[0]) == [0, 0, 5, 10, 10]
    assert list(stock[0]) == [0, 10, 5, 0, 0]


def test_withdraw_fifo_refuses_to_create_stock():
    stock = np.array([[0, 5, 5]])
    try:
        withdraw_fifo(stock, np.array([11]), max_age=2)
    except RuntimeError:
        return
    raise AssertionError("expected an error when withdrawing more than the stock")


def test_allocation_hand_example():
    """3 channels. Ages accepted: A <= 2 (strict), B <= 4, C <= 6.
    Stock: age 6: 5, age 4: 10, age 2: 10, age 1: 10. Demand: A 12, B 8, C 20."""
    stock = np.zeros((1, 7), dtype=np.int64)
    stock[0, [1, 2, 4, 6]] = [10, 10, 10, 5]
    demand = [np.array([12]), np.array([8]), np.array([20])]
    sales, lost = allocate_demand(stock, demand, max_age=[2, 4, 6], priority=[0, 1, 2])
    # age 6 -> only C (5). age 4 -> B first (8), C gets 2. age 2 -> A first (10), then C (0 left).
    # age 1 -> A takes 2 more, C takes 8. C total 5 + 2 + 8 = 15 -> lost 5.
    assert list(sales[0][0]) == [0, 2, 10, 0, 0, 0, 0]
    assert list(sales[1][0]) == [0, 0, 0, 0, 8, 0, 0]
    assert list(sales[2][0]) == [0, 8, 0, 0, 2, 0, 5]
    assert [int(x[0]) for x in lost] == [0, 0, 5]
    assert stock[0].sum() == 0


def test_allocation_bucket_first_equals_channel_first():
    """Oldest-bucket-first with channel priority gives the same result as serving the
    channels one after another (strictest first, oldest eligible). Randomised check."""
    rng = np.random.default_rng(1)
    for _ in range(3000):
        n_ch = rng.integers(1, 4)
        max_age = sorted(rng.integers(1, 10, size=n_ch).tolist())
        stock = np.zeros((1, 11), dtype=np.int64)
        stock[0, 1:] = rng.integers(0, 6, size=10)
        demand = [np.array([rng.integers(0, 15)]) for _ in range(n_ch)]
        s1 = stock.copy()
        sales, lost = allocate_demand(s1, demand, max_age, list(range(n_ch)))
        s2 = stock.copy()
        for c in range(n_ch):                              # channel-first reference
            open_d = int(demand[c][0])
            for age in range(max_age[c], 0, -1):
                take = min(s2[0, age], open_d)
                s2[0, age] -= take
                open_d -= take
            assert open_d == lost[c][0]
        assert (s1 == s2).all()


def test_arrivals_are_order_preserving():
    arrivals = np.zeros((1, 30), dtype=np.int64)
    last = np.zeros(1, dtype=np.int64)
    a1 = schedule_arrivals(arrivals, last, week=1, quantity=np.array([10]), lead_time=np.array([8]))
    a2 = schedule_arrivals(arrivals, last, week=2, quantity=np.array([10]), lead_time=np.array([5]))
    a3 = schedule_arrivals(arrivals, last, week=3, quantity=np.array([0]), lead_time=np.array([2]))
    a4 = schedule_arrivals(arrivals, last, week=4, quantity=np.array([10]), lead_time=np.array([6]))
    assert (a1[0], a2[0], a3[0], a4[0]) == (9, 9, 0, 10)   # order 2 held up; no order in week 3
    assert arrivals[0, 9] == 20 and arrivals[0, 10] == 10


# ---------------------------------------------------------------------------
# Whole simulation
# ---------------------------------------------------------------------------
def _example_run(n_seeds=300, trace=(0, 1)):
    model = build_example_input()
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=1000))
    seeds = build_scenarios(model, n_seeds, 11, "test")
    return model, schedule, simulate(model, schedule, seeds, trace_seeds=list(trace))


def test_conservation_of_units():
    model, _, r = _example_run()
    dc = r.dc["FG1"]
    init = sum(model.initial_state.dc_stock["FG1"].values())
    bal = init + dc["receipts"][:, 1:].sum(1) - dc["sales"][:, 1:].sum(1) - dc["waste"][:, 1:].sum(1) - dc["on_hand_end"][:, -1]
    assert np.abs(bal).max() == 0
    for m in model.materials:
        rm = r.rm[m.name]
        init = sum(model.initial_state.rm_stock[m.name].values())
        bal = init + rm["receipts"][:, 1:].sum(1) - rm["shipped_T"][:, 1:].sum(1) - rm["waste"][:, 1:].sum(1) - rm["on_hand_end"][:, -1]
        assert np.abs(bal).max() == 0
        assert (rm["shipped_T"] == dc["released_P"] * model.products[0].bom[m.name]).all()


def test_echelon_position_is_not_changed_by_internal_moves():
    """EIP(t+1) = EIP(t) + O(t) - RM waste(t) - a * (FG sales(t) + FG waste(t)).
    Shipping RM to production and producing FG do not change the echelon position."""
    model, _, r = _example_run()
    dc = r.dc["FG1"]
    for m in model.materials:
        rm, a = r.rm[m.name], model.products[0].bom[m.name]
        eip = rm["echelon_position"]
        predicted = eip[:, 1:-1] + rm["ordered_O"][:, 1:-1] - rm["waste"][:, 1:-1] \
            - a * (dc["sales"][:, 1:-1] + dc["waste"][:, 1:-1])
        assert (predicted == eip[:, 2:]).all()


def test_trace_orders_arrive_in_order():
    _, _, r = _example_run(n_seeds=50, trace=(0, 1, 2))
    orders = pd.DataFrame(r.trace.orders)
    for (seed, lane, item), grp in orders.groupby(["seed", "lane", "item"]):
        arr = grp.sort_values("order_week")["actual_arrival"].dropna().to_numpy()
        assert (np.diff(arr) >= 0).all(), f"orders overtake each other on {lane} {item}"


def test_week1_decisions_identical_in_all_seeds():
    model, _, r = _example_run()
    assert len(np.unique(r.dc["FG1"]["ordered_Q"][:, 1])) == 1
    for m in model.materials:
        assert len(np.unique(r.rm[m.name]["ordered_O"][:, 1])) == 1


def _tiny_model(stock_b: int) -> ModelInput:
    """1 product from materials A and B (1 each), deterministic lead times."""
    fg = Product("F", shelf_life=6, channels=[Channel("Only", 1, 0.9)], bom={"A": 1, "B": 1},
                 batch_size=10, moq=30, holding_cost=0.1, waste_cost=1, fixed_cost_per_release=10,
                 production_tiers=[Tier(0, 10**9, 1.0)], transport_tiers=[Tier(0, 10**9, 0.1)],
                 lead_time_dist={2: 1.0})
    mats = [Material(x, shelf_life=10, min_life_at_shipment=1, batch_size=10, moq=10, unit_cost=1,
                     fixed_order_cost=1, holding_cost=0.01, waste_cost=1, transport_cost=0.1,
                     lead_time_dist={3: 1.0}) for x in ("A", "B")]
    H = 6
    mean = {("F", "Only"): np.full(H + 1, 0.0)}
    from meio.config import DemandForecast
    demand = DemandForecast(mean=mean, sd={("F", "Only"): np.zeros(H + 1)})
    state = InitialState(dc_stock={"F": {}}, rm_stock={"A": {1: 500}, "B": {1: stock_b}},
                         dc_pipeline={"F": []}, rm_pipeline={"A": [], "B": []})
    return ModelInput(horizon=H, products=[fg], materials=mats, demand=demand,
                      initial_state=state, production_capacity=1000)


def test_release_is_cut_by_the_short_material_and_rounded_to_supply_rules():
    model = _tiny_model(stock_b=57)          # B allows only 57 units -> release 50 (batch 10, MOQ 30)
    H = model.horizon
    sched = PolicySchedule(dc_s={"F": np.full(H + 1, 100)}, dc_S={"F": np.full(H + 1, 100)},
                           rm_s={"A": np.zeros(H + 1, int), "B": np.zeros(H + 1, int)},
                           rm_S={"A": np.zeros(H + 1, int), "B": np.zeros(H + 1, int)})
    r = simulate(model, sched, build_scenarios(model, 3, 1, "t"))
    dc = r.dc["F"]
    assert dc["ordered_Q"][0, 1] == 100
    assert dc["released_P"][0, 1] == 50
    assert dc["cancelled"][0, 1] == 50
    assert dc["cut_by_rm"][0, 1] == 1 and dc["cut_by_capacity"][0, 1] == 0
    assert r.rm["B"]["limited_release"][0, 1] == 1 and r.rm["A"]["limited_release"][0, 1] == 0
    assert r.rm["A"]["shipped_T"][0, 1] == 50 and r.rm["B"]["shipped_T"][0, 1] == 50
    # week 2: B has 7 left -> below MOQ 30 -> nothing is released
    assert dc["released_P"][0, 2] == 0 and dc["cancelled"][0, 2] == dc["ordered_Q"][0, 2]
    # the release of week 1 arrives in week 1 + 2 = 3
    assert dc["receipts"][0, 3] == 50


# ---------------------------------------------------------------------------
# Fill-rate rules
# ---------------------------------------------------------------------------
def test_fill_rate_rules():
    cells = pd.DataFrame({"product": ["F"] * 3, "channel": ["C"] * 3, "week": [9, 10, 11],
                          "target_F": [0.95] * 3, "n_seeds_with_demand": [100, 100, 0],
                          "mean_fill": [0.97, 0.955, np.nan], "se": [0.005, 0.001, 0.0]})
    margins = {("F", "C", 10): 0.01}
    out = service.apply_search_rule(cells, z=2.0, margins=margins)
    # week 9: 0.97 - 0.01 - 0 = 0.96 >= 0.95 ok ; week 10: 0.955 - 0.002 - 0.01 = 0.943 fails ; week 11: no demand
    assert list(out["search_feasible"]) == [True, False, True]

    hold = cells.copy()
    hold["mean_fill"] = [0.949, 0.96, np.nan]
    weak = service.holdout_check(hold, margins, min_margin_bump=0.005)
    assert list(weak["week"]) == [9]
    assert abs(margins[("F", "C", 9)] - 0.005) < 1e-12        # max(0.005, 0.95 - 0.949 = 0.001)

    final = service.final_verdict(hold)
    assert list(final["test_pass"]) == [False, True, True]


# ---------------------------------------------------------------------------
# Policy variants B / C
# ---------------------------------------------------------------------------
def test_median_lead_time():
    assert median_of({5: 0.20, 6: 0.55, 7: 0.20, 8: 0.05}) == 6
    assert median_of({10: 0.3, 12: 0.4, 14: 0.3}) == 12
    assert median_of({4: 0.5, 5: 0.3, 6: 0.2}) == 4


def test_projected_fg_waste_hand_example():
    """50 units of age 9 (oldest sellable age 10), mean demand 20 per week for 3 weeks.
    Week 0: 20 used, 30 left. Week 1: now age 10, 20 used, 10 left -> expire. Waste = 10.
    A strict channel (accepts age <= 3) cannot take these units, so it does not change the result."""
    stock = np.zeros((1, 11))
    stock[0, 9] = 50
    one_channel = projected_fg_waste(stock, [[20.0]] * 3, max_age=[10], priority=[0])
    two_channels = projected_fg_waste(stock, [[100.0, 20.0]] * 3, max_age=[3, 10], priority=[0, 1])
    assert abs(one_channel[0] - 10) < 1e-9 and abs(two_channels[0] - 10) < 1e-9
    assert stock[0, 9] == 50                                  # input not changed


def test_projected_rm_waste_matches_brute_force_fifo():
    rng = np.random.default_rng(3)
    for _ in range(500):
        max_age = int(rng.integers(3, 12))
        stock = np.zeros((1, max_age + 1))
        stock[0, 1:] = rng.integers(0, 40, size=max_age)
        use = rng.uniform(0, 30, size=int(rng.integers(1, 8))).tolist()
        closed = projected_rm_waste(stock, use, max_age)[0]
        proj, waste = stock.astype(float).copy(), 0.0          # brute force: week by week
        for u in use:
            left = u
            for age in range(max_age, 0, -1):
                take = min(proj[0, age], left)
                proj[0, age] -= take
                left -= take
            waste += proj[0, max_age]
            proj[0, max_age] = 0
            proj[0, 2:] = proj[0, 1:-1].copy()
            proj[0, 1] = 0
        assert abs(closed - waste) < 1e-6


def _tiny_schedule(model, dc_level=100, rm_level=0, cap=1000, floor=0):
    H = model.horizon
    return PolicySchedule(dc_s={"F": np.full(H + 1, dc_level)}, dc_S={"F": np.full(H + 1, dc_level)},
                          rm_s={"A": np.full(H + 1, rm_level), "B": np.full(H + 1, rm_level)},
                          rm_S={"A": np.full(H + 1, rm_level), "B": np.full(H + 1, rm_level)},
                          dc_cap={"F": cap}, rm_floor={"A": floor, "B": floor})


def test_order_cap_limits_dc_order_in_variant_b_only():
    model = _tiny_model(stock_b=500)
    seeds = build_scenarios(model, 2, 1, "t")
    a = simulate(model, _tiny_schedule(model, cap=40), seeds, variant=VARIANT_A)
    b = simulate(model, _tiny_schedule(model, cap=40), seeds, variant=VARIANT_B)
    assert a.dc["F"]["ordered_Q"][0, 1] == 100
    assert b.dc["F"]["ordered_Q"][0, 1] == 40 and b.dc["F"]["cut_by_policy_cap"][0, 1] == 1


def test_rmw_minimum_triggers_an_order_in_variant_b_only():
    """B has 57 units; the week-1 release uses 50 -> 7 left. With a minimum of 200 the RMW
    orders 200 - 7 = 193 -> rounded up to 200. Variant A ignores the minimum."""
    model = _tiny_model(stock_b=57)
    seeds = build_scenarios(model, 2, 1, "t")
    a = simulate(model, _tiny_schedule(model, floor=200), seeds, variant=VARIANT_A)
    b = simulate(model, _tiny_schedule(model, floor=200), seeds, variant=VARIANT_B)
    assert a.rm["B"]["ordered_O"][0, 1] == 0
    assert b.rm["B"]["installation_position"][0, 1] == 7
    assert b.rm["B"]["floor_triggered"][0, 1] == 1 and b.rm["B"]["ordered_O"][0, 1] == 200


def test_week1_override_with_the_rules_own_orders_changes_nothing():
    model = build_example_input()
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=1000))
    seeds = build_scenarios(model, 100, 5, "t")
    for variant in (VARIANT_A, VARIANT_B):
        plain = simulate(model, schedule, seeds, variant=variant)
        orders = {"dc": {"FG1": int(plain.dc["FG1"]["ordered_Q"][0, 1])},
                  "rm": {m.name: int(plain.rm[m.name]["ordered_O"][0, 1]) for m in model.materials}}
        forced = simulate(model, schedule, seeds, variant=variant, week1_orders=orders)
        assert np.array_equal(plain.total_cost_per_seed(), forced.total_cost_per_seed())
        assert np.array_equal(plain.dc["FG1"]["on_hand_end"], forced.dc["FG1"]["on_hand_end"])


def test_lookahead_never_costlier_and_never_worse_in_service_than_the_rule():
    from meio.config import VARIANT_C
    from meio.lookahead import lookahead_week1
    model = build_example_input()
    settings = SearchSettings(n_quantile_samples=1000)
    schedule = initial_schedule(model, settings)
    seeds = build_scenarios(model, 60, 9, "la")
    orders, rule_orders, log = lookahead_week1(model, schedule, VARIANT_C, seeds, settings, {}, set())
    for (step, item), grp in log.groupby(["step", "item"]):
        chosen = grp[grp["chosen"]].iloc[0]
        rule = grp[grp["is_rule_quantity"]].iloc[0]
        assert chosen["acceptable"] and chosen["mean_cost"] <= rule["mean_cost"]
        assert chosen["failing_cells"] <= rule["failing_cells"]


# ---------------------------------------------------------------------------
# JSON interface
# ---------------------------------------------------------------------------
def test_json_round_trip_gives_identical_simulation():
    from meio.io_json import model_from_dict, model_to_dict
    import json
    model = build_example_input()
    again = model_from_dict(json.loads(json.dumps(model_to_dict(model))))
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=500))
    a = simulate(model, schedule, build_scenarios(model, 50, 3, "t"), variant=VARIANT_B)
    b = simulate(again, schedule, build_scenarios(again, 50, 3, "t"), variant=VARIANT_B)
    assert np.array_equal(a.total_cost_per_seed(), b.total_cost_per_seed())


def test_example_input_file_is_valid():
    from meio.config import validate_input
    from meio.io_json import load_model
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "example_input.json")
    validate_input(load_model(path))


if __name__ == "__main__":
    tests = [(name, f) for name, f in sorted(globals().items()) if name.startswith("test_")]
    for name, f in tests:
        f()
        print(f"ok   {name}")
    print(f"\nall {len(tests)} tests passed")
