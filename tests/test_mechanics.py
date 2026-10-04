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
from meio.config import (Channel, InitialState, Material, ModelInput, Product, SearchSettings, Tier,
                         build_example_input, max_fg_from, median_of, rm_units_for)
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
        assert (rm["shipped_T"] == rm_units_for(model.products[0].bom[m.name], dc["released_P"])).all()


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
                           rm_S={"A": np.zeros(H + 1, int), "B": np.zeros(H + 1, int)},
                           dc_cap={"F": 1000}, rm_floor={"A": 0, "B": 0})
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
# Ordering policy: expected waste, order cap, RMW minimum, lookahead
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


def test_order_cap_limits_the_dc_order():
    """The rule asks for 100. A cap of 40 cuts the order to 40; a cap of 1000 does not bind."""
    model = _tiny_model(stock_b=500)
    seeds = build_scenarios(model, 2, 1, "t")
    loose = simulate(model, _tiny_schedule(model, cap=1000), seeds)
    tight = simulate(model, _tiny_schedule(model, cap=40), seeds)
    assert loose.dc["F"]["ordered_Q"][0, 1] == 100 and loose.dc["F"]["cut_by_policy_cap"][0, 1] == 0
    assert tight.dc["F"]["ordered_Q"][0, 1] == 40 and tight.dc["F"]["cut_by_policy_cap"][0, 1] == 1


def test_rmw_minimum_triggers_an_order():
    """B has 57 units; the week-1 release uses 50 -> 7 left. The (s,S) levels are 0, so only
    the minimum can trigger: with 200 the RMW orders 200 - 7 = 193 -> rounded up to 200;
    with 0 it orders nothing."""
    model = _tiny_model(stock_b=57)
    seeds = build_scenarios(model, 2, 1, "t")
    none = simulate(model, _tiny_schedule(model, floor=0), seeds)
    some = simulate(model, _tiny_schedule(model, floor=200), seeds)
    assert none.rm["B"]["floor_triggered"][0, 1] == 0 and none.rm["B"]["ordered_O"][0, 1] == 0
    assert some.rm["B"]["installation_position"][0, 1] == 7
    assert some.rm["B"]["floor_triggered"][0, 1] == 1 and some.rm["B"]["ordered_O"][0, 1] == 200


def test_dc_position_subtracts_stock_expected_to_expire():
    """50 units of age 5 at the DC (oldest sellable age 5, no demand) all expire before a new
    order arrives. The rule therefore sees an effective position of 0 and orders up to
    S = 100, not 100 - 50 = 50 as a rule on the plain position would."""
    model = _tiny_model(stock_b=500)
    model.initial_state.dc_stock["F"] = {5: 50}
    r = simulate(model, _tiny_schedule(model), build_scenarios(model, 2, 1, "t"))
    dc = r.dc["F"]
    assert dc["inventory_position"][0, 1] == 50 and dc["expected_waste"][0, 1] == 50
    assert dc["effective_position"][0, 1] == 0 and dc["ordered_Q"][0, 1] == 100


def test_rm_echelon_position_subtracts_raw_material_expected_to_expire():
    """100 units of A at age 9 (oldest shippable age 9) with no RM use expire before a new
    supplier order arrives. The effective echelon position is 0 < s = 100, so the RMW orders
    100; a rule on the plain echelon position (100, not below s) would order nothing."""
    model = _tiny_model(stock_b=500)
    model.initial_state.rm_stock["A"] = {9: 100}
    r = simulate(model, _tiny_schedule(model, dc_level=0, rm_level=100), build_scenarios(model, 2, 1, "t"))
    rm = r.rm["A"]
    assert rm["echelon_position"][0, 1] == 100 and rm["effective_echelon_position"][0, 1] == 0
    assert rm["ordered_O"][0, 1] == 100


def test_average_remaining_shelf_life_at_end_of_week():
    """DC: 30 units age 2 and 10 units age 4, shelf life 6, no demand, no orders.
    End W1: (30*4 + 10*2) / 40 = 3.5. W2: ages 3 and 5 -> age 5 is scrapped -> 30 units, 3.0.
    W4: the last units reach age 5 and are scrapped -> no stock -> NaN.
    RMW A: 500 units age 1, shelf life 10, nothing shipped -> 9 in W1."""
    model = _tiny_model(stock_b=500)
    model.initial_state.dc_stock["F"] = {2: 30, 4: 10}
    r = simulate(model, _tiny_schedule(model, dc_level=0), build_scenarios(model, 2, 1, "t"))
    life = r.dc_remaining_life_end["F"]
    assert life[0, 1] == 3.5 and life[0, 2] == 3.0 and life[0, 3] == 2.0 and np.isnan(life[0, 4])
    assert r.dc["F"]["waste"][0, 2] == 10 and r.dc["F"]["waste"][0, 4] == 30
    assert r.rm_remaining_life_end["A"][0, 1] == 9


def test_schedule_without_order_cap_or_rmw_minimum_is_rejected():
    """The policy always applies the cap and the minimum, so a schedule must carry both."""
    levels = {"F": np.zeros(7)}
    for missing in ("dc_cap", "rm_floor"):
        parts = dict(dc_s=levels, dc_S=levels, rm_s=levels, rm_S=levels, dc_cap={"F": 10}, rm_floor={"A": 0})
        del parts[missing]
        try:
            PolicySchedule(**parts)
        except TypeError:
            continue
        raise AssertionError(f"PolicySchedule accepted a schedule without {missing}")


def test_only_one_policy_exists_in_the_code():
    """Fitness function: no policy variants, variant switches or benchmark script may come back."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    forbidden = re.compile(r"PolicyVariant|VARIANT_?[ABCS]?\b|--variant|\bvariant\b", re.IGNORECASE)
    sources = [os.path.join(root, f) for f in ("main.py", "rolling_demo.py", "api.py")]
    sources += [os.path.join(root, "meio", f) for f in os.listdir(os.path.join(root, "meio")) if f.endswith(".py")]
    for path in sources:
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                assert not forbidden.search(line), f"{os.path.relpath(path, root)}:{line_no}: {line.strip()}"
    assert not os.path.exists(os.path.join(root, "compare_policies.py"))


def test_week1_override_with_the_rules_own_orders_changes_nothing():
    model = build_example_input()
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=1000))
    seeds = build_scenarios(model, 100, 5, "t")
    plain = simulate(model, schedule, seeds)
    orders = {"dc": {"FG1": int(plain.dc["FG1"]["ordered_Q"][0, 1])},
              "rm": {m.name: int(plain.rm[m.name]["ordered_O"][0, 1]) for m in model.materials}}
    forced = simulate(model, schedule, seeds, week1_orders=orders)
    assert np.array_equal(plain.total_cost_per_seed(), forced.total_cost_per_seed())
    assert np.array_equal(plain.dc["FG1"]["on_hand_end"], forced.dc["FG1"]["on_hand_end"])


def test_lookahead_never_costlier_and_never_worse_in_service_than_the_rule():
    from meio.lookahead import lookahead_week1
    model = build_example_input()
    settings = SearchSettings(n_quantile_samples=1000)
    schedule = initial_schedule(model, settings)
    seeds = build_scenarios(model, 60, 9, "la")
    orders, rule_orders, log = lookahead_week1(model, schedule, seeds, settings, {}, set())
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
    a = simulate(model, schedule, build_scenarios(model, 50, 3, "t"))
    b = simulate(again, schedule, build_scenarios(again, 50, 3, "t"))
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


# ---------------------------------------------------------------------------
# RMW -> PF lead time (tau_r per material; tau_p = slowest material of the BOM)
# ---------------------------------------------------------------------------
def _with_tau(model, tau):
    """Copy of the model with the same RMW -> PF lead time for every material (int), or per
    material ({name: tau})."""
    from dataclasses import replace
    taus = tau if isinstance(tau, dict) else {m.name: tau for m in model.materials}
    return replace(model, materials=[replace(m, rmw_to_pf_lead_time=taus.get(m.name, 0)) for m in model.materials])


def _tau_model(tau, **changes):
    from dataclasses import replace
    return _with_tau(replace(_tiny_model(stock_b=500), **changes), tau)


def test_rmw_to_pf_lead_time_delays_the_fg_arrival_but_rm_leaves_in_the_release_week():
    """FG lead time 2, release of 100 in W1: arrival W3 with tau = 0, W4 with tau = 1.
    The raw material leaves the RMW in W1 in both cases."""
    for tau, arrival_week in ((0, 3), (1, 4)):
        model = _tau_model(tau)
        r = simulate(model, _tiny_schedule(model), build_scenarios(model, 2, 1, "t"))
        receipts = r.dc["F"]["receipts"][0]
        assert r.dc["F"]["released_P"][0, 1] == 100 and receipts[arrival_week] == 100
        assert receipts[1:arrival_week].sum() == 0
        assert r.rm["A"]["shipped_T"][0, 1] == 100 and r.rm["B"]["shipped_T"][0, 1] == 100


def test_capacity_of_the_production_week_limits_the_release():
    """tau = 1: a W1 release is produced in W2, so the capacity of W2 applies, not of W1."""
    in_w2 = _tau_model(1, capacity_overrides={2: 30})
    in_w1 = _tau_model(1, capacity_overrides={1: 30})
    for model, released in ((in_w2, 30), (in_w1, 100)):
        r = simulate(model, _tiny_schedule(model), build_scenarios(model, 2, 1, "t"))
        assert r.dc["F"]["released_P"][0, 1] == released


def test_order_weeks_and_evaluation_window_include_tau():
    """H = 6, L = 2. tau = 1: last DC order week 6 - 3 = 3, a closed production week 3 blocks
    ordering in week 2, evaluation starts after tau + L_max = 3. RM (G = 3): last week 6 - 3 - 3 = 0."""
    from dataclasses import replace
    from meio.policy import dc_order_weeks, rm_order_weeks
    base = _tau_model(0)
    closed = replace(base.products[0], closed_production_weeks=[3])
    for tau, dc_weeks, first_eval in ((0, [1, 2, 4], 3), (1, [1, 3], 4)):
        model = _tau_model(tau, products=[closed])
        assert dc_order_weeks(model, model.products[0]) == dc_weeks
        assert model.evaluation_weeks[0] == first_eval
    assert rm_order_weeks(_tau_model(0), _tau_model(0).materials[0]) == [1]
    assert rm_order_weeks(_tau_model(1), _tau_model(1).materials[0]) == []


def test_expected_waste_window_covers_tau_plus_lead_time():
    """40 units of age 3 (oldest sellable age 5), no demand. Window = tau + median L:
    2 weeks (tau = 0) -> they do not expire in time -> 0; 3 weeks (tau = 1) -> 40 expire,
    so the effective position drops and the order grows from 60 to 100."""
    for tau, waste, order in ((0, 0, 60), (1, 40, 100)):
        model = _tau_model(tau)
        model.initial_state.dc_stock["F"] = {3: 40}
        r = simulate(model, _tiny_schedule(model), build_scenarios(model, 2, 1, "t"))
        assert r.dc["F"]["expected_waste"][0, 1] == waste and r.dc["F"]["ordered_Q"][0, 1] == order


def test_open_releases_of_the_initial_state_also_need_tau():
    """A release of 50 in week 0 with L = 2 arrives in W2 (tau = 0) or W3 (tau = 1)."""
    for tau, week in ((0, 2), (1, 3)):
        model = _tau_model(tau)
        model.initial_state.dc_pipeline["F"] = [(0, 50)]
        r = simulate(model, _tiny_schedule(model, dc_level=0), build_scenarios(model, 2, 1, "t"))
        assert r.dc["F"]["receipts"][0, week] == 50 and r.dc["F"]["receipts"][0, 1:week].sum() == 0


def test_heuristic_start_levels_grow_with_tau():
    settings = SearchSettings(n_quantile_samples=1000)
    model = build_example_input()
    s0 = initial_schedule(_with_tau(model, 0), settings)
    s1 = initial_schedule(_with_tau(model, 1), settings)
    from meio.policy import dc_order_weeks
    fg0, fg1 = _with_tau(model, 0), _with_tau(model, 1)
    weeks = sorted(set(dc_order_weeks(fg0, fg0.products[0])) & set(dc_order_weeks(fg1, fg1.products[0])))
    assert 17 not in dc_order_weeks(fg1, fg1.products[0])          # closed production week 18 = 17 + tau
    assert (s1.dc_s["FG1"][weeks] > s0.dc_s["FG1"][weeks]).all()
    for mat in ("RM_A", "RM_D"):                    # about one more week of demand to cover
        assert (s1.rm_s[mat][1:12] - s0.rm_s[mat][1:12]).mean() >= 75


def test_lookahead_uses_the_capacity_of_the_production_week():
    """tau = 1 and capacity 100 in W2: DC candidates other than the rule's own quantity stay <= 100."""
    from dataclasses import replace
    from meio.lookahead import lookahead_week1
    model = replace(build_example_input(), capacity_overrides={2: 100})
    settings = SearchSettings(n_quantile_samples=1000)
    _, rule, log = lookahead_week1(model, initial_schedule(model, settings), build_scenarios(model, 30, 9, "la"),
                                   settings, {}, set())
    dc = log[log["step"] == "1 DC order"]
    assert dc[~dc["is_rule_quantity"]]["candidate"].max() <= 100


def test_rmw_to_pf_lead_time_in_json_and_validation():
    import json
    from dataclasses import replace
    from meio.config import validate_input
    from meio.io_json import model_from_dict, model_to_dict
    model = _with_tau(build_example_input(), {"RM_A": 0, "RM_B": 2, "RM_C": 1, "RM_D": 1})
    d = json.loads(json.dumps(model_to_dict(model)))
    again = model_from_dict(d)
    assert [m.rmw_to_pf_lead_time for m in again.materials] == [0, 2, 1, 1]
    assert again.rmw_to_pf_lead_time(again.products[0]) == 2
    del d["materials"][1]["rmw_to_pf_lead_time"]
    assert model_from_dict(d).materials[1].rmw_to_pf_lead_time == 0     # older input files: same week
    assert all(m.rmw_to_pf_lead_time == 1 for m in build_example_input().materials)
    for bad in (-1, 1.5, "1", True, model.horizon):
        materials = [replace(model.materials[0], rmw_to_pf_lead_time=bad)] + model.materials[1:]
        try:
            validate_input(replace(model, materials=materials))
        except ValueError as err:
            assert "RM_A: RMW -> PF lead time" in str(err)
            continue
        raise AssertionError(f"tau = {bad!r} was accepted")


def test_feeding_release_weeks_include_tau():
    """L = 2: FG arriving in W5 was released in W3 (tau = 0) or W2 (tau = 1)."""
    from meio.policy import feeding_release_weeks
    assert feeding_release_weeks(_tau_model(0), _tau_model(0).products[0], 5) == [3]
    assert feeding_release_weeks(_tau_model(1), _tau_model(1).products[0], 5) == [2]


def test_rm_expected_waste_uses_demand_tau_plus_lead_time_ahead():
    """RM A: 100 units at the oldest shippable age 9, no release in W1. Demand of 100 only in W3.
    The RM projection assumes releases serve demand tau + L weeks later, so W1's use is the
    demand of W3 with tau = 0 (the units are used, no waste expected) but of W4 with tau = 1
    (nothing uses them in time -> 100 expected to expire)."""
    for tau, waste in ((0, 0), (1, 100)):
        model = _tau_model(tau)
        model.demand.mean[("F", "Only")][3] = 100.0
        model.initial_state.rm_stock["A"] = {9: 100}
        r = simulate(model, _tiny_schedule(model, dc_level=0), build_scenarios(model, 2, 1, "t"))   # no release in W1
        assert r.rm["A"]["shipped_T"][0, 1] == 0 and r.rm["A"]["expected_waste"][0, 1] == waste


def test_production_waits_for_the_slowest_bom_material():
    """A needs 0 weeks, B needs 2 weeks from the RMW to production: tau_p = 2. Both leave the
    RMW in the release week W1; production is in W3; with L = 2 the FG arrives in W5."""
    model = _tau_model({"A": 0, "B": 2})
    assert model.rmw_to_pf_lead_time(model.products[0]) == 2 and model.production_week(model.products[0], 1) == 3
    r = simulate(model, _tiny_schedule(model), build_scenarios(model, 2, 1, "t"))
    assert r.rm["A"]["shipped_T"][0, 1] == 100 and r.rm["B"]["shipped_T"][0, 1] == 100
    assert r.dc["F"]["receipts"][0, 5] == 100 and r.dc["F"]["receipts"][0, 1:5].sum() == 0


def _two_product_model(capacity_week_2: int):
    """F1 made from A (tau 1), F2 made from B (tau 0); no demand; capacity 1000 except W2."""
    from meio.config import DemandForecast
    def fg(name, material):
        return Product(name, shelf_life=6, channels=[Channel("Only", 1, 0.9)], bom={material: 1},
                       batch_size=10, moq=30, holding_cost=0.1, waste_cost=1, fixed_cost_per_release=10,
                       production_tiers=[Tier(0, 10**9, 1.0)], transport_tiers=[Tier(0, 10**9, 0.1)],
                       lead_time_dist={2: 1.0})
    mats = [Material(x, shelf_life=10, min_life_at_shipment=1, batch_size=10, moq=10, unit_cost=1,
                     fixed_order_cost=1, holding_cost=0.01, waste_cost=1, transport_cost=0.1,
                     lead_time_dist={3: 1.0}, rmw_to_pf_lead_time=tau) for x, tau in (("A", 1), ("B", 0))]
    H = 6
    zeros = np.zeros(H + 1)
    demand = DemandForecast(mean={("F1", "Only"): zeros, ("F2", "Only"): zeros},
                            sd={("F1", "Only"): zeros, ("F2", "Only"): zeros})
    state = InitialState(dc_stock={"F1": {}, "F2": {}}, rm_stock={"A": {1: 500}, "B": {1: 500}},
                         dc_pipeline={"F1": [], "F2": []}, rm_pipeline={"A": [], "B": []})
    return ModelInput(horizon=H, products=[fg("F1", "A"), fg("F2", "B")], materials=mats, demand=demand,
                      initial_state=state, production_capacity=1000, capacity_overrides={2: capacity_week_2})


def test_products_with_different_tau_share_the_capacity_of_their_production_week():
    """Capacity 100 in W2. F1 (tau 1) orders 100 in W1 -> produced in W2 and uses all of it.
    F2 (tau 0) orders 100 in W2 -> also produced in W2 -> nothing left, cut by capacity.
    Without F1's order, F2 gets the full 100."""
    H = 6
    def schedule(f1_level):
        f2 = np.zeros(H + 1, int); f2[2] = 100                   # F2 orders only in W2
        f1 = np.zeros(H + 1, int); f1[1] = f1_level              # F1 orders only in W1
        zero = np.zeros(H + 1, int)
        return PolicySchedule(dc_s={"F1": f1, "F2": f2}, dc_S={"F1": f1, "F2": f2},
                              rm_s={"A": zero, "B": zero}, rm_S={"A": zero, "B": zero},
                              dc_cap={"F1": 1000, "F2": 1000}, rm_floor={"A": 0, "B": 0})
    model = _two_product_model(capacity_week_2=100)
    seeds = build_scenarios(model, 2, 1, "t")
    shared = simulate(model, schedule(100), seeds)
    assert shared.dc["F1"]["released_P"][0, 1] == 100
    assert shared.dc["F2"]["ordered_Q"][0, 2] == 100 and shared.dc["F2"]["released_P"][0, 2] == 0
    assert shared.dc["F2"]["cut_by_capacity"][0, 2] == 1
    alone = simulate(model, schedule(0), seeds)
    assert alone.dc["F2"]["released_P"][0, 2] == 100


# ---------------------------------------------------------------------------
# Speed-ups must not change results: fast paths against the step-by-step reference
# ---------------------------------------------------------------------------
def _withdraw_fifo_reference(stock, quantity, max_age):
    taken = np.zeros_like(stock)
    remaining = quantity.astype(np.int64).copy()
    for age in range(max_age, 0, -1):
        take = np.minimum(stock[:, age], remaining)
        stock[:, age] -= take
        taken[:, age] = take
        remaining -= take
    assert not (remaining > 0).any()
    return taken


def test_vectorised_withdraw_fifo_equals_the_age_by_age_reference():
    from meio.simulation import withdraw_fifo
    rng = np.random.default_rng(11)
    for _ in range(2000):
        n, ages = int(rng.integers(1, 6)), int(rng.integers(2, 14))
        stock = rng.integers(0, 60, size=(n, ages + 1))
        stock[:, 0] = 0
        max_age = int(rng.integers(1, ages + 1))
        quantity = (rng.random(n) * (stock[:, 1:max_age + 1].sum(axis=1) + 1)).astype(np.int64)
        quantity = np.minimum(quantity, stock[:, 1:max_age + 1].sum(axis=1))
        fast, reference = stock.copy(), stock.copy()
        assert np.array_equal(withdraw_fifo(fast, quantity, max_age),
                              _withdraw_fifo_reference(reference, quantity, max_age))
        assert np.array_equal(fast, reference)


def test_withdraw_fifo_still_refuses_more_than_the_stock():
    from meio.simulation import withdraw_fifo
    stock = np.array([[0, 5, 5, 5]])
    try:
        withdraw_fifo(stock, np.array([11]), 2)             # only ages 1-2 usable: 10 units
    except RuntimeError:
        return
    raise AssertionError("withdrawing more than the usable stock was accepted")


def test_vectorised_allocation_equals_the_age_by_age_reference():
    """Random stock, demand, shelf-life gates and ties; mostly the model's priority
    (strictest first), every fifth case an arbitrary priority order."""
    from meio.simulation import _allocate_demand_loop, allocate_demand
    rng = np.random.default_rng(12)
    for trial in range(3000):
        n, ages, k = int(rng.integers(1, 6)), int(rng.integers(2, 14)), int(rng.integers(1, 5))
        stock = rng.integers(0, 50, size=(n, ages + 1))
        stock[:, 0] = 0
        max_age = [int(a) for a in rng.integers(0, ages + 3, size=k)]
        priority = sorted(range(k), key=lambda c: (max_age[c], c))
        if trial % 5 == 0:
            priority = [int(c) for c in rng.permutation(k)]     # any order gives the same result
        demand = [rng.integers(0, 120, size=n) for _ in range(k)]
        fast, reference = stock.copy(), stock.copy()
        sales_f, lost_f = allocate_demand(fast, demand, max_age, priority)
        sales_r, lost_r = _allocate_demand_loop(reference, demand, max_age, priority)
        assert np.array_equal(fast, reference)
        for c in range(k):
            assert np.array_equal(sales_f[c], sales_r[c]) and np.array_equal(lost_f[c], lost_r[c])


def test_search_simulation_without_report_details_makes_the_same_decisions():
    model = build_example_input()
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=1000))
    seeds = build_scenarios(model, 50, 4, "d")
    full = simulate(model, schedule, seeds)
    lean = simulate(model, schedule, seeds, report_details=False)
    for p in model.products:
        for key in full.dc[p.name]:
            assert np.array_equal(full.dc[p.name][key], lean.dc[p.name][key])
    for m in model.materials:
        for key in full.rm[m.name]:
            assert np.array_equal(full.rm[m.name][key], lean.rm[m.name][key])
    assert np.array_equal(full.total_cost_per_seed(), lean.total_cost_per_seed())
    assert lean.dc_remaining_life_end == {} and len(full.dc_remaining_life_end) == len(model.products)


def test_projected_fg_waste_equals_the_projection_through_allocate_demand():
    """The projection runs its own allocation loop (no sales bookkeeping); it must give
    bit-identical floats to projecting with the reference allocation."""
    from meio.simulation import _allocate_demand_loop, projected_fg_waste

    def reference(stock, weekly, max_age, priority):
        proj = stock.astype(float)
        n, oldest = proj.shape[0], proj.shape[1] - 1
        waste = np.zeros(n)
        for w, means in enumerate(weekly):
            if w > 0:
                proj[:, 2:] = proj[:, 1:-1].copy()
                proj[:, 1] = 0.0
            _allocate_demand_loop(proj, [np.full(n, d) for d in means], max_age, priority)
            waste += proj[:, oldest]
            proj[:, oldest] = 0.0
        return waste

    rng = np.random.default_rng(13)
    for _ in range(1000):
        n, ages, k = int(rng.integers(1, 5)), int(rng.integers(2, 13)), int(rng.integers(1, 4))
        stock = rng.integers(0, 80, size=(n, ages + 1))
        stock[:, 0] = 0
        max_age = [int(a) for a in rng.integers(1, ages + 2, size=k)]
        priority = sorted(range(k), key=lambda c: (max_age[c], c))
        weekly = [[float(x) if rng.random() > 0.2 else 0.0 for x in rng.uniform(0, 40, size=k)]
                  for _ in range(int(rng.integers(1, 9)))]
        assert np.array_equal(projected_fg_waste(stock, weekly, max_age, priority),
                              reference(stock, weekly, max_age, priority))


# ---------------------------------------------------------------------------
# Fractional BOM quantities (e.g. 0.2 units of a material per FG unit)
# ---------------------------------------------------------------------------
def test_bom_quantities_whole_and_fractional_use_exact_integer_arithmetic():
    P = np.array([0, 1, 4, 5, 7, 5000, 12345])
    assert list(rm_units_for(1, P)) == list(P)                       # whole numbers: unchanged
    assert list(rm_units_for(3, P)) == list(3 * P)
    assert list(rm_units_for(0.2, P)) == [0, 1, 1, 1, 2, 1000, 2469]  # 0.2 x P rounded UP
    assert list(rm_units_for(1.5, P)) == [0, 2, 6, 8, 11, 7500, 18518]
    usable = np.array([0, 1, 3, 1000, 999, 7])
    assert list(max_fg_from(1, usable)) == list(usable)
    assert list(max_fg_from(3, usable)) == list(usable // 3)
    assert list(max_fg_from(0.2, usable)) == [0, 5, 15, 5000, 4995, 35]
    # max_fg_from is exactly the largest release whose RM need fits into the stock
    for q in (0.2, 0.25, 0.3, 1 / 3, 1, 1.5, 2, 7):
        for u in range(0, 200):
            k = max_fg_from(q, u)
            assert rm_units_for(q, k) <= u < rm_units_for(q, k + 1)


def _fractional_bom_model():
    from dataclasses import replace
    model = build_example_input()
    fg = replace(model.products[0], bom={"RM_A": 0.2, "RM_B": 1, "RM_C": 1, "RM_D": 1})
    return replace(model, products=[fg])


def test_fractional_bom_ships_the_rounded_up_share_and_conserves_units():
    from meio.tables import conservation_checks
    model = _fractional_bom_model()
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=500))
    r = simulate(model, schedule, build_scenarios(model, 100, 5, "t"))
    released = r.dc["FG1"]["released_P"]
    assert released.sum() > 0
    assert (r.rm["RM_A"]["shipped_T"] == -(-released // 5)).all()    # ceil(0.2 x P)
    assert (r.rm["RM_B"]["shipped_T"] == released).all()
    assert conservation_checks(model, r)["ok"].all()


def test_fractional_bom_limits_the_release_by_the_fractional_need():
    from dataclasses import replace
    model = _tiny_model(stock_b=500)
    model = replace(model, products=[replace(model.products[0], bom={"A": 0.25, "B": 1})],
                    initial_state=replace(model.initial_state, rm_stock={"A": {1: 14}, "B": {1: 500}}))
    H = model.horizon
    sched = PolicySchedule(dc_s={"F": np.full(H + 1, 100)}, dc_S={"F": np.full(H + 1, 100)},
                           rm_s={"A": np.zeros(H + 1, int), "B": np.zeros(H + 1, int)},
                           rm_S={"A": np.zeros(H + 1, int), "B": np.zeros(H + 1, int)},
                           dc_cap={"F": 1000}, rm_floor={"A": 0, "B": 0})
    r = simulate(model, sched, build_scenarios(model, 3, 1, "t"))
    # 14 units of A make at most 56 FG (0.25 each) -> release 50 (batch 10), uses ceil(12.5) = 13 A
    assert r.dc["F"]["released_P"][0, 1] == 50 and r.dc["F"]["cut_by_rm"][0, 1] == 1
    assert r.rm["A"]["shipped_T"][0, 1] == 13 and r.rm["A"]["limited_release"][0, 1] == 1


def test_fractional_bom_in_json_and_validation():
    import json
    from dataclasses import replace
    from meio.config import validate_input
    from meio.io_json import model_from_dict, model_to_dict
    model = _fractional_bom_model()
    again = model_from_dict(json.loads(json.dumps(model_to_dict(model))))
    assert again.products[0].bom == {"RM_A": 0.2, "RM_B": 1, "RM_C": 1, "RM_D": 1}
    assert isinstance(again.products[0].bom["RM_B"], int)              # 1 stays a whole number
    validate_input(again)
    for bad in (0, -1, 0.00001):
        fg = replace(model.products[0], bom={**model.products[0].bom, "RM_A": bad})
        try:
            validate_input(replace(model, products=[fg]))
        except ValueError as exc:
            assert "BOM quantity of RM_A" in str(exc)
        else:
            raise AssertionError(f"BOM quantity {bad} was accepted")


# ---------------------------------------------------------------------------
# Price-break round-up of the DC order (all-units discounts)
# ---------------------------------------------------------------------------
def _tier_product(**changes):
    from dataclasses import replace
    fg = Product("F", shelf_life=24, channels=[Channel("Only", 1, 0.9)], bom={"A": 1}, batch_size=100,
                 moq=5000, holding_cost=0.2, waste_cost=0.5, fixed_cost_per_release=250,
                 production_tiers=[Tier(5000, 10000, 1.3), Tier(10001, 15000, 0.9), Tier(15001, 10**9, 0.6)],
                 transport_tiers=[Tier(0, 3000, 0.8), Tier(3001, 10000, 0.6), Tier(10001, 10**9, 0.4)],
                 lead_time_dist={6: 1.0})
    return replace(fg, **changes)


def test_order_is_rounded_up_to_a_price_break_only_when_that_is_cheaper_in_total():
    from meio.simulation import price_break_quantities, round_up_to_price_break, variable_release_cost
    p = _tier_product()
    assert price_break_quantities(p) == [3100, 10100, 15100]
    q = np.array([0, 5000, 6900, 7000, 9900, 10100, 11600, 11700, 15100, 20000])
    out = round_up_to_price_break(q, p, cap=30000)
    # 5000 / 6900 x 1.90 <= 10100 x 1.30 = 13,130 -> keep; 7000 x 1.90 = 13,300 -> 10100;
    # 11600 x 1.30 = 15,080 < 15,100 -> keep; 11700 x 1.30 = 15,210 -> 15100
    assert list(out) == [0, 5000, 6900, 10100, 10100, 10100, 11600, 15100, 15100, 20000]
    assert (variable_release_cost(out, p) <= variable_release_cost(q, p)).all()   # never dearer
    assert (out >= q).all()                                                         # never smaller
    # the order cap is respected: no break above it
    assert list(round_up_to_price_break(np.array([9900, 14000]), p, cap=12000)) == [10100, 14000]
    # without discount bands nothing changes
    flat = _tier_product(production_tiers=[Tier(0, 10**9, 1.0)], transport_tiers=[Tier(0, 10**9, 0.5)])
    assert list(round_up_to_price_break(q, flat, cap=30000)) == list(q)


def test_dc_order_in_the_simulation_uses_the_price_break_round_up():
    """A steep discount at 300 units (3.00 -> 1.00, transport 0.50): an order of Q < 300 costs
    3.50 Q, 300 units cost 450, so every order above 128 units is raised to 300 as long as the
    cap allows it (batch 20: from 140 on); smaller orders stay."""
    from dataclasses import replace
    model = build_example_input()
    p = replace(model.products[0], production_tiers=[Tier(0, 299, 3.0), Tier(300, 10**9, 1.0)],
                transport_tiers=[Tier(0, 10**9, 0.5)])
    model = replace(model, products=[p])
    schedule = initial_schedule(model, SearchSettings(n_quantile_samples=300))
    seeds = build_scenarios(model, 50, 4, "t")
    for cap, expect_rounding in ((1000, True), (280, False)):
        schedule.dc_cap["FG1"] = cap
        dc = simulate(model, schedule, seeds, report_details=False).dc["FG1"]
        Q = dc["ordered_Q"][:, 1:]
        if expect_rounding:
            assert (Q == 300).any()
            assert not ((Q >= 140) & (Q < 300)).any()    # always cheaper to order 300
        else:
            assert not (Q == 300).any()                    # break above the cap: no round-up
        assert (dc["released_P"][:, 1:] <= Q).all()


# ---------------------------------------------------------------------------
# Search: start schedules, block moves, guard for unfixable cells
# ---------------------------------------------------------------------------
def test_start_schedules_contain_the_classic_start_and_price_break_lots():
    from meio.io_json import load_model
    from meio.policy import economic_cover_weeks, price_break_lots, start_schedules
    settings = SearchSettings(n_quantile_samples=500)
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "example_input.json")
    model = load_model(path)
    starts = start_schedules(model, settings)
    classic = initial_schedule(model, settings)
    label, first = starts[0]
    assert label == "quantile start"
    assert all(np.array_equal(first.dc_S[k], classic.dc_S[k]) for k in classic.dc_S)
    assert all(np.array_equal(first.rm_s[k], classic.rm_s[k]) for k in classic.rm_s)
    lots = price_break_lots(model, model.products[0])
    assert lots == [10100, 15100]                      # the MOQ itself is no price break
    for lot in lots:
        sched = dict(starts)[f"price-break start FG1 lot {lot}"]
        weeks = sched.dc_s["FG1"] > 0
        assert ((sched.dc_S["FG1"] - sched.dc_s["FG1"])[weeks] >= lot).all()
        assert sched.dc_cap["FG1"] >= lot                # the cap does not cut the lot
    dc_cover, rm_cover = economic_cover_weeks(model)
    strictest = min(model.products[0].max_age_for_channel(c) for c in model.products[0].channels)
    assert 1 <= dc_cover["FG1"] <= strictest - 1          # a lot sells before it is too old
    assert all(c >= 1 for c in rm_cover.values())


def _searcher(model=None, n=60):
    from meio.search import Searcher
    model = model or build_example_input()
    settings = SearchSettings(n_search_seeds=n, n_holdout_seeds=n, n_quantile_samples=300)
    return Searcher(model, settings, build_scenarios(model, n, 1, "s"), build_scenarios(model, n, 2, "h"),
                    verbose=False)


def test_block_moves_change_exactly_the_block_and_lift_the_cap():
    s = _searcher()
    sched = initial_schedule(s.model, s.settings)
    weeks = [3, 4, 5, 6]
    new = s.moved_block(sched, "DC", "FG1", weeks, "raise S (larger orders)", 0.10)
    diff = new.dc_S["FG1"] - sched.dc_S["FG1"]
    step = diff[3]
    assert step > 0 and step % 20 == 0 and (diff[weeks] == step).all()
    assert (np.delete(diff, weeks) == 0).all() and np.array_equal(new.dc_s["FG1"], sched.dc_s["FG1"])
    assert new.dc_cap["FG1"] >= int((new.dc_S["FG1"] - new.dc_s["FG1"]).max())
    lowered = s.moved_block(sched, "DC", "FG1", weeks, "lower s and S", 0.10)
    assert ((sched.dc_s["FG1"] - lowered.dc_s["FG1"])[weeks] > 0).all()
    assert s.moved_block(sched, "DC", "FG1", weeks, "lower s and S", 100.0) is None   # below zero


def test_coordinated_lot_move_carries_the_rmw_levels_that_feed_the_releases():
    s = _searcher()
    sched = initial_schedule(s.model, s.settings)
    weeks = [10, 11, 12]
    new = s.moved_block(sched, "DC", "FG1", weeks, "raise lot with RM support", 0.10)
    step = int(new.dc_S["FG1"][10] - sched.dc_S["FG1"][10])
    feeding = s.rm_weeks_feeding("FG1", weeks)
    for m in s.model.materials:
        rw = feeding[m.name]
        assert rw, "every material has order weeks that feed weeks 10-12"
        d_s = new.rm_s[m.name] - sched.rm_s[m.name]
        d_S = new.rm_S[m.name] - sched.rm_S[m.name]
        expect = int(np.ceil(s.model.products[0].bom[m.name] * step))
        assert (d_s[rw] == expect).all() and (d_S[rw] == expect).all()
        assert (np.delete(d_s, rw) == 0).all()
        # an RM order week feeds a release only if its delivery can arrive by then
        assert all(any(r - m.lead_time_max <= o <= r - m.lead_time_min for r in weeks) for o in rw)
    back = s.moved_block(new, "DC", "FG1", weeks, "lower lot with RM support", 0.10)
    assert back is not None


def test_block_sizes_go_from_the_whole_horizon_down_to_single_weeks():
    from meio.search import Searcher
    assert Searcher.block_sizes(16) == [16, 8, 4, 2, 1]
    assert Searcher.block_sizes(5) == [5, 3, 2, 1]
    assert Searcher.block_sizes(1) == [1]
    assert Searcher.week_blocks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_improve_never_lowers_the_service_of_unfixable_cells():
    s = _searcher()
    sched = initial_schedule(s.model, s.settings)
    ev = s.evaluate(sched)
    cells = ev.cells
    key = tuple(cells.iloc[5][["product", "channel", "week"]])
    key = (key[0], key[1], int(key[2]))
    s.unfixable = {key}
    s.unfixable_floor = s.service_floors(ev)
    assert s.evaluate(sched).n_failing == ev.n_failing - (0 if ev.cells.iloc[5]["search_feasible"] else 1)
    s.unfixable_floor[key] += 0.5                        # pretend the cell had much better service
    assert not s.evaluate(sched).feasible                # now the same schedule violates the floor
    s.unfixable_floor = {}


def test_search_keeps_the_best_start_and_never_ends_worse_than_its_repaired_start():
    s = _searcher(build_example_input(horizon=16), n=30)  # short horizon: a fast full search
    s.settings.max_improve_passes = 1
    s.settings.max_outer_rounds = 1
    out = s.run()
    start_rows = [r for r in out.search_log if r["phase"] == "start"]
    assert [r["action"] for r in start_rows][0] == "quantile start"
    repaired = [r for r in out.search_log if r["round"] == 0 and r["phase"] != "start"]
    assert out.chosen_start in [r["action"] for r in start_rows]
    improve = [r for r in out.search_log if r["phase"] == "improve" and r["round"] == 1]
    costs = [r["mean_cost"] for r in improve]
    assert costs == sorted(costs, reverse=True)          # every accepted move lowers the cost
    assert all(r["feasible"] for r in out.search_log if r["phase"] == "improve")
    chosen_rows = [r for r in start_rows if r["action"] == out.chosen_start]
    assert len(chosen_rows) == 1


def test_bullwhip_ratio_hand_examples():
    from meio.tables import bullwhip_ratio
    rng = np.random.default_rng(0)
    demand = np.zeros((50, 30))
    demand[:, 1:] = rng.normal(100, 20, (50, 29))
    weeks = list(range(1, 25))
    assert abs(bullwhip_ratio(demand, demand, weeks, 0) - 1) < 1e-12        # orders = demand
    assert abs(bullwhip_ratio(2 * demand, demand, weeks, 0) - 4) < 1e-12    # amplified x 2
    lumpy = np.zeros_like(demand)                                            # all demand of a
    for start in range(1, 25, 4):                                            # 4-week bucket in one
        lumpy[:, start] = demand[:, start:start + 4].sum(axis=1)              # order: no bullwhip
    assert abs(bullwhip_ratio(lumpy, demand, weeks, 0) - 1) < 1e-9
    assert bullwhip_ratio(demand, demand, [1, 2, 3, 4, 5], 0) is None       # too few buckets


def test_confirmation_seeds_reject_a_move_that_makes_a_cell_weak():
    s = _searcher()
    assert s.confirm_seeds is not None and s.confirm_seeds.n_seeds == 2 * s.settings.n_search_seeds
    sched = initial_schedule(s.model, s.settings)
    s.confirm_failing, s.confirm_floor = s.confirmation(sched)
    assert s.confirmed(sched)                            # the same schedule: nothing new is weak
    starved = sched.copy()
    starved.dc_s["FG1"][5:] = 0                          # no more orders from week 5 on
    starved.dc_S["FG1"][5:] = 20
    weak_before = set(s.confirm_failing)
    assert not s.confirmed(starved)                      # new weak cells on the confirmation seeds
    assert s.confirm_failing == weak_before              # a rejected move changes nothing
    s.settings.confirm_seed_factor = 0
    from meio.search import Searcher
    plain = Searcher(s.model, s.settings, s.search_seeds, s.holdout_seeds, verbose=False)
    assert plain.confirm_seeds is None and plain.confirmed(starved)   # switched off: no check
