"""summary.json version 2: bands, service cells, baseline, meta, settings.
The version-1 keys are checked against the committed example (examples/example_summary.json)."""
import json
import os
import sys
from dataclasses import asdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from main import evaluate_baseline
from meio import service, tables
from meio.config import SearchSettings, build_example_input
from meio.io_json import build_summary, snake
from meio.policy import initial_schedule
from meio.scenarios import build_scenarios
from meio.simulation import simulate

MODEL = build_example_input()
SCHEDULE = initial_schedule(MODEL, SearchSettings(n_quantile_samples=1000))
SEEDS = build_scenarios(MODEL, 200, 7, "test")
RESULT = simulate(MODEL, SCHEDULE, SEEDS)
BANDS = tables.weekly_band_series(MODEL, RESULT)


def _weekly_key(band):
    """Name of the matching column in weekly_means_test_seeds."""
    if band["location"] == "channel":
        return snake(f"{band['item']} {band['channel']} {band['metric']}")
    if band["location"] == "cost":
        return f"cost_{band['metric']}"
    return snake(f"{band['location']} {band['item']} {band['metric']}")


def test_every_weekly_series_has_a_band_whose_mean_is_the_weekly_mean():
    weekly = tables.weekly_means_table(MODEL, RESULT).rename(columns=snake)
    by_key = {_weekly_key(b): b for b in BANDS if b["metric"] != "avg_remaining_life_end"}
    assert set(by_key) == set(weekly.columns) - {"week"}
    for key, band in by_key.items():
        assert np.allclose(band["mean"], weekly[key].to_numpy(), atol=1e-3), key


def test_bands_are_the_percentiles_over_the_seeds_and_ordered():
    band = next(b for b in BANDS if b["location"] == "DC" and b["metric"] == "on_hand_end")
    values = RESULT.dc["FG1"]["on_hand_end"][:, 1:]
    for q in (5, 25, 50, 75, 95):
        assert np.allclose(band[f"p{q}"], np.round(np.percentile(values, q, axis=0), 3))
    for b in BANDS:
        stacked = np.vstack([b["p5"], b["p25"], b["p50"], b["p75"], b["p95"]])
        ok = np.isnan(stacked).any(axis=0) | (np.diff(stacked, axis=0) >= -1e-9).all(axis=0)
        assert ok.all() and len(b["mean"]) == MODEL.horizon, (b["location"], b["item"], b["metric"])


def test_remaining_life_bands_exist_for_every_item():
    items = {(b["location"], b["item"]) for b in BANDS if b["metric"] == "avg_remaining_life_end"}
    assert items == {("DC", p.name) for p in MODEL.products} | {("RMW", m.name) for m in MODEL.materials}


def test_baseline_is_the_start_schedule_on_the_same_seeds():
    base = evaluate_baseline(MODEL, SCHEDULE, SEEDS)
    assert abs(base["mean_cost_over_horizon_test_seeds"] - RESULT.mean_total_cost()) < 1e-6
    assert np.array_equal(base["policy"]["dc"]["FG1"]["S"], SCHEDULE.dc_S["FG1"][1:])
    total = next(c for c in base["costs"] if c["cost_component"] == "TOTAL")
    assert abs(total["mean"] - RESULT.mean_total_cost()) < 1e-6
    assert {row["channel"] for row in base["service_by_channel"]} == {c.name for c in MODEL.products[0].channels}


def _summary(**v2):
    cells = service.as_test_columns(service.final_verdict(service.cell_table(MODEL, RESULT)))
    empty = pd.DataFrame({"a": [1]})
    return build_summary(MODEL, {"policy": "x"}, pd.DataFrame(columns=["Decision"]), cells, empty, empty,
                         SCHEDULE, tables.weekly_means_table(MODEL, RESULT), **v2)


def test_version_2_keys_are_added_without_changing_version_1():
    v1 = _summary()
    v2 = _summary(weekly_bands=BANDS, baseline={"x": 1}, settings=asdict(SearchSettings()))
    assert set(v2) - set(v1) == {"meta", "weekly_bands", "baseline", "settings"}
    assert set(v2["service"]) - set(v1["service"]) == {"cells"}
    for key in v1:
        if key != "service":
            assert v1[key] == v2[key], key
    cell = v2["service"]["cells"][0]
    assert set(cell) == {"product", "channel", "week", "target_fill_rate", "mean_fill", "se",
                         "seeds_with_demand", "pass",
                         # chance constraint (Oct 2026): F met in at least alpha of the futures
                         "target_share_of_futures", "futures_meeting_target", "share_of_futures_meeting_target"}
    assert cell["pass"] == (cell["share_of_futures_meeting_target"] >= cell["target_share_of_futures"])
    assert v2["meta"]["evaluation_weeks"] == {"first": MODEL.evaluation_weeks[0], "last": MODEL.horizon}
    json.dumps(v2, allow_nan=False)                         # strict JSON: NaN became null


def test_example_summary_file_has_all_version_2_keys():
    with open(os.path.join(ROOT, "examples", "example_summary.json"), encoding="utf-8") as fh:
        example = json.load(fh)
    assert {"meta", "weekly_bands", "baseline", "settings"} <= set(example)
    assert "cells" in example["service"] and example["meta"]["summary_version"] == 2
