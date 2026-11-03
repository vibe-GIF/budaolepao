"""HTTP security boundary and browser/MCP endpoint contract."""

import base64
import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from budaolepao_cli.control import Controller
from budaolepao_cli.web import create_app, serve


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "poc/emulator_run"
    directory.mkdir(parents=True)
    (directory / "config.example.json").write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(project):
    controller = Controller(project, preview_duration=0.02)
    with TestClient(create_app(project, controller=controller, token="test-local-token"),
                    base_url="http://127.0.0.1:8765") as client:
        yield client


def authorized(client):
    token = client.get("/api/bootstrap").json()["token"]
    return {"X-Lepao-Token": token}


def test_api_requires_token_and_does_not_allow_query_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status?token=test-local-token").status_code == 401
    assert client.get("/api/status", headers={"X-Lepao-Token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={b"X-Lepao-Token": b"\xff"}).status_code == 401
    response = client.get("/api/status", headers=authorized(client))
    assert response.status_code == 200
    assert response.json()["status"] == "idle"
    assert response.json()["platform_accepted"] is None
    assert response.headers["cache-control"] == "no-store"


def test_frontend_entry_and_static_assets(client):
    entry = client.get("/")
    assert entry.status_code == 200
    assert "/static/app.js" in entry.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example:8765"}, {"Host": "127.0.0.1.attacker.example:8765"},
    {"Origin": "http://attacker.example"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9999"}, {"Origin": "http://localhost:8765"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Host": "user@127.0.0.1:8765"}, {"Host": "127.0.0.1:notaport"},
])
def test_cross_origin_and_dns_rebinding_block_bootstrap(client, headers):
    assert client.get("/api/bootstrap", headers=headers).status_code == 403


def test_same_origin_browser_and_headerless_local_mcp_allowed(client):
    headers = {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}
    assert client.get("/api/bootstrap", headers=headers).status_code == 200
    assert client.get("/api/bootstrap").status_code == 200


def test_config_contract_and_validation(client):
    headers = authorized(client)
    response = client.get("/api/config", headers=headers)
    assert response.json()["source"] == "example"
    config = response.json()["config"]
    config["dist_limit_m"] = 5
    saved = client.put("/api/config", headers=headers, json={"config": config})
    assert saved.status_code == 200
    assert saved.json()["source"] == "saved"
    assert client.put("/api/config", headers=headers, json={"config": {"walk_path": []}}).status_code == 400
    assert client.put("/api/config", headers=headers, json={"config": {}, "command": "cmd"}).status_code == 422
    assert client.get("/api/config", headers=headers).json()["config"]["dist_limit_m"] == 5


def test_config_revision_and_explicit_repair_api(client, project):
    headers = authorized(client)
    original = client.get("/api/config", headers=headers).json()
    saved = client.put("/api/config", headers=headers,
                       json={"config": original["config"], "expected_revision": original["revision"]})
    assert saved.status_code == 200
    assert saved.json()["revision"] != original["revision"]
    stale = client.put("/api/config", headers=headers,
                       json={"config": {"walk_path": []}, "expected_revision": original["revision"]})
    assert stale.status_code == 409
    path = project / "poc/emulator_run/config.json"
    path.write_text("{broken", encoding="utf-8")
    assert client.get("/api/config", headers=headers).status_code == 400
    draft = client.get("/api/config/example", headers=headers).json()
    assert draft["source"] == "example"
    assert path.read_text(encoding="utf-8") == "{broken"
    repaired = client.put("/api/config", headers=headers,
                          json={"config": draft["config"], "expected_revision": draft["revision"]})
    assert repaired.status_code == 200
    assert client.get("/api/config", headers=headers).json() == repaired.json()


def test_preflight_and_template_contract(client):
    headers = authorized(client)
    preflight = client.get("/api/preflight", headers=headers).json()
    assert preflight["ready"] is False
    assert all(set(check) == {"id", "label", "ok", "detail"} for check in preflight["checks"])
    templates = client.get("/api/templates", headers=headers).json()["items"]
    assert len(templates) == 5
    assert not any(template["exists"] for template in templates)
    _, png = cv2.imencode(".png", np.zeros((3, 3, 3), dtype=np.uint8))
    upload = client.put("/api/templates/free_run.png", headers=headers,
                        json={"content_base64": base64.b64encode(png.tobytes()).decode()})
    assert upload.status_code == 200
    assert next(item for item in upload.json()["items"] if item["name"] == "free_run.png")["readable"]
    assert client.get("/api/templates/free_run.png").status_code == 401
    image = client.get("/api/templates/free_run.png", headers=headers)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == png.tobytes()
    assert client.put("/api/templates/evil.png", headers=headers, json={"content_base64": ""}).status_code == 400
    assert client.post("/api/start", headers=headers, json={"mode": "emulator"}).status_code == 400
    assert client.post("/api/start", headers=headers, json={"mode": "shell"}).status_code == 400


def test_status_logs_and_idempotent_stop(client):
    headers = authorized(client)
    result = client.post("/api/stop", headers=headers, json={})
    assert result.json()["status"] == "idle"
    logs = client.get("/api/logs?after=0", headers=headers).json()
    assert logs == {"entries": [], "cursor": 0}
    assert client.get("/api/logs?after=-1", headers=headers).status_code == 400
    assert client.get("/api/emulator", headers=headers).json() == {"emu_dir": "", "player_path": ""}


def test_discovery_exclusive_creation_and_cleanup(project):
    app = create_app(project, token="first-token")
    app.state.discovery_url = "http://127.0.0.1:8765"
    path = project / ".lepao/controller.json"
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        original = path.read_bytes()
        assert json.loads(original)["token"] == "first-token"
        second = create_app(project, token="second-token")
        second.state.discovery_url = "http://127.0.0.1:8766"
        with pytest.raises(RuntimeError, match="already exists"):
            with TestClient(second, base_url="http://127.0.0.1:8766"):
                pass
        assert path.read_bytes() == original
    assert not path.exists()


def test_serve_rejects_non_loopback_bind_before_starting_server():
    with pytest.raises(ValueError, match="loopback"):
        serve(host="0.0.0.0")
