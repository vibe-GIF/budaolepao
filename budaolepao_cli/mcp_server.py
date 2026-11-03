"""Stdio MCP bridge to the single controller owned by ``lepao web``.

This module never imports or creates a runner/controller. The discovery token is
used only as an HTTP header and is redacted from controller responses.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

try:
    import httpx
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
except ImportError:
    raise SystemExit("MCP dependencies are missing. Install with: python -m pip install -e \".[mcp]\"") from None


DEFAULT_CONTROLLER_FILE = Path(__file__).resolve().parents[1] / ".lepao" / "controller.json"
TEMPLATE_NAMES = (
    "begin_run.png", "free_run.png", "pause_run.png", "end_run.png", "confirm_end.png",
)
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _error(code: str, message: str, status_code: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if status_code is not None:
        result["status_code"] = status_code
    return {"error": result}


def _redact(value: Any, token: str) -> Any:
    """Do not echo a token even if the controller includes it in a response."""
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    if isinstance(value, dict):
        return {_redact(key, token): _redact(item, token) for key, item in value.items()}
    return value


def _validate_url(value: Any) -> str:
    # A literal IPv4 loopback address avoids DNS resolution/rebinding, credentials,
    # redirects, and alternative numeric spellings. The port must be explicit.
    if not isinstance(value, str) or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/?", value):
        raise ValueError("Controller URL must be http://127.0.0.1:<port> without a path or credentials.")
    port = urlsplit(value).port
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Controller URL has an invalid port.")
    return value.rstrip("/")


class ControllerClient:
    """Read discovery afresh on every call, so controller restarts are supported."""

    def __init__(self, controller_file: Path = DEFAULT_CONTROLLER_FILE, *,
                 timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self.controller_file = Path(controller_file)
        self.timeout = timeout
        self._transport = transport

    def _discover(self) -> tuple[str, str]:
        # Bound the discovery file before parsing; its contents are never logged.
        with self.controller_file.open("rb") as stream:
            content = stream.read(16 * 1024 + 1)
        if len(content) > 16 * 1024:
            raise ValueError("Controller discovery file is too large.")
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("Controller discovery file is not valid JSON.") from None
        if not isinstance(data, dict):
            raise ValueError("Controller discovery file must contain an object.")
        url = _validate_url(data.get("url"))
        token = data.get("token")
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise ValueError("Controller discovery file has an invalid authentication token.")
        if type(data.get("pid")) is not int or data["pid"] <= 0:
            raise ValueError("Controller discovery file has an invalid process ID.")
        return url, token

    def request(self, method: str, path: str, *, payload: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            url, token = self._discover()
        except FileNotFoundError:
            return _error("controller_not_running", "Start 'lepao web' before using MCP tools.")
        except OSError:
            return _error("discovery_unreadable", "Cannot read the controller discovery file; check local file access.")
        except ValueError as exc:
            return _error("invalid_discovery", str(exc))

        try:
            # Never consult proxy environment variables or forward the token on a redirect.
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False,
                              transport=self._transport) as client:
                response = client.request(method, url + path, json=payload, params=params,
                                          headers={"X-Lepao-Token": token})
        except httpx.TimeoutException:
            return _error("controller_timeout", "Controller request timed out; inspect 'lepao web' and retry.")
        except (httpx.HTTPError, OSError):
            # HTTP exception strings can contain request data. Do not return them.
            return _error("controller_unavailable", "Cannot reach the local controller; restart 'lepao web' if needed.")
        except (ValueError, TypeError, OverflowError):
            return _error("invalid_payload", "Request payload must be valid finite JSON data.")

        if 300 <= response.status_code < 400:
            return _error("redirect_refused", "Controller redirects are not followed.", response.status_code)
        if response.status_code in (401, 403):
            return _error("authentication_failed", "Controller rejected authentication; restart 'lepao web' to refresh discovery.",
                          response.status_code)
        try:
            data = response.json()
        except ValueError:
            return _error("invalid_response", "Controller returned a non-JSON response.", response.status_code)
        data = _redact(data, token)
        if response.is_error:
            detail = data.get("detail", data.get("error", "Request rejected.")) if isinstance(data, dict) else "Request rejected."
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
            return _error("controller_error", detail, response.status_code)
        if not isinstance(data, dict):
            return _error("invalid_response", "Controller response must be a JSON object.")
        return data


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                         idempotentHint=True, openWorldHint=False)
START = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                        idempotentHint=False, openWorldHint=False)


def create_server(client: ControllerClient | None = None) -> FastMCP:
    client = client if client is not None else ControllerClient()
    server = FastMCP(
        "Lepao local controller", log_level="WARNING",
        instructions="Connects to the already running lepao web controller. Preview is local only; "
                     "emulator mode operates MuMu. Local completion never confirms platform acceptance.",
    )

    @server.tool(annotations=READ_ONLY)
    def get_status() -> dict[str, Any]:
        """Read the same run status and progress shown in lepao web."""
        return client.request("GET", "/api/status")

    @server.tool(annotations=READ_ONLY)
    def read_config() -> dict[str, Any]:
        """Read the current config and its source from the controller."""
        return client.request("GET", "/api/config")

    @server.tool(annotations=UPDATE)
    def update_config(config: dict[str, Any], expected_revision: str | None = None) -> dict[str, Any]:
        """Save a complete config. Pass the revision from read_config to avoid overwriting another client's changes."""
        payload: dict[str, Any] = {"config": config}
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        return client.request("PUT", "/api/config", payload=payload)

    @server.tool(annotations=READ_ONLY)
    def check_readiness() -> dict[str, Any]:
        """Check config, emulator files and button templates without starting a run."""
        return client.request("GET", "/api/preflight")

    @server.tool(annotations=READ_ONLY)
    def get_logs(after: int = 0) -> dict[str, Any]:
        """Read log entries after a sequence cursor; pass the returned cursor next time."""
        if after < 0:
            return _error("invalid_argument", "after must be a nonnegative sequence number.")
        return client.request("GET", "/api/logs", params={"after": after})

    @server.tool(annotations=START)
    def start_run(mode: Literal["preview", "emulator"] = "preview") -> dict[str, Any]:
        """Start one run. Preview does not operate devices; emulator mode operates MuMu."""
        return client.request("POST", "/api/start", payload={"mode": mode})

    @server.tool(annotations=UPDATE)
    def stop_run() -> dict[str, Any]:
        """Stop the controller's run. MuMu stays open; inspect/end any active App session manually."""
        return client.request("POST", "/api/stop", payload={})

    @server.tool(annotations=READ_ONLY)
    def get_emulator() -> dict[str, Any]:
        """Read the configured MuMu directory and player path."""
        return client.request("GET", "/api/emulator")

    @server.tool(annotations=UPDATE)
    def set_emulator(emu_dir: str, player_path: str) -> dict[str, Any]:
        """Save local MuMu paths through the controller; does not start MuMu."""
        return client.request("POST", "/api/emulator", payload={"emu_dir": emu_dir, "player_path": player_path})

    @server.tool(annotations=READ_ONLY)
    def list_templates() -> dict[str, Any]:
        """List the five allowed button templates and whether each is present/readable."""
        return client.request("GET", "/api/templates")

    @server.tool(annotations=UPDATE)
    def upload_template(name: Literal["begin_run.png", "free_run.png", "pause_run.png", "end_run.png", "confirm_end.png"],
                        content_base64: str) -> dict[str, Any]:
        """Replace one allowed PNG template from base64 data, at most 2 MiB decoded."""
        if name not in TEMPLATE_NAMES:
            return _error("invalid_argument", "Unknown template name.")
        if len(content_base64) > ((MAX_TEMPLATE_BYTES + 2) // 3) * 4:
            return _error("invalid_argument", "PNG template exceeds the 2 MiB limit.")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            return _error("invalid_argument", "content_base64 must be valid base64 PNG data.")
        if len(content) > MAX_TEMPLATE_BYTES or not content.startswith(PNG_SIGNATURE):
            return _error("invalid_argument", "Template must be PNG data no larger than 2 MiB.")
        return client.request("PUT", "/api/templates/" + name, payload={"content_base64": content_base64})

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stdio MCP bridge to a running lepao web controller.")
    parser.add_argument("--controller-file", type=Path, default=DEFAULT_CONTROLLER_FILE,
                        help="Path to the discovery file written by lepao web.")
    args = parser.parse_args(argv)
    create_server(ControllerClient(args.controller_file)).run(transport="stdio")


if __name__ == "__main__":
    main()
