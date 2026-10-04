"""
Simulation-optimisation of the (s, S) schedule (spec v5, Section 12.3).

    MULTI-START: build several start schedules (policy.start_schedules): the classic
                 quantile start, an economic-lot start (EOQ cover per item, bounded by
                 shelf life) and price-break starts (lots at the all-units discount
                 breaks). Repair each and continue from the cheapest feasible one.
    repeat (outer round):
        A. REPAIR  : while a cell fails the search rule, raise the levels that feed
                     the earliest failing week (DC levels, or RMW levels if the
                     releases in those weeks were cut by missing raw material)
        B. IMPROVE : coarse-to-fine pattern search. Moves act on BLOCKS of order weeks:
                     first the whole horizon of an item (its safety level and lot size
                     as a whole), then halves, quarters ... down to single weeks. Move
                     types: lower s and S (less safety stock), lower / raise S (smaller /
                     larger lots), and a coordinated echelon move that changes the DC
                     lot together with the RMW levels that feed those releases. A move
                     is kept only if the mean cost falls, the schedule stays feasible on
                     the search seeds and - on an independent set of confirmation seeds -
                     no cell becomes weak (mean fill < F) that was not weak before (guards against the
                     optimizer's curse: among many candidates, some look feasible on the
                     search seeds by chance). An accepted move is repeated in the same
                     direction while it keeps paying off.
        C. HOLD-OUT: simulate the hold-out seeds, raise the margin of weak cells
    until the hold-out check finds no weak cell (or max rounds)

All candidates are compared on the same search seeds (common random numbers).
Moving whole blocks keeps the levels following the forecast (no erratic week-to-week
jumps that would make orders nervous), and searches the few directions that matter
economically first; single-week moves then fine-tune.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import service
from .config import ModelInput, SearchSettings
from .policy import PolicySchedule, dc_order_weeks, feeding_release_weeks, rm_order_weeks, start_schedules
from .scenarios import ScenarioSet, build_scenarios
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
    chosen_start: str = ""


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
        self.unfixable_floor: dict = {}     # unfixable cell -> search lower bound it must keep
        self.log: list[dict] = []
        self.n_evaluations = 0
        self.rng = np.random.default_rng(settings.base_seed + 7)
        self.start_schedule = start_schedule      # warm start (e.g. last week's result), else quantile start
        # confirmation seeds: an independent set that only accepted moves are simulated on
        n_confirm = int(round(settings.confirm_seed_factor * settings.n_search_seeds))
        self.confirm_seeds = (build_scenarios(model, n_confirm, settings.base_seed + 4, "confirm")
                              if n_confirm > 0 else None)
        self.confirm_failing: set = set()    # cells failing on the confirmation seeds (current schedule)
        self.confirm_floor: dict = {}        # unfixable cell -> mean fill on the confirmation seeds

    # ------------------------------------------------------------------
    def say(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    def record(self, round_no, phase, action, detail, ev: Evaluation) -> None:
        self.log.append({"round": round_no, "phase": phase, "evaluation_no": self.n_evaluations,
                         "action": action, "detail": detail, "mean_cost": round(ev.cost, 2),
                         "feasible": ev.feasible, "failing_cells": ev.n_failing})

    def evaluate(self, schedule: PolicySchedule) -> Evaluation:
        """Simulate the search seeds and apply the search rule. Cells declared unfixable do
        not count, except during an improve phase: there they must keep at least the service
        (search lower bound) they had when the phase started (self.unfixable_floor), so cost
        is never bought with less service on cells the search could not fix."""
        self.n_evaluations += 1
        result = simulate(self.model, schedule, self.search_seeds, report_details=False)
        cells = service.apply_search_rule(service.cell_table(self.model, result),
                                          self.settings.z, self.margins)
        keys = list(zip(cells["product"], cells["channel"], cells["week"]))
        bound = cells["search_lower_bound"].to_numpy()
        unfixable = np.array([k in self.unfixable for k in keys])
        kept = np.array([np.isnan(b) or b >= self.unfixable_floor.get(k, -np.inf) - 1e-9
                         for k, b in zip(keys, bound)])
        failing = int((~cells["search_feasible"].to_numpy() & ~unfixable).sum() + (unfixable & ~kept).sum())
        return Evaluation(schedule, result, cells, result.mean_total_cost(), failing == 0, failing)

    def confirmation(self, schedule: PolicySchedule) -> tuple[set, dict]:
        """Cells that are weak on the confirmation seeds - mean fill below target, the hold-out
        rule (the search seeds already carry Z x SE and the margins) - and the mean fill of the
        unfixable cells there (an unfixable cell counts as weak below its confirmation floor)."""
        self.n_evaluations += 1
        result = simulate(self.model, schedule, self.confirm_seeds, report_details=False)
        cells = service.cell_table(self.model, result)
        failing, means = set(), {}
        for p, c, w, mean, target in zip(cells["product"], cells["channel"], cells["week"],
                                         cells["mean_fill"], cells["target_F"]):
            key = (p, c, int(w))
            if np.isnan(mean):
                continue
            if key in self.unfixable:
                means[key] = float(mean)
                if mean < self.confirm_floor.get(key, -np.inf) - 1e-9:
                    failing.add(key)
            elif mean < target:
                failing.add(key)
        return failing, means

    def confirmed(self, schedule: PolicySchedule) -> bool:
        """Optimizer's-curse check of a move the search seeds accepted: on the independent
        confirmation seeds no cell may become weak (mean fill < F) that was not weak before
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
        """Search lower bound of every unfixable cell in `ev` (cells without demand: no floor)."""
        floors = {}
        for k, b in zip(zip(ev.cells["product"], ev.cells["channel"], ev.cells["week"]),
                        ev.cells["search_lower_bound"]):
            if k in self.unfixable and not np.isnan(b):
                floors[k] = float(b)
        return floors

    @staticmethod
    def service_gap(ev: Evaluation) -> float:
        """Total gap to target over all cells, sum of max(0, F - search lower bound):
        0 when every cell passes. Compares starts on service before cost."""
        cells = ev.cells
        return float((cells["target_F"] - cells["search_lower_bound"]).clip(lower=0).fillna(0).sum())

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

    def repair(self, schedule: PolicySchedule, round_no: int, give_up_above: float = np.inf) -> Evaluation:
        """Raise levels until every cell passes the search rule. A cell that shows no
        progress for `repair_patience` steps is declared unfixable; the level increases
        made for it since its last progress are undone, so they do not inflate the schedule.
        give_up_above: stop as soon as more cells than this are unfixable (multi-start: such
        a start can no longer beat the best one)."""
        st = self.settings
        ev = self.evaluate(schedule)
        progress: dict = {}             # cell -> (best lower bound, steps without progress)
        checkpoint: dict = {}           # cell -> schedule at its last progress
        for _ in range(st.max_repair_steps):
            if len(self.unfixable) > give_up_above:
                break
            cells = ev.cells
            keys = list(zip(cells["product"], cells["channel"], cells["week"]))
            open_mask = ~cells["search_feasible"].to_numpy() & np.array([k not in self.unfixable for k in keys])
            if not open_mask.any():
                break
            failing = cells[open_mask].sort_values(["week", "search_lower_bound"])
            cell = failing.iloc[0]
            key = (cell["product"], cell["channel"], int(cell["week"]))

            best, stuck = progress.get(key, (-np.inf, 0))
            if cell["search_lower_bound"] > best + st.repair_min_progress:
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
    def improve(self, current: Evaluation, round_no: int, n_levels: int | None = None,
                n_passes: int | None = None, fraction: float | None = None) -> Evaluation:
        """Coarse-to-fine pattern search (see the module docstring). One pass goes through
        all block levels, from whole-horizon blocks down to single weeks; at every block
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
            for level in range(n_levels):
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
                    for move in moves[kind]:
                        if productive is not None and (kind, level, move) not in productive:
                            continue
                        candidate = self.moved_block(current.schedule, kind, name, block, move, fraction)
                        if candidate is None:
                            continue
                        ev = self.evaluate(candidate)
                        if not (ev.feasible and ev.cost < current.cost - 1e-6 and self.confirmed(candidate)):
                            continue
                        hits.add((kind, level, move))
                        repeats = 0
                        while True:                  # accepted: keep going in this direction
                            current = ev
                            accepted += 1
                            where = (f"weeks {block[0]}-{block[-1]}" if kind in ("DC", "RM")
                                     else "(whole horizon)")
                            self.record(round_no, "improve", f"{move}: {kind} {name} {where}",
                                        f"pass {pass_no}, step fraction {fraction:.3f}", ev)
                            repeats += 1
                            if repeats > st.max_move_repeats:
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
    # Main loop
    # ------------------------------------------------------------------
    def choose_start(self) -> tuple[PolicySchedule, Evaluation, str]:
        """Multi-start: repair every start schedule, give each feasible one a coarse racing
        pass (whole-horizon and half-horizon blocks) and keep the best one: the smallest total
        service gap over all cells (sum of F - lower bound where below target; differences
        under 0.01 count as equal), then the lowest mean cost.
        The classic start is repaired first; the cells it cannot fix (structural: freshness
        or lead times) are known to the later starts, which therefore do not spend repair
        steps on them again."""
        st = self.settings
        if self.start_schedule is not None:
            starts = [("warm start (previous review, shifted)", self.start_schedule.copy())]
        else:
            starts = start_schedules(self.model, self.settings)
        best, known_unfixable = None, set()
        for label, schedule in starts:
            self.unfixable = set(known_unfixable)
            ev = self.evaluate(schedule.copy())
            self.record(0, "start", label, "", ev)
            self.say(f"  {label}: mean cost {ev.cost:,.0f}, failing cells {ev.n_failing}")
            if len(starts) > 1:
                ev = self.repair(ev.schedule, 0, give_up_above=len(best[2]) if best else np.inf)
                self.say(f"    repaired: feasible={ev.feasible}, mean cost {ev.cost:,.0f}, "
                         f"unfixable cells {len(self.unfixable)}")
                if st.race_levels > 0 and ev.feasible and (best is None or len(self.unfixable) <= len(best[2])):
                    # racing: one coarse pass (whole horizon and halves) before comparing,
                    # because the cost right after repair says little about where a start leads
                    ev = self.improve(ev, 0, n_levels=self.settings.race_levels, n_passes=1)
            key = (round(self.service_gap(ev), 2), ev.cost)
            if best is None or key < best[0]:
                best = (key, ev, set(self.unfixable), label)
            known_unfixable |= self.unfixable
        _, ev, self.unfixable, label = best
        if len(starts) > 1:
            self.say(f"  chosen start: {label}")
        return starts[0][1], ev, label

    def run(self) -> OptimisationOutcome:
        st = self.settings
        start, ev, label = self.choose_start()
        schedule = ev.schedule

        holdout_rounds, hold_cells = [], None
        for round_no in range(1, st.max_outer_rounds + 1):
            self.say(f"  round {round_no}")
            ev = self.repair(schedule, round_no)
            schedule = ev.schedule
            self.say(f"    repair: feasible={ev.feasible}, mean cost {ev.cost:,.0f}, "
                     f"unfixable cells so far {len(self.unfixable)}")
            if round_no == 1:
                ev = self.improve(ev, round_no)
            else:                                 # converged already: one finer pass after the repair
                ev = self.improve(ev, round_no, n_passes=1, fraction=st.step_fraction / 2)
            schedule = ev.schedule

            hold_result = simulate(self.model, schedule, self.holdout_seeds, report_details=False)
            hold_cells = service.cell_table(self.model, hold_result)
            weak = service.holdout_check(hold_cells, self.margins, st.min_margin_bump)
            weak.insert(0, "round", round_no)
            holdout_rounds.append(weak)
            self.record(round_no, "hold-out", "hold-out check",
                        f"{len(weak)} weak cells, margins raised", ev)
            self.say(f"    hold-out check: {len(weak)} weak cells (hold-out mean fill < F)")
            if weak.empty:
                break
            if round_no == st.max_outer_rounds:            # last round: make the raised margins count
                ev = self.repair(schedule, round_no + 1)
                ev = self.improve(ev, round_no + 1, n_passes=1, fraction=st.step_fraction / 2)
                schedule = ev.schedule
                self.say(f"    max rounds reached - final repair + improve with the raised margins: "
                         f"feasible={ev.feasible}, mean cost {ev.cost:,.0f}")

        return OptimisationOutcome(start, schedule, self.margins, self.unfixable, self.log,
                                   holdout_rounds, ev, hold_cells, self.n_evaluations, label)
