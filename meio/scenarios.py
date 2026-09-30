"""
Random futures ("seeds") for the simulation.

A seed is one complete random future: demand of every (product, channel)
in every week, plus lead-time draws on every lane. All candidate policies
are evaluated on the same seeds (common random numbers). Lead times are
drawn per ORDER WEEK, so a seed gives every candidate the same random
environment, even if two candidates order in different weeks.

Three disjoint seed sets are used: search, hold-out and test. They are
created from different random-number streams.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ModelInput


@dataclass
class ScenarioSet:
    name: str
    n_seeds: int
    demand: dict[tuple[str, str], np.ndarray]     # (product, channel) -> int (n_seeds, H+1)
    dc_lead_time: dict[str, np.ndarray]           # product  -> int (n_seeds, H+1): draw of L~ for a release in week t (without tau)
    rm_lead_time: dict[str, np.ndarray]           # material -> int (n_seeds, H+1): draw for an order in week t
    dc_pipeline_arrival: dict[str, list[np.ndarray]]  # product  -> one (n_seeds,) array per open release
    rm_pipeline_arrival: dict[str, list[np.ndarray]]  # material -> one (n_seeds,) array per open order


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------
def sample_demand(mean: float, sd: float, size: int, rng: np.random.Generator) -> np.ndarray:
    """Integer demand with given mean and standard deviation.
    Negative binomial if variance > mean, otherwise Poisson."""
    if mean <= 0:
        return np.zeros(size, dtype=np.int64)
    variance = sd ** 2
    if variance <= mean:
        return rng.poisson(mean, size)
    n = mean ** 2 / (variance - mean)       # "number of successes" parameter
    p = n / (n + mean)
    return rng.negative_binomial(n, p, size)


def sample_lead_time(dist: dict[int, float], size: int, rng: np.random.Generator,
                     at_least: int | None = None) -> np.ndarray:
    """Draw lead times from a discrete distribution {weeks: probability}.

    at_least: condition on lead time >= at_least (used for orders that are
    already under way). If the condition leaves no possible value the order
    is overdue and arrives as early as possible (lead time = at_least).
    """
    weeks = np.array(sorted(dist))
    probs = np.array([dist[w] for w in weeks], dtype=float)
    if at_least is not None:
        keep = weeks >= at_least
        if not keep.any():
            return np.full(size, at_least, dtype=np.int64)
        weeks, probs = weeks[keep], probs[keep]
    probs = probs / probs.sum()
    return rng.choice(weeks, size=size, p=probs)


# ---------------------------------------------------------------------------
# Build a seed set
# ---------------------------------------------------------------------------
def build_scenarios(model: ModelInput, n_seeds: int, rng_seed: int, name: str) -> ScenarioSet:
    """Draw n_seeds random futures. The drawing order is fixed, so the same
    rng_seed always gives the same scenarios."""
    rng = np.random.default_rng(rng_seed)
    H = model.horizon

    demand = {}
    for p in model.products:
        for c in p.channels:
            d = np.zeros((n_seeds, H + 1), dtype=np.int64)
            for t in range(1, H + 1):
                d[:, t] = sample_demand(model.demand.mean[(p.name, c.name)][t],
                                        model.demand.sd[(p.name, c.name)][t], n_seeds, rng)
            demand[(p.name, c.name)] = d

    dc_lead = {}
    for p in model.products:
        lt = np.zeros((n_seeds, H + 1), dtype=np.int64)
        for t in range(1, H + 1):
            lt[:, t] = sample_lead_time(p.lead_time_dist, n_seeds, rng)
        dc_lead[p.name] = lt

    rm_lead = {}
    for m in model.materials:
        lt = np.zeros((n_seeds, H + 1), dtype=np.int64)
        for t in range(1, H + 1):
            lt[:, t] = sample_lead_time(m.lead_time_dist, n_seeds, rng)
        rm_lead[m.name] = lt

    # Open orders of the initial state: they have not arrived by week 1, so their
    # arrival week is at least 2 -> lead time >= 2 - order_week (conditional draw).
    # FG releases also need the RMW -> PF time tau: arrival = release week + tau + L~.
    tau = model.rmw_to_pf_lead_time
    dc_pipe = {}
    for p in model.products:
        dc_pipe[p.name] = []
        for order_week, _qty in model.initial_state.dc_pipeline.get(p.name, []):
            lt = sample_lead_time(p.lead_time_dist, n_seeds, rng, at_least=2 - order_week - tau)
            dc_pipe[p.name].append(order_week + tau + lt)
    rm_pipe = {}
    for m in model.materials:
        rm_pipe[m.name] = []
        for order_week, _qty in model.initial_state.rm_pipeline.get(m.name, []):
            lt = sample_lead_time(m.lead_time_dist, n_seeds, rng, at_least=2 - order_week)
            rm_pipe[m.name].append(order_week + lt)

    return ScenarioSet(name, n_seeds, demand, dc_lead, rm_lead, dc_pipe, rm_pipe)
