"""
Input data of the MEIO simulation-optimisation model.

Everything the model needs is defined here as plain dataclasses:
products (finished goods), sales channels, raw materials, cost tiers,
lead-time distributions, demand forecasts, the initial state and the
settings of the search.

Conventions used in the whole project
-------------------------------------
* Time unit is one week. Week 1 is the current review week.
* Arrays that run over weeks are indexed by the week number itself,
  i.e. array[t] belongs to week t. Index 0 is never used.
* Stock arrays are indexed by age the same way: stock[:, b] = units of
  age b. Age 1 = arrived this week. Index 0 is never used.
* The initial state describes the situation at the start of week 1,
  AFTER this week's receipts have been put on stock. Orders that are
  still open ("pipeline") therefore arrive in week 2 or later.

The function build_example_input() at the bottom creates the small built-in
reference instance (1 finished good, 4 raw materials, 3 channels) that the unit
tests and `python main.py` without --input use. The app's default dataset is
examples/example_input.json (a separate, larger instance).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np


# ---------------------------------------------------------------------------
# Bill of materials: whole or fractional raw-material units per FG unit
# ---------------------------------------------------------------------------
MAX_BOM_DENOMINATOR = 10_000      # a BOM quantity must be a fraction with at most this denominator


def bom_fraction(quantity: float) -> Fraction:
    """The BOM quantity as an exact fraction (0.2 -> 1/5, 3 -> 3/1). Whole and fractional
    quantities use the same integer arithmetic below, so no float rounding can creep in."""
    return Fraction(quantity).limit_denominator(MAX_BOM_DENOMINATOR)


def rm_units_for(quantity: float, fg_units):
    """Raw-material units shipped for a release of fg_units: quantity x fg_units, rounded UP to
    whole units (stock is counted in whole units). Works on numbers and integer arrays."""
    f = bom_fraction(quantity)
    return -(-(f.numerator * fg_units) // f.denominator)


def max_fg_from(quantity: float, rm_units):
    """Largest number of FG units whose raw-material need (rm_units_for) fits into rm_units."""
    f = bom_fraction(quantity)
    return (rm_units * f.denominator) // f.numerator


# ---------------------------------------------------------------------------
# Basic building blocks
# ---------------------------------------------------------------------------
@dataclass
class Channel:
    """A sales channel of a finished good."""
    name: str
    min_remaining_life: int   # rho_c: channel accepts a unit only with >= this many weeks left
    target_fill_rate: float   # F_c: required fill rate per (week x channel) cell


@dataclass
class Tier:
    """One quantity band of an all-units discount (bounds inclusive)."""
    lower: int
    upper: int
    unit_cost: float


@dataclass
class Product:
    """A finished good (FG) that is produced at the PF and stocked at the DC."""
    name: str
    shelf_life: int                    # A_f [weeks], starts on arrival at the DC
    channels: list[Channel]
    bom: dict[str, float]              # material name -> units per FG unit (whole or fractional, e.g. 0.2)
    batch_size: int                    # releases are multiples of this
    moq: int                           # minimum release quantity (multiple of batch_size)
    holding_cost: float                # per unit per week (end-of-week stock)
    waste_cost: float                  # per scrapped unit
    fixed_cost_per_release: float      # charged in weeks with a release > 0
    production_tiers: list[Tier]       # conversion cost per unit (no material cost!)
    transport_tiers: list[Tier]        # PF -> DC transport cost per unit
    lead_time_dist: dict[int, float]   # P(L~ = weeks), production start at PF -> usable at DC
    closed_production_weeks: list[int] = field(default_factory=list)
    site: str = ""                     # production site (ModelInput.sites); "" = the default site

    # ----- derived quantities (read-only helpers) -----
    def max_age_for_channel(self, channel: Channel) -> int:
        """Oldest age channel c accepts: A - rho_c (remaining life A - b >= rho_c)."""
        return self.shelf_life - channel.min_remaining_life

    @property
    def max_sellable_age(self) -> int:
        """Oldest age any channel accepts. Stock that reaches it unsold is scrapped."""
        return max(self.max_age_for_channel(c) for c in self.channels)

    def channel_priority(self) -> list[int]:
        """Channel indices in service order: tightest shelf-life requirement first,
        ties broken by higher target fill rate."""
        return sorted(range(len(self.channels)),
                      key=lambda i: (self.max_age_for_channel(self.channels[i]),
                                     -self.channels[i].target_fill_rate))

    @property
    def lead_time_min(self) -> int:
        return min(self.lead_time_dist)

    @property
    def lead_time_max(self) -> int:
        return max(self.lead_time_dist)

    @property
    def lead_time_median(self) -> int:
        return median_of(self.lead_time_dist)


@dataclass
class Material:
    """A raw material (RM) bought from one supplier and stocked at the RMW."""
    name: str
    shelf_life: int                    # A_RM [weeks], starts on arrival at the RMW
    min_life_at_shipment: int          # rho_RM: remaining life needed when shipped to the PF
    batch_size: int
    moq: int
    unit_cost: float                   # purchase cost per unit
    fixed_order_cost: float            # per supplier order
    holding_cost: float                # per unit per week
    waste_cost: float                  # per scrapped unit
    transport_cost: float              # RMW -> PF per unit
    lead_time_dist: dict[int, float]   # P(G~ = weeks), order -> usable at RMW
    supplier_capacity: int | None = None      # max per order, None = unlimited
    closed_order_weeks: list[int] = field(default_factory=list)
    rmw_to_pf_lead_time: int = 0       # tau_r: weeks from the RMW to production (deterministic, 0 = same week)

    @property
    def max_shippable_age(self) -> int:
        """Oldest age that may still be shipped: A_RM - rho_RM."""
        return self.shelf_life - self.min_life_at_shipment

    @property
    def lead_time_min(self) -> int:
        return min(self.lead_time_dist)

    @property
    def lead_time_max(self) -> int:
        return max(self.lead_time_dist)

    @property
    def lead_time_median(self) -> int:
        return median_of(self.lead_time_dist)


def median_of(dist: dict[int, float]) -> int:
    """Median of a discrete distribution {value: probability}: the smallest value
    with cumulative probability >= 0.5."""
    cumulative = 0.0
    for value in sorted(dist):
        cumulative += dist[value]
        if cumulative >= 0.5 - 1e-12:
            return value
    return max(dist)


@dataclass
class DemandForecast:
    """Week-specific forecast distribution per (product, channel).

    mean[(p, c)][t] and sd[(p, c)][t] give mean and standard deviation of the
    demand in week t. Demand is sampled as a negative binomial (integer units,
    variance > mean) or Poisson when sd^2 <= mean. Weeks are independent.
    The forecast must reach further than the horizon, because the initial
    quantile schedule looks ahead over long lead times.
    """
    mean: dict[tuple[str, str], np.ndarray]
    sd: dict[tuple[str, str], np.ndarray]

    @property
    def last_week(self) -> int:
        return len(next(iter(self.mean.values()))) - 1


@dataclass
class InitialState:
    """Known state at the start of week 1 (after this week's receipts).

    dc_stock[product]    = {age: units} on hand at the DC
    rm_stock[material]   = {age: units} on hand at the RMW
    dc_pipeline[product] = [(release_week, units), ...]  released, not yet at the DC
    rm_pipeline[material]= [(order_week, units), ...]    ordered, not yet at the RMW
    Release / order weeks are <= 0 (they happened before the current week).
    """
    dc_stock: dict[str, dict[int, int]]
    rm_stock: dict[str, dict[int, int]]
    dc_pipeline: dict[str, list[tuple[int, int]]]
    rm_pipeline: dict[str, list[tuple[int, int]]]


DEFAULT_SITE = "PF"


@dataclass
class ProductionSite:
    """A production facility. The products assigned to it share its weekly capacity (FG units,
    booked in the production week); in its closed weeks none of them is produced."""
    name: str
    capacity: int                                          # FG units per week
    capacity_overrides: dict[int, int] = field(default_factory=dict)   # week -> capacity
    closed_weeks: list[int] = field(default_factory=list)


@dataclass
class ModelInput:
    """Complete description of the supply chain for one review."""
    horizon: int                               # H: number of simulated weeks
    products: list[Product]
    materials: list[Material]
    demand: DemandForecast
    initial_state: InitialState
    production_capacity: int                   # FG units per week, shared by all products
    capacity_overrides: dict[int, int] = field(default_factory=dict)  # week -> capacity
    target_share_of_futures: float = 0.98      # alpha: every cell must meet its fill rate F in this share of futures
    sites: list[ProductionSite] = field(default_factory=list)   # empty: one default site with
                                                                # production_capacity / capacity_overrides
    evaluation_start: int = 0                  # first evaluated week; 0 = automatic (global L_max + 1).
                                               # Set for the sub-models of independent groups (decompose.py)

    # ----- helpers -----
    def material(self, name: str) -> Material:
        return next(m for m in self.materials if m.name == name)

    def all_sites(self) -> list[ProductionSite]:
        """The production sites; without explicit sites the single default site."""
        if self.sites:
            return self.sites
        return [ProductionSite(DEFAULT_SITE, self.production_capacity, self.capacity_overrides, [])]

    def site_of(self, product: Product) -> ProductionSite:
        """The site that produces a product (the default site if no sites are given)."""
        if not self.sites:
            return self.all_sites()[0]
        return next(s for s in self.sites if s.name == product.site)

    def capacity_in_week(self, t: int, product: Product | None = None) -> int:
        """Capacity of the product's site (default: the first site) in production week t."""
        site = self.site_of(product) if product is not None else self.all_sites()[0]
        return site.capacity_overrides.get(t, site.capacity)

    def production_closed(self, product: Product, week: int) -> bool:
        """True if the product cannot be produced in this week (its own or its site's closed weeks)."""
        return week in product.closed_production_weeks or week in self.site_of(product).closed_weeks

    def products_using(self, material_name: str) -> list[Product]:
        return [p for p in self.products if material_name in p.bom]

    # Release -> DC: a release in week t ships all BOM materials from the RMW in week t.
    # Material r needs tau_r weeks to production; production starts when the slowest one has
    # arrived, in week t + tau_p with tau_p = max over the BOM of tau_r. The FG is usable at the
    # DC after the random production + transport lead time L~ on top: arrival = t + tau_p + L~.
    def rmw_to_pf_lead_time(self, product: Product) -> int:
        """tau_p: weeks from release to production start = slowest BOM material's RMW -> PF time."""
        return max((self.material(m).rmw_to_pf_lead_time for m in product.bom), default=0)

    def production_week(self, product: Product, release_week: int) -> int:
        """Week in which a release of week t is produced (capacity and closed weeks apply here)."""
        return release_week + self.rmw_to_pf_lead_time(product)

    def release_to_dc_min(self, product: Product) -> int:
        return self.rmw_to_pf_lead_time(product) + product.lead_time_min

    def release_to_dc_max(self, product: Product) -> int:
        return self.rmw_to_pf_lead_time(product) + product.lead_time_max

    def release_to_dc_median(self, product: Product) -> int:
        return self.rmw_to_pf_lead_time(product) + product.lead_time_median

    @property
    def lead_time_max_global(self) -> int:
        """L_max: largest possible time from release to arrival at the DC (tau_p + L) over all products."""
        return max(self.release_to_dc_max(p) for p in self.products)

    @property
    def evaluation_weeks(self) -> list[int]:
        """Weeks in which service is evaluated: L_max + 1 ... H (or from evaluation_start)."""
        return list(range(self.evaluation_start or self.lead_time_max_global + 1, self.horizon + 1))


# The model uses one ordering policy (see docs/policy_choice.md):
#   * age-aware (s,S): positions minus the stock expected to expire before a new order
#     arrives (median lead-time window),
#   * a DC order cap per product and a minimum physical stock per raw material at the RMW,
#   * the committed week-1 orders are chosen by a lookahead that simulates candidate
#     quantities from the current state (meio/lookahead.py).
POLICY_NAME = "age-aware capped (s,S) + week-1 lookahead"


@dataclass
class SearchSettings:
    """Settings of the simulation-optimisation."""
    n_search_seeds: int = 500        # seeds used to accept or reject moves
    n_holdout_seeds: int = 1000      # seeds used to detect weak cells
    n_test_seeds: int = 10000        # untouched seeds for the final verdict
    z: float = 2.0                   # safety multiplier on the standard error
    min_margin_bump: float = 0.005   # smallest margin increase for a weak cell
    max_outer_rounds: int = 3        # repair -> improve -> hold-out check rounds
    max_repair_steps: int = 300
    repair_patience: int = 8         # steps without progress before a cell is declared unfixable
    repair_min_progress: float = 0.001  # a repair step is progress if the cell's lower bound rises by this
    max_improve_passes: int = 4
    step_fraction: float = 0.10      # step size as fraction of the level (at least one batch)
    min_step_fraction: float = 0.02
    cut_share_threshold: float = 0.02  # share of seeds with RM-limited releases that points to the RMW
    initial_quantile: float | None = None   # None -> highest target fill rate of the product
    initial_extra_cover_weeks: int = 1      # m0: extra weeks of cover for S in the initial schedule
    n_quantile_samples: int = 4000   # Monte Carlo samples for the initial schedule
    base_seed: int = 2026            # all random numbers derive from this
    trace_seeds: list[int] = field(default_factory=lambda: [0, 1, 2])  # test seeds shown in detail
    initial_cap_weeks: float = 2.0     # start value of the DC order cap: weeks of mean demand
    initial_floor_share: float = 1.0   # start value of the RMW minimum: share of mean use over the median supplier lead time
    lookahead_rm_steps: int = 4        # lookahead: RM candidates = rule quantity +/- up to this many steps
    max_starts: int = 5                # multi-start: quantile, economic-lot and price-break starts (policy.start_schedules)
    max_move_repeats: int = 8          # improve: repeat an accepted move in the same direction up to this often
    race_levels: int = 2               # multi-start: coarse block levels of the racing pass per start (0 = no racing)
    confirm_seed_factor: float = 2.0   # confirmation seeds = factor x search seeds (0 = no confirmation)
    restructure_shares: tuple = (0.5, 1.0)  # restructure: RM support as share of the extra lot need (() = off)
    restructure_tolerance: float = 0.05  # restructure: improve a repaired jump if its cost is within this share
    n_workers: int = 0                 # parallel worker processes of the search (0 = automatic, 1 = none)


PRESETS = {
    # name: (search seeds, hold-out seeds, test seeds, improve passes, outer rounds)
    # The hold-out set is simulated only once per round, so it is large: fill rates per seed
    # are skewed (mostly 100 %, rare deep shortages) and small samples overestimate them.
    "quick": (200, 1000, 2000, 2, 2),       # for trying things out
    "standard": (300, 1500, 5000, 3, 2),    # default
    "full": (500, 2500, 10000, 4, 3),       # for decisions you rely on
}


def settings_for_preset(name: str, **overrides) -> SearchSettings:
    """SearchSettings for a named preset; keyword arguments override single fields."""
    search, holdout, test, passes, rounds = PRESETS[name]
    values = dict(n_search_seeds=search, n_holdout_seeds=holdout, n_test_seeds=test,
                  max_improve_passes=passes, max_outer_rounds=rounds)
    values.update(overrides)
    return SearchSettings(**values)


# ---------------------------------------------------------------------------
# Example instance
# ---------------------------------------------------------------------------
def _example_demand(products: list[Product], n_weeks: int) -> DemandForecast:
    """Non-stationary example forecast: seasonal means and a coefficient of
    variation that grows with the forecast distance (far weeks are less certain)."""
    weeks = np.arange(0, n_weeks + 1)          # index = week, week 0 unused
    cv = 0.20 + 0.15 * np.clip((weeks - 1) / 30.0, 0.0, 1.0)

    base = {
        "Retail": 60 * (1 + 0.25 * np.sin(2 * np.pi * (weeks - 1) / 26)),
        "Online": 40 * (1 + 0.15 * np.sin(2 * np.pi * (weeks + 5) / 26)) + 0.3 * weeks,
        "Outlet": 25 + 5 * np.cos(2 * np.pi * weeks / 13),
    }
    mean, sd = {}, {}
    for p in products:
        for c in p.channels:
            m = np.round(base[c.name], 1)
            m[0] = 0.0
            mean[(p.name, c.name)] = m
            sd[(p.name, c.name)] = np.round(cv * m, 1)
    return DemandForecast(mean=mean, sd=sd)


def build_example_input(horizon: int = 36) -> ModelInput:
    """Example: one finished good made of four raw materials (1 unit each)."""
    fg = Product(
        name="FG1",
        shelf_life=12,
        channels=[
            Channel("Retail", min_remaining_life=7, target_fill_rate=0.98),
            Channel("Online", min_remaining_life=5, target_fill_rate=0.95),
            Channel("Outlet", min_remaining_life=2, target_fill_rate=0.90),
        ],
        bom={"RM_A": 1, "RM_B": 1, "RM_C": 1, "RM_D": 1},
        batch_size=20,
        moq=60,
        holding_cost=0.30,
        waste_cost=8.00,
        fixed_cost_per_release=200.0,
        production_tiers=[Tier(0, 199, 3.00), Tier(200, 399, 2.70), Tier(400, 10**9, 2.50)],
        transport_tiers=[Tier(0, 149, 0.90), Tier(150, 349, 0.75), Tier(350, 10**9, 0.60)],
        lead_time_dist={5: 0.20, 6: 0.55, 7: 0.20, 8: 0.05},
        closed_production_weeks=[18],          # e.g. plant holiday
    )

    materials = [
        Material("RM_A", shelf_life=26, min_life_at_shipment=1, batch_size=50, moq=200,
                 unit_cost=1.20, fixed_order_cost=120.0, holding_cost=0.020, waste_cost=1.40,
                 transport_cost=0.05, lead_time_dist={9: 0.2, 10: 0.5, 11: 0.2, 12: 0.1},
                 rmw_to_pf_lead_time=1),
        Material("RM_B", shelf_life=20, min_life_at_shipment=1, batch_size=100, moq=300,
                 unit_cost=0.80, fixed_order_cost=80.0, holding_cost=0.015, waste_cost=1.00,
                 transport_cost=0.05, lead_time_dist={6: 0.3, 7: 0.4, 8: 0.3},
                 rmw_to_pf_lead_time=1),
        Material("RM_C", shelf_life=30, min_life_at_shipment=1, batch_size=50, moq=500,
                 unit_cost=2.50, fixed_order_cost=250.0, holding_cost=0.040, waste_cost=2.70,
                 transport_cost=0.05, lead_time_dist={10: 0.3, 12: 0.4, 14: 0.3},
                 rmw_to_pf_lead_time=1),
        Material("RM_D", shelf_life=12, min_life_at_shipment=1, batch_size=25, moq=100,
                 unit_cost=0.60, fixed_order_cost=60.0, holding_cost=0.012, waste_cost=0.80,
                 transport_cost=0.05, lead_time_dist={4: 0.5, 5: 0.3, 6: 0.2},
                 rmw_to_pf_lead_time=1),
    ]

    # Forecast must reach beyond the horizon (initial schedule looks ahead up to G + tau + L + m0)
    longest_look_ahead = (max(m.lead_time_max for m in materials) + max(m.rmw_to_pf_lead_time for m in materials)
                          + fg.lead_time_max + 5)
    demand = _example_demand([fg], horizon + longest_look_ahead)

    # Initial state: roughly 2.5 weeks of FG on hand, one release per past week in
    # transit, RM stock of about 5 weeks and one supplier order per past week in transit.
    initial = InitialState(
        dc_stock={"FG1": {1: 130, 2: 110, 3: 60, 4: 30, 6: 20}},
        rm_stock={
            "RM_A": {1: 150, 3: 200, 6: 250},
            "RM_B": {2: 200, 4: 200, 7: 200},
            "RM_C": {1: 300, 5: 300, 9: 350},
            "RM_D": {1: 150, 2: 150, 4: 100},
        },
        dc_pipeline={"FG1": [(-5, 120), (-4, 140), (-3, 120), (-2, 140), (-1, 120), (0, 140)]},
        rm_pipeline={
            "RM_A": [(w, 150) for w in range(-10, 1)],
            "RM_B": [(w, 200) for w in range(-6, 1, 2)] + [(w, 100) for w in range(-5, 1, 2)],
            "RM_C": [(w, 150) for w in range(-12, 1)],
            "RM_D": [(w, 125) for w in range(-4, 1)],
        },
    )
    # keep pipelines sorted by order week (needed for order-preserving arrivals)
    for lane in (initial.dc_pipeline, initial.rm_pipeline):
        for key in lane:
            lane[key] = sorted(lane[key])

    return ModelInput(
        horizon=horizon,
        products=[fg],
        materials=materials,
        demand=demand,
        initial_state=initial,
        production_capacity=450,
    )


def validate_input(model: ModelInput) -> None:
    """Basic consistency checks. Raises ValueError with a clear message."""
    problems = []
    site_names = [s.name for s in model.sites]
    if len(set(site_names)) != len(site_names):
        problems.append("production site names must be unique")
    for s in model.sites:
        if s.capacity < 0 or any(c < 0 for c in s.capacity_overrides.values()):
            problems.append(f"site {s.name}: capacity must be >= 0")
    for p in model.products:
        if model.sites and p.site not in site_names:
            problems.append(f"{p.name}: production site {p.site!r} is not defined (sites: {site_names})")
        if not model.sites and p.site not in ("", DEFAULT_SITE):
            problems.append(f"{p.name}: production site {p.site!r} given, but the input defines no sites")
        if abs(sum(p.lead_time_dist.values()) - 1) > 1e-9:
            problems.append(f"{p.name}: lead-time probabilities do not sum to 1")
        if p.moq % p.batch_size != 0:
            problems.append(f"{p.name}: MOQ must be a multiple of the batch size")
        for c in p.channels:
            if p.max_age_for_channel(c) < 1:
                problems.append(f"{p.name}/{c.name}: required remaining life >= shelf life")
        for m, quantity in p.bom.items():
            if m not in [x.name for x in model.materials]:
                problems.append(f"{p.name}: BOM material {m} is not defined")
            if not quantity > 0 or abs(float(bom_fraction(quantity)) - quantity) > 1e-9:
                problems.append(f"{p.name}: BOM quantity of {m} must be > 0 and a simple fraction "
                                f"(e.g. 0.2, 0.25, 1, 3; got {quantity!r})")
        for tiers, label in ((p.production_tiers, "production"), (p.transport_tiers, "transport")):
            for a, b in zip(tiers, tiers[1:]):
                if b.lower != a.upper + 1:
                    problems.append(f"{p.name}: {label} tiers are not contiguous")
        for age in model.initial_state.dc_stock.get(p.name, {}):
            if not 1 <= age <= p.max_sellable_age:
                problems.append(f"{p.name}: initial stock age {age} outside 1..{p.max_sellable_age}")
    for m in model.materials:
        if abs(sum(m.lead_time_dist.values()) - 1) > 1e-9:
            problems.append(f"{m.name}: lead-time probabilities do not sum to 1")
        if m.moq % m.batch_size != 0:
            problems.append(f"{m.name}: MOQ must be a multiple of the batch size")
        for age in model.initial_state.rm_stock.get(m.name, {}):
            if not 1 <= age <= m.max_shippable_age:
                problems.append(f"{m.name}: initial stock age {age} outside 1..{m.max_shippable_age}")
        tau = m.rmw_to_pf_lead_time
        if not isinstance(tau, int) or isinstance(tau, bool) or not 0 <= tau < model.horizon:
            problems.append(f"{m.name}: RMW -> PF lead time must be a whole number of weeks, "
                            f"0 <= tau < horizon (got {tau!r})")
    if not 0 < model.target_share_of_futures <= 1:
        problems.append(f"target_share_of_futures must be in (0, 1] (got {model.target_share_of_futures!r})")
    last_needed = model.horizon + 1
    if model.demand.last_week < last_needed:
        problems.append("demand forecast is shorter than the horizon")
    if problems:
        raise ValueError("Input problems:\n  - " + "\n  - ".join(problems))
