"""Tests of the web API (api.py). The simulation itself is replaced by a fast stand-in,
so these tests check the HTTP behaviour: input checks, run lifecycle, downloads, API key.
A full run through the API is checked by hand (see README, "Web API")."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import api
from meio.config import POLICY_NAME, build_example_input
from meio.io_json import model_to_dict

EXAMPLE = model_to_dict(build_example_input())


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Client with its own output folder, an empty registry, no API key and a fake run
    that writes results.xlsx and then summary.json, like main.run."""
    monkeypatch.setattr(api, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(api, "RUN_JOBS", {})
    monkeypatch.delenv("MEIO_API_KEY", raising=False)
    calls = []

    def fake_run(model, settings, run_dir, preset_name=""):
        calls.append((model, settings, run_dir, preset_name))
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "results.xlsx"), "wb") as fh:
            fh.write(b"workbook")
        summary = {"run": {"policy": POLICY_NAME, "preset": preset_name}, "decisions_to_commit": []}
        with open(os.path.join(run_dir, "summary.json"), "w") as fh:
            json.dump(summary, fh)
        return summary

    monkeypatch.setattr(api, "run", fake_run)
    test_client = TestClient(api.app)
    test_client.calls = calls
    return test_client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_valid_run_is_queued_then_completed_with_summary_and_workbook(client):
    r = client.post("/runs?preset=quick", json=EXAMPLE)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["preset"] == "quick" and body["policy"] == POLICY_NAME
    run_id = body["run_id"]
    # TestClient runs background tasks before returning, so the run has finished here
    status = client.get(f"/runs/{run_id}").json()
    assert status["status"] == "completed" and status["error"] is None
    assert status["summary"]["run"] == {"policy": POLICY_NAME, "preset": "quick"}
    model, settings, run_dir, preset = client.calls[0]
    assert run_dir == os.path.join(api.OUTPUT_DIR, f"run_{run_id}") and preset == "quick"
    assert model.horizon == EXAMPLE["horizon"] and settings.n_search_seeds == 200
    xlsx = client.get(f"/runs/{run_id}/results.xlsx")
    assert xlsx.status_code == 200 and xlsx.content == b"workbook"


def test_finished_run_is_found_on_disk_after_a_restart(client, monkeypatch):
    run_id = client.post("/runs", json=EXAMPLE).json()["run_id"]
    monkeypatch.setattr(api, "RUN_JOBS", {})                  # server restarted
    status = client.get(f"/runs/{run_id}").json()
    assert status["status"] == "completed" and status["preset"] == "quick"


@pytest.mark.parametrize("payload, fragment", [
    ({}, "missing field"),
    ({**EXAMPLE, "horizon": "many"}, "invalid input"),
])
def test_invalid_input_is_rejected_before_a_run_is_created(client, payload, fragment):
    r = client.post("/runs", json=payload)
    assert r.status_code == 422 and fragment in r.json()["detail"]
    assert client.calls == [] and api.RUN_JOBS == {}


def test_input_failing_the_model_checks_is_rejected(client):
    bad = json.loads(json.dumps(EXAMPLE))
    bad["products"][0]["lead_time_dist"] = {"2": 0.5, "3": 0.2}
    r = client.post("/runs", json=bad)
    assert r.status_code == 422 and "do not sum to 1" in r.json()["detail"] and client.calls == []


def test_non_object_body_and_unknown_preset_are_rejected(client):
    assert client.post("/runs", json=[1, 2]).status_code == 422
    r = client.post("/runs?preset=huge", json=EXAMPLE)
    assert r.status_code == 422 and "unknown or disabled preset" in r.json()["detail"] and client.calls == []


def test_failing_run_reports_the_error(client, monkeypatch):
    def broken_run(*args, **kwargs):
        raise RuntimeError("solver exploded")
    monkeypatch.setattr(api, "run", broken_run)
    run_id = client.post("/runs", json=EXAMPLE).json()["run_id"]
    status = client.get(f"/runs/{run_id}").json()
    assert status["status"] == "failed" and status["error"] == "RuntimeError: solver exploded"
    assert client.get(f"/runs/{run_id}/results.xlsx").status_code == 404


@pytest.mark.parametrize("run_id", ["0123456789ab", "..", "RUN", "0123456789abc"])
def test_unknown_or_malformed_run_id_gives_404(client, run_id):
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.get(f"/runs/{run_id}/results.xlsx").status_code == 404


def test_workbook_is_not_served_before_the_summary_exists(client):
    run_id = "abcdef012345"
    folder = os.path.join(api.OUTPUT_DIR, f"run_{run_id}")
    os.makedirs(folder)
    with open(os.path.join(folder, "results.xlsx"), "wb") as fh:
        fh.write(b"half written")
    assert client.get(f"/runs/{run_id}/results.xlsx").status_code == 404


def test_api_key_is_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("MEIO_API_KEY", "s3cret")
    assert client.get("/health").status_code == 200
    assert client.post("/runs", json=EXAMPLE).status_code == 401
    assert client.post("/runs", json=EXAMPLE, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/runs/0123456789ab").status_code == 401
    r = client.post("/runs", json=EXAMPLE, headers={"X-API-Key": "s3cret"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert client.get(f"/runs/{run_id}", headers={"X-API-Key": "s3cret"}).json()["status"] == "completed"
    assert client.get(f"/runs/{run_id}/results.xlsx").status_code == 401


def test_runs_are_executed_one_at_a_time(client, monkeypatch):
    """The worker must hold the run slot while the simulation runs."""
    seen = []
    monkeypatch.setattr(api, "run", lambda *a, **k: seen.append(api.RUN_SLOT.locked()) or {})
    client.post("/runs", json=EXAMPLE)
    assert seen == [True] and not api.RUN_SLOT.locked()


def test_run_folders_not_created_by_the_api_are_not_exposed(client):
    """A folder such as output/run_manual (e.g. from  main.py --name run_manual) has no API run id."""
    folder = os.path.join(api.OUTPUT_DIR, "run_manual")
    os.makedirs(folder)
    for name, content in (("results.xlsx", "x"), ("summary.json", "{}")):
        with open(os.path.join(folder, name), "w") as fh:
            fh.write(content)
    assert client.get("/runs/manual").status_code == 404
    assert client.get("/runs/manual/results.xlsx").status_code == 404


def test_presets_can_be_limited_per_deployment(client, monkeypatch):
    monkeypatch.setenv("MEIO_ALLOWED_PRESETS", "quick, standard")
    r = client.post("/runs?preset=full", json=EXAMPLE)
    assert r.status_code == 422 and "['quick', 'standard']" in r.json()["detail"] and client.calls == []
    assert client.post("/runs?preset=standard", json=EXAMPLE).status_code == 200
    monkeypatch.delenv("MEIO_ALLOWED_PRESETS")
    assert client.post("/runs?preset=full", json=EXAMPLE).status_code == 200


def test_settings_can_be_sent_with_the_input(client):
    body = {"input": EXAMPLE, "settings": {"z": 1.5, "n_test_seeds": 1000, "max_outer_rounds": 1}}
    r = client.post("/runs?preset=standard", json=body)
    assert r.status_code == 200 and r.json()["settings_overridden"] == body["settings"]
    _, settings, _, preset = client.calls[0]
    assert (settings.z, settings.n_test_seeds, settings.max_outer_rounds) == (1.5, 1000, 1)
    assert settings.n_search_seeds == 300 and preset == "standard"       # rest from the preset


def test_plain_input_body_still_uses_the_preset_settings(client):
    client.post("/runs?preset=quick", json=EXAMPLE)
    settings = client.calls[0][1]
    assert (settings.n_search_seeds, settings.n_test_seeds, settings.z) == (200, 2000, 2.0)


@pytest.mark.parametrize("settings, fragment", [
    ({"foo": 1}, "unknown setting 'foo'"),
    ({"z": "high"}, "z must be a number"),
    ({"z": True}, "z must be a number"),
    ({"max_outer_rounds": 1.5}, "must be an integer"),
    ({"z": 9}, "z must be between"),
    ({"n_test_seeds": 10}, "n_test_seeds must be between"),
    ([1, 2], "settings must be an object"),
])
def test_invalid_settings_are_rejected(client, settings, fragment):
    r = client.post("/runs", json={"input": EXAMPLE, "settings": settings})
    assert r.status_code == 422 and fragment in r.json()["detail"] and client.calls == []


def test_seed_counts_are_capped_by_the_allowed_presets(client, monkeypatch):
    monkeypatch.setenv("MEIO_ALLOWED_PRESETS", "quick,standard")
    r = client.post("/runs", json={"input": EXAMPLE, "settings": {"n_test_seeds": 10000}})
    assert r.status_code == 422 and "between 500 and 5000" in r.json()["detail"]
    assert client.post("/runs", json={"input": EXAMPLE, "settings": {"n_test_seeds": 5000}}).status_code == 200


def test_wrapped_body_with_extra_keys_or_bad_input_is_rejected(client):
    assert client.post("/runs", json={"input": EXAMPLE, "other": 1}).status_code == 422
    assert client.post("/runs", json={"input": [1]}).status_code == 422
    assert client.calls == []


def test_invalid_rmw_to_pf_lead_time_is_rejected(client):
    r = client.post("/runs", json={**EXAMPLE, "rmw_to_pf_lead_time": -1})
    assert r.status_code == 422 and "RMW -> PF lead time" in r.json()["detail"] and client.calls == []
    r = client.post("/runs", json={**EXAMPLE, "rmw_to_pf_lead_time": 2})
    assert r.status_code == 200 and client.calls[0][0].rmw_to_pf_lead_time == 2
