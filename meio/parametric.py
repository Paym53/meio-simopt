"""
Forecast-scaled (s, S) levels: the whole schedule from a few parameters per item.

Why (literature, see docs/search_algorithm.md):
  - Safety stock for non-stationary demand should follow the forecast over the
    protection interval that ends when the next order can arrive (Neale & Willems 2009,
    2015; Graves & Willems 2008), not a fixed "weeks of forward cover": with a constant
    safety factor the levels move with the forecast and its uncertainty week by week.
  - Non-stationary (s, S) policies are well approximated by stationary parameters applied
    to the local forecast (Silver 1978; Bollapragada & Morton 1999): a lot that covers
    m weeks of forecast and a reorder level at a service quantile.
  - Perishables: the order-up-to level is compared with a position net of the expected
    waste (Broekmeulen & van Donselaar 2009; Pauls-Worm et al. 2014) - the policy already
    does this - and the lot must sell within the usable shelf life, which bounds m.

Parameters per finished good p (DC):
    z      safety factor: s_t = Phi(z)-quantile of demand over the protection interval
           t .. t + tau_p + L~ (random lead time, simulated as in the start schedule)
    cover  m weeks of mean forecast after that interval: lot_t = S_t - s_t
    min_lot  smallest lot, 0 or a price-break quantity (all-units discounts)
    cap    order cap in weeks of mean demand (never below the largest lot)
Parameters per raw material r (RMW, echelon):
    z      s_t = Phi(z)-quantile of the BOM-weighted demand over t .. t + G~ + tau_p + L~
    cover  m weeks of mean BOM-weighted forecast after that interval
    floor  minimum physical RM stock in weeks of mean use

The lot cover ends at the horizon (demand after week H is outside the cost window).
Levels are rounded up to whole batches. Week-specific exceptions that no parameter can
express (pre-building before closed weeks, the first weeks after the review) are kept as
sparse offsets on top of the levels (see search.Searcher).
"""
from __future__ import annotations

from math import erf, sqrt

import numpy as np

from .config import ModelInput, SearchSettings
from .policy import (PolicySchedule, _mean_weekly_demand, _round_up, _sample_total_demand_paths, _window_sum,
                     dc_order_weeks, economic_cover_weeks, price_break_lots, rm_order_weeks)
from .scenarios import sample_lead_time

Key = tuple[str, str, str]            # (kind "DC"/"RM", item name, parameter name)

Z_MIN, Z_MAX = -1.0, 4.0


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def normal_quantile(q: float) -> float:
    """Inverse standard normal CDF by bisection (no scipy needed; 50 steps, |error| < 1e-12)."""
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if normal_cdf(mid) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class LevelBuilder:
    """Precomputes, once per model, what the levels are made of; build() then turns a
    parameter set into a full PolicySchedule in a few milliseconds.

      protection samples : per item and order week, sorted Monte Carlo samples of the
                           demand over the protection interval (same draws as the
                           classic start schedule uses, so z = Phi^-1(q0) reproduces it)
      lot means          : per item, mean forecast per week after the interval
    """

    def __init__(self, model: ModelInput, settings: SearchSettings):
        self.model, self.settings = model, settings
        H = model.horizon
        rng = np.random.default_rng(settings.base_seed + 999)     # the start schedule's stream
        n = settings.n_quantile_samples
        tau = {p.name: model.rmw_to_pf_lead_time(p) for p in model.products}
        cumulative = {}
        for p in model.products:
            cumulative[p.name] = np.cumsum(_sample_total_demand_paths(model, p, n, rng), axis=1)
        # mean forecast per week, zero after the horizon (outside the cost window)
        self.mean = {}
        for p in model.products:
            mu = np.zeros(model.demand.last_week + 60)
            for c in p.channels:
                series = model.demand.mean[(p.name, c.name)]
                mu[1:H + 1] += series[1:H + 1]
            self.mean[p.name] = mu
        self.mean_weekly = {p.name: _mean_weekly_demand(model, p) for p in model.products}

        # demand after the horizon is outside the cost and service window: windows end at H
        for p in model.products:
            cumulative[p.name][:, H + 1:] = cumulative[p.name][:, [H]]
        self.dc_weeks, self.dc_samples, self.dc_remaining, self.dc_lot_start = {}, {}, {}, {}
        for p in model.products:
            weeks = dc_order_weeks(model, p)
            self.dc_weeks[p.name] = weeks
            samples, remaining = np.zeros((H + 1, n)), np.zeros((H + 1, n))
            for t in weeks:
                L = tau[p.name] + sample_lead_time(p.lead_time_dist, n, rng)
                samples[t] = np.sort(_window_sum(cumulative[p.name], t, t + L))
                remaining[t] = np.sort(cumulative[p.name][:, H] - cumulative[p.name][:, t - 1])
            self.dc_samples[p.name], self.dc_remaining[p.name] = samples, remaining
            self.dc_lot_start[p.name] = {t: t + model.release_to_dc_median(p) for t in weeks}

        self.rm_weeks, self.rm_samples, self.rm_remaining, self.rm_lot_start, self.rm_use = {}, {}, {}, {}, {}
        for m in model.materials:
            users = model.products_using(m.name)
            weeks = rm_order_weeks(model, m)
            self.rm_weeks[m.name] = weeks
            samples, remaining = np.zeros((H + 1, n)), np.zeros((H + 1, n))
            for t in weeks:
                G = sample_lead_time(m.lead_time_dist, n, rng)
                need, rest = np.zeros(n), np.zeros(n)
                for p in users:
                    L = tau[p.name] + sample_lead_time(p.lead_time_dist, n, rng)
                    need += p.bom[m.name] * _window_sum(cumulative[p.name], t, t + G + L)
                    rest += p.bom[m.name] * (cumulative[p.name][:, H] - cumulative[p.name][:, t - 1])
                samples[t], remaining[t] = np.sort(need), np.sort(rest)
            self.rm_samples[m.name], self.rm_remaining[m.name] = samples, remaining
            self.rm_lot_start[m.name] = {t: {p.name: t + m.lead_time_median + model.release_to_dc_median(p)
                                             for p in users} for t in weeks}
            self.rm_use[m.name] = sum(p.bom[m.name] * self.mean_weekly[p.name] for p in users)

    # ------------------------------------------------------------------
    def _quantile(self, sorted_samples: np.ndarray, z: float) -> float:
        """Phi(z)-quantile of presorted samples (linear interpolation, as numpy.quantile). A
        residue below 1e-6 units is dropped, so that Phi(Phi^-1(q)) = q up to rounding error
        gives exactly the classic start's batch rounding."""
        q = normal_cdf(z)
        pos = q * (len(sorted_samples) - 1)
        i = int(pos)
        if i >= len(sorted_samples) - 1:
            return float(sorted_samples[-1])
        value = float(sorted_samples[i] + (pos - i) * (sorted_samples[i + 1] - sorted_samples[i]))
        return float(np.round(value, 6))

    def _cover(self, product_name: str, after: int, weeks: float) -> float:
        """Mean forecast of the `weeks` weeks after week `after` (fractional weeks pro rata)."""
        mu = self.mean[product_name]
        whole = int(weeks)
        total = float(mu[after + 1:after + 1 + whole].sum())
        return total + (weeks - whole) * float(mu[after + 1 + whole])

    def build(self, values: dict[Key, float]) -> PolicySchedule:
        model, H = self.model, self.model.horizon
        dc_s, dc_S, rm_s, rm_S, dc_cap, rm_floor = {}, {}, {}, {}, {}, {}
        for p in model.products:
            s_arr = np.zeros(H + 1, dtype=np.int64)
            S_arr = np.zeros(H + 1, dtype=np.int64)
            z, cover = values[("DC", p.name, "z")], values[("DC", p.name, "cover")]
            min_lot = values[("DC", p.name, "min_lot")]
            end = values[("DC", p.name, "end")]
            for t in self.dc_weeks[p.name]:
                s_val = _round_up(self._quantile(self.dc_samples[p.name][t], z), p.batch_size)
                lot = max(self._cover(p.name, self.dc_lot_start[p.name][t], cover), min_lot)
                # never order up to more than can sell by the horizon (at the end quantile)
                ceiling = _round_up(self._quantile(self.dc_remaining[p.name][t], z + end), p.batch_size)
                s_val = min(s_val, ceiling)
                s_arr[t] = s_val
                S_arr[t] = max(s_val + p.batch_size, min(s_val + _round_up(lot, p.batch_size), ceiling))
            dc_s[p.name], dc_S[p.name] = s_arr, S_arr
            largest_lot = int((S_arr - s_arr).max())
            dc_cap[p.name] = max(p.moq, largest_lot,
                                 _round_up(values[("DC", p.name, "cap")] * self.mean_weekly[p.name], p.batch_size))
        for m in model.materials:
            s_arr = np.zeros(H + 1, dtype=np.int64)
            S_arr = np.zeros(H + 1, dtype=np.int64)
            z, cover = values[("RM", m.name, "z")], values[("RM", m.name, "cover")]
            end = values[("RM", m.name, "end")]
            users = model.products_using(m.name)
            for t in self.rm_weeks[m.name]:
                s_val = _round_up(self._quantile(self.rm_samples[m.name][t], z), m.batch_size)
                lot = sum(p.bom[m.name] * self._cover(p.name, self.rm_lot_start[m.name][t][p.name], cover)
                          for p in users)
                ceiling = _round_up(self._quantile(self.rm_remaining[m.name][t], z + end), m.batch_size)
                s_val = min(s_val, ceiling)
                s_arr[t] = s_val
                S_arr[t] = max(s_val + m.batch_size, min(s_val + _round_up(lot, m.batch_size), ceiling))
            rm_s[m.name], rm_S[m.name] = s_arr, S_arr
            rm_floor[m.name] = _round_up(max(0.0, values[("RM", m.name, "floor")]) * self.rm_use[m.name],
                                         m.batch_size)
        return PolicySchedule(dc_s, dc_S, rm_s, rm_S, dc_cap, rm_floor)

    # ------------------------------------------------------------------
    def max_cover(self, kind: str, name: str) -> float:
        """Shelf life bounds the lot: an FG lot must sell before it is too old for the
        strictest channel, an RM lot before it can no longer be shipped."""
        if kind == "DC":
            p = next(x for x in self.model.products if x.name == name)
            return float(max(1, min(p.max_age_for_channel(c) for c in p.channels) - 1))
        return float(max(1, self.model.material(name).max_shippable_age - 1))

    def start_values(self, cover: str = "classic") -> dict[Key, float]:
        """Start parameters: z at the strictest channel's fill-rate target (the classic
        quantile q0), cover 1 week ("classic") or the EOQ cover ("economic"), the cap at the
        settings' start value, no RMW minimum and no end-of-horizon reduction."""
        model, st = self.model, self.settings
        dc_cover, rm_cover = economic_cover_weeks(model) if cover == "economic" else ({}, {})
        values: dict[Key, float] = {}
        for p in model.products:
            q0 = st.initial_quantile or max(c.target_fill_rate for c in p.channels)
            values[("DC", p.name, "z")] = normal_quantile(q0)
            values[("DC", p.name, "cover")] = float(dc_cover.get(p.name, st.initial_extra_cover_weeks))
            values[("DC", p.name, "min_lot")] = 0.0
            values[("DC", p.name, "cap")] = float(st.initial_cap_weeks)
            values[("DC", p.name, "end")] = 0.0
        for m in model.materials:
            users = model.products_using(m.name)
            q0 = st.initial_quantile or max(c.target_fill_rate for p in users for c in p.channels)
            values[("RM", m.name, "z")] = normal_quantile(q0)
            values[("RM", m.name, "cover")] = float(rm_cover.get(m.name, st.initial_extra_cover_weeks))
            values[("RM", m.name, "floor")] = 0.0      # the echelon rule protects first; the search adds a minimum
            values[("RM", m.name, "end")] = 0.0
        return values

    def typical_lot(self, product_name: str, values: dict[Key, float]) -> float:
        """Median lot (forecast over the cover) of a product over its order weeks."""
        weeks = self.dc_weeks[product_name]
        cover = values[("DC", product_name, "cover")]
        return float(np.median([self._cover(product_name, self.dc_lot_start[product_name][t], cover)
                                for t in weeks])) if weeks else 0.0

    def price_breaks(self, product_name: str) -> list[float]:
        p = next(x for x in self.model.products if x.name == product_name)
        return [0.0] + [float(b) for b in price_break_lots(self.model, p)]


def add_offsets(schedule: PolicySchedule, offsets: PolicySchedule | None) -> PolicySchedule:
    """Levels + week-specific offsets (the order cap never below the largest lot)."""
    if offsets is None:
        return schedule
    new = schedule.copy()
    for name in new.dc_s:
        new.dc_s[name] += offsets.dc_s[name]
        new.dc_S[name] += offsets.dc_S[name]
        new.dc_cap[name] = max(new.dc_cap[name] + offsets.dc_cap[name], int((new.dc_S[name] - new.dc_s[name]).max()))
    for name in new.rm_s:
        new.rm_s[name] += offsets.rm_s[name]
        new.rm_S[name] += offsets.rm_S[name]
        new.rm_floor[name] += offsets.rm_floor[name]
    return new


def sum_offsets(a: PolicySchedule, b: PolicySchedule) -> PolicySchedule:
    """a + b, item by item (offsets are plain differences, no rounding or cap rule)."""
    return PolicySchedule({k: a.dc_s[k] + b.dc_s[k] for k in a.dc_s}, {k: a.dc_S[k] + b.dc_S[k] for k in a.dc_S},
                          {k: a.rm_s[k] + b.rm_s[k] for k in a.rm_s}, {k: a.rm_S[k] + b.rm_S[k] for k in a.rm_S},
                          {k: a.dc_cap[k] + b.dc_cap[k] for k in a.dc_cap},
                          {k: a.rm_floor[k] + b.rm_floor[k] for k in a.rm_floor})


def zero_offsets(model: ModelInput) -> PolicySchedule:
    H = model.horizon
    zeros = lambda: np.zeros(H + 1, dtype=np.int64)  # noqa: E731
    return PolicySchedule({p.name: zeros() for p in model.products}, {p.name: zeros() for p in model.products},
                          {m.name: zeros() for m in model.materials}, {m.name: zeros() for m in model.materials},
                          {p.name: 0 for p in model.products}, {m.name: 0 for m in model.materials})


def difference(after: PolicySchedule, before: PolicySchedule) -> PolicySchedule:
    """after - before, item by item (used to turn an in-place local raise into an offset)."""
    return PolicySchedule({k: after.dc_s[k] - before.dc_s[k] for k in after.dc_s},
                          {k: after.dc_S[k] - before.dc_S[k] for k in after.dc_S},
                          {k: after.rm_s[k] - before.rm_s[k] for k in after.rm_s},
                          {k: after.rm_S[k] - before.rm_S[k] for k in after.rm_S},
                          {k: after.dc_cap[k] - before.dc_cap[k] for k in after.dc_cap},
                          {k: after.rm_floor[k] - before.rm_floor[k] for k in after.rm_floor})
