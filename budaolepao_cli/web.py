"""Loopback-only browser API. The session token never enters a URL."""

from contextlib import asynccontextmanager
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .control import CODE_ROOT, ConflictError, Controller, encode_json


def is_loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_request_origin(request):
    host = request.headers.get("host", "")
    try:
        if any(char in host for char in ("@", "/", "\\", " ", "\t", ",")):
            return False
        target = urlsplit("http://" + host)
        if not target.hostname or not is_loopback_host(target.hostname):
            return False
        # Access .port explicitly: malformed values must fail rather than fall back.
        target_port = target.port or 80
        fetch_site = request.headers.get("sec-fetch-site", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return False
        origin = request.headers.get("origin")
        if origin is not None:
            source = urlsplit(origin)
            if source.scheme != "http" or source.hostname != target.hostname or (source.port or 80) != target_port or source.username or source.password:
                return False
            if source.path not in ("", "/") or source.query or source.fragment:
                return False
        return True
    except (ValueError, TypeError):
        return False


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict
    expected_revision: str | None = None


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str


class EmulatorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emu_dir: str
    player_path: str


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_base64: str


def create_app(root=None, *, controller=None, token=None):
    control = controller or Controller(root)
    session_token = token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        discovery = None
        discovery_url = getattr(app.state, "discovery_url", None)
        try:
            if discovery_url:
                discovery = control.root / ".lepao/controller.json"
                discovery.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation prevents two services from replacing each other's token.
                try:
                    with discovery.open("xb") as stream:
                        stream.write(encode_json({"url": discovery_url, "token": session_token, "pid": os.getpid()}))
                    try:
                        discovery.chmod(0o600)
                    except OSError:
                        pass
                except FileExistsError as exc:
                    discovery = None
                    raise RuntimeError("A controller discovery file already exists. Stop the other service, or remove the stale .lepao/controller.json after checking its process.") from exc
            yield
        finally:
            control.close()
            if discovery:
                try:
                    saved = json.loads(discovery.read_text(encoding="utf-8"))
                    if saved.get("token") == session_token and saved.get("pid") == os.getpid():
                        discovery.unlink()
                except (OSError, ValueError):
                    pass

    app = FastAPI(title="Lepao local controller", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.controller = control
    app.state.token = session_token

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        if not check_request_origin(request):
            return JSONResponse({"detail": "Only same-origin loopback access is allowed"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/bootstrap":
            supplied = request.headers.get("x-lepao-token", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), session_token.encode("utf-8")):
                return JSONResponse({"detail": "Invalid local session token"}, status_code=401)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > 3 * 1024 * 1024:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ConflictError)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def runtime_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"token": session_token}

    @app.get("/api/config")
    def config():
        return control.config()

    @app.put("/api/config")
    def save_config(body: ConfigBody):
        return control.save_config(body.config, expected_revision=body.expected_revision)

    @app.get("/api/config/example")
    def config_example():
        return control.config_example()

    @app.get("/api/preflight")
    def preflight():
        return control.preflight()

    @app.get("/api/status")
    def status():
        return control.status()

    @app.get("/api/logs")
    def logs(after: int = 0):
        if after < 0:
            raise ValueError("after must be nonnegative")
        return control.logs(after)

    @app.post("/api/start")
    def start(body: StartBody):
        return control.start(body.mode)

    @app.post("/api/stop")
    def stop():
        return control.stop()

    @app.get("/api/emulator")
    def emulator():
        return control.emulator()

    @app.post("/api/emulator")
    def save_emulator(body: EmulatorBody):
        return control.save_emulator(body.model_dump())

    @app.get("/api/templates")
    def templates():
        return control.templates()

    @app.put("/api/templates/{name}")
    def save_template(name: str, body: TemplateBody):
        return control.save_template(name, body.content_base64)

    @app.get("/api/templates/{name}")
    def template(name: str):
        return Response(control.template(name), media_type="image/png")

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="assets")
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


def serve(host="127.0.0.1", port=8765, root=None):
    if host != "127.0.0.1":
        raise ValueError("The controller must bind to the loopback address 127.0.0.1")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    import uvicorn
    app = create_app(root)
    address = f"[{host}]" if ":" in host else host
    app.state.discovery_url = f"http://{address}:{port}"
    print(f"Lepao local controller: {app.state.discovery_url}")
    uvicorn.run(app, host=host, port=port, access_log=False)
