"""Configuration and command failures must never be reported as saved changes."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from budaolepao_cli import cli


@pytest.fixture
def config_root(tmp_path):
    folder = tmp_path / "poc" / "emulator_run"
    folder.mkdir(parents=True)
    config = {
        "walk_path": [[114.4, 30.4], [114.5, 30.4], [114.5, 30.5]],
        "base_speed_mps": 4.0,
        "speed_amp": 0.6,
        "dist_limit_m": 3000,
    }
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (folder / "config.example.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def read_config(root):
    return json.loads((root / "poc/emulator_run/config.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("args", [
    ["--speed", "nan"], ["--speed", "inf"], ["--speed", "-1"],
    ["--speed", "0"], ["--speed", "5-4"], ["--speed", "3-nan"],
    ["--speed-min", "-1"], ["--speed-max", "inf"],
    ["--speed-min", "10"], ["--distance", "nan"],
    ["--distance", "0"], ["--distance", "-20"],
    ["--speed"], ["--unknown", "1"],
])
def test_invalid_settings_preserve_config(config_root, args):
    path = config_root / "poc/emulator_run/config.json"
    before = path.read_bytes()
    assert cli._set_settings(config_root, args) == 2
    assert path.read_bytes() == before


def test_range_and_distance_round_trip(config_root):
    assert cli._set_settings(config_root, ["--speed", "4-5", "--distance", "2500"]) == 0
    config = read_config(config_root)
    assert (config["min_speed_mps"], config["max_speed_mps"], config["base_speed_mps"]) == (4, 5, 4.5)
    assert config["dist_limit_m"] == 2500
    assert len(config["walk_path"]) == 3


def test_distance_only_keeps_legacy_speed_profile(config_root):
    assert cli._set_settings(config_root, ["--distance", "2500"]) == 0
    config = read_config(config_root)
    assert config["base_speed_mps"] == 4.0
    assert config["speed_amp"] == 0.6
    assert "min_speed_mps" not in config
    assert "max_speed_mps" not in config


def test_scientific_notation_is_a_number_not_a_range(config_root):
    assert cli._set_settings(config_root, ["--speed", "1e-1"]) == 0
    config = read_config(config_root)
    assert config["min_speed_mps"] == config["max_speed_mps"] == 0.1


@pytest.mark.parametrize("old_values,args,expected", [
    ({"dist_limit_m": 0}, ["--distance", "3000"], {"dist_limit_m": 3000}),
    ({"min_speed_mps": 5, "max_speed_mps": 4}, ["--speed", "4-5"],
     {"min_speed_mps": 4, "max_speed_mps": 5}),
    ({"base_speed_mps": 0}, ["--speed", "4-5"], {"base_speed_mps": 4.5}),
    ({"min_speed_mps": 5, "max_speed_mps": 4}, ["--speed-min", "3"],
     {"min_speed_mps": 3, "max_speed_mps": 4}),
])
def test_explicit_settings_can_repair_invalid_saved_values(config_root, old_values, args, expected):
    config = read_config(config_root)
    config.update(old_values)
    path = config_root / "poc/emulator_run/config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert cli._set_settings(config_root, args) == 0
    saved = read_config(config_root)
    assert all(saved[key] == value for key, value in expected.items())


def test_settings_accept_utf8_bom(config_root):
    path = config_root / "poc/emulator_run/config.json"
    path.write_text(json.dumps(read_config(config_root)), encoding="utf-8-sig")
    assert cli._set_settings(config_root, ["--distance", "2500"]) == 0
    assert read_config(config_root)["dist_limit_m"] == 2500


@pytest.mark.parametrize("coords", [
    ["114,30"], ["114,30", "115,30"],
    ["114,30", "114,30", "115,31"],
    ["114,30", "115,30", "115,31", "114,30"],
    ["114,30", "181,30", "115,31"],
    ["114,30", "115,90", "115,31"],
    ["114,30", "115,nan", "115,31"],
    ["114,30", "115,30,99", "115,31"],
    ["170,30", "175,31", "-170,30"],
])
def test_invalid_route_preserves_config(config_root, coords):
    path = config_root / "poc/emulator_run/config.json"
    before = path.read_bytes()
    assert cli._set_route(config_root, coords) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("contents", ["{", "[]", '{"other": NaN}'])
def test_malformed_config_is_not_overwritten(config_root, contents):
    path = config_root / "poc/emulator_run/config.json"
    path.write_text(contents, encoding="utf-8")
    assert cli._set_settings(config_root, ["--speed", "4-5"]) == 2
    assert path.read_text(encoding="utf-8") == contents


def test_configuration_failure_becomes_nonzero_exit(config_root, monkeypatch):
    monkeypatch.setattr(cli, "__file__", str(config_root / "budaolepao_cli/cli.py"))
    monkeypatch.setattr(sys, "argv", ["lepao", "config", "set", "--distance", "nan"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_failed_atomic_replace_preserves_existing_config(config_root, monkeypatch):
    folder = config_root / "poc/emulator_run"
    before = (folder / "config.json").read_bytes()
    original_files = set(folder.iterdir())

    def fail_replace(self, target):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert cli._set_settings(config_root, ["--distance", "2000"]) == 2
    assert (folder / "config.json").read_bytes() == before
    assert set(folder.iterdir()) == original_files


def test_failed_install_does_not_print_success(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["lepao", "setup"])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 7
    output = capsys.readouterr()
    assert "Done" not in output.out
    assert "failed" in output.err


def test_map_propagates_child_failure(config_root, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=2))
    assert cli._config(config_root, ["map"]) == 2


def test_panel_does_not_report_failed_save(monkeypatch):
    from budaolepao_cli import panel

    answers = iter(["nan", "3000", ""])
    messages = []
    monkeypatch.setattr(panel.Prompt, "ask", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(panel, "_spawn", lambda args: 2)
    monkeypatch.setattr(panel.console, "print", lambda text: messages.append(text))
    panel._config_set()
    assert any("未保存" in text for text in messages)
    assert not any("已保存" in text for text in messages)


@pytest.mark.parametrize("returncode", [0, 2])
def test_panel_waits_after_run_before_clearing_result(monkeypatch, returncode):
    from budaolepao_cli import panel

    events = []
    answers = iter(["1", "", "q"])

    def ask(prompt, **kwargs):
        events.append(prompt)
        return next(answers)

    monkeypatch.setattr(panel.Prompt, "ask", ask)
    monkeypatch.setattr(panel, "_spawn", lambda args: events.append("run") or returncode)
    monkeypatch.setattr(panel.console, "print", lambda text: events.append(str(text)))
    monkeypatch.setattr(panel.console, "clear", lambda: events.append("clear"))
    panel.main()
    run_index = events.index("run")
    wait_index = events.index("按回车返回", run_index)
    next_clear_index = events.index("clear", run_index)
    assert run_index < wait_index < next_clear_index
    assert any("模拟已停止" in text for text in events) == (returncode != 0)


@pytest.fixture
def picker():
    path = Path(__file__).resolve().parents[1] / "poc/emulator_run/picker.py"
    spec = importlib.util.spec_from_file_location("route_picker_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("clipboard,expected", [
    ("114.4,30.4 114.5,30.4 114.5,30.5", 0),
    ("114.4,30.4", 2),
    ("no coordinates", 2),
])
def test_picker_validates_and_never_overwrites_example(config_root, picker, monkeypatch, clipboard, expected):
    folder = config_root / "poc/emulator_run"
    (folder / "config.json").unlink()
    before = (folder / "config.example.json").read_bytes()
    monkeypatch.setattr(picker, "__file__", str(folder / "picker.py"))
    monkeypatch.setattr(picker.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(picker, "get_clipboard", lambda: clipboard)
    answers = iter(["", "done"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    assert picker.main() == expected
    assert (folder / "config.example.json").read_bytes() == before
    assert (folder / "config.json").exists() == (expected == 0)


def test_picker_supports_signed_coordinates_and_rejects_out_of_range(picker):
    assert picker.parse_coords("-73.5,40.2 +10.2,-20.5 181,30 114,90") == [[-73.5, 40.2], [10.2, -20.5]]
