"""Checks that the Render Blueprint (render.yaml) matches the code and CI, so a change on
one side cannot silently break the deployment. Plain text parsing, no YAML dependency."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import api
from meio.config import PRESETS


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


RENDER = _read("render.yaml")


def _field(name):
    match = re.search(rf"^\s*{name}:\s*(.+?)\s*(#.*)?$", RENDER, re.MULTILINE)
    assert match, f"render.yaml has no field '{name}'"
    return match.group(1).strip().strip('"')


def _env():
    pairs = re.findall(r"-\s*key:\s*(\S+).*?\n\s*(value|generateValue):\s*(\S+)", RENDER)
    return {key: value.strip('"') for key, _, value in pairs}


def test_start_command_serves_the_api_app_with_one_process():
    start = _field("startCommand")
    assert start.startswith("uvicorn api:app ") and "--port $PORT" in start and "--host 0.0.0.0" in start
    assert "--workers 1" in start                         # in-memory registry and run lock
    assert _field("buildCommand") == "pip install -r requirements.txt"
    requirements = _read("requirements.txt")
    assert "fastapi" in requirements and "uvicorn" in requirements


def test_health_check_path_is_an_open_route_that_answers_ok(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("MEIO_API_KEY", "set-on-render")    # the probe must work with a key configured
    response = TestClient(api.app).get(_field("healthCheckPath"))
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_python_version_matches_ci():
    ci = re.search(r'python-version:\s*"?([\d.]+)"?', _read(".github/workflows/tests.yml")).group(1)
    assert _env()["PYTHON_VERSION"].startswith(ci + ".")


def test_api_key_is_generated_and_full_preset_is_disabled():
    env = _env()
    assert env["MEIO_API_KEY"] == "true"                   # generateValue: true, never a literal key
    allowed = env["MEIO_ALLOWED_PRESETS"].split(",")
    assert allowed and set(allowed) <= set(PRESETS) and "full" not in allowed


def test_every_configured_variable_is_one_the_api_reads():
    for key in _env():
        assert key == "PYTHON_VERSION" or key in _read("api.py"), f"{key} is not used by api.py"
