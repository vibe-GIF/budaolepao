"""Offline regressions: no emulator, account, network, or real ADB commands."""

import json
import subprocess
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import main


@pytest.mark.parametrize("updates", [
    {"dist_limit_m": 0}, {"dist_limit_m": -1}, {"dist_limit_m": True},
    {"base_speed_mps": float("nan")}, {"speed_cycle_sec": 0},
    {"lateral_cycle_sec": -1}, {"speed_amp": 6},
    {"min_speed_mps": 4}, {"min_speed_mps": 5, "max_speed_mps": 4},
    {"min_speed_mps": 0, "max_speed_mps": 4},
    {"min_speed_mps": 1, "max_speed_mps": float("inf")},
    {"click_threshold": 2}, {"window_delay_sec": -1},
    {"auto_start_run": "false"}, {"allow_coordinate_fallback": 1},
    {"walk_path": []}, {"walk_path": [[1, 2], [2, 3]]},
    {"walk_path": [[1, 2], [1, 2], [2, 3]]},
    {"walk_path": [[1, 2], [2, 3], [3, 4], [1, 2]]},
    {"walk_path": [[1, 90], [2, 3], [3, 4]]},
    {"walk_path": [[181, 2], [2, 3], [3, 4]]},
    {"walk_path": [[float("nan"), 2], [2, 3], [3, 4]]},
    {"walk_path": [[179, 2], [-179, 3], [178, 4]]},
    {"walk_path": [None, [2, 3], [3, 4]]},
])
def test_invalid_config_rejected_before_emulator_action(updates, monkeypatch):
    def unexpected():
        pytest.fail("Invalid config must not reach emulator discovery")
    monkeypatch.setattr(main, "find_emu_dir", unexpected)
    with pytest.raises(ValueError):
        main.run(main.Config(**updates))


@pytest.mark.parametrize("content", ["{invalid", "[]", '{"dist_limit_m": NaN}', '{"dist_limt_m": 2}'])
def test_invalid_config_file_is_not_silently_ignored(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        main.load_config(path)


def test_missing_config_requires_explicit_configuration(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.json"):
        main.load_config(tmp_path / "missing.json")


def test_load_config_preserves_cli_speed_range(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"min_speed_mps": 4, "max_speed_mps": 5}), encoding="utf-8-sig")
    cfg = main.load_config(path)
    gps = main.GPSSimulator(cfg)
    speeds = [gps.current_speed(i * cfg.speed_cycle_sec / 100) for i in range(100)]
    assert min(speeds) == pytest.approx(4)
    assert max(speeds) == pytest.approx(5)


def test_fixed_speed_and_legacy_amplitude():
    fixed = main.GPSSimulator(main.Config(min_speed_mps=4, max_speed_mps=4))
    assert [fixed.current_speed(t) for t in (0, 5, 10, 25)] == [4] * 4
    legacy = main.GPSSimulator(main.Config(base_speed_mps=4, speed_amp=0.5, speed_cycle_sec=20))
    assert legacy.current_speed(5) == pytest.approx(4.5)
    assert legacy.current_speed(15) == pytest.approx(3.5)


def test_extreme_finite_speed_bounds_do_not_overflow():
    cfg = main.Config(min_speed_mps=1e308, max_speed_mps=1e308)
    main.validate_config(cfg)
    gps = main.GPSSimulator(cfg)
    assert [gps.current_speed(t) for t in (0, 5, 10, 25)] == [1e308] * 4
    with pytest.raises(ValueError, match="must remain finite"):
        main.validate_config(main.Config(base_speed_mps=1.5e308, speed_amp=1e308))


def test_geographic_distance_scales_longitude_by_latitude():
    assert main.geo_dist_m(60, 10, 60, 10.01) == pytest.approx(556.6)
    assert main.geo_dist_m(0, 10, 0, 10.01) == pytest.approx(1113.2)
    assert main.geo_dist_m(60, 10, 60.01, 10) == pytest.approx(1113.2)


def test_missing_template_preflight_happens_before_emulator_discovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "find_emu_dir", lambda: pytest.fail("Emulator must not start"))
    with pytest.raises(ValueError, match="button templates"):
        main.run(main.Config())


def test_button_miss_stops_without_clicking_coordinates():
    ui = SimpleNamespace(click_icon=lambda *a, **k: False,
                         tap=lambda *a: pytest.fail("Unexpected coordinate tap"))
    with pytest.raises(RuntimeError, match="not matched"):
        main._click_btn(ui, main.Config(), "missing.png", 10, 20)


def test_coordinate_fallback_requires_opt_in():
    taps = []
    ui = SimpleNamespace(click_icon=lambda *a, **k: False, tap=lambda *a: taps.append(a))
    main._click_btn(ui, main.Config(allow_coordinate_fallback=True), "missing.png", 10, 20)
    assert taps == [(10, 20)]


def test_failed_adb_click_is_not_retried_using_coordinates():
    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "adb input tap")
    ui = SimpleNamespace(click_icon=failed,
                         tap=lambda *a: pytest.fail("Failed click must not be retried"))
    with pytest.raises(subprocess.CalledProcessError):
        main._click_btn(ui, main.Config(allow_coordinate_fallback=True), "button.png", 10, 20)


def test_oversized_image_template_is_a_miss(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    template = tmp_path / "button.png"
    template.write_bytes(b"stub")
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(main.cv2, "imread", lambda p: np.zeros((20, 20, 3), dtype=np.uint8)
                        if p == str(template) else np.zeros((10, 10, 3), dtype=np.uint8))
    assert not main.UIController(Path("adb"), "fake").click_icon(str(template))


def test_adb_connection_error_with_zero_exit_is_not_ready(monkeypatch):
    mu = main.MuMuController(Path("emu"), Path("player"), main.Config())
    monkeypatch.setattr(mu, "get_adb_addr", lambda: "127.0.0.1:1234")
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout="cannot connect to 127.0.0.1:1234"))
    assert not mu.adb_connect()


def test_android_launch_error_on_stderr_is_not_success(monkeypatch):
    mu = main.MuMuController(Path("emu"), Path("player"), main.Config())
    mu.adb_addr = "fake"
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout="", stderr="Error: Activity class does not exist"))
    assert mu.adb_shell("am start -n invalid/Activity") is None


@pytest.fixture
def fake_runner(monkeypatch, tmp_path):
    calls = []
    faults = {"missing_app": False, "launch": False, "location": 0, "stop": False}

    class FakeMuMu:
        def __init__(self, *args):
            self.adb, self.adb_addr = Path("fake-adb"), "fake"
            self.locations = 0
        def start_player(self):
            calls.append("start_player")
        def adb_connect(self):
            return True
        def installed_pkgs(self):
            return {"com.tencent.mm"} if faults["missing_app"] else {"com.lptiyu.tanke"}
        def force_stop(self, pkg):
            return not faults["stop"]
        def adb_shell(self, cmd):
            calls.append(cmd)
            return None if faults["launch"] else ""
        def set_location(self, *coords):
            self.locations += 1
            calls.append(("location", coords))
            return self.locations != faults["location"]
        def shutdown(self):
            pytest.fail("Leave the emulator open for inspection")

    clock = count(0, 0.1)
    monkeypatch.setattr(main, "MuMuController", FakeMuMu)
    monkeypatch.setattr(main, "find_emu_dir", lambda: (Path("emu"), Path("player")))
    monkeypatch.setattr(main, "time", SimpleNamespace(perf_counter=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.setattr(main.os, "system", lambda _: None)
    monkeypatch.chdir(tmp_path)
    cfg = main.Config(auto_start_run=False, dist_limit_m=3, window_delay_sec=0,
                      min_speed_mps=4, max_speed_mps=4)
    return cfg, calls, faults


@pytest.mark.parametrize("fault, value, message", [
    ("missing_app", True, "not installed"), ("launch", True, "Could not launch"),
    ("stop", True, "Could not stop"), ("location", 1, "initial emulator location"),
    ("location", 2, "location update failed"),
])
def test_required_action_failure_stops_without_success(fake_runner, fault, value, message, capsys):
    cfg, calls, faults = fake_runner
    faults[fault] = value
    with pytest.raises(RuntimeError, match=message):
        main.run(cfg)
    assert "Simulation complete" not in capsys.readouterr().out


def test_completion_is_unverified_and_steps_retain_fractions(fake_runner, capsys):
    cfg, calls, _ = fake_runner
    result = main.run(cfg)
    assert result["status"] == "simulation_complete"
    assert result["platform_accepted"] is None
    assert result["simulated_steps"] > 0
    assert not any("step_counter" in str(call) for call in calls)
    assert "platform acceptance is unverified" in capsys.readouterr().out
