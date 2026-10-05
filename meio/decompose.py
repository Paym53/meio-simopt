"""
Independent groups of products (decomposition before the optimisation).

Products interact only through shared resources: a production site (its weekly capacity) or
a raw material (its stock at the RMW). Two products are in the same GROUP if they are
connected by such sharing, directly or through other products (connected components of the
product-resource graph). Groups do not influence each other at all, so each group is
optimised on its own - a smaller search space and a search that cannot trade one group's
cost against another's service - and the results are combined into one plan:

    groups  = independent_groups(model)              e.g. [["FG1", "FG2", "FG3"], ["FG4"]]
    for every group: sub_model -> Searcher(...).run()
    combine the schedules, margins, unfixable cells and logs (merge_outcomes)

With one group (e.g. a single product) the Searcher runs on the full model exactly as before.
Later multi-item constraints (e.g. a joint MOQ over several products) will be one more kind of
link in independent_groups: products bound by such a constraint must be in the same group.
"""
from __future__ import annotations

from dataclasses import replace

import pandas as pd

from .config import DemandForecast, InitialState, ModelInput, SearchSettings
from .policy import PolicySchedule
from .scenarios import ScenarioSet, build_scenarios
from .search import Evaluation, OptimisationOutcome, Searcher


def independent_groups(model: ModelInput) -> list[list[str]]:
    """Products grouped by shared production sites and raw materials (union-find).
    Groups are ordered by their first product in the input; products keep the input order."""
    parent = {p.name: p.name for p in model.products}

    def root(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    first_user: dict[str, str] = {}                    # resource -> first product that uses it
    for p in model.products:
        resources = [f"site:{model.site_of(p).name}"] + [f"material:{m}" for m in p.bom]
        for r in resources:
            if r in first_user:
                parent[root(p.name)] = root(first_user[r])
            else:
                first_user[r] = p.name
    groups: dict[str, list[str]] = {}
    for p in model.products:
        groups.setdefault(root(p.name), []).append(p.name)
    order = [p.name for p in model.products]
    return sorted(groups.values(), key=lambda g: order.index(g[0]))


def sub_model(model: ModelInput, product_names: list[str]) -> ModelInput:
    """The part of the model that belongs to some products: those products, their raw
    materials and sites, their forecasts and initial state. The evaluation window of the full
    model is kept, so every group is judged on the same weeks."""
    names = set(product_names)
    products = [p for p in model.products if p.name in names]
    used = {m for p in products for m in p.bom}
    materials = [m for m in model.materials if m.name in used]
    sites = [s for s in model.sites if any(model.site_of(p).name == s.name for p in products)]
    demand = DemandForecast(mean={k: v for k, v in model.demand.mean.items() if k[0] in names},
                            sd={k: v for k, v in model.demand.sd.items() if k[0] in names})
    s = model.initial_state
    state = InitialState(dc_stock={k: v for k, v in s.dc_stock.items() if k in names},
                         rm_stock={k: v for k, v in s.rm_stock.items() if k in used},
                         dc_pipeline={k: v for k, v in s.dc_pipeline.items() if k in names},
                         rm_pipeline={k: v for k, v in s.rm_pipeline.items() if k in used})
    return replace(model, products=products, materials=materials, demand=demand, initial_state=state,
                   sites=sites, evaluation_start=model.evaluation_weeks[0] if model.evaluation_weeks else 0)


def merge_outcomes(groups: list[list[str]], outcomes: list[OptimisationOutcome]) -> OptimisationOutcome:
    """One outcome for the whole model from the outcomes of independent groups."""
    def merged_schedule(schedules: list[PolicySchedule]) -> PolicySchedule:
        out = PolicySchedule({}, {}, {}, {}, {}, {})
        for sch in schedules:
            for field_name in ("dc_s", "dc_S", "rm_s", "rm_S", "dc_cap", "rm_floor"):
                getattr(out, field_name).update(getattr(sch, field_name))
        return out

    log = [dict(row, group=", ".join(g)) for g, o in zip(groups, outcomes) for row in o.search_log]
    holdout = [h.assign(group=", ".join(g)) for g, o in zip(groups, outcomes) for h in o.holdout_rounds]
    evals = [o.last_search_eval for o in outcomes]
    schedule = merged_schedule([o.schedule for o in outcomes])
    last = Evaluation(schedule, None, pd.concat([e.cells for e in evals], ignore_index=True),
                      sum(e.cost for e in evals), all(e.feasible for e in evals), sum(e.n_failing for e in evals))
    hold_cells = [o.last_holdout_cells for o in outcomes if o.last_holdout_cells is not None]
    return OptimisationOutcome(
        start_schedule=merged_schedule([o.start_schedule for o in outcomes]), schedule=schedule,
        margins={k: v for o in outcomes for k, v in o.margins.items()},
        unfixable=set().union(*(o.unfixable for o in outcomes)),
        search_log=log, holdout_rounds=holdout, last_search_eval=last,
        last_holdout_cells=pd.concat(hold_cells, ignore_index=True) if hold_cells else None,
        n_evaluations=sum(o.n_evaluations for o in outcomes),
        chosen_start="; ".join(f"{', '.join(g)}: {o.chosen_start}" for g, o in zip(groups, outcomes)))


def optimise(model: ModelInput, settings: SearchSettings, search_seeds: ScenarioSet,
             holdout_seeds: ScenarioSet, verbose: bool = True) -> tuple[OptimisationOutcome, list[list[str]]]:
    """Tune the policy group by group. Returns the combined outcome and the groups.
    One group: the Searcher runs on the full model with the given seeds (unchanged behaviour).
    Several groups: each group gets its own search and hold-out seeds (same seed numbers,
    drawn for the group's sub-model)."""
    groups = independent_groups(model)
    if len(groups) == 1:
        return Searcher(model, settings, search_seeds, holdout_seeds, verbose=verbose).run(), groups
    outcomes = []
    for k, names in enumerate(groups, start=1):
        if verbose:
            print(f"\nGroup {k}/{len(groups)}: {', '.join(names)}", flush=True)
        sub = sub_model(model, names)
        sub_search = build_scenarios(sub, settings.n_search_seeds, settings.base_seed + 1, "search")
        sub_holdout = build_scenarios(sub, settings.n_holdout_seeds, settings.base_seed + 2, "hold-out")
        outcomes.append(Searcher(sub, settings, sub_search, sub_holdout, verbose=verbose).run())
    return merge_outcomes(groups, outcomes), groups
