"""Local controller shared by the browser UI and MCP HTTP client."""

import base64
import binascii
from collections import deque
from copy import deepcopy
from dataclasses import asdict, fields
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import cv2
import numpy as np

EVENT_PREFIX = "LEPAO_EVENT "
TEMPLATE_NAMES = ("begin_run.png", "free_run.png", "pause_run.png", "end_run.png", "confirm_end.png")
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
CODE_ROOT = Path(__file__).resolve().parent.parent


class ConflictError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def runner_module():
    """Import without starting the runner or altering the working directory."""
    spec = importlib.util.spec_from_file_location("_lepao_control_runner", CODE_ROOT / "poc/emulator_run/main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_configuration(data):
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    runner = runner_module()
    unknown = set(data) - {item.name for item in fields(runner.Config)}
    if unknown:
        raise ValueError("Unknown config fields: " + ", ".join(sorted(unknown)))
    cfg = runner.Config(**data)
    try:
        runner.validate_config(cfg)
    except (OverflowError, TypeError) as exc:
        raise ValueError("Config contains an unsupported numeric value") from exc
    for attr, filename in zip(("tpl_begin", "tpl_free", "tpl_pause", "tpl_end", "tpl_confirm_end"), TEMPLATE_NAMES):
        if getattr(cfg, attr).replace("\\", "/") != "img/" + filename:
            raise ValueError(f"{attr} must use img/{filename} in the local controller")
    return asdict(cfg)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encode_json(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


class Controller:
    def __init__(self, root=None, *, process_factory=subprocess.Popen, preview_duration=8.0, max_logs=500):
        self.root = Path(root or CODE_ROOT).resolve()
        self.run_dir = self.root / "poc/emulator_run"
        self._process_factory = process_factory
        self.preview_duration = preview_duration
        self._lock = threading.RLock()
        self._process = None
        self._thread = None
        self._closed = False
        self._logs = deque(maxlen=max_logs)
        self._seq = 0
        self._status = {"id": None, "status": "idle", "mode": None, "started_at": None,
                        "finished_at": None, "returncode": None, "message": "Ready",
                        "progress": self._empty_progress(), "target_distance_m": None,
                        "platform_accepted": None}

    @staticmethod
    def _empty_progress():
        return {"distance_m": 0.0, "speed_mps": 0.0, "elapsed_s": 0.0,
                "steps": 0, "lon": None, "lat": None}

    def _ensure_idle(self):
        if self._closed:
            raise ConflictError("Controller has shut down")
        if self._status["status"] in ("running", "stopping"):
            raise ConflictError("A task is active; stop it before changing configuration or starting another")

    def _log(self, message, level="info"):
        with self._lock:
            self._seq += 1
            message = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(message))
            self._logs.append({"seq": self._seq, "time": time.time(), "level": level,
                               "message": message[:4000]})

    def logs(self, after=0):
        with self._lock:
            return {"entries": deepcopy([item for item in self._logs if item["seq"] > after]), "cursor": self._seq}

    def status(self):
        with self._lock:
            return deepcopy(self._status)

    def _configuration_bytes(self):
        saved = self.run_dir / "config.json"
        path = saved if saved.exists() else self.run_dir / "config.example.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Cannot read {path.name}: {exc}") from exc
        return path, raw, "saved" if path == saved else "example"

    @staticmethod
    def _decode_configuration(path, raw):
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read {path.name}: {exc}") from exc
        return validate_configuration(data)

    def config(self):
        with self._lock:
            path, raw, source = self._configuration_bytes()
            return {"config": self._decode_configuration(path, raw), "source": source,
                    "revision": hashlib.sha256(raw).hexdigest()}

    def config_example(self):
        """Return an explicit repair draft, with a revision of the current raw file."""
        with self._lock:
            _, current_raw, _ = self._configuration_bytes()
            example = self.run_dir / "config.example.json"
            try:
                raw = example.read_bytes()
            except OSError as exc:
                raise ValueError(f"Cannot read {example.name}: {exc}") from exc
            return {"config": self._decode_configuration(example, raw), "source": "example",
                    "revision": hashlib.sha256(current_raw).hexdigest()}

    def save_config(self, data, expected_revision=None):
        with self._lock:
            self._ensure_idle()
            if expected_revision is not None:
                _, current_raw, _ = self._configuration_bytes()
                if expected_revision != hashlib.sha256(current_raw).hexdigest():
                    raise ConflictError("Configuration changed since it was loaded. Reload the current configuration or explicitly load an example draft before saving.")
            validated = validate_configuration(data)
            raw = encode_json(validated)
            atomic_write(self.run_dir / "config.json", raw)
            self._log("Configuration saved")
            return {"config": validated, "source": "saved", "revision": hashlib.sha256(raw).hexdigest()}

    def emulator(self):
        with self._lock:
            path = self.run_dir / "emu_path.json"
            if not path.exists():
                return {"emu_dir": "", "player_path": ""}
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                return {key: str(data.get(key, "")) for key in ("emu_dir", "player_path")}
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
                raise ValueError(f"Invalid emulator configuration: {exc}") from exc

    @staticmethod
    def validate_emulator(data):
        if not isinstance(data, dict) or not data.get("emu_dir") or not data.get("player_path"):
            raise ValueError("emu_dir and player_path are required")
        directory, player = Path(data["emu_dir"]).resolve(), Path(data["player_path"]).resolve()
        for filename in ("MuMuManager.exe", "adb.exe"):
            if not (directory / filename).is_file():
                raise ValueError(f"{filename} not found in emulator directory")
        if player.name not in ("MuMuPlayer.exe", "MuMuPlayer-12.0.exe", "GameViewer.exe", "GameViewerLauncher.exe") or not player.is_file():
            raise ValueError("Select an existing MuMu player executable")
        return {"emu_dir": str(directory), "player_path": str(player)}

    def save_emulator(self, data):
        with self._lock:
            self._ensure_idle()
            validated = self.validate_emulator(data)
            atomic_write(self.run_dir / "emu_path.json", encode_json(validated))
            return validated

    def templates(self):
        with self._lock:
            items = []
            for name in TEMPLATE_NAMES:
                path = self.run_dir / "img" / name
                exists = path.is_file()
                image = cv2.imread(str(path)) if exists else None
                items.append({"name": name, "exists": exists, "readable": image is not None})
            return {"items": items}

    def template(self, name):
        if name not in TEMPLATE_NAMES:
            raise ValueError("Unsupported template name")
        with self._lock:
            path = self.run_dir / "img" / name
            if path.resolve().parent != (self.run_dir / "img").resolve():
                raise ValueError("Template must remain in the configured image directory")
            try:
                with path.open("rb") as stream:
                    raw = stream.read(MAX_TEMPLATE_BYTES + 1)
            except OSError as exc:
                raise ValueError(f"Template unavailable: {name}") from exc
            if len(raw) > MAX_TEMPLATE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Template must be a PNG of at most 2 MiB")
            return raw

    def save_template(self, name, content_base64):
        with self._lock:
            self._ensure_idle()
            if name not in TEMPLATE_NAMES:
                raise ValueError("Unsupported template name")
            if not isinstance(content_base64, str) or len(content_base64) > ((MAX_TEMPLATE_BYTES + 2) // 3) * 4:
                raise ValueError("Template must be a base64 PNG of at most 2 MiB")
            try:
                raw = base64.b64decode(content_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Invalid base64 PNG") from exc
            if len(raw) > MAX_TEMPLATE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Template must be a PNG of at most 2 MiB")
            # Reject oversized dimensions before image decompression.
            if len(raw) < 24 or raw[12:16] != b"IHDR":
                raise ValueError("Invalid PNG header")
            width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
            if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 8_000_000:
                raise ValueError("Template dimensions are too large")
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("PNG image could not be decoded")
            atomic_write(self.run_dir / "img" / name, raw)
            return self.templates()

    def preflight(self):
        with self._lock:
            checks = []
            def add(identifier, label, ok, detail):
                checks.append({"id": identifier, "label": label, "ok": bool(ok), "detail": detail})
            config = None
            try:
                result = self.config()
                config = result["config"]
                add("config", "Configuration", True, f"Valid {result['source']} configuration")
            except ValueError as exc:
                add("config", "Configuration", False, str(exc))
            add("windows", "Windows host", os.name == "nt", "Emulator mode requires Windows; preview works offline")
            try:
                self.validate_emulator(self.emulator())
                add("emulator", "MuMu executables", True, "Configured files exist; connection is not yet verified")
            except (ValueError, TypeError, OSError) as exc:
                add("emulator", "MuMu executables", False, str(exc))
            items = self.templates()["items"]
            required = bool(config and config["auto_start_run"] and not config["allow_coordinate_fallback"])
            missing = [item["name"] for item in items if not item["readable"]]
            add("templates", "Button templates", not required or not missing,
                "Missing/unreadable: " + ", ".join(missing) if required and missing else "Templates ready or automatic matching not required")
            add("idle", "Task slot", self._status["status"] not in ("running", "stopping"), "Only one task may run at a time")
            return {"ready": all(item["ok"] for item in checks), "checks": checks}

    def start(self, mode):
        with self._lock:
            self._ensure_idle()
            if mode not in ("preview", "emulator"):
                raise ValueError("mode must be preview or emulator")
            configuration = self.config()["config"]
            if mode == "emulator":
                checks = self.preflight()
                if not checks["ready"]:
                    raise ValueError("Preflight failed: " + "; ".join(item["detail"] for item in checks["checks"] if not item["ok"]))
            task_id = uuid.uuid4().hex
            snapshot = self.root / ".lepao" / (task_id + ".json")
            atomic_write(snapshot, encode_json(configuration))
            self._status = {"id": task_id, "status": "running", "mode": mode, "started_at": time.time(),
                            "finished_at": None, "returncode": None, "message": "Offline preview running" if mode == "preview" else "Emulator task running; platform acceptance unverified",
                            "progress": self._empty_progress(), "target_distance_m": configuration["dist_limit_m"],
                            "platform_accepted": None}
            command = [sys.executable, "-u", "-m", "budaolepao_cli.worker", "--root", str(self.root),
                       "--config", str(snapshot), "--mode", mode, "--preview-duration", str(self.preview_duration)]
            options = {"cwd": str(CODE_ROOT), "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                       "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                process = self._process_factory(command, **options)
            except Exception as exc:
                snapshot.unlink(missing_ok=True)
                self._status.update(status="failed", finished_at=time.time(), message=f"Could not start worker: {exc}")
                raise RuntimeError(self._status["message"]) from exc
            self._process = process
            self._log(self._status["message"])
            self._thread = threading.Thread(target=self._monitor, args=(process, task_id, snapshot), daemon=True)
            self._thread.start()
            return self.status()

    def _monitor(self, process, task_id, snapshot):
        completed = False
        try:
            while True:
                line = process.stdout.readline(65536)
                if not line:
                    break
                line = line.rstrip()
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                        if event.get("type") == "progress":
                            progress = event["progress"]
                            if not isinstance(progress, dict) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in progress.values()):
                                raise ValueError("Invalid worker progress")
                            with self._lock:
                                if self._status["id"] == task_id:
                                    self._status["progress"].update({k: v for k, v in progress.items() if k in self._empty_progress()})
                        elif event.get("type") == "completed":
                            completed = True
                        elif event.get("type") == "error":
                            self._log(event.get("message", "Worker error"), "error")
                    except (ValueError, KeyError, TypeError, AttributeError):
                        self._log("Ignored invalid worker event", "warning")
                elif line:
                    self._log(line)
            returncode = process.wait()
            with self._lock:
                if self._status["id"] != task_id:
                    return
                stopped = self._status["status"] == "stopping"
                status = "stopped" if stopped else "completed" if returncode == 0 and completed else "failed"
                message = ("Offline preview stopped; no emulator or platform was contacted." if stopped and self._status["mode"] == "preview" else
                           "Stopped. Check the app and end any active session manually." if stopped else
                           "Offline preview complete; no emulator or platform was contacted." if status == "completed" and self._status["mode"] == "preview" else
                           "Local simulation complete. Check the app record; platform acceptance is unverified." if status == "completed" else
                           f"Worker exited with code {returncode}; no platform result verified.")
                self._status.update(status=status, returncode=returncode, finished_at=time.time(), message=message)
                self._process = None
                self._log(message, "error" if status == "failed" else "info")
        except Exception as exc:
            with self._lock:
                self._status.update(status="failed", finished_at=time.time(), message=f"Worker monitor failed: {exc}")
                self._process = None
            if process.poll() is None:
                process.kill()
                process.wait()
        finally:
            process.stdout.close()
            snapshot.unlink(missing_ok=True)

    def stop(self):
        with self._lock:
            process = self._process
            if process is None or self._status["status"] not in ("running", "stopping"):
                return self.status()
            if self._status["status"] == "stopping":
                return self.status()
            message = ("Stopping offline preview." if self._status["mode"] == "preview" else
                       "Stopping worker; MuMu remains open. Check any active app session.")
            self._status.update(status="stopping", message=message)
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            threading.Thread(target=self._force_stop, args=(process,), daemon=True).start()
            self._log(self._status["message"], "warning")
            return self.status()

    @staticmethod
    def _force_stop(process):
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
