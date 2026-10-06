"""
Simulation-optimisation of the (s, S) schedule: a search over a few forecast-scaled
parameters per item, then week-specific exceptions (docs/search_algorithm.md).

The levels follow the forecast (meio/parametric.py): per finished good a safety factor z
(s = the Phi(z)-quantile of the demand over the protection interval), a lot cover m (weeks
of forecast), a minimum lot (a price break), an order cap and an end-of-horizon quantile;
per raw material z, m, a minimum physical stock and an end quantile. About 4-5 numbers per
item instead of an s and an S per item and week.

    MULTI-START : classic (z at the fill-rate target, one week of cover), economic-lot
                  (EOQ cover) and price-break starts (minimum lot = a discount break); each
                  gets a global repair and one line-search pass; the best one continues
    PHASE A     : parameters only
        repair      greedy marginal analysis: for the earliest failing cell simulate every
                    parameter that can feed it (z, end quantile, cap of the product or of a
                    raw material that cut its releases) and raise the one with the largest
                    service gain per unit of extra cost; cells no parameter can fix are
                    deferred to phase B (and may not get worse meanwhile)
        line search per parameter a grid of values around the current one, all simulated
                    at once; the cheapest feasible one that the confirmation seeds accept is
                    kept; the grid halves every pass. A move that is much cheaper but breaks
                    a few cells (a price-break lot, one supplier order less) gets a short
                    repair first (large-neighbourhood move).
    PHASE B     : week-specific exceptions on top of the levels (sparse offsets)
        local repair  the same marginal analysis on the weeks that feed the deferred cells
                      (pre-building before closed weeks, the first weeks after the review)
        polish        one coarse-to-fine pass of single-item block moves (halves ... weeks)
    HOLD-OUT    : weak cells get a margin and are repaired on the hold-out futures themselves
                  (a risk too rare for the search seeds), then a local repair and a finer
                  line-search pass; up to max_outer_rounds rounds

All candidates are compared on the same search seeds (common random numbers); candidates of
one step are simulated in parallel worker processes with the same result as one after the
other.
"""
from __future__ import annotations

import multiprocessing
import os
import warnings
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import service
from .config import ModelInput, SearchSettings
from .parametric import Key, LevelBuilder, Z_MAX, Z_MIN, add_offsets, difference, zero_offsets
from .policy import PolicySchedule, dc_order_weeks, feeding_release_weeks, initial_schedule, rm_order_weeks
from .scenarios import ScenarioSet, build_scenarios
from .simulation import SimResult, simulate


# ---------------------------------------------------------------------------
# Parallel evaluation of candidate moves (a speed-up only: results are identical)
# ---------------------------------------------------------------------------
_WORKER: dict = {}


def _init_worker(model: ModelInput, seeds: ScenarioSet) -> None:
    """Runs once in every worker process: keep the model and the search seeds there."""
    _WORKER["model"], _WORKER["seeds"] = model, seeds


def _simulate_cells(schedule: PolicySchedule, z: float, margins: dict):
    """Worker task: mean cost, cell table (search rule applied) and cut shares of one schedule."""
    model = _WORKER["model"]
    result = simulate(model, schedule, _WORKER["seeds"], report_details=False)
    return (result.mean_total_cost(), service.apply_search_rule(service.cell_table(model, result), z, margins),
            cut_shares(model, result))


def cut_shares(model: ModelInput, result: SimResult) -> dict:
    """What cut the releases, per week: the share of futures in which the order cap, the
    production capacity or a raw material was the binding limit. All the repair diagnosis
    needs from a simulation (small enough to come back from a worker process)."""
    out = {}
    for p in model.products:
        out[("cap", p.name)] = result.dc[p.name]["cut_by_policy_cap"].mean(axis=0)
        out[("capacity", p.name)] = result.dc[p.name]["cut_by_capacity"].mean(axis=0)
    for m in model.materials:
        out[("rm", m.name)] = result.rm[m.name]["limited_release"].mean(axis=0)
    return out


def default_workers() -> int:
    """Worker processes for the search: one core stays free, at most 4 (0/1 = sequential)."""
    return max(1, min(4, (os.cpu_count() or 1) - 1))


@dataclass
class Evaluation:
    """One schedule evaluated on the search seeds."""
    schedule: PolicySchedule
    result: SimResult
    cells: pd.DataFrame
    cost: float
    feasible: bool
    n_failing: int
    cuts: dict | None = None          # cut_shares: what limited the releases, per week


@dataclass
class OptimisationOutcome:
    start_schedule: PolicySchedule
    schedule: PolicySchedule
    margins: dict
    unfixable: set
    search_log: list[dict] = field(default_factory=list)
    holdout_rounds: list[pd.DataFrame] = field(default_factory=list)
    last_search_eval: Evaluation | None = None
    last_holdout_cells: pd.DataFrame | None = None
    n_evaluations: int = 0
    chosen_start: str = ""
    parameters: dict = field(default_factory=dict)    # the tuned parameters {(kind, item, name): value}


class Searcher:
    def __init__(self, model: ModelInput, settings: SearchSettings,
                 search_seeds: ScenarioSet, holdout_seeds: ScenarioSet, verbose: bool = True,
                 start_values: dict | None = None):
        self.model = model
        self.settings = settings
        self.search_seeds = search_seeds
        self.holdout_seeds = holdout_seeds
        self.verbose = verbose
        self.margins: dict = {}
        self.unfixable: set = set()
        self.unfixable_floor: dict = {}     # unfixable cell -> share of search futures it must keep
        self.log: list[dict] = []
        self.n_evaluations = 0
        self.rng = np.random.default_rng(settings.base_seed + 7)
        self.start_values = start_values          # warm start: last review's parameters (horizon-free)
        self.pool = None                          # worker processes while run() is active
        # confirmation seeds: an independent set that only accepted moves are simulated on
        n_confirm = int(round(settings.confirm_seed_factor * settings.n_search_seeds))
        self.confirm_seeds = (build_scenarios(model, n_confirm, settings.base_seed + 4, "confirm")
                              if n_confirm > 0 else None)
        self.confirm_failing: set = set()    # cells failing on the confirmation seeds (current schedule)
        self.confirm_floor: dict = {}        # unfixable cell -> mean fill on the confirmation seeds
        self.builder = LevelBuilder(model, settings)  # levels from parameters
        self.offsets = zero_offsets(model)            # week-specific exceptions on top of the levels
        self.deferred: set = set()                    # cells no parameter can fix: left to the local repair

    # ------------------------------------------------------------------
    def say(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    def record(self, round_no, phase, action, detail, ev: Evaluation) -> None:
        self.log.append({"round": round_no, "phase": phase, "evaluation_no": self.n_evaluations,
                         "action": action, "detail": detail, "mean_cost": round(ev.cost, 2),
                         "feasible": ev.feasible, "failing_cells": ev.n_failing})

    def evaluate(self, schedule: PolicySchedule) -> Evaluation:
        """Simulate the search seeds and apply the search rule. Cells declared unfixable (or
        deferred) do not count, except during an improvement phase (line search, polish): there
        they must keep at least the service (share of search futures meeting F) they had when
        the phase started (self.unfixable_floor), so cost is never bought with less service on
        cells the search could not fix."""
        self.n_evaluations += 1
        result = simulate(self.model, schedule, self.search_seeds, report_details=False)
        cells = service.apply_search_rule(service.cell_table(self.model, result),
                                          self.settings.z, self.margins)
        return self.judge(schedule, result.mean_total_cost(), cells, result, cut_shares(self.model, result))

    def evaluate_many(self, schedules: list[PolicySchedule]) -> list[Evaluation]:
        """Evaluate several candidates, in parallel worker processes if there are any.
        The simulations are deterministic, so the evaluations are identical either way
        (they only lack the SimResult, which the improve phase does not need)."""
        if self.pool is None or len(schedules) < 2:
            return [self.evaluate(schedule) for schedule in schedules]
        try:
            futures = [self.pool.submit(_simulate_cells, schedule, self.settings.z, self.margins)
                       for schedule in schedules]
            outcomes = [future.result() for future in futures]
        except BrokenProcessPool:
            # e.g. a script without `if __name__ == "__main__":` - continue one after the other
            warnings.warn("search worker processes failed; continuing without parallel evaluation")
            self.pool = None
            return [self.evaluate(schedule) for schedule in schedules]
        self.n_evaluations += len(schedules)
        return [self.judge(schedule, cost, cells, None, cuts)
                for schedule, (cost, cells, cuts) in zip(schedules, outcomes)]

    @contextmanager
    def workers(self):
        """Worker processes for evaluate_many during the search (none if n_workers <= 1)."""
        n = self.settings.n_workers if self.settings.n_workers > 0 else default_workers()
        if n <= 1:
            yield
            return
        # "spawn" works the same on Windows, macOS and Linux and is safe inside the API's threads;
        # a broken worker raises BrokenProcessPool (handled in evaluate_many) instead of hanging
        pool = ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                                   initializer=_init_worker, initargs=(self.model, self.search_seeds))
        self.pool = pool
        try:
            yield
        finally:
            self.pool = None
            pool.shutdown(wait=True, cancel_futures=True)

    def judge(self, schedule: PolicySchedule, cost: float, cells: pd.DataFrame, result=None,
              cuts: dict | None = None) -> Evaluation:
        """Feasibility of an evaluated schedule (see evaluate)."""
        keys = list(zip(cells["product"], cells["channel"], cells["week"]))
        share = cells["share_met"].to_numpy()
        unfixable = np.array([k in self.unfixable for k in keys])
        kept = np.array([np.isnan(b) or b >= self.unfixable_floor.get(k, -np.inf) - 1e-9
                         for k, b in zip(keys, share)])
        failing = int((~cells["search_feasible"].to_numpy() & ~unfixable).sum() + (unfixable & ~kept).sum())
        return Evaluation(schedule, result, cells, cost, failing == 0, failing, cuts)

    def confirmation(self, schedule: PolicySchedule, with_cost: bool = False):
        """Cells that are weak on the confirmation seeds - share of futures meeting F below alpha,
        the hold-out rule (the search seeds already carry Z x SE and the margins) - and that
        share for the unfixable cells (an unfixable cell is weak below its confirmation floor)."""
        self.n_evaluations += 1
        result = simulate(self.model, schedule, self.confirm_seeds, report_details=False)
        cells = service.cell_table(self.model, result)
        failing, means = set(), {}
        for p, c, w, mean, target in zip(cells["product"], cells["channel"], cells["week"],
                                         cells["share_met"], cells["target_share"]):
            key = (p, c, int(w))
            if np.isnan(mean):
                continue
            if key in self.unfixable:
                means[key] = float(mean)
                if mean < self.confirm_floor.get(key, -np.inf) - 1e-9:
                    failing.add(key)
            elif mean < target:
                failing.add(key)
        return (failing, means, result.mean_total_cost()) if with_cost else (failing, means)

    def confirmed(self, schedule: PolicySchedule) -> bool:
        """Optimizer's-curse check of a move the search seeds accepted: on the independent
        confirmation seeds no cell may become weak (share of futures meeting F < alpha) that was not weak before
        the move (the same "no cell worse" idea as the week-1 lookahead). Without
        confirmation seeds: True."""
        if self.confirm_seeds is None:
            return True
        failing, _ = self.confirmation(schedule)
        if failing <= self.confirm_failing:
            self.confirm_failing = failing
            return True
        return False

    def service_floors(self, ev: Evaluation) -> dict:
        """Share of search futures meeting F of every unfixable cell in `ev` (cells without
        demand: no floor)."""
        floors = {}
        for k, b in zip(zip(ev.cells["product"], ev.cells["channel"], ev.cells["week"]), ev.cells["share_met"]):
            k = (k[0], k[1], int(k[2]))
            if k in self.unfixable and not np.isnan(b):
                floors[k] = float(b)
        return floors

    @staticmethod
    def service_gap(ev: Evaluation) -> float:
        """Total gap to target over all cells, sum of max(0, alpha - search lower bound):
        0 when every cell passes. Compares starts on service before cost."""
        cells = ev.cells
        return float((cells["target_share"] - cells["search_lower_bound"]).clip(lower=0).fillna(0).sum())

    # ------------------------------------------------------------------
    # Step sizes and moves
    # ------------------------------------------------------------------
    @staticmethod
    def step_size(level: int, batch: int, fraction: float) -> int:
        """Step = fraction of the current level, rounded up to whole batches (at least one batch)."""
        return max(batch, int(np.ceil(fraction * level / batch)) * batch)

    def levels(self, schedule: PolicySchedule, kind: str, name: str):
        """Return (s array, S array, batch size) of one product (DC) or material (RM)."""
        if kind == "DC":
            p = next(x for x in self.model.products if x.name == name)
            return schedule.dc_s[name], schedule.dc_S[name], p.batch_size
        m = self.model.material(name)
        return schedule.rm_s[name], schedule.rm_S[name], m.batch_size

    def moved(self, schedule: PolicySchedule, kind: str, name: str, t: int,
              move: str, fraction: float) -> PolicySchedule | None:
        """Copy of the schedule with one move applied, or None if the move is not allowed."""
        new = schedule.copy()
        if kind == "DC cap":                             # order cap of a product
            p = next(x for x in self.model.products if x.name == name)
            step = self.step_size(new.dc_cap[name], p.batch_size, fraction)
            new.dc_cap[name] += step if move == "raise" else -step
            return new if new.dc_cap[name] >= p.moq else None
        if kind == "RM floor":                           # minimum physical RM stock
            m = self.model.material(name)
            step = self.step_size(max(new.rm_floor[name], m.batch_size), m.batch_size, fraction)
            new.rm_floor[name] += step if move == "raise" else -step
            return new if new.rm_floor[name] >= 0 else None
        s, S, batch = self.levels(new, kind, name)
        step = self.step_size(S[t], batch, fraction)
        if move == "raise s and S":
            s[t] += step
            S[t] += step
        elif move == "lower s and S":
            if s[t] - step < 0:
                return None
            s[t] -= step
            S[t] -= step
        elif move == "lower S (smaller orders)":
            if S[t] - step < s[t] + batch:
                return None
            S[t] -= step
        elif move == "raise S (larger orders)":
            S[t] += step
        else:
            raise ValueError(move)
        return new

    def rm_weeks_feeding(self, product_name: str, release_weeks) -> dict[str, list[int]]:
        """RMW order weeks of each BOM material whose deliveries can arrive by the given
        DC release weeks (the RM leaves the RMW in the release week)."""
        p = next(x for x in self.model.products if x.name == product_name)
        feeding = {}
        for mat_name in p.bom:
            m = self.model.material(mat_name)
            allowed = set(rm_order_weeks(self.model, m))
            feeding[mat_name] = sorted({o for r in release_weeks
                                        for o in range(r - m.lead_time_max, r - m.lead_time_min + 1)
                                        if o in allowed})
        return feeding

    def moved_block(self, schedule: PolicySchedule, kind: str, name: str, weeks: list[int],
                    move: str, fraction: float) -> PolicySchedule | None:
        """Copy of the schedule with one move applied to a block of order weeks (the same
        absolute step in every week of the block), or None if the move is not allowed.

        A block of one week is the classic single-week move. "lot with RM support" moves
        the DC lot S - s and, by BOM x the same step, the RMW (s, S) of the weeks that
        feed those releases: the echelon levels upstream follow the downstream lot, so
        a larger FG lot is not cut by missing raw material (and vice versa)."""
        if kind in ("DC cap", "RM floor"):
            return self.moved(schedule, kind, name, 0, move, fraction)
        new = schedule.copy()
        s, S, batch = self.levels(new, kind, name)
        w = np.asarray(weeks)
        step = self.step_size(int(S[w].mean()), batch, fraction)
        if move == "lower s and S":
            if (s[w] - step < 0).any():
                return None
            s[w] -= step
            S[w] -= step
        elif move == "raise s and S":
            s[w] += step
            S[w] += step
        elif move in ("lower S (smaller orders)", "lower lot with RM support"):
            if (S[w] - step < s[w] + batch).any():
                return None
            S[w] -= step
        elif move in ("raise S (larger orders)", "raise lot with RM support"):
            S[w] += step
        else:
            raise ValueError(move)
        if move.endswith("with RM support"):
            p = next(x for x in self.model.products if x.name == name)
            sign = 1 if move.startswith("raise") else -1
            for mat_name, rm_weeks in self.rm_weeks_feeding(name, list(weeks)).items():
                if not rm_weeks:
                    continue
                rs, rS = new.rm_s[mat_name], new.rm_S[mat_name]
                rw = np.asarray(rm_weeks)
                rm_step = int(np.ceil(p.bom[mat_name] * step))
                if sign < 0 and (rs[rw] - rm_step < 0).any():
                    return None
                rs[rw] += sign * rm_step
                rS[rw] += sign * rm_step
        if kind == "DC":                       # the order cap must not cut the new lot
            new.dc_cap[name] = max(new.dc_cap[name], int((S - s).max()))
        return new

    @staticmethod
    def week_blocks(weeks: list[int], size: int) -> list[list[int]]:
        """Consecutive blocks of `size` order weeks (the last one may be shorter)."""
        return [weeks[i:i + size] for i in range(0, len(weeks), size)]

    @staticmethod
    def block_sizes(n_weeks: int) -> list[int]:
        """Coarse-to-fine block sizes for an item with n_weeks order weeks:
        n, n/2, n/4, ... (rounded up), ending with single weeks."""
        sizes, size = [], n_weeks
        while size > 1:
            sizes.append(size)
            size = int(np.ceil(size / 2))
        return sizes + [1] if n_weeks >= 1 else []

    # ------------------------------------------------------------------
    # Polish: coarse-to-fine block moves on the full schedule (phase B)
    # ------------------------------------------------------------------
    def improve(self, current: Evaluation, round_no: int, n_levels: int | None = None,
                n_passes: int | None = None, fraction: float | None = None,
                phase: str = "improve", first_level: int = 0) -> Evaluation:
        """Coarse-to-fine pattern search on the full schedule (the polish of phase B). One pass
        goes through the block levels from first_level on (0 = whole horizon, 1 = halves, ...)
        down to single weeks; at every block
        the first move that lowers the cost (and stays feasible) is accepted and repeated
        while it keeps paying off. The step fraction is halved after every pass.

        Adaptive move selection: a move type that found nothing at a block level in one
        pass is not tried at that level in the next pass (unless the whole pass found
        nothing). This spends the simulations where improvements are found."""
        st, model = self.settings, self.model
        if not current.feasible:
            self.say("    improve skipped: schedule is not feasible on the search seeds")
            return current
        items = [("DC", p.name, dc_order_weeks(model, p)) for p in model.products]
        items += [("RM", m.name, rm_order_weeks(model, m)) for m in model.materials]
        items = [(kind, name, weeks) for kind, name, weeks in items if weeks]
        moves = {
            "DC": ["lower s and S", "lower lot with RM support", "lower S (smaller orders)",
                   "raise lot with RM support", "raise S (larger orders)"],
            "RM": ["lower s and S", "lower S (smaller orders)", "raise S (larger orders)"],
            "DC cap": ["lower", "raise"], "RM floor": ["lower", "raise"],
        }
        all_levels = max(len(self.block_sizes(len(weeks))) for _, _, weeks in items)
        n_levels = min(n_levels or all_levels, all_levels)
        fraction = fraction or st.step_fraction
        self.unfixable_floor = self.service_floors(current)     # unfixable cells: never worse
        if self.confirm_seeds is not None:                      # ... on the confirmation seeds too
            self.confirm_floor = {}
            self.confirm_failing, self.confirm_floor = self.confirmation(current.schedule)
        productive = None                     # (kind, level, move) that found something last pass

        for pass_no in range(1, (n_passes or st.max_improve_passes) + 1):
            accepted = 0
            cost_before = current.cost
            hits: set = set()
            for level in range(first_level, n_levels):
                blocks = []
                for kind, name, weeks in items:
                    sizes = self.block_sizes(len(weeks))
                    if level < len(sizes):
                        blocks += [(kind, name, block) for block in self.week_blocks(weeks, sizes[level])]
                if level == 0:                   # the scalar levels belong to the coarsest level
                    blocks += [("DC cap", p.name, [0]) for p in model.products]
                    blocks += [("RM floor", m.name, [0]) for m in model.materials]
                for i in self.rng.permutation(len(blocks)):
                    kind, name, block = blocks[i]
                    tried = [(move, self.moved_block(current.schedule, kind, name, block, move, fraction))
                             for move in moves[kind]
                             if productive is None or (kind, level, move) in productive]
                    tried = [(move, candidate) for move, candidate in tried if candidate is not None]
                    if self.pool is not None:            # all moves of the block at once ...
                        evaluated = self.evaluate_many([candidate for _, candidate in tried])
                    else:                                # ... or one after the other, lazily
                        evaluated = None
                    for j, (move, candidate) in enumerate(tried):
                        ev = evaluated[j] if evaluated is not None else self.evaluate(candidate)
                        # the first move in the fixed order that pays off wins (same choice either way)
                        if not (ev.feasible and ev.cost < current.cost - 1e-6 and self.confirmed(candidate)):
                            continue
                        hits.add((kind, level, move))
                        repeats = 0
                        while True:                  # accepted: keep going in this direction
                            current = ev
                            accepted += 1
                            where = (f"weeks {block[0]}-{block[-1]}" if kind in ("DC", "RM")
                                     else "(whole horizon)")
                            self.record(round_no, phase, f"{move}: {kind} {name} {where}",
                                        f"pass {pass_no}, step fraction {fraction:.3f}", ev)
                            repeats += 1
                            if repeats > (st.polish_move_repeats if phase == "polish" else st.max_move_repeats):
                                break
                            candidate = self.moved_block(current.schedule, kind, name, block, move, fraction)
                            if candidate is None:
                                break
                            ev = self.evaluate(candidate)
                            if not (ev.feasible and ev.cost < current.cost - 1e-6 and self.confirmed(candidate)):
                                break
                        break
            self.say(f"    improve pass {pass_no}: {accepted:3d} moves accepted, "
                     f"mean cost {cost_before:,.0f} -> {current.cost:,.0f} (step fraction {fraction:.3f})")
            productive = hits if accepted else None
            fraction /= 2                        # finer steps in the next pass
            if fraction < st.min_step_fraction:
                break
        self.unfixable_floor, self.confirm_floor = {}, {}
        return current

    # ------------------------------------------------------------------
    # B2. Restructure: large-neighbourhood moves to the price-break lots
    def no_new_weak_cells(self, before: Evaluation, after: Evaluation) -> bool:
        """Confirmation for a large move: on the confirmation seeds it is cheaper as well and no
        cell is weak after it that was not weak before (True without confirmation seeds).
        A large jump changes many weeks at once, so its cost gain on the search seeds is
        checked on independent seeds too, not only its service."""
        if self.confirm_seeds is None:
            return True
        self.confirm_floor = {}
        weak_before, _, cost_before = self.confirmation(before.schedule, with_cost=True)
        weak_after, _, cost_after = self.confirmation(after.schedule, with_cost=True)
        return weak_after <= weak_before and cost_after < cost_before

    # ------------------------------------------------------------------
    # Main loop

    # ------------------------------------------------------------------
    # Parameters -> schedule
    # ------------------------------------------------------------------
    def schedule_of(self, values: dict[Key, float], offsets: PolicySchedule | None = None) -> PolicySchedule:
        """The full (s, S) schedule: forecast-scaled levels plus the week-specific offsets."""
        return add_offsets(self.builder.build(values), self.offsets if offsets is None else offsets)

    def coordinates(self) -> list[Key]:
        """The search variables, in a fixed order: 5 per finished good, 4 per raw material."""
        keys = []
        for p in self.model.products:
            keys += [("DC", p.name, "z"), ("DC", p.name, "cover"), ("DC", p.name, "min_lot"), ("DC", p.name, "cap"),
                     ("DC", p.name, "end")]
        for m in self.model.materials:
            keys += [("RM", m.name, "z"), ("RM", m.name, "cover"), ("RM", m.name, "floor"), ("RM", m.name, "end")]
        return keys

    # ------------------------------------------------------------------
    # Repair: greedy marginal analysis
    # ------------------------------------------------------------------
    def feeding_weeks(self, product, week: int) -> list[int]:
        """Release weeks whose FG can arrive in `week`; if all of them are closed, the latest
        open order week before them (pre-build)."""
        release_weeks = feeding_release_weeks(self.model, product, week)
        if release_weeks:
            return release_weeks
        first = week - self.model.release_to_dc_max(product)
        earlier = [r for r in dc_order_weeks(self.model, product) if r < first]
        return [max(earlier)] if first >= 1 and earlier else []

    def repair_candidates(self, values, ev: Evaluation, product_name: str, week: int,
                          dz: float, fraction: float | None, mode: str) -> list[tuple[str, dict, object]]:
        """Possible fixes for one failing cell, as (label, values, offsets):
          local  : raise the DC levels of the release weeks that feed the week, or the RMW
                   levels (and minimum) of a raw material that cut those releases
          global : raise the safety factor or the end quantile of the product or of a
                   binding material, or the order cap if it cut the orders"""
        model, st = self.model, self.settings
        frac = fraction or st.step_fraction
        p = next(x for x in model.products if x.name == product_name)
        weeks = self.feeding_weeks(p, week)
        if not weeks:
            return []
        cut = lambda kind, name: ev.cuts[(kind, name)][weeks].max() > st.cut_share_threshold  # noqa: E731
        # local fixes: a material that cut the releases in any future is a candidate (with a chance
        # constraint the failing futures are the rare ones; the marginal analysis decides if it pays);
        # parameters act on the whole horizon: only materials that cut often
        short = [mat for mat in p.bom
                 if (ev.cuts[("rm", mat)][weeks].max() > 0 if mode == "local" else cut("rm", mat))]
        out = []
        if mode == "global":                            # parameters only: z, end quantile, cap
            if cut("cap", p.name):
                new = dict(values)
                new[("DC", p.name, "cap")] += 0.5
                out.append((f"cap {p.name} +0.5", new, self.offsets))
            for kind, name in [("DC", p.name)] + [("RM", mat) for mat in short]:
                if values[(kind, name, "end")] < 0:
                    new = dict(values)
                    new[(kind, name, "end")] = min(0.0, values[(kind, name, "end")] + 0.25)
                    out.append((f"end {kind} {name} +0.25", new, self.offsets))
                if values[(kind, name, "z")] + dz <= Z_MAX:
                    new = dict(values)
                    new[(kind, name, "z")] += dz
                    out.append((f"z {kind} {name} +{dz:g}", new, self.offsets))
            return out
        off = self.offsets.copy()                       # local DC raise (pre-build if capacity binds)
        dc_weeks = weeks
        if cut("capacity", p.name):
            span = p.lead_time_max - p.lead_time_min + 1
            allowed = set(dc_order_weeks(model, p))
            dc_weeks = [r for r in range(weeks[0] - span, weeks[0]) if r in allowed] or weeks
        S = ev.schedule.dc_S[p.name]
        for r in dc_weeks:
            step = self.step_size(S[r], p.batch_size, frac)
            off.dc_s[p.name][r] += step
            off.dc_S[p.name][r] += step
        out.append((f"local DC {p.name} weeks {dc_weeks[0]}-{dc_weeks[-1]}", values, off))
        for mat in short:                               # local RM raise per binding material
            m = model.material(mat)
            allowed = set(rm_order_weeks(model, m))
            rm_weeks = sorted({o for r in weeks for o in range(r - m.lead_time_max, r - m.lead_time_min + 1)
                               if o in allowed})
            if not rm_weeks:
                continue
            off = self.offsets.copy()
            S = ev.schedule.rm_S[mat]
            for o in rm_weeks:
                step = self.step_size(S[o], m.batch_size, frac)
                off.rm_s[mat][o] += step
                off.rm_S[mat][o] += step
            off.rm_floor[mat] += self.step_size(max(ev.schedule.rm_floor[mat], m.batch_size), m.batch_size, frac)
            out.append((f"local RM {mat} weeks {rm_weeks[0]}-{rm_weeks[-1]}", values, off))
        if cut("cap", p.name):
            new = dict(values)
            new[("DC", p.name, "cap")] += 0.5
            out.append((f"cap {p.name} +0.5", new, self.offsets))
        for kind, name in [("DC", p.name)] + [("RM", mat) for mat in short]:
            # the end quantile only acts on the last weeks: a local fix as well
            if values[(kind, name, "end")] < 0:
                new = dict(values)
                new[(kind, name, "end")] = min(0.0, values[(kind, name, "end")] + 0.25)
                out.append((f"end {kind} {name} +0.25", new, self.offsets))
        return out

    @staticmethod
    def open_gap(ev: Evaluation, unfixable: set) -> tuple[float, dict]:
        """Service gap over the cells that are still to be fixed - the sum of the squared
        shortfalls (alpha - search lower bound)^2: every cell must reach alpha, so starving one
        cell weighs more than small gains on several others - and the lower bound per cell."""
        cells = ev.cells
        bounds = {}
        gap = 0.0
        for p, c, w, target, bound, ok in zip(cells["product"], cells["channel"], cells["week"], cells["target_share"],
                                             cells["search_lower_bound"], cells["search_feasible"]):
            key = (p, c, int(w))
            b = -1.0 if np.isnan(bound) else float(bound)
            bounds[key] = b
            if not ok and key not in unfixable:
                gap += max(0.0, target - b) ** 2
        return gap, bounds

    def repair(self, values, round_no: int, dz: float | None = None, give_up_above: float = np.inf,
               mode: str = "local", fraction: float | None = None, max_steps: int | None = None,
               cost_limit: float = np.inf):
        """Greedy marginal analysis (cf. Sherbrooke's METRIC): while a cell fails the search
        rule, take the earliest failing cell, simulate every possible fix for it at once
        (repair_candidates, in parallel) and keep, among the fixes that lower the total service
        gap of all open cells, one that moves the cell itself with the largest gap reduction per
        unit of extra cost.

          mode "global": parameters only (phase A); a cell they do not move in 2 steps is
                         deferred to the local repair and may not get worse meanwhile
          mode "local" : week-specific raises (phase B, hold-out rounds); a cell no fix moves
                         for repair_patience steps is declared unfixable
        A step without progress doubles the next step (at most 2 x step_fraction). The raises
        made for a cell that is given up are undone. Stops early when more cells than
        give_up_above are left out or the cost reaches cost_limit (a repair only adds cost).
        Returns (evaluation, values); the offsets are updated in self.offsets."""
        st = self.settings
        dz = dz or st.repair_dz
        ev = self.evaluate(self.schedule_of(values))
        progress, checkpoint = {}, {}
        for _ in range(max_steps or st.max_repair_steps):
            if len(self.unfixable) > give_up_above or ev.cost >= cost_limit:
                break                          # (a repair only adds cost: past the limit it cannot win)
            cells = ev.cells
            keys = list(zip(cells["product"], cells["channel"], cells["week"]))
            open_mask = ~cells["search_feasible"].to_numpy() & np.array([k not in self.unfixable for k in keys])
            if not open_mask.any():
                break
            cell = cells[open_mask].sort_values(["week", "search_lower_bound"]).iloc[0]
            key = (cell["product"], cell["channel"], int(cell["week"]))
            best, stuck = progress.get(key, (-np.inf, 0))
            if cell["search_lower_bound"] > best + st.repair_min_progress:
                progress[key] = (cell["search_lower_bound"], 0)
                checkpoint[key] = (dict(values), self.offsets.copy())
            else:
                progress[key] = (best, stuck + 1)
            # a step that brought no progress is doubled next time (small steps may not move a
            # share measured on a few hundred futures at all)
            step = min((fraction or st.step_fraction) * 2 ** progress[key][1], 2 * st.step_fraction)
            candidates = self.repair_candidates(values, ev, key[0], key[2], dz * 2 ** progress[key][1], step, mode)
            patience = st.repair_patience if mode == "local" else 2
            if progress[key][1] >= patience or not candidates:
                self.unfixable.add(key)
                if mode == "global":                   # left to the local repair (phase B)
                    self.deferred.add(key)
                if progress[key][1] >= patience:
                    values, self.offsets = dict(checkpoint[key][0]), checkpoint[key][1].copy()
                    ev = self.evaluate(self.schedule_of(values))
                self.record(round_no, "repair", "cell deferred" if mode == "global" else "cell declared unfixable",
                            f"{key}", ev)
                continue
            gap_before, bounds_before = self.open_gap(ev, self.unfixable)
            evaluated = self.evaluate_many([self.schedule_of(v, off) for _, v, off in candidates])
            # a fix must lower the total gap over all open cells (raising one week's stock can starve
            # another, e.g. older stock for a strict channel); among those: fixes that move the cell
            # itself first, then gap reduction per unit of extra cost
            best_choice, best_score = None, (-1, -np.inf)
            for (label, v, off), cand in zip(candidates, evaluated):
                gap_after, bounds_after = self.open_gap(cand, self.unfixable)
                gain = gap_before - gap_after
                own = bounds_after[key] - bounds_before[key]
                if gain <= 1e-9:
                    continue
                score = (int(own > st.repair_min_progress), gain / max(cand.cost - ev.cost, 1.0))
                if score > best_score:
                    best_choice, best_score = (label, v, off, cand), score
            if best_choice is None:                    # nothing helps this step: count as no progress
                label, v, off, cand = candidates[0][0], candidates[0][1], candidates[0][2], evaluated[0]
            else:
                label, v, off, cand = best_choice
            values, self.offsets = dict(v), off.copy()
            ev = cand
            self.record(round_no, "repair", f"fix {key[1]} week {key[2]}", label, ev)
        return ev, values

    GRID = {"z": (-1.0, -0.6, -0.3, -0.15, 0.15, 0.3), "cover": (-2, -1, -0.5, 0.5, 1, 2),
            "cap": (-1, -0.5, 0.5, 1), "floor": (-1, -0.5, 0.5, 1), "end": (-1, -0.5, -0.25, 0.25, 0.5)}
    FLOOR_FACTORS = (0.0, 0.5, 0.75, 1.25)      # the floor also moves multiplicatively (0 = no minimum)
    COARSE_GRID = {"z": (-0.6, -0.3, 0.3), "cover": (-1, 1), "cap": (-1, 1), "floor": (-1, 1), "end": (-1, -0.5)}
    COARSE_FLOOR_FACTORS = (0.0, 0.5)            # racing the starts: fewer points per parameter

    def line_candidates(self, values, key: Key, scale: float, coarse: bool = False) -> list[tuple[str, dict]]:
        """Grid of values for one parameter around its current value (a line search);
        coarse = the smaller grid used to race the starts."""
        kind, name, param = key
        out = []
        if param == "min_lot":
            for lot in self.builder.price_breaks(name):
                if lot != values[key]:
                    new = dict(values)
                    new[key] = lot
                    out.append((f"min lot {name} -> {lot:.0f}", new))
            return out
        grid, factors = (self.COARSE_GRID, self.COARSE_FLOOR_FACTORS) if coarse else (self.GRID, self.FLOOR_FACTORS)
        targets = [(values[key] + delta * scale, f"{delta * scale:+.3g}") for delta in grid[param]]
        if param == "floor":
            targets += [(values[key] * f, f"x{f:g}") for f in factors if values[key] * f != values[key]]
        for target, d in targets:
            new = dict(values)
            new[key] = target
            if param == "z" and not Z_MIN <= new[key] <= Z_MAX:
                continue
            if param == "cover" and not 0 <= new[key] <= self.builder.max_cover(kind, name):
                continue
            if param in ("cap", "floor") and new[key] < 0:
                continue
            if param == "end" and not -3 <= new[key] <= 0:
                continue
            out.append((f"{param} {kind} {name} {d}", new))
            step = target - values[key]
            if param == "cover" and kind == "DC" and abs(abs(step) - scale) < 1e-9:
                both = dict(new)          # the RM covers follow the FG lot (echelon coordination)
                for mat in next(p for p in self.model.products if p.name == name).bom:
                    rk = ("RM", mat, "cover")
                    both[rk] = min(max(0.0, values[rk] + step), self.builder.max_cover("RM", mat))
                out.append((f"cover {kind} {name} {d} with RM", both))
        return out

    def line_search(self, ev: Evaluation, values, round_no: int, n_passes: int, first_pass: int = 0,
                    phase: str = "improve"):
        """Coordinate line search: per parameter a grid of values around the current one,
        all simulated at once (parallel); the cheapest feasible and confirmed one is kept.
        The grid shrinks by half every pass."""
        if not ev.feasible:
            return ev, values
        self.unfixable_floor = self.service_floors(ev)
        if self.confirm_seeds is not None:
            self.confirm_floor = {}
            self.confirm_failing, self.confirm_floor = self.confirmation(ev.schedule)
        for pass_no in range(first_pass, first_pass + n_passes):
            scale = 1 / 2 ** pass_no
            accepted, cost_before = 0, ev.cost
            for key in self.coordinates():
                moves = self.line_candidates(values, key, scale, coarse=(phase == "race"))
                if not moves:
                    continue
                evaluated = self.evaluate_many([self.schedule_of(v) for _, v in moves])
                order = sorted((e.cost, j) for j, e in enumerate(evaluated)
                               if e.feasible and e.cost < ev.cost - 1e-6)
                found = False
                for _, j in order[:2]:
                    if self.confirmed(evaluated[j].schedule):
                        ev, values = evaluated[j], moves[j][1]
                        accepted += 1
                        found = True
                        self.record(round_no, phase, moves[j][0], f"pass {pass_no}", ev)
                        break
                if not found and pass_no <= first_pass + 1:
                    # a big saving that breaks a few cells (a price-break lot, one supplier order
                    # less of an expensive material) gets a short repair before it is judged:
                    # a large-neighbourhood move (cf. the restructure step of the old search)
                    limit = ev.cost * (1 - self.settings.trial_repair_saving)
                    promising = sorted((e.cost, j) for j, e in enumerate(evaluated)
                                       if not e.feasible and e.cost < limit)
                    for _, j in promising[:1]:
                        trial = self.trial_with_repair(moves[j][1], ev, round_no)
                        if trial is not None:
                            ev, values = trial
                            accepted += 1
                            self.record(round_no, phase, moves[j][0] + " (repaired)", f"pass {pass_no}", ev)
                            break
            self.say(f"    {phase} pass {pass_no}: {accepted:3d} moves accepted, "
                     f"mean cost {cost_before:,.0f} -> {ev.cost:,.0f}")
            if pass_no >= first_pass + 1 and cost_before - ev.cost < self.settings.min_pass_gain * cost_before:
                break                              # converged: the last pass gained (almost) nothing
        self.unfixable_floor, self.confirm_floor = {}, {}
        return ev, values

    def trial_with_repair(self, values, current: Evaluation, round_no: int):
        """Repair a structural move (few steps, no new unfixable cells) and keep it only if it
        is then feasible, cheaper and confirmed on the confirmation seeds. Returns
        (evaluation, values) or None; on None the search state is unchanged."""
        saved = (set(self.unfixable), self.offsets.copy(), dict(self.unfixable_floor), set(self.deferred))
        ev, new_values = self.repair(values, round_no, max_steps=self.settings.trial_repair_steps, mode="global",
                                     give_up_above=len(self.unfixable), cost_limit=current.cost)
        if (ev.feasible and len(self.unfixable) == len(saved[0]) and ev.cost < current.cost - 1e-6
                and self.no_new_weak_cells(current, ev)):
            self.confirm_failing, _ = self.confirmation(ev.schedule)
            return ev, new_values
        self.unfixable, self.offsets, self.unfixable_floor, self.deferred = saved
        return None

    def holdout_repair(self, ev: Evaluation, values, weak: pd.DataFrame, round_no: int):
        """Cells the hold-out check found weak are repaired on the hold-out futures themselves:
        a cell can pass every search future and still be weak on the larger hold-out set (its
        risk is too rare to show in a few hundred futures); then no repair on the search seeds
        can see progress. Per weak cell (earliest week first) every possible fix is simulated
        on the hold-out seeds; the one with the largest gain in that cell's share (up to what it
        still needs) per unit of extra cost is kept, until the share clears alpha by Z standard
        errors (at most holdout_repair_steps)."""
        if weak.empty:
            return ev, values
        # like the search rule: the share must clear alpha by Z standard errors (n = hold-out seeds)
        n = self.settings.n_holdout_seeds
        alpha = self.model.target_share_of_futures
        p_hat = min(alpha, 1 - 1 / (n + 2))
        alpha = min(alpha + self.settings.z * np.sqrt(p_hat * (1 - p_hat) / n), 1 - 0.5 / n)
        n_search = self.settings.n_search_seeds
        saturated = {(p, c, int(w)): share >= 1 - 1.5 / n_search
                     for p, c, w, share in zip(ev.cells["product"], ev.cells["channel"], ev.cells["week"],
                                               ev.cells["share_met"])}
        for _, row in weak.sort_values("week").iterrows():
            key = (row["product"], row["channel"], int(row["week"]))
            if not saturated.get(key, False):
                continue          # the search seeds see the risk too: the margin and the normal repair do it
            for step in range(self.settings.holdout_repair_steps):
                base = simulate(self.model, ev.schedule, self.holdout_seeds, report_details=False)
                share = self._share(base, key)
                if share >= alpha:
                    break
                frac = min(self.settings.step_fraction * 2 ** step, 2 * self.settings.step_fraction)
                candidates = (self.repair_candidates(values, ev, key[0], key[2], self.settings.repair_dz, frac, "local")
                              + self.repair_candidates(values, ev, key[0], key[2], self.settings.repair_dz, frac,
                                                       "global"))
                best, best_score = None, -np.inf
                for label, v, off in candidates:
                    sched = self.schedule_of(v, off)
                    trial = simulate(self.model, sched, self.holdout_seeds, report_details=False)
                    gain = min(self._share(trial, key), alpha) - share        # only what the cell still needs
                    if gain <= 0:
                        continue
                    score = gain / max(trial.mean_total_cost() - base.mean_total_cost(), 1.0)
                    if score > best_score:
                        best, best_score = (label, v, off, sched), score
                if best is None:
                    break
                label, v, off, sched = best
                values, self.offsets = dict(v), off.copy()
                ev = self.evaluate(sched)
                self.record(round_no, "hold-out repair", f"fix {key[1]} week {key[2]}", label, ev)
        return ev, values

    def _share(self, result, key) -> float:
        cells = service.cell_table(self.model, result)
        row = cells[(cells["product"] == key[0]) & (cells["channel"] == key[1]) & (cells["week"] == key[2])]
        share = float(row["share_met"].iloc[0])
        return 1.0 if np.isnan(share) else share

    def trim_offsets(self, ev: Evaluation, values, round_no: int):
        """Try to drop the local offsets item by item (cheaper if still feasible)."""
        for kind, store in (("DC", self.offsets.dc_s), ("RM", self.offsets.rm_s)):
            for name in list(store):
                if not store[name].any():
                    continue
                trial = self.offsets.copy()
                if kind == "DC":
                    trial.dc_s[name][:] = 0
                    trial.dc_S[name][:] = 0
                else:
                    trial.rm_s[name][:] = 0
                    trial.rm_S[name][:] = 0
                cand = self.evaluate(self.schedule_of(values, trial))
                if cand.feasible and cand.cost < ev.cost - 1e-6 and self.confirmed(cand.schedule):
                    self.offsets, ev = trial, cand
                    self.record(round_no, "trim", f"dropped offsets {kind} {name}", "", ev)
        return ev

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def starts(self) -> list[tuple[str, dict]]:
        """Start parameters (multi-start): the classic start (z at the strictest fill-rate
        target, one week of cover - the simple heuristic), the economic-lot start (EOQ cover
        per item, bounded by shelf life) and, per product, the economic-lot start with the
        minimum lot at each price break above its typical lot (all-units discounts: the
        local search cannot cross the dearer band in between). A warm start (the last
        review's parameters) replaces them all."""
        if self.start_values is not None:
            return [("warm start (parameters of the last review)", dict(self.start_values))]
        b = self.builder
        out = [("classic start", b.start_values("classic")), ("economic-lot start", b.start_values("economic"))]
        econ = out[1][1]
        for p in self.model.products:
            typical = b.typical_lot(p.name, econ)
            for lot in b.price_breaks(p.name)[1:]:
                if lot > typical:
                    v = dict(econ)
                    v[("DC", p.name, "min_lot")] = lot
                    out.append((f"price-break start {p.name} lot {lot:.0f}", v))
        return out[:max(1, self.settings.max_starts)]

    def run(self) -> OptimisationOutcome:
        with self.workers():
            return self._run()

    def _run(self) -> OptimisationOutcome:
        st = self.settings
        best = None
        for label, values in self.starts():
            self.offsets = zero_offsets(self.model)
            self.unfixable, self.deferred, self.unfixable_floor = set(), set(), {}
            ev = self.evaluate(self.schedule_of(values))
            self.record(0, "start", label, "", ev)
            self.say(f"  {label}: mean cost {ev.cost:,.0f}, failing cells {ev.n_failing}")
            ev, values = self.repair(values, 0, mode="global",
                                            give_up_above=len(best[3]) + 2 if best else np.inf)
            ev, values = self.line_search(ev, values, 0, n_passes=1, phase="race")
            self.say(f"    repaired + raced: feasible={ev.feasible}, mean cost {ev.cost:,.0f}, "
                     f"cells left to the local repair {len(self.deferred)}")
            # feasible first, then fewer cells left out, smaller service gap, lower cost
            key = (not ev.feasible, len(self.unfixable), round(self.service_gap(ev), 2), ev.cost)
            if best is None or key < best[0]:
                best = (key, ev, values, set(self.unfixable), set(self.deferred), label, dict(self.unfixable_floor))
        _, ev, values, self.unfixable, self.deferred, label, self.unfixable_floor = best
        self.offsets = zero_offsets(self.model)
        self.say(f"  chosen start: {label}")
        holdout_rounds, hold_cells = [], None
        for round_no in range(1, st.max_outer_rounds + 1):
            if round_no == 1:
                # phase A: the parameters (global repair, line search)
                ev, values = self.repair(values, round_no, mode="global")
                ev, values = self.line_search(ev, values, round_no, n_passes=st.max_improve_passes + 1,
                                              first_pass=1)
                # phase B: week-specific exceptions (local repair of the deferred cells, polish)
                self.unfixable -= self.deferred
                self.deferred = set()
                ev, values = self.repair(values, round_no, mode="local")
                ev = self.trim_offsets(ev, values, round_no)
                polished = self.improve(ev, round_no, n_passes=1, fraction=st.step_fraction / 2,
                                        phase="polish", first_level=st.polish_first_level)
                self.offsets = difference(polished.schedule, self.builder.build(values))
                ev = polished
            else:
                # after a hold-out check the repair is a small local correction (margins of ~0.5 pp)
                ev, values = self.repair(values, round_no, mode="local", fraction=st.step_fraction / 4)
                ev, values = self.line_search(ev, values, round_no, n_passes=1, first_pass=3)
            hold_result = simulate(self.model, ev.schedule, self.holdout_seeds, report_details=False)
            hold_cells = service.cell_table(self.model, hold_result)
            weak = service.holdout_check(hold_cells, self.margins, st.min_margin_bump)
            weak.insert(0, "round", round_no)
            holdout_rounds.append(weak)
            self.say(f"    hold-out check: {len(weak)} weak cells")
            if weak.empty:
                break
            ev, values = self.holdout_repair(ev, values, weak, round_no)
            if round_no == st.max_outer_rounds:
                ev, values = self.repair(values, round_no + 1, mode="local", fraction=st.step_fraction / 4)
                ev, values = self.line_search(ev, values, round_no + 1, n_passes=1, first_pass=3)
        # the reported baseline is the simple heuristic (s,S): the classic quantile start
        start = initial_schedule(self.model, st)
        return OptimisationOutcome(start, ev.schedule, self.margins, self.unfixable, self.log,
                                   holdout_rounds, ev, hold_cells, self.n_evaluations, label, dict(values))
