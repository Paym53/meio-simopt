"""
Fill-rate measurement and feasibility rules (spec v5, Section 11).

A CELL is one (product, channel, week) with the week inside the evaluation
window (weeks L_max + 1 ... H, L_max = largest possible DC lead time).

Per seed:      fill = 1 - lost / demand          (only seeds with demand > 0)
Per cell:      mean_fill = average of fill over those seeds
               SE        = standard deviation of fill / sqrt(number of those seeds)
               A cell without demand in any seed passes automatically.

Three rules, used on three different seed sets:

1. SEARCH rule (search seeds) - decides whether a schedule is accepted:
       mean_fill - Z * SE - cell_margin >= F        for every cell
2. HOLD-OUT rule (hold-out seeds) - finds cells the search over-fitted:
       a cell is WEAK if mean_fill < F
       its margin is raised:  cell_margin += max(min_margin_bump, F - mean_fill)
3. FINAL verdict (untouched test seeds) - the honest result that is reported:
       mean_fill >= F                                for every cell
   No Z and no margin here. The margin is a search-time discipline only.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from .config import ModelInput

CellKey = tuple[str, str, int]      # (product, channel, week)


def cell_table(model: ModelInput, result) -> pd.DataFrame:
    """Mean fill, standard error and number of seeds with demand for every cell."""
    weeks = model.evaluation_weeks
    rows = []
    for p in model.products:
        for c in p.channels:
            fill = result.fill[(p.name, c.name)][:, weeks]         # (n_seeds, n_weeks)
            n_with_demand = (~np.isnan(fill)).sum(axis=0)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)   # all-NaN columns
                mean = np.nanmean(fill, axis=0)
                sd = np.nanstd(fill, axis=0, ddof=1)
            se = np.where(n_with_demand > 1, sd / np.sqrt(np.maximum(n_with_demand, 1)), 0.0)
            for j, week in enumerate(weeks):
                rows.append({"product": p.name, "channel": c.name, "week": week,
                             "target_F": c.target_fill_rate, "n_seeds_with_demand": int(n_with_demand[j]),
                             "mean_fill": float(mean[j]) if n_with_demand[j] > 0 else np.nan,
                             "se": float(se[j])})
    return pd.DataFrame(rows)


def apply_search_rule(cells: pd.DataFrame, z: float, margins: dict[CellKey, float]) -> pd.DataFrame:
    """Rule 1: feasible if mean_fill - Z*SE - cell_margin >= F (cells without demand pass)."""
    out = cells.copy()
    out["cell_margin"] = [margins.get((p, c, w), 0.0)
                          for p, c, w in zip(out["product"], out["channel"], out["week"])]
    out["search_lower_bound"] = out["mean_fill"] - z * out["se"] - out["cell_margin"]
    no_demand = out["n_seeds_with_demand"] == 0
    out["search_feasible"] = no_demand | (out["search_lower_bound"] >= out["target_F"] - 1e-12)
    return out


def holdout_check(cells: pd.DataFrame, margins: dict[CellKey, float],
                  min_margin_bump: float) -> pd.DataFrame:
    """Rule 2: find weak cells on the hold-out seeds and raise their margins (in place).
    Returns the table of weak cells with the applied bump."""
    weak = cells[(cells["n_seeds_with_demand"] > 0) & (cells["mean_fill"] < cells["target_F"])].copy()
    bumps = []
    for _, row in weak.iterrows():
        key = (row["product"], row["channel"], int(row["week"]))
        bump = max(min_margin_bump, row["target_F"] - row["mean_fill"])
        margins[key] = margins.get(key, 0.0) + bump
        bumps.append(bump)
    weak["margin_bump"] = bumps
    weak["new_cell_margin"] = [margins[(p, c, int(w))] for p, c, w in
                               zip(weak["product"], weak["channel"], weak["week"])]
    return weak


def final_verdict(cells: pd.DataFrame) -> pd.DataFrame:
    """Rule 3: honest verdict on the test seeds: mean_fill >= F, no Z, no margin."""
    out = cells.copy()
    out["test_pass"] = (out["n_seeds_with_demand"] == 0) | (out["mean_fill"] >= out["target_F"])
    return out
