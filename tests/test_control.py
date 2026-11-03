"""Controller lifecycle and offline-only worker tests."""

import base64
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

import cv2
import numpy as np
import pytest

from budaolepao_cli.control import (Controller, ConflictError, EVENT_PREFIX,
                                  MAX_TEMPLATE_BYTES, runner_module, validate_configuration)
from budaolepao_cli import worker


@pytest.fixture
def project(tmp_path):
    run_dir = tmp_path / "poc/emulator_run"
    run_dir.mkdir(parents=True)
    (run_dir / "config.example.json").write_text(json.dumps({
        "walk_path": [[114.0, 30.0], [114.01, 30.0], [114.01, 30.01]],
        "dist_limit_m": 10,
    }), encoding="utf-8")
    return tmp_path


def wait_status(controller, expected, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if controller.status()["status"] in expected:
            return controller.status()
        time.sleep(0.01)
    pytest.fail(f"Timed out waiting for {expected}: {controller.status()}")


class BlockingOutput:
    def __init__(self):
        self.queue = queue.Queue()
    def readline(self, size=-1):
        return self.queue.get(timeout=5)
    def close(self):
        pass


class FakeProcess:
    def __init__(self):
        self.stdout = BlockingOutput()
        self.finished = threading.Event()
        self.returncode = None
        self.terminated = False
    def complete(self, code=0, completed=True):
        if completed:
            self.stdout.queue.put(EVENT_PREFIX + '{"type":"completed"}\n')
        self.returncode = code
        self.finished.set()
        self.stdout.queue.put("")
    def wait(self, timeout=None):
        if not self.finished.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode
    def poll(self):
        return self.returncode
    def terminate(self):
        self.terminated = True
        self.complete(-15, completed=False)
    def kill(self):
        self.complete(-9, completed=False)


def test_example_config_save_is_validated_and_atomic(project):
    controller = Controller(project)
    result = controller.config()
    assert result["source"] == "example"
    saved = controller.save_config(result["config"])
    assert saved["source"] == "saved"
    path = project / "poc/emulator_run/config.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        controller.save_config({"dist_limit_m": float("nan")})
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_corrupt_saved_config_never_falls_back_to_example(project):
    (project / "poc/emulator_run/config.json").write_text("{invalid", encoding="utf-8")
    controller = Controller(project)
    with pytest.raises(ValueError, match="Cannot read config.json"):
        controller.config()
    preflight = controller.preflight()
    assert not preflight["ready"]
    assert not next(check for check in preflight["checks"] if check["id"] == "config")["ok"]
    with pytest.raises(ValueError):
        controller.start("preview")
    assert controller.status()["status"] == "idle"


def test_concurrent_saves_reject_stale_revision(project):
    controller = Controller(project)
    original = controller.config()
    barrier = threading.Barrier(3)
    outcomes = []
    def save(distance):
        draft = dict(original["config"], dist_limit_m=distance)
        barrier.wait()
        try:
            controller.save_config(draft, expected_revision=original["revision"])
            outcomes.append(("saved", distance))
        except ConflictError:
            outcomes.append(("conflict", distance))
    threads = [threading.Thread(target=save, args=(distance,)) for distance in (20, 30)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert sorted(result[0] for result in outcomes) == ["conflict", "saved"]
    winner = next(distance for status, distance in outcomes if status == "saved")
    saved = controller.config()
    assert saved["config"]["dist_limit_m"] == winner
    assert saved["revision"] != original["revision"]
    # Check the lease before validating data; a stale invalid draft is still a conflict.
    with pytest.raises(ConflictError):
        controller.save_config({"walk_path": []}, expected_revision=original["revision"])


def test_explicit_example_draft_repairs_corrupt_configuration(project):
    controller = Controller(project)
    path = project / "poc/emulator_run/config.json"
    broken = b"{bad JSON\xff"
    path.write_bytes(broken)
    with pytest.raises(ValueError):
        controller.config()
    draft = controller.config_example()
    assert draft["source"] == "example"
    assert draft["revision"] == hashlib.sha256(broken).hexdigest()
    assert path.read_bytes() == broken  # Loading a draft never silently writes it.
    repaired = controller.save_config(draft["config"], expected_revision=draft["revision"])
    assert repaired["source"] == "saved"
    assert repaired["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert controller.config() == repaired


@pytest.mark.parametrize("data", [
    {"tpl_begin": "../outside.png"}, {"tpl_free": "C:/outside.png"},
    {"run_pkg": "com.example;reboot"}, {"run_pkg": "com.example$(reboot)"},
    {"run_pkg": "com.example\nreboot"}, {"shell": "cmd"},
])
def test_configuration_cannot_supply_commands_or_external_template_paths(data):
    with pytest.raises(ValueError):
        validate_configuration(data)


@pytest.mark.parametrize("package", ["com.example;reboot", "com.example$(id)", "com.example`id`",
                                   "com.example|id", "com.example\nreboot"])
def test_cli_runner_itself_rejects_shell_metacharacters(package):
    runner = runner_module()
    with pytest.raises(ValueError, match="package identifier"):
        runner.validate_config(runner.Config(run_pkg=package))


def test_single_task_start_and_configuration_lock(project):
    process = FakeProcess()
    calls = []
    def factory(command, **kwargs):
        calls.append((command, kwargs))
        return process
    controller = Controller(project, process_factory=factory)
    try:
        barrier = threading.Barrier(3)
        outcomes = []
        def start():
            barrier.wait()
            try:
                controller.start("preview")
                outcomes.append("started")
            except ConflictError:
                outcomes.append("conflict")
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
        assert sorted(outcomes) == ["conflict", "started"]
        assert len(calls) == 1
        assert "shell" not in calls[0][1]
        with pytest.raises(ConflictError):
            controller.save_config(controller.config()["config"])
        stopped = controller.stop()
        assert stopped["status"] in ("stopping", "stopped")
        result = wait_status(controller, {"stopped"})
        assert result["platform_accepted"] is None
        assert process.terminated
        assert not list((project / ".lepao").glob("*.json"))
    finally:
        controller.close()


@pytest.mark.parametrize("exit_code,event,expected", [(0, True, "completed"), (0, False, "failed"), (1, True, "failed")])
def test_exit_without_completion_is_not_reported_as_success(project, exit_code, event, expected):
    process = FakeProcess()
    controller = Controller(project, process_factory=lambda *a, **k: process)
    try:
        controller.start("preview")
        process.stdout.queue.put(EVENT_PREFIX + '{"type":"progress","progress":{"distance_m":3.5}}\n')
        process.stdout.queue.put(EVENT_PREFIX + '{invalid\n')
        process.complete(exit_code, event)
        result = wait_status(controller, {expected})
        assert result["progress"]["distance_m"] == 3.5
        assert result["platform_accepted"] is None
        assert any("invalid worker event" in entry["message"] for entry in controller.logs()["entries"])
    finally:
        controller.close()


def test_failed_process_creation_is_recoverable(project):
    def failed(*a, **k):
        raise OSError("simulated startup failure")
    controller = Controller(project, process_factory=failed)
    with pytest.raises(RuntimeError, match="startup failure"):
        controller.start("preview")
    assert controller.status()["status"] == "failed"
    assert not list((project / ".lepao").glob("*.json"))
    controller.save_config(controller.config()["config"])


def test_finished_run_keeps_its_target_when_next_config_changes(project):
    process = FakeProcess()
    controller = Controller(project, process_factory=lambda *a, **k: process)
    try:
        original = controller.config()["config"]["dist_limit_m"]
        controller.start("preview")
        process.complete(0, True)
        wait_status(controller, {"completed"})
        next_config = controller.config()["config"]
        next_config["dist_limit_m"] = original + 100
        controller.save_config(next_config)
        assert controller.status()["target_distance_m"] == original
        assert controller.config()["config"]["dist_limit_m"] == original + 100
    finally:
        controller.close()


def test_logs_are_bounded_and_cursor_is_monotonic(project):
    controller = Controller(project, max_logs=3)
    for index in range(6):
        controller._log(str(index) + "x" * 5000)
    logs = controller.logs()
    assert len(logs["entries"]) == 3
    assert logs["cursor"] == 6
    assert all(len(entry["message"]) <= 4000 for entry in logs["entries"])
    assert [entry["seq"] for entry in controller.logs(after=5)["entries"]] == [6]


def test_template_upload_whitelist_validation_and_atomicity(project):
    controller = Controller(project)
    ok, png = cv2.imencode(".png", np.zeros((5, 6, 3), dtype=np.uint8))
    assert ok
    content = base64.b64encode(png.tobytes()).decode()
    controller.save_template("begin_run.png", content)
    path = project / "poc/emulator_run/img/begin_run.png"
    before = path.read_bytes()
    for name, value in (("../escaped.png", content), ("begin_run.png", "not base64"),
                        ("begin_run.png", base64.b64encode(b"JPEG").decode()),
                        ("begin_run.png", "A" * (MAX_TEMPLATE_BYTES * 2))):
        with pytest.raises(ValueError):
            controller.save_template(name, value)
    assert path.read_bytes() == before
    assert controller.templates()["items"][0]["readable"]


def test_declared_png_decompression_bomb_rejected_before_decode(project, monkeypatch):
    controller = Controller(project)
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (100_000).to_bytes(4, "big") * 2
    monkeypatch.setattr(cv2, "imdecode", lambda *a: pytest.fail("Must validate header before decoding"))
    with pytest.raises(ValueError, match="dimensions"):
        controller.save_template("begin_run.png", base64.b64encode(raw).decode())


def test_emulator_paths_require_known_executables(project, tmp_path):
    controller = Controller(project)
    directory = tmp_path / "emu"
    directory.mkdir()
    with pytest.raises(ValueError):
        controller.save_emulator({"emu_dir": str(directory), "player_path": str(directory / "evil.exe")})
    for name in ("MuMuManager.exe", "adb.exe", "MuMuPlayer.exe"):
        (directory / name).write_bytes(b"test-only; never executed")
    saved = controller.save_emulator({"emu_dir": str(directory), "player_path": str(directory / "MuMuPlayer.exe")})
    assert controller.emulator() == saved


def test_preview_never_calls_runner_or_emulator(project, monkeypatch):
    module = runner_module()
    monkeypatch.setattr(module, "run", lambda *a, **k: pytest.fail("Preview must not run emulator"))
    monkeypatch.setattr(module, "MuMuController", lambda *a: pytest.fail("Preview must not construct emulator"))
    events = []
    ticks = iter([0, 0, 0.5, 1.0])
    config = Controller(project).config()["config"]
    worker.preview(config, duration=1, emit_event=events.append, sleep=lambda _: None, clock=lambda: next(ticks))
    assert events[-1]["type"] == "completed"
    assert events[-2]["progress"]["distance_m"] == 10
    assert events[-1]["platform_accepted"] is None


def test_real_offline_preview_child_lifecycle(project):
    controller = Controller(project, preview_duration=0.02)
    try:
        controller.start("preview")
        result = wait_status(controller, {"completed", "failed"})
        assert result["status"] == "completed", controller.logs()
        assert result["progress"]["distance_m"] == 10
        assert result["returncode"] == 0
        assert result["platform_accepted"] is None
    finally:
        controller.close()


def test_shutdown_terminates_only_owned_preview_worker(project):
    controller = Controller(project, preview_duration=60)
    controller.start("preview")
    process = controller._process
    controller.close()
    assert process.poll() is not None
    assert not controller._thread.is_alive()
    assert controller.status()["status"] == "stopped"
    with pytest.raises(ConflictError, match="shut down"):
        controller.start("preview")
