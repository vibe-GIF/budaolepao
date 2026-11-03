"""
步道乐跑 电脑端模拟器 PoC（改进版）
======================================
基于 ranfey/RunInMumu 重构，聚焦 MuMuManager 实际支持的 API。

改进项：
  1. Ornstein-Uhlenbeck 有色噪声 → 真实 GPS 漂移特性
  2. 正弦波速度轮廓 → 平滑加速度变化
  3. 对数正态时间抖动 → 非固定上报间隔
  4. 路径横向漂移 → 自然左右摆动
  5. 动态精度 → 坐标小数位随机截断模拟精度变化
  6. 步频日志 → 记录步频-速度关联数据供后续分析

仅限授权沙箱测试账号使用，禁止外传为通用刷分工具。
"""

import io
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Optional, Tuple

# stdio 包装移到 __main__（避免被导入/测试时劫持）

import cv2
import numpy as np
from prettytable import PrettyTable

# ============================================================
#  配置（支持从 JSON 文件覆盖）
# ============================================================

@dataclass
class Config:
    walk_path: List[Tuple[float, float]] = field(default_factory=lambda: [
        (106.573302, 29.508911),
        (106.574330, 29.509245),
        (106.575602, 29.508467),
        (106.574259, 29.508012),
        (106.571092, 29.508342),
        (106.573513, 29.508640),
    ])
    dist_limit_m: float = 16000.0
    base_speed_mps: float = 4.5
    min_speed_mps: Optional[float] = None
    max_speed_mps: Optional[float] = None
    speed_amp: float = 0.8
    speed_cycle_sec: float = 30.0
    jitter_radius_m: float = 2.0
    ou_theta: float = 0.8
    ou_sigma: float = 1.5
    tick_base_sec: float = 0.40
    tick_log_mu: float = -1.0
    tick_log_sigma: float = 0.25
    lateral_amp: float = 1.5
    lateral_cycle_sec: float = 15.0
    tap_delay_sec: float = 1.0
    window_delay_sec: float = 15.0
    instance_index: int = 0
    run_pkg: str = "com.lptiyu.tanke"
    auto_start_run: bool = True
    allow_coordinate_fallback: bool = False
    # 旧版 900x1600 界面的坐标；仅在明确启用回退时使用。
    click_threshold: float = 0.7
    # 图片识别模板（img/）；默认识别失败即停止。
    tpl_begin: str = "img/begin_run.png"
    tpl_free: str = "img/free_run.png"
    tpl_pause: str = "img/pause_run.png"
    tpl_end: str = "img/end_run.png"
    tpl_confirm_end: str = "img/confirm_end.png"
    begin_x: int = 454; begin_y: int = 788       # 开始乐跑(兜底坐标)
    free_x: int = 600;  free_y: int = 985        # 自由跑确认(兜底)
    pause_x: int = 450; pause_y: int = 1500      # 长按暂停(兜底)
    end_x: int = 659;   end_y: int = 1520        # 结束(红,兜底)
    confirm_x: int = 575; confirm_y: int = 936   # 确认结束(绿,兜底)


def validate_config(cfg: Config) -> None:
    """Reject invalid input before any emulator or application action."""
    def number(name, value, minimum=None, strict=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if minimum is not None and (value <= minimum if strict else value < minimum):
            relation = ">" if strict else ">="
            raise ValueError(f"{name} must be {relation} {minimum}")

    for name in ("dist_limit_m", "base_speed_mps", "speed_cycle_sec", "lateral_cycle_sec", "tick_base_sec"):
        number(name, getattr(cfg, name), 0, strict=True)
    for name in ("speed_amp", "jitter_radius_m", "ou_theta", "ou_sigma", "tick_log_sigma",
                 "lateral_amp", "tap_delay_sec", "window_delay_sec"):
        number(name, getattr(cfg, name), 0)
    number("tick_log_mu", cfg.tick_log_mu)
    if (cfg.min_speed_mps is None) != (cfg.max_speed_mps is None):
        raise ValueError("min_speed_mps and max_speed_mps must be set together")
    if cfg.min_speed_mps is not None:
        number("min_speed_mps", cfg.min_speed_mps, 0, strict=True)
        number("max_speed_mps", cfg.max_speed_mps, 0, strict=True)
        if cfg.min_speed_mps > cfg.max_speed_mps:
            raise ValueError("min_speed_mps must not exceed max_speed_mps")
    else:
        if cfg.base_speed_mps <= cfg.speed_amp:
            raise ValueError("base_speed_mps must exceed speed_amp")
        if not math.isfinite(cfg.base_speed_mps + cfg.speed_amp):
            raise ValueError("base_speed_mps + speed_amp must remain finite")
    number("click_threshold", cfg.click_threshold, 0, strict=True)
    if cfg.click_threshold > 1:
        raise ValueError("click_threshold must not exceed 1")
    if type(cfg.instance_index) is not int or cfg.instance_index < 0:
        raise ValueError("instance_index must be a nonnegative integer")
    for name in ("auto_start_run", "allow_coordinate_fallback"):
        if type(getattr(cfg, name)) is not bool:
            raise ValueError(f"{name} must be a boolean")
    if not isinstance(cfg.run_pkg, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", cfg.run_pkg):
        raise ValueError("run_pkg must be an Android package identifier")
    for name in ("tpl_begin", "tpl_free", "tpl_pause", "tpl_end", "tpl_confirm_end"):
        if not isinstance(getattr(cfg, name), str) or not getattr(cfg, name):
            raise ValueError(f"{name} must be a nonempty path")
    for name in ("begin_x", "begin_y", "free_x", "free_y", "pause_x", "pause_y",
                 "end_x", "end_y", "confirm_x", "confirm_y"):
        if type(getattr(cfg, name)) is not int or getattr(cfg, name) < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if not isinstance(cfg.walk_path, (list, tuple)) or len(cfg.walk_path) < 3:
        raise ValueError("walk_path requires at least 3 distinct points")
    points = []
    for i, point in enumerate(cfg.walk_path):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"walk_path[{i}] must be [longitude, latitude]")
        lon, lat = point
        number(f"walk_path[{i}] longitude", lon)
        number(f"walk_path[{i}] latitude", lat)
        if not -180 <= lon <= 180 or not -90 < lat < 90:
            raise ValueError(f"walk_path[{i}] coordinates out of range (poles are unsupported)")
        points.append((lon, lat))
    if len(set(points)) < 3:
        raise ValueError("walk_path requires at least 3 distinct points")
    for a, b in zip(points, points[1:] + points[:1]):
        if a == b:
            raise ValueError("walk_path has a zero-length segment; do not repeat the first point at the end")
        if abs(a[0] - b[0]) > 180:
            raise ValueError("walk_path crossing the antimeridian is unsupported")


def load_config(path: str = "config.json") -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Config not found: {p}. Copy and edit config.example.json first.")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid config {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object")
    unknown = set(data) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"Unknown config field(s): {', '.join(sorted(unknown))}")
    cfg = Config(**data)
    validate_config(cfg)
    return cfg


# ============================================================
#  工具函数
# ============================================================

CLR_A = "\x1b[01;38;5;117m"
CLR_P = "\x1b[01;38;5;153m"
CLR_C = "\x1b[01;38;5;123m"
HEART = "\x1b[01;38;5;195m"
CLR_RST = "\x1b[0m"


def find_emu_dir() -> Tuple[Path, Path]:
    cfg = Path("emu_path.json")
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            mgr_dir = Path(data["emu_dir"])
            if mgr_dir.joinpath("MuMuManager.exe").is_file():
                player = Path(data.get("player_path", str(mgr_dir / "MuMuPlayer.exe")))
                return mgr_dir, player
        except Exception:
            pass
    result = _scan_common_paths() or _scan_all_drives()
    if result:
        mgr_dir, player = result
        cfg.write_text(json.dumps({
            "emu_dir": str(mgr_dir),
            "player_path": str(player),
        }), encoding="utf-8")
        return mgr_dir, player
    sys.exit(f"{CLR_A}x MuMu not found. Make sure MuMu Player is installed.{CLR_RST}")


def _scan_common_paths() -> Tuple[Path, Path]:
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for root in [
            f"{d}:\\Program Files\\NetEase",
            f"{d}:\\Program Files\\Netease",
            f"{d}:\\Program Files (x86)\\NetEase",
            f"{d}:\\Program Files (x86)\\Netease",
        ]:
            base = Path(root)
            if base.exists():
                for p in base.rglob("MuMuManager.exe"):
                    player = _find_player(p.parent)
                    if player:
                        return p.parent, player
    return None


def _scan_all_drives() -> Tuple[Path, Path]:
    print(f"{CLR_P}Scanning for MuMu (this may take a moment)...{CLR_RST}")
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{d}:\\")
        if not drive.exists():
            continue
        search_dirs = [
            drive / "Program Files",
            drive / "Program Files (x86)",
            drive / "ProgramData",
        ]
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for vendor in ["NetEase", "Netease"]:
                vendor_dir = search_dir / vendor
                if vendor_dir.exists():
                    try:
                        for p in vendor_dir.rglob("MuMuManager.exe"):
                            if p.is_file():
                                player = _find_player(p.parent)
                                if player:
                                    print(f"{CLR_C}Found MuMu at {p.parent}{CLR_RST}")
                                    return p.parent, player
                    except (PermissionError, OSError):
                        continue
    return None


def _find_player(mgr_dir: Path) -> Path:
    candidates = [
        mgr_dir / "MuMuPlayer.exe",
        mgr_dir / "MuMuPlayer-12.0.exe",
        mgr_dir.parent / "MuMuPlayer.exe",
        mgr_dir.parent / "MuMuPlayer-12.0.exe",
        mgr_dir.parent.parent / "MuMuPlayer.exe",
        mgr_dir.parent / "GameViewer.exe",
        mgr_dir.parent / "GameViewerLauncher.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c
    for p in mgr_dir.parent.rglob("MuMuPlayer*.exe"):
        return p
    for p in mgr_dir.parent.rglob("GameViewer*.exe"):
        return p
    return None


def meter_to_deg(lat: float, dx: float, dy: float) -> Tuple[float, float]:
    d_lat = dy / 111_320
    d_lon = dx / (111_320 * math.cos(math.radians(lat)))
    return d_lat, d_lon


def geo_dist_m(lat1, lon1, lat2, lon2) -> float:
    avg_lat = math.radians((lat1 + lat2) / 2.0)
    d_lon = (lon2 - lon1 + 180) % 360 - 180
    return math.hypot(lat2 - lat1, d_lon * math.cos(avg_lat)) * 111_320


# ============================================================
#  改进核心：多维度 GPS 仿真增强
# ============================================================


class GPSSimulator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ou_x = 0.0
        self._ou_y = 0.0

    def gps_noise_ou(self, dt: float) -> Tuple[float, float]:
        self._ou_x += self.cfg.ou_theta * (-self._ou_x) * dt \
                      + self.cfg.ou_sigma * math.sqrt(dt) * random.gauss(0, 1)
        self._ou_y += self.cfg.ou_theta * (-self._ou_y) * dt \
                      + self.cfg.ou_sigma * math.sqrt(dt) * random.gauss(0, 1)
        return self._ou_x * self.cfg.jitter_radius_m, self._ou_y * self.cfg.jitter_radius_m

    def tick_interval(self) -> float:
        return max(0.1, random.lognormvariate(self.cfg.tick_log_mu, self.cfg.tick_log_sigma))

    def current_speed(self, elapsed: float) -> float:
        phase = (elapsed % self.cfg.speed_cycle_sec) / self.cfg.speed_cycle_sec * 2 * math.pi
        if self.cfg.min_speed_mps is not None:
            lo, hi = self.cfg.min_speed_mps, self.cfg.max_speed_mps
            return lo / 2 + hi / 2 + (hi - lo) / 2 * math.sin(phase)
        return self.cfg.base_speed_mps + self.cfg.speed_amp * math.sin(phase)

    def lateral_offset(self, elapsed: float) -> float:
        phase = elapsed * 2 * math.pi / self.cfg.lateral_cycle_sec
        return self.cfg.lateral_amp * math.sin(phase)


# ============================================================
#  MuMuManager 通信
# ============================================================

class MuMuController:
    def __init__(self, emu_dir: Path, player_path: Path, cfg: Config):
        self.mgr = emu_dir / "MuMuManager.exe"
        self.player = player_path
        self.adb = emu_dir / "adb.exe"
        self.instance = cfg.instance_index
        self.adb_addr: Optional[str] = None

    def run(self, args: List[str], check: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
        cmd = [str(self.mgr)] + args
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=check)

    def set_location(self, lon: float, lat: float) -> bool:
        r = self.run([
            "control", "-v", str(self.instance),
            "tool", "location",
            "-lon", f"{lon:.6f}",
            "-lat", f"{lat:.6f}",
        ], check=False)
        return r.returncode == 0

    def launch_app(self, pkg: str) -> bool:
        r = self.run([
            "control", "-v", str(self.instance),
            "app", "launch", "-pkg", pkg,
        ], check=False)
        return r.returncode == 0

    def force_stop(self, pkg: str) -> bool:
        """强制结束 App 进程，确保下次启动从头（主页）开始，避免恢复上次的残留页。"""
        return self.adb_shell(f"am force-stop {pkg}") is not None

    def shutdown(self):
        """关闭指定 MuMu 实例（清状态，再从头启动）。"""
        try:
            self.run(["control", "-v", str(self.instance), "shutdown"], check=False, timeout=15)
        except Exception:
            pass

    def installed_pkgs(self) -> set:
        r = self.run(["control", "-v", str(self.instance), "app", "info", "-i"], check=False)
        if r.returncode != 0:
            return set()
        try:
            return set(json.loads(r.stdout))
        except Exception:
            return set()

    def get_adb_addr(self) -> Optional[str]:
        r = self.run(["info", "-v", str(self.instance)], check=False)
        if r.returncode != 0:
            return None
        try:
            info = json.loads(r.stdout)
            return f"{info['adb_host_ip']}:{info['adb_port']}"
        except Exception:
            return None

    def start_player(self):
        if self.player.is_file():
            # GUI player must not keep a controller worker's stdout pipe open.
            subprocess.Popen([str(self.player)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(f"{CLR_P}MuMuPlayer not found at {self.player}, assuming already running{CLR_RST}")

    def adb_shell(self, cmd: str) -> Optional[str]:
        if not self.adb_addr:
            return None
        try:
            r = subprocess.run(
                [str(self.adb), "-s", self.adb_addr, "shell"] + cmd.split(),
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
            )
            output = (r.stdout + "\n" + r.stderr).lower()
            if r.returncode != 0 or "error:" in output or "exception" in output:
                return None
            return r.stdout.strip()
        except Exception:
            return None

    def adb_connect(self) -> bool:
        addr = self.get_adb_addr()
        if not addr:
            return False
        self.adb_addr = addr
        r = subprocess.run(
            [str(self.adb), "connect", addr],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "connected to" in r.stdout.lower()


# ============================================================
#  UI 控制（图像模板匹配）
# ============================================================

class UIController:
    def __init__(self, adb_path: Path, adb_addr: str):
        self.adb = adb_path
        self.addr = adb_addr

    def click_icon(self, icon_png: str, threshold: float = 0.75,
                   offset: Tuple[int, int] = (0, 0), long_press: bool = False) -> bool:
        if not Path(icon_png).is_file():
            return False
        icon = cv2.imread(icon_png)
        if icon is None:
            return False
        screen_png = Path("screen.png")
        with screen_png.open("wb") as fp:
            subprocess.run(
                [str(self.adb), "-s", self.addr, "exec-out", "screencap", "-p"],
                stdout=fp, timeout=10, check=True,
            )
        screen = cv2.imread(str(screen_png))
        if screen is None:
            raise RuntimeError("ADB screenshot could not be decoded")
        if icon.shape[0] > screen.shape[0] or icon.shape[1] > screen.shape[1]:
            return False
        res = cv2.matchTemplate(screen, icon, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if not math.isfinite(score) or score < threshold:
            return False
        x = loc[0] + icon.shape[1] // 2 + offset[0]
        y = loc[1] + icon.shape[0] // 2 + offset[1]
        if long_press:
            self.longpress(x, y)
        else:
            self.tap(x, y)
        return True

    def tap(self, x: int, y: int):
        subprocess.run(
            [str(self.adb), "-s", self.addr, "shell", "input", "tap",
             str(x), str(y)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=True,
        )

    def longpress(self, x: int, y: int):
        subprocess.run(
            [str(self.adb), "-s", self.addr, "shell", "input", "swipe",
             str(x), str(y), str(x), str(y), "2500"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=True,
        )

    def back(self):
        subprocess.run(
            [str(self.adb), "-s", self.addr, "shell", "input", "keyevent", "4"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=True,
        )


# ============================================================
#  主运行逻辑
# ============================================================

def run(cfg: Config, *, event_callback=None, quiet: bool = False):
    validate_config(cfg)
    if cfg.auto_start_run and not cfg.allow_coordinate_fallback:
        missing = [path for path in (cfg.tpl_begin, cfg.tpl_free, cfg.tpl_pause,
                                     cfg.tpl_end, cfg.tpl_confirm_end)
                   if not Path(path).is_file() or cv2.imread(path) is None]
        if missing:
            raise ValueError("Missing or unreadable button templates: " + ", ".join(missing)
                             + ". Capture templates for the current app before running.")
    emu_dir, player_path = find_emu_dir()
    mu = MuMuController(emu_dir, player_path, cfg)
    gps = GPSSimulator(cfg)

    # 启动模拟器（ADB 就绪作为探测，等不齐自动重试启动，最可靠）
    # 注：不在启动前 shutdown —— 脚本自身 shutdown 后重启 MuMu 不可靠；
    #    干净主页由 force-stop + am start SplashActivity 保证。
    mu.start_player()
    print(f"{CLR_P}Waiting for MuMu to start...{CLR_RST}")
    ready = False
    for attempt in range(3):
        for _ in range(45):
            if mu.adb_connect():
                ready = True
                break
            time.sleep(2)
        if ready:
            break
        print(f"{CLR_P}MuMu not ready, retrying launch...{CLR_RST}")
        mu.start_player()

    if not ready:
        raise RuntimeError("Timed out waiting for MuMu")
    print(f"{CLR_C}ADB connected: {mu.adb_addr}{CLR_RST}")

    # 取已安装应用列表（识别跑步软件）
    pkgs = mu.installed_pkgs()

    ui = UIController(mu.adb, mu.adb_addr)

    if cfg.run_pkg not in pkgs:
        raise RuntimeError(f"Configured app is not installed: {cfg.run_pkg}")
    if not mu.force_stop(cfg.run_pkg):
        raise RuntimeError(f"Could not stop app: {cfg.run_pkg}")
    time.sleep(1.0)
    if mu.adb_shell(f"am start -n {cfg.run_pkg}/.activities.splash.SplashActivity") is None:
        raise RuntimeError(f"Could not launch app: {cfg.run_pkg}")
    print(f"{CLR_C}Launched run app: {cfg.run_pkg}{CLR_RST}")
    time.sleep(cfg.window_delay_sec)

    # 定位到起点
    start_lon, start_lat = cfg.walk_path[0]
    if not mu.set_location(start_lon, start_lat):
        raise RuntimeError("Could not set the initial emulator location")
    print(f"{CLR_C}Initial position set{CLR_RST}")

    # 起点设置成功后才发送开始操作。
    _auto_start_run(ui, cfg)

    # 主循环：模拟行走
    idx, seg_dist, total_dist = 0, 0.0, 0.0
    t_start = t_prev = time.perf_counter()
    frame = 0
    simulated_steps = 0.0
    route = cfg.walk_path

    while True:
        now = time.perf_counter()
        tick = gps.tick_interval()
        wait = tick - (now - t_prev)
        if wait > 0:
            time.sleep(wait)
            now = time.perf_counter()
        dt = now - t_prev
        t_prev = now
        elapsed = now - t_start

        lon1, lat1 = route[idx]
        lon2, lat2 = route[(idx + 1) % len(route)]
        seg_len = geo_dist_m(lat1, lon1, lat2, lon2)

        speed = gps.current_speed(elapsed)
        move = speed * dt
        seg_dist += move
        total_dist += move

        while seg_dist >= seg_len and seg_len > 0:
            seg_dist -= seg_len
            idx = (idx + 1) % len(route)
            lon1, lat1 = route[idx]
            lon2, lat2 = route[(idx + 1) % len(route)]
            seg_len = geo_dist_m(lat1, lon1, lat2, lon2)

        ratio = seg_dist / seg_len if seg_len > 0 else 0

        lat_off = gps.lateral_offset(elapsed) / 111_320
        lon_off = gps.lateral_offset(elapsed + 3.0) \
                  / (111_320 * math.cos(math.radians(lat1)))

        base_lon = lon1 + (lon2 - lon1) * ratio + lon_off
        base_lat = lat1 + (lat2 - lat1) * ratio + lat_off

        dx, dy = gps.gps_noise_ou(dt)
        d_lat, d_lon = meter_to_deg(base_lat, dx, dy)
        final_lon = base_lon + d_lon
        final_lat = base_lat + d_lat

        if not mu.set_location(final_lon, final_lat):
            raise RuntimeError("Emulator location update failed; simulation stopped")

        # 步频模拟
        step_hz = 1.2 + (speed - 1.0) / 4.0 * 1.6
        step_hz = max(1.0, min(3.0, step_hz))
        # 仅本地诊断数据；没有生成或注入 Android 传感器事件。
        simulated_steps += step_hz * dt

        frame += 1

        tbl = PrettyTable(["时间", "模拟速度", "模拟路程", "模拟步频", "帧数"])
        tbl.add_row([
            f"{elapsed:7.1f}s",
            f"{speed:5.2f}m/s",
            f"{total_dist:7.1f}m",
            f"{step_hz:5.2f}Hz",
            f"{frame}",
        ])
        if event_callback:
            event_callback({"type": "progress", "progress": {
                "distance_m": total_dist, "speed_mps": speed, "elapsed_s": elapsed,
                "steps": int(simulated_steps), "lon": final_lon, "lat": final_lat,
            }})
        if not quiet:
            os.system("cls" if os.name == "nt" else "clear")
            print(f"{HEART}Budao Lepao Simulator (Enhanced){CLR_RST}")
            print(tbl)
            print(f"  Simulated steps (diagnostic only): {int(simulated_steps)}"
                  f"  |  OU noise: ({dx:+.2f}, {dy:+.2f})m")

        if total_dist >= cfg.dist_limit_m:
            print(f"{CLR_A}Local simulated distance reached.{CLR_RST}")
            # 自动结束（长按暂停 → 结束 → 确认结束）
            _auto_finish_run(ui, cfg)
            break

    print(f"{CLR_P}Simulation complete; platform acceptance is unverified. "
          f"MuMu remains open. Check the app's final record manually.{CLR_RST}")
    if not cfg.auto_start_run:
        print("Automatic finish is disabled. End any active session in the app manually.")
    return {"status": "simulation_complete", "simulated_distance_m": total_dist,
            "simulated_steps": int(simulated_steps), "platform_accepted": None}


def _click_btn(ui: "UIController", cfg: Config, tpl: str, x: int, y: int, long_press: bool = False):
    """Only use legacy coordinates when explicitly enabled in configuration."""
    if ui.click_icon(tpl, threshold=cfg.click_threshold, long_press=long_press):
        print(f"[auto] {tpl}: image-match ok")
        return True
    if not cfg.allow_coordinate_fallback:
        raise RuntimeError(f"Button template not matched: {tpl}. Stopped without coordinate fallback.")
    print(f"[auto] {tpl}: explicitly enabled coordinate fallback ({x},{y})")
    if long_press:
        ui.longpress(x, y)
    else:
        ui.tap(x, y)
    return True


def _auto_start_run(ui: "UIController", cfg: Config):
    """自动点击进入跑步：开始乐跑 → 自由跑确认 → 等 3-2-1 倒计时。（图片识别优先）"""
    if not cfg.auto_start_run:
        return
    # 先把 App 退回到主页（启动可能恢复在上次的成绩/运行页；已在家则仅弹提示，不退出）
    ui.back()
    time.sleep(0.8)
    _click_btn(ui, cfg, cfg.tpl_begin, cfg.begin_x, cfg.begin_y)
    time.sleep(1.8)
    _click_btn(ui, cfg, cfg.tpl_free, cfg.free_x, cfg.free_y)
    time.sleep(6.0)  # 等 3-2-1 倒计时
    print(f"{CLR_C}[auto] start clicks sent; app state is not verified{CLR_RST}")


def _auto_finish_run(ui: "UIController", cfg: Config):
    """跑到里程后自动结束：长按暂停 → 结束 → 确认结束。（图片识别优先）"""
    if not cfg.auto_start_run:
        return
    print(f"{CLR_C}[auto] 结束流程...{CLR_RST}")
    _click_btn(ui, cfg, cfg.tpl_pause, cfg.pause_x, cfg.pause_y, long_press=True)
    time.sleep(1.8)
    _click_btn(ui, cfg, cfg.tpl_end, cfg.end_x, cfg.end_y)
    time.sleep(1.8)
    _click_btn(ui, cfg, cfg.tpl_confirm_end, cfg.confirm_x, cfg.confirm_y)
    print(f"{CLR_C}[auto] finish clicks sent; inspect the result in the app{CLR_RST}")


if __name__ == "__main__":
    # 仅作脚本运行时包装 stdio（被导入/测试时不劫持）
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    try:
        cfg = load_config()
        run(cfg)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Stopped: {exc}. No platform result has been verified.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Stopped by user. Inspect and end any active session in the app.", file=sys.stderr)
        sys.exit(130)
