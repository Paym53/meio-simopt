"""
Fill-rate measurement and feasibility rules (spec v5, Section 11; chance constraint since Oct 2026).

A CELL is one (product, channel, week) with the week inside the evaluation
window (weeks L_max + 1 ... H, L_max = largest possible DC lead time).

Per seed (one possible future):
    fill = 1 - lost / demand                 (only seeds with demand > 0 in that cell)
    the future MEETS the target if fill >= F (F = the channel's target fill rate)
Per cell:
    share_met = futures that meet F / futures with demand        ("9,995 of 10,000 futures")
    share_se  = standard error of that share, sqrt(p (1 - p) / n) with p = (met + 1) / (n + 2),
                so a perfect 200 / 200 is not taken as certainty
    mean_fill = average fill over the futures (information only)
    A cell without demand in any seed passes automatically.

SERVICE TARGET (chance constraint): in every cell, the share of futures that meet F must be at
least alpha = model.target_share_of_futures (default 0.98).

Three rules, used on three different seed sets:

1. SEARCH rule (search seeds) - decides whether a schedule is accepted:
       share_met - Z * share_se - cell_margin >= alpha        for every cell
2. HOLD-OUT rule (hold-out seeds) - finds cells the search over-fitted:
       a cell is WEAK if share_met < alpha
       its margin is raised:  cell_margin += max(min_margin_bump, alpha - share_met)
3. FINAL verdict (untouched test seeds) - the honest result that is reported:
       share_met >= alpha                                     for every cell
   No Z and no margin here. The margin is a search-time discipline only.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from .config import ModelInput

CellKey = tuple[str, str, int]      # (product, channel, week)


def cell_table(model: ModelInput, result) -> pd.DataFrame:
    """Per cell: futures with demand, futures meeting F, their share and its standard error
    (the service target), plus the mean fill and its standard error (information)."""
    weeks = model.evaluation_weeks
    alpha = model.target_share_of_futures
    rows = []
    for p in model.products:
        for c in p.channels:
            fill = result.fill[(p.name, c.name)][:, weeks]         # (n_seeds, n_weeks)
            has_demand = ~np.isnan(fill)
            n_with_demand = has_demand.sum(axis=0)
            n_met = (has_demand & (np.nan_to_num(fill, nan=0.0) >= c.target_fill_rate - 1e-12)).sum(axis=0)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)   # all-NaN columns
                mean = np.nanmean(fill, axis=0)
                sd = np.nanstd(fill, axis=0, ddof=1)
            se = np.where(n_with_demand > 1, sd / np.sqrt(np.maximum(n_with_demand, 1)), 0.0)
            share = np.where(n_with_demand > 0, n_met / np.maximum(n_with_demand, 1), np.nan)
            smoothed = (n_met + 1) / (n_with_demand + 2)
            share_se = np.sqrt(smoothed * (1 - smoothed) / np.maximum(n_with_demand, 1))
            for j, week in enumerate(weeks):
                rows.append({"product": p.name, "channel": c.name, "week": week,
                             "target_F": c.target_fill_rate, "target_share": alpha,
                             "n_seeds_with_demand": int(n_with_demand[j]), "n_meeting_F": int(n_met[j]),
                             "share_met": float(share[j]), "share_se": float(share_se[j]),
                             "mean_fill": float(mean[j]) if n_with_demand[j] > 0 else np.nan,
                             "se": float(se[j])})
    return pd.DataFrame(rows)


def apply_search_rule(cells: pd.DataFrame, z: float, margins: dict[CellKey, float]) -> pd.DataFrame:
    """Rule 1: feasible if share_met - Z*share_se - cell_margin >= alpha (cells without demand pass)."""
    out = cells.copy()
    out["cell_margin"] = [margins.get((p, c, w), 0.0)
                          for p, c, w in zip(out["product"], out["channel"], out["week"])]
    out["search_lower_bound"] = out["share_met"] - z * out["share_se"] - out["cell_margin"]
    no_demand = out["n_seeds_with_demand"] == 0
    out["search_feasible"] = no_demand | (out["search_lower_bound"] >= out["target_share"] - 1e-12)
    return out


def holdout_check(cells: pd.DataFrame, margins: dict[CellKey, float],
                  min_margin_bump: float) -> pd.DataFrame:
    """Rule 2: find weak cells on the hold-out seeds and raise their margins (in place).
    Returns the table of weak cells with the applied bump."""
    weak = cells[(cells["n_seeds_with_demand"] > 0) & (cells["share_met"] < cells["target_share"])].copy()
    bumps = []
    for _, row in weak.iterrows():
        key = (row["product"], row["channel"], int(row["week"]))
        bump = max(min_margin_bump, row["target_share"] - row["share_met"])
        margins[key] = margins.get(key, 0.0) + bump
        bumps.append(bump)
    weak["margin_bump"] = bumps
    weak["new_cell_margin"] = [margins[(p, c, int(w))] for p, c, w in
                               zip(weak["product"], weak["channel"], weak["week"])]
    return weak


def final_verdict(cells: pd.DataFrame) -> pd.DataFrame:
    """Rule 3: honest verdict on the test seeds: share_met >= alpha, no Z, no margin."""
    out = cells.copy()
    out["test_pass"] = (out["n_seeds_with_demand"] == 0) | (out["share_met"] >= out["target_share"] - 1e-12)
    return out


def as_test_columns(cells: pd.DataFrame) -> pd.DataFrame:
    """The final-verdict table with the column names the reports use for the test seeds."""
    return cells.rename(columns={"mean_fill": "test_mean_fill", "se": "test_se", "share_met": "test_share_met",
                                 "n_meeting_F": "test_futures_meeting_F",
                                 "n_seeds_with_demand": "test_seeds_with_demand"})
