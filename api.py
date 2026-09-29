"""
Web API of the MEIO simulation-optimisation model (FastAPI), for the Lovable app.

    python api.py                                   local server on http://localhost:8000 (docs: /docs)
    uvicorn api:app --host 0.0.0.0 --port $PORT     start command on a hosting platform (e.g. Render)

Endpoints
    GET  /health                      liveness probe, never needs a key
    POST /runs?preset=quick           body = input JSON (format: examples/example_input.json);
                                      checks the input, queues the run, returns {run_id, status}
    GET  /runs/{run_id}               status "queued" | "running" | "completed" | "failed";
                                      when completed also the summary (same as summary.json)
    GET  /runs/{run_id}/results.xlsx  the Excel workbook of a completed run

A run takes minutes, so POST /runs only queues it. Runs are executed one at a time (a
small hosting instance has little memory); later requests wait with status "queued".

Environment variables (all optional)
    MEIO_API_KEY       if set, every endpoint except /health needs the header  X-API-Key: <key>
    MEIO_CORS_ORIGINS  comma-separated origins allowed to call the API from a browser (default *)
    MEIO_OUTPUT_DIR    folder for the run folders (default: output)

The policy is always the model's only policy (age-aware capped (s,S) + week-1 lookahead).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from main import run
from meio.config import POLICY_NAME, PRESETS, ModelInput, settings_for_preset, validate_input
from meio.io_json import model_from_dict

OUTPUT_DIR = os.path.abspath(os.environ.get("MEIO_OUTPUT_DIR", "output"))
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

app = FastAPI(title="MEIO SimOpt API", version="1.0.0",
              description=f"Simulation-optimisation of a perishable supply chain. Policy: {POLICY_NAME}.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("MEIO_CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=False,          # no cookies are used; with "*" origins this must stay off
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Run registry in memory. After a restart, finished runs are still found on disk (summary.json).
RUN_JOBS: dict[str, dict[str, Any]] = {}
REGISTRY_LOCK = threading.Lock()      # protects RUN_JOBS
RUN_SLOT = threading.Lock()           # only one simulation at a time


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """If MEIO_API_KEY is set, the request must carry the same key in the X-API-Key header."""
    expected = os.environ.get("MEIO_API_KEY")
    if expected and not (x_api_key and secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key header")


def run_folder(run_id: str) -> str:
    return os.path.join(OUTPUT_DIR, f"run_{run_id}")


def set_job(run_id: str, **fields) -> None:
    with REGISTRY_LOCK:
        RUN_JOBS.setdefault(run_id, {}).update(fields)


def parse_input(payload: dict) -> ModelInput:
    """Build and check the model from the request body. Raises HTTP 422 with a clear message."""
    try:
        model = model_from_dict(payload)
        validate_input(model)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        kind = "missing field" if isinstance(exc, KeyError) else "invalid input"
        raise HTTPException(status_code=422, detail=f"{kind}: {exc}") from exc
    return model


def execute_run(run_id: str, model: ModelInput, preset: str) -> None:
    """Background worker: wait for the run slot, run one review, record the outcome."""
    with RUN_SLOT:
        set_job(run_id, status="running")
        try:
            summary = run(model, settings_for_preset(preset), run_folder(run_id), preset_name=preset)
            set_job(run_id, status="completed", summary=summary)
        except Exception as exc:          # report every failure to the client instead of losing it
            set_job(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")


@app.get("/health")
def health() -> dict:
    """Liveness probe for the hosting platform."""
    return {"status": "ok"}


@app.post("/runs", dependencies=[Depends(require_api_key)])
def create_run(payload: dict[str, Any], background_tasks: BackgroundTasks,
               preset: str = Query("quick", description=f"one of {list(PRESETS)}")) -> dict:
    """Check the input, queue the run and return at once with its run_id."""
    if preset not in PRESETS:
        raise HTTPException(status_code=422, detail=f"unknown preset '{preset}', choose from {list(PRESETS)}")
    model = parse_input(payload)
    run_id = uuid.uuid4().hex[:12]
    set_job(run_id, status="queued", preset=preset, summary=None, error=None)
    background_tasks.add_task(execute_run, run_id, model, preset)
    return {"run_id": run_id, "status": "queued", "preset": preset, "policy": POLICY_NAME}


def summary_on_disk(run_id: str) -> dict | None:
    path = os.path.join(run_folder(run_id), "summary.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_run(run_id: str) -> dict:
    """Status of a run; the summary once it is completed."""
    if not RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    with REGISTRY_LOCK:
        job = dict(RUN_JOBS.get(run_id, {}))
    if not job:
        summary = summary_on_disk(run_id)          # e.g. finished before a server restart
        if summary is None:
            raise HTTPException(status_code=404, detail="Run not found")
        job = {"status": "completed", "preset": summary.get("run", {}).get("preset"), "summary": summary}
    return {"run_id": run_id, "status": job["status"], "preset": job.get("preset"),
            "summary": job.get("summary"), "error": job.get("error")}


@app.get("/runs/{run_id}/results.xlsx", dependencies=[Depends(require_api_key)])
def download_workbook(run_id: str) -> FileResponse:
    """The Excel workbook. Only served once summary.json exists, which main.run writes after
    the workbook, so a half-written file is never sent."""
    folder = run_folder(run_id)
    path = os.path.join(folder, "results.xlsx")
    if not (RUN_ID_PATTERN.match(run_id) and os.path.exists(os.path.join(folder, "summary.json"))
            and os.path.exists(path)):
        raise HTTPException(status_code=404, detail="Workbook not ready, run failed or run not found")
    return FileResponse(path, filename=f"meio_results_{run_id}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), reload=True)
