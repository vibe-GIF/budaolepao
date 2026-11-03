"""MCP tests use fake HTTP controllers only; no emulator or account operations."""

import asyncio
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp", reason="Install the optional mcp extra to test the stdio bridge")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from budaolepao_cli.mcp_server import ControllerClient, create_server, MAX_TEMPLATE_BYTES, PNG_SIGNATURE


TOKEN = "test-controller-secret-not-for-output"


@pytest.fixture
def discovery(tmp_path):
    path = tmp_path / "controller.json"
    path.write_text(json.dumps({"url": "http://127.0.0.1:8765", "token": TOKEN, "pid": 123}), encoding="utf-8")
    return path


def call_tool(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    # Official FastMCP exposes both text content and the typed dict response.
    return result[1]


@pytest.mark.parametrize("tool,arguments,method,path,payload", [
    ("get_status", {}, "GET", "/api/status", None),
    ("read_config", {}, "GET", "/api/config", None),
    ("update_config", {"config": {"dist_limit_m": 100}}, "PUT", "/api/config", {"config": {"dist_limit_m": 100}}),
    ("update_config", {"config": {"dist_limit_m": 100}, "expected_revision": "revision-1"}, "PUT", "/api/config",
     {"config": {"dist_limit_m": 100}, "expected_revision": "revision-1"}),
    ("check_readiness", {}, "GET", "/api/preflight", None),
    ("get_logs", {"after": 9}, "GET", "/api/logs?after=9", None),
    ("start_run", {}, "POST", "/api/start", {"mode": "preview"}),
    ("start_run", {"mode": "emulator"}, "POST", "/api/start", {"mode": "emulator"}),
    ("stop_run", {}, "POST", "/api/stop", {}),
    ("get_emulator", {}, "GET", "/api/emulator", None),
    ("set_emulator", {"emu_dir": "C:/MuMu", "player_path": "C:/MuMu/MuMuPlayer.exe"}, "POST", "/api/emulator",
     {"emu_dir": "C:/MuMu", "player_path": "C:/MuMu/MuMuPlayer.exe"}),
    ("list_templates", {}, "GET", "/api/templates", None),
])
def test_tools_forward_payload_to_same_controller(discovery, tool, arguments, method, path, payload):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"result": "from-controller"})

    server = create_server(ControllerClient(discovery, transport=httpx.MockTransport(handle)))
    assert call_tool(server, tool, arguments) == {"result": "from-controller"}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == method
    assert str(request.url) == "http://127.0.0.1:8765" + path
    assert request.headers["X-Lepao-Token"] == TOKEN
    assert (json.loads(request.content) if request.content else None) == payload


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8765", "http://localhost:8765", "http://example.com:8765",
    "http://127.0.0.1.evil.test:8765", "http://127.0.0.1:8765@evil.test:80",
    "http://user:secret@127.0.0.1:8765", "http://127.0.0.1:8765/path",
    "http://127.0.0.1:8765?token=secret", "http://127.0.0.1:8765#fragment",
    "http://127.1:8765", "http://2130706433:8765", "http://[::1]:8765",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1",
    "http://127.0.0.1:8765\n", None,
])
def test_invalid_controller_urls_send_no_request(discovery, url):
    discovery.write_text(json.dumps({"url": url, "token": TOKEN, "pid": 123}), encoding="utf-8")
    transport = httpx.MockTransport(lambda request: pytest.fail("Invalid discovery must not send authentication"))
    result = ControllerClient(discovery, transport=transport).request("GET", "/api/status")
    assert result["error"]["code"] == "invalid_discovery"
    assert TOKEN not in str(result)


def test_discovery_reloaded_for_restarted_controller(discovery):
    seen = []

    def handle(request):
        seen.append((str(request.url), request.headers["X-Lepao-Token"]))
        return httpx.Response(200, json={})

    client = ControllerClient(discovery, transport=httpx.MockTransport(handle))
    client.request("GET", "/api/status")
    discovery.write_text(json.dumps({"url": "http://127.0.0.1:8888/", "token": "new-secret", "pid": 456}), encoding="utf-8")
    client.request("GET", "/api/status")
    assert seen == [("http://127.0.0.1:8765/api/status", TOKEN), ("http://127.0.0.1:8888/api/status", "new-secret")]


def test_config_revision_conflict_is_returned_without_retry(discovery):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"config": {"dist_limit_m": 3000}, "source": "saved", "revision": "r1"})
        assert json.loads(request.content)["expected_revision"] == "r1"
        return httpx.Response(409, json={"detail": "Configuration changed; read the current configuration before saving."})

    server = create_server(ControllerClient(discovery, transport=httpx.MockTransport(handle)))
    current = call_tool(server, "read_config")
    assert current["revision"] == "r1"
    result = call_tool(server, "update_config", {"config": {"dist_limit_m": 2500},
                                                  "expected_revision": current["revision"]})
    assert result["error"]["status_code"] == 409
    assert result["error"]["code"] == "controller_error"
    assert "Configuration changed" in result["error"]["message"]
    assert len(requests) == 2


def test_missing_discovery_gives_start_instruction(tmp_path):
    result = ControllerClient(tmp_path / "missing.json").request("GET", "/api/status")
    assert result["error"]["code"] == "controller_not_running"
    assert "lepao web" in result["error"]["message"]


@pytest.mark.parametrize("status,body,expected", [
    (401, {"detail": TOKEN}, "authentication_failed"),
    (403, {"detail": TOKEN}, "authentication_failed"),
    (409, {"detail": "Already running " + TOKEN}, "controller_error"),
    (422, {"detail": [{"message": TOKEN}]}, "controller_error"),
])
def test_http_failures_are_structured_and_never_leak_auth(discovery, status, body, expected, capsys, caplog):
    client = ControllerClient(discovery, transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)))
    result = client.request("POST", "/api/start", payload={"mode": "preview"})
    assert result["error"]["code"] == expected
    assert result["error"]["status_code"] == status
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in capsys.readouterr().out + caplog.text


def test_success_data_redacts_nested_token(discovery):
    data = {"logs": [{"message": TOKEN}], TOKEN: TOKEN, "cursor": 1}
    client = ControllerClient(discovery, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data)))
    result = client.request("GET", "/api/logs")
    assert TOKEN not in json.dumps(result)
    assert result["cursor"] == 1


def test_redirect_never_forwards_token(discovery):
    requests = []

    def redirect(request):
        requests.append(str(request.url))
        return httpx.Response(307, headers={"location": "http://example.com/" + TOKEN})

    result = ControllerClient(discovery, transport=httpx.MockTransport(redirect)).request("GET", "/api/status")
    assert len(requests) == 1
    assert result["error"]["code"] == "redirect_refused"
    assert TOKEN not in str(result)


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "controller_timeout"),
                                          (httpx.ConnectError, "controller_unavailable")])
def test_transport_error_does_not_echo_exception(discovery, exception, code):
    def fail(request):
        raise exception(TOKEN, request=request)

    result = ControllerClient(discovery, transport=httpx.MockTransport(fail)).request("GET", "/api/status")
    assert result["error"]["code"] == code
    assert TOKEN not in str(result)


@pytest.mark.parametrize("response", [httpx.Response(200, text="not JSON"), httpx.Response(200, json=[1, 2])])
def test_invalid_response_is_structured(discovery, response):
    result = ControllerClient(discovery, transport=httpx.MockTransport(lambda req: response)).request("GET", "/api/status")
    assert result["error"]["code"] == "invalid_response"


def test_upload_png_payload_and_local_rejections(discovery):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"saved": True})

    server = create_server(ControllerClient(discovery, transport=httpx.MockTransport(handle)))
    png = base64.b64encode(PNG_SIGNATURE + b"fake-data-validated-by-controller").decode("ascii")
    assert call_tool(server, "upload_template", {"name": "free_run.png", "content_base64": png}) == {"saved": True}
    assert requests[0].url.path == "/api/templates/free_run.png"
    assert json.loads(requests[0].content) == {"content_base64": png}
    for invalid in ("not-base64", base64.b64encode(b"not-png").decode(), "a" * (MAX_TEMPLATE_BYTES * 2)):
        result = call_tool(server, "upload_template", {"name": "free_run.png", "content_base64": invalid})
        assert result["error"]["code"] == "invalid_argument"
    assert len(requests) == 1
    assert call_tool(server, "get_logs", {"after": -1})["error"]["code"] == "invalid_argument"
    assert len(requests) == 1


def test_official_sdk_stdio_initialize_list_and_call(tmp_path):
    """Real MCP subprocess/transport against a fake, authenticated local HTTP API."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("X-Lepao-Token"), None))
            self.respond({"status": "idle", "platform_accepted": None})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append((self.path, self.headers.get("X-Lepao-Token"), body))
            self.respond({"status": "running", "mode": body["mode"], "platform_accepted": None})

        def respond(self, data):
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    discovery = tmp_path / "controller.json"
    discovery.write_text(json.dumps({"url": f"http://127.0.0.1:{http_server.server_port}", "token": TOKEN,
                                    "pid": os.getpid()}), encoding="utf-8")

    async def smoke():
        environment = dict(os.environ)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment[key] = "http://127.0.0.1:1"
        environment["NO_PROXY"] = environment["no_proxy"] = ""
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "budaolepao_cli.mcp_server", "--controller-file", str(discovery)],
                                       env=environment,
                                       cwd=str(Path(__file__).resolve().parents[1]))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                assert len(tools) == 11
                assert tools["get_status"].annotations.readOnlyHint is True
                assert tools["start_run"].annotations.destructiveHint is True
                assert tools["start_run"].inputSchema["properties"]["mode"]["default"] == "preview"
                status = await session.call_tool("get_status", {})
                assert not status.isError
                assert status.structuredContent == {"status": "idle", "platform_accepted": None}
                started = await session.call_tool("start_run", {})
                assert started.structuredContent["mode"] == "preview"
                invalid = await session.call_tool("start_run", {"mode": "unknown"})
                assert invalid.isError
                assert TOKEN not in str(status) + str(started) + str(invalid)

    try:
        asyncio.run(asyncio.wait_for(smoke(), timeout=30))
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=3)
    assert requests == [("/api/status", TOKEN, None), ("/api/start", TOKEN, {"mode": "preview"})]
