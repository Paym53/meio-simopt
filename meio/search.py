"""
Simulation-optimisation of the (s, S) schedule (spec v5, Section 12.3).

The search is a simple, transparent baseline:

    start from the quantile-based schedule
    repeat (outer round):
        A. REPAIR  : while a cell fails the search rule, raise the levels that feed
                     the earliest failing week (DC levels, or RMW levels if the
                     releases in those weeks were cut by missing raw material)
        B. IMPROVE : go through all (location, item, week) blocks and try small moves
                     that lower the mean cost; keep a move only if the schedule stays
                     feasible on the search seeds
        C. HOLD-OUT: simulate the hold-out seeds, raise the margin of weak cells
    until the hold-out check finds no weak cell (or max rounds)

All candidates are compared on the same search seeds (common random numbers).
Improvements of the algorithm itself are planned for later.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import service
from .config import ModelInput, SearchSettings
from .policy import PolicySchedule, dc_order_weeks, feeding_release_weeks, initial_schedule, rm_order_weeks
from .scenarios import ScenarioSet
from .simulation import SimResult, simulate


@dataclass
class Evaluation:
    """One schedule evaluated on the search seeds."""
    schedule: PolicySchedule
    result: SimResult
    cells: pd.DataFrame
    cost: float
    feasible: bool
    n_failing: int


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


class Searcher:
    def __init__(self, model: ModelInput, settings: SearchSettings,
                 search_seeds: ScenarioSet, holdout_seeds: ScenarioSet, verbose: bool = True,
                 start_schedule: PolicySchedule | None = None):
        self.model = model
        self.settings = settings
        self.search_seeds = search_seeds
        self.holdout_seeds = holdout_seeds
        self.verbose = verbose
        self.margins: dict = {}
        self.unfixable: set = set()
        self.log: list[dict] = []
        self.n_evaluations = 0
        self.rng = np.random.default_rng(settings.base_seed + 7)
        self.start_schedule = start_schedule      # warm start (e.g. last week's result), else quantile start

    # ------------------------------------------------------------------
    def say(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    def record(self, round_no, phase, action, detail, ev: Evaluation) -> None:
        self.log.append({"round": round_no, "phase": phase, "evaluation_no": self.n_evaluations,
                         "action": action, "detail": detail, "mean_cost": round(ev.cost, 2),
                         "feasible": ev.feasible, "failing_cells": ev.n_failing})

    def evaluate(self, schedule: PolicySchedule) -> Evaluation:
        """Simulate the search seeds and apply the search rule (unfixable cells excluded)."""
        self.n_evaluations += 1
        result = simulate(self.model, schedule, self.search_seeds)
        cells = service.apply_search_rule(service.cell_table(self.model, result),
                                          self.settings.z, self.margins)
        keys = list(zip(cells["product"], cells["channel"], cells["week"]))
        counted = np.array([k not in self.unfixable for k in keys])
        failing = int((~cells["search_feasible"].to_numpy() & counted).sum())
        return Evaluation(schedule, result, cells, result.mean_total_cost(), failing == 0, failing)

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

    # ------------------------------------------------------------------
    # A. Repair
    # ------------------------------------------------------------------
    def raise_levels_for_cell(self, schedule: PolicySchedule, result: SimResult,
                              product_name: str, week: int) -> tuple[bool, str]:
        """Raise the levels that feed `week`. Changes schedule in place.
        Returns (changed, description).

        Diagnosis on the releases that can arrive in `week` (release weeks
        week - L_max ... week - L_min):
          1. releases cut because a raw material was the binding limit
             -> raise the RMW levels of that material in the weeks whose orders
                can arrive before those releases
          2. releases cut because production capacity was the binding limit
             -> pre-build: raise the DC levels in the weeks just before
          3. otherwise -> raise the DC levels in the feeding release weeks
        """
        model, st = self.model, self.settings
        p = next(x for x in model.products if x.name == product_name)
        allowed_dc = set(dc_order_weeks(model, p))
        release_weeks = feeding_release_weeks(model, p, week)
        if not release_weeks:
            return False, "no release week can still reach this week"

        # 1. raw material binding?
        short_materials = [mat for mat in p.bom
                           if result.rm[mat]["limited_release"][:, release_weeks].mean(axis=0).max()
                           > st.cut_share_threshold]
        if short_materials:
            changed_weeks = {}
            for mat_name in short_materials:
                m = model.material(mat_name)
                allowed = set(rm_order_weeks(model, m))
                weeks = sorted({o for r in release_weeks
                                for o in range(r - m.lead_time_max, r - m.lead_time_min + 1) if o in allowed})
                s, S, batch = self.levels(schedule, "RM", mat_name)
                for o in weeks:
                    step = self.step_size(S[o], batch, st.step_fraction)
                    s[o] += step
                    S[o] += step
                if weeks:
                    changed_weeks[mat_name] = weeks
            if changed_weeks:
                for mat_name in changed_weeks:           # also raise the physical minimum
                    m = model.material(mat_name)
                    schedule.rm_floor[mat_name] += self.step_size(max(schedule.rm_floor[mat_name], m.batch_size),
                                                                  m.batch_size, st.step_fraction)
                text = "; ".join(f"{k} weeks {v[0]}-{v[-1]}" for k, v in changed_weeks.items())
                return True, f"RM was binding -> raised RMW (s,S) and minimum: {text}"
            return False, f"releases cut by {short_materials}, but no RM order can arrive in time"

        # 1b. orders limited by the policy's own cap? -> raise the cap
        cap_share = result.dc[p.name]["cut_by_policy_cap"][:, release_weeks].mean(axis=0).max()
        if cap_share > st.cut_share_threshold:
            schedule.dc_cap[p.name] += self.step_size(schedule.dc_cap[p.name], p.batch_size, st.step_fraction)
            return True, f"order cap was binding -> raised DC cap of {p.name} to {schedule.dc_cap[p.name]}"

        # 2. capacity binding? -> produce earlier
        capacity_share = result.dc[p.name]["cut_by_capacity"][:, release_weeks].mean(axis=0).max()
        if capacity_share > st.cut_share_threshold:
            first = release_weeks[0]
            span = p.lead_time_max - p.lead_time_min + 1
            weeks = [r for r in range(first - span, first) if r in allowed_dc]
            if not weeks:
                return False, "capacity binding and no earlier week left for pre-building"
            target_weeks, text = weeks, "capacity was binding -> pre-build: raised DC (s,S)"
        else:
            # 3. plain shortage of FG ordering
            target_weeks, text = release_weeks, "raised DC (s,S)"

        s, S, batch = self.levels(schedule, "DC", p.name)
        for r in target_weeks:
            step = self.step_size(S[r], batch, st.step_fraction)
            s[r] += step
            S[r] += step
        return True, f"{text} of {p.name}: weeks {target_weeks[0]}-{target_weeks[-1]}"

    def repair(self, schedule: PolicySchedule, round_no: int) -> Evaluation:
        """Raise levels until every cell passes the search rule. A cell that shows no
        progress for `repair_patience` steps is declared unfixable; the level increases
        made for it since its last progress are undone, so they do not inflate the schedule."""
        st = self.settings
        ev = self.evaluate(schedule)
        progress: dict = {}             # cell -> (best lower bound, steps without progress)
        checkpoint: dict = {}           # cell -> schedule at its last progress
        for _ in range(st.max_repair_steps):
            cells = ev.cells
            keys = list(zip(cells["product"], cells["channel"], cells["week"]))
            open_mask = ~cells["search_feasible"].to_numpy() & np.array([k not in self.unfixable for k in keys])
            if not open_mask.any():
                break
            failing = cells[open_mask].sort_values(["week", "search_lower_bound"])
            cell = failing.iloc[0]
            key = (cell["product"], cell["channel"], int(cell["week"]))

            best, stuck = progress.get(key, (-np.inf, 0))
            if cell["search_lower_bound"] > best + 1e-4:
                progress[key] = (cell["search_lower_bound"], 0)
                checkpoint[key] = schedule.copy()
            else:
                progress[key] = (best, stuck + 1)
            if progress[key][1] >= st.repair_patience:
                self.unfixable.add(key)
                schedule = checkpoint[key].copy()          # undo the raises that did not help
                ev = self.evaluate(schedule)
                self.record(round_no, "repair", "cell declared unfixable",
                            f"{key}: no progress in {st.repair_patience} steps, raises undone", ev)
                continue

            changed, text = self.raise_levels_for_cell(schedule, ev.result, key[0], key[2])
            if not changed:
                self.unfixable.add(key)
                ev = self.evaluate(schedule)
                self.record(round_no, "repair", "cell declared unfixable", f"{key}: {text}", ev)
                continue
            ev = self.evaluate(schedule)
            self.record(round_no, "repair", f"fix {key[1]} week {key[2]}", text, ev)
        return ev

    # ------------------------------------------------------------------
    # B. Improve
    # ------------------------------------------------------------------
    def improve(self, current: Evaluation, round_no: int) -> Evaluation:
        st, model = self.settings, self.model
        if not current.feasible:
            self.say("    improve skipped: schedule is not feasible on the search seeds")
            return current
        blocks = [("DC", p.name, t) for p in model.products for t in dc_order_weeks(model, p)]
        blocks += [("RM", m.name, t) for m in model.materials for t in rm_order_weeks(model, m)]
        blocks += [("DC cap", p.name, 0) for p in model.products]
        blocks += [("RM floor", m.name, 0) for m in model.materials]
        level_moves = ["lower s and S", "lower S (smaller orders)", "raise S (larger orders)"]
        fraction = st.step_fraction

        for pass_no in range(1, st.max_improve_passes + 1):
            accepted = 0
            cost_before = current.cost
            for i in self.rng.permutation(len(blocks)):
                kind, name, t = blocks[i]
                moves = level_moves if kind in ("DC", "RM") else ["lower", "raise"]
                for move in moves:
                    candidate = self.moved(current.schedule, kind, name, t, move, fraction)
                    if candidate is None:
                        continue
                    ev = self.evaluate(candidate)
                    if ev.feasible and ev.cost < current.cost - 1e-6:
                        current = ev
                        accepted += 1
                        where = f"week {t}" if kind in ("DC", "RM") else "(whole horizon)"
                        self.record(round_no, "improve", f"{move}: {kind} {name} {where}",
                                    f"pass {pass_no}, step fraction {fraction:.3f}", ev)
                        break
            self.say(f"    improve pass {pass_no}: {accepted:3d} moves accepted, "
                     f"mean cost {cost_before:,.0f} -> {current.cost:,.0f} (step fraction {fraction:.3f})")
            if accepted == 0:
                fraction /= 2
                if fraction < st.min_step_fraction:
                    break
        return current

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> OptimisationOutcome:
        st = self.settings
        if self.start_schedule is not None:
            start, label = self.start_schedule.copy(), "warm start (previous review, shifted)"
        else:
            start, label = initial_schedule(self.model, st), "quantile-based start schedule"
        schedule = start.copy()
        ev = self.evaluate(schedule)
        self.record(0, "start", label, "", ev)
        self.say(f"  start schedule: mean cost {ev.cost:,.0f}, failing cells {ev.n_failing}")

        holdout_rounds, hold_cells = [], None
        for round_no in range(1, st.max_outer_rounds + 1):
            self.say(f"  round {round_no}")
            ev = self.repair(schedule, round_no)
            schedule = ev.schedule
            self.say(f"    repair: feasible={ev.feasible}, mean cost {ev.cost:,.0f}, "
                     f"unfixable cells so far {len(self.unfixable)}")
            ev = self.improve(ev, round_no)
            schedule = ev.schedule

            hold_result = simulate(self.model, schedule, self.holdout_seeds)
            hold_cells = service.cell_table(self.model, hold_result)
            weak = service.holdout_check(hold_cells, self.margins, st.min_margin_bump)
            weak.insert(0, "round", round_no)
            holdout_rounds.append(weak)
            self.record(round_no, "hold-out", "hold-out check",
                        f"{len(weak)} weak cells, margins raised", ev)
            self.say(f"    hold-out check: {len(weak)} weak cells (hold-out mean fill < F)")
            if weak.empty:
                break
            if round_no == st.max_outer_rounds:
                self.say("    max rounds reached - margins were raised but not searched again")

        return OptimisationOutcome(start, schedule, self.margins, self.unfixable, self.log,
                                   holdout_rounds, ev, hold_cells, self.n_evaluations)
