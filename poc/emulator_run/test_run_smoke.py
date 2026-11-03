"""
刷跑启动链路冒烟测试（回归防漏）

stub 掉 MuMuController / GPSSimulator / 磁盘扫描 / os.system，
让 run() 真正跑到“启动跑步软件”那一步并走完一轮主循环，
用来接住“pkgs 未定义 / 漏调用 launch_app”这一类回归。
"""

from pathlib import Path
from types import SimpleNamespace
from itertools import count

import main


def test_run_launches_run_app(tmp_path, monkeypatch):
    calls = []

    class FakeMuMu:
        def __init__(self, emu_dir, player_path, cfg):
            self.adb = str(emu_dir / "adb.exe")
            self.adb_addr = "127.0.0.1:16384"
            self.cfg = cfg

        def start_player(self):
            calls.append("start_player")

        def adb_connect(self):
            calls.append("adb_connect")
            return True

        def installed_pkgs(self):
            calls.append("installed_pkgs")
            return {"com.lptiyu.tanke", "com.tencent.mobileqq"}

        def launch_app(self, pkg):
            calls.append(("launch_app", pkg))

        def launch_instance(self):
            calls.append("launch_instance")

        def force_stop(self, pkg):
            calls.append(("force_stop", pkg))
            return True

        def shutdown(self):
            calls.append("shutdown")

        def set_location(self, lon, lat):
            calls.append(("set_location", lon, lat))
            return True

        def adb_shell(self, cmd):
            calls.append(("adb_shell", cmd))
            return ""

    class FakeGPS:
        def __init__(self, cfg):
            self.cfg = cfg

        def tick_interval(self):
            return 0.1

        def current_speed(self, elapsed):
            return 4.0

        def lateral_offset(self, elapsed):
            return 0.0

        def gps_noise_ou(self, dt):
            return (0.0, 0.0)

    monkeypatch.setattr(main, "MuMuController", FakeMuMu)
    monkeypatch.setattr(main, "GPSSimulator", FakeGPS)
    monkeypatch.setattr(main, "find_emu_dir", lambda: (Path("emu"), Path("emu/MuMuPlayer.exe")))
    monkeypatch.setattr(main.os, "system", lambda x: None)
    clock = count(0, 0.1)
    monkeypatch.setattr(main, "time", SimpleNamespace(perf_counter=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.chdir(tmp_path)

    cfg = main.Config(
        walk_path=[(114.4, 30.4), (114.5, 30.5), (114.6, 30.6)],
        dist_limit_m=0.001,      # 极小，让主循环第一轮就结束
        window_delay_sec=0,      # 避免真实 sleep
        auto_start_run=False,    # 冒烟测试不点真按钮（避免 fake adb 报错）
    )
    result = main.run(cfg)

    # 关键断言：启动链路走到了，且用 launcher 意图启动了步道乐跑
    assert "adb_connect" in calls
    assert "installed_pkgs" in calls
    assert any(c[0] == "adb_shell" and "SplashActivity" in c[1] for c in calls)
    assert ("set_location", 114.4, 30.4) in calls
    assert "shutdown" not in calls
    assert not any("step_counter" in str(call) for call in calls)
    assert result["platform_accepted"] is None
    assert result["status"] == "simulation_complete"
