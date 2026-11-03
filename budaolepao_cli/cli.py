import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(
        prog="lepao",
        description="Budao Lepao - MuMu emulator runner",
        add_help=False,
    )
    parser.add_argument("command", nargs="?", default="panel",
                        choices=["run", "web", "mcp", "detect", "eval", "capture", "config", "scan", "route", "map", "settings", "dashboard", "setup", "panel", "-h", "--help"])
    parser.add_argument("-h", "--help", action="store_true", dest="help_flag")

    args, _ = parser.parse_known_args()

    if args.command == "web":
        web_main(sys.argv[2:])
        return
    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main(sys.argv[2:])
        return

    if args.help_flag or args.command in ("-h", "--help"):
        print("Usage: lepao [command]")
        print()
        print("命令：")
        print("  (默认)   打开 TUI 控制中心，所有功能都在菜单里选")
        print("  run      启动模拟器 PoC（需完成配置与模板准备）")
        print("  web      启动本地可视化控制台（默认 http://127.0.0.1:8765）")
        print("  mcp      启动 stdio MCP，连接同一个本地控制台服务")
        print("  -h       显示本帮助")
        print()
        print("其余检测/看板/评估/配置/工具都收在控制中心菜单里；")
        print("也可直接调用，如：lepao config show / lepao detect / lepao dashboard /")
        print("lepao eval / lepao setup")
        return

    if args.command == "panel":
        from .panel import main as panel_main
        panel_main()
        return

    root = Path(__file__).parent.parent

    if args.command == "run":
        script = root / "poc" / "emulator_run" / "main.py"
        os.chdir(str(root / "poc" / "emulator_run"))
        import subprocess
        sys.exit(subprocess.run([sys.executable, str(script)],
                                 cwd=str(root / "poc" / "emulator_run")).returncode)

    elif args.command == "detect":
        sys.path.insert(0, str(root / "detection"))
        from server.main import app
        import uvicorn

        print("Starting detection server at http://127.0.0.1:8000")
        print("浏览器打开 http://127.0.0.1:8000 （不要用 0.0.0.0）")
        uvicorn.run(app, host="127.0.0.1", port=8000)

    elif args.command == "eval":
        sys.path.insert(0, str(root / "eval"))
        from metrics import run_eval
        from regression import run_m5

        run_eval(n_normal=30, n_attack=30)
        print()
        run_m5()

    elif args.command == "capture":
        _capture(root)

    elif args.command == "config":
        sys.exit(_config(root, sys.argv[2:]))

    elif args.command == "scan":
        _scan_mumu(root)

    elif args.command == "route":
        sys.exit(_set_route(root, sys.argv[2:]))

    elif args.command == "map":
        sys.exit(_map(root, sys.argv[2:]))

    elif args.command == "settings":
        sys.exit(_set_settings(root, sys.argv[2:]))

    elif args.command == "dashboard":
        import subprocess
        script = root / "detection" / "dashboard.py"
        cmd = [sys.executable, str(script)]
        if "--demo" in sys.argv:
            cmd.append("--demo")
        sys.exit(subprocess.run(cmd, cwd=str(root / "detection")).returncode)

    elif args.command == "setup":
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(root), "-q"],
            cwd=str(root),
        )
        if result.returncode == 0:
            print("Done. Try: lepao -h")
        else:
            print("Installation failed.", file=sys.stderr)
        sys.exit(result.returncode)


def web_main(argv=None):
    parser = argparse.ArgumentParser(prog="lepao web", description="本地可视化控制台")
    parser.add_argument("--port", type=int, default=8765, help="本机监听端口（默认 8765）")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    from .web import serve
    serve(port=args.port)


def _show_config(root: Path) -> int:
    cfg = root / "poc" / "emulator_run" / "config.json"
    if cfg.exists():
        try:
            print(json.dumps(_read_config(root), indent=2, ensure_ascii=False,
                             allow_nan=False))
        except (OSError, ValueError, TypeError) as exc:
            return _config_error(exc)
        return 0
    else:
        return _config_error("config.json not found")


def _config(root: Path, args: list) -> int:
    """统一配置入口：lepao config [show|set|route|map] [options]"""
    if not args or args[0] == "show":
        if len(args) > 1:
            return _config_error("config show does not accept arguments")
        return _show_config(root)
    elif args[0] == "set":
        return _set_settings(root, args[1:])
    elif args[0] == "route":
        return _set_route(root, args[1:])
    elif args[0] == "map":
        return _map(root, args[1:])
    else:
        print("Usage: lepao config [show|set|route|map]")
        print("  config show                          # 显示当前配置")
        print("  config set --speed <m/s> --distance <m>  # 设置配速/里程")
        print("  config route lon,lat lon,lat ...     # 设置路线")
        print("  config map [次数]                    # 拾取器读剪贴板坐标")
        return 2


def _map(root: Path, args: list):
    import subprocess
    script = root / "poc" / "emulator_run" / "picker.py"
    args_list = [sys.executable, str(script)]
    if len(args) > 0 and args[0].isdigit():
        args_list.append(args[0])  # count
    if len(args) > 1:
        args_list.append(args[1])  # mode (auto/manual)
    if len(args) > 2:
        args_list.append(args[2])  # address
    return subprocess.run(args_list, cwd=str(root / "poc" / "emulator_run")).returncode


def _capture(root: Path):
    import subprocess
    import cv2

    emu_run = root / "poc" / "emulator_run"
    cfg = emu_run / "emu_path.json"
    emu_dir = None
    if cfg.exists():
        import json
        emu_dir = Path(json.loads(cfg.read_text(encoding="utf-8"))["emu_dir"])
    else:
        for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            for base in [Path(f"{d}:\\Program Files\\NetEase"),
                         Path(f"{d}:\\Program Files (x86)\\NetEase")]:
                if base.exists():
                    for p in base.rglob("adb.exe"):
                        emu_dir = p.parent
                        break
                if emu_dir:
                    break
            if emu_dir:
                break

    if not emu_dir:
        print("MuMu not found. Run 'budaolepao' first to auto-detect.")
        return

    adb = emu_dir / "adb.exe"
    adb_info = None
    for port in ["16384", "7555", "7556"]:
        r = subprocess.run([str(adb), "connect", f"127.0.0.1:{port}"],
                           capture_output=True, text=True, timeout=5)
        if "connected" in r.stdout:
            adb_info = f"127.0.0.1:{port}"
            break

    if not adb_info:
        print("Can't connect. Make sure MuMu is running.")
        return

    print(f"Connected to {adb_info}")
    img_dir = emu_run / "img"
    img_dir.mkdir(exist_ok=True)

    screen = emu_run / "screen.png"
    with screen.open("wb") as fp:
        subprocess.run([str(adb), "-s", adb_info, "exec-out", "screencap", "-p"],
                       stdout=fp, timeout=10)

    img = cv2.imread(str(screen))
    if img is None:
        print("Screenshot failed")
        return

    print(f"Screenshot: {img.shape[1]}x{img.shape[0]}")
    print("Select button region, press SPACE to confirm, ESC to skip")

    cv2.namedWindow("Capture", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Capture", 1200, 800)
    roi = cv2.selectROI("Capture", img, False)
    cv2.destroyAllWindows()

    if roi[2] > 0 and roi[3] > 0:
        x, y, w, h = [int(v) for v in roi]
        crop = img[y:y+h, x:x+w]
        name = input("Save as (e.g. lepao.png): ").strip()
        if name:
            cv2.imwrite(str(img_dir / name), crop)
            print(f"Saved to img/{name}")
        else:
            print("Cancelled")
    else:
        print("No region selected")


def _scan_mumu(root: Path):
    try:
        sys.path.insert(0, str(root / "poc" / "emulator_run"))
        from main import _scan_common_paths, _scan_all_drives
    except ImportError:
        print("Can't import scanner. Make sure dependencies are installed.")
        print("Run: pip install numpy opencv-python prettytable")
        return

    import json

    result = _scan_common_paths() or _scan_all_drives()
    if result:
        mgr_dir, player = result
        Path(root / "poc" / "emulator_run" / "emu_path.json").write_text(
            json.dumps({"emu_dir": str(mgr_dir), "player_path": str(player)}),
            encoding="utf-8")
        print(f"Found MuMu at: {mgr_dir}")
        print(f"Player: {player}")
    else:
        print("MuMu not found on any drive")


def _config_error(error) -> int:
    print(f"Configuration error: {error}", file=sys.stderr)
    return 2


def _read_config(root: Path) -> dict:
    cfg_path = root / "poc" / "emulator_run" / "config.json"
    if not cfg_path.exists():
        cfg_path = cfg_path.with_name("config.example.json")

    def reject_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    config = json.loads(cfg_path.read_text(encoding="utf-8-sig"),
                        parse_constant=reject_constant)
    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    return config


def _write_config(root: Path, config: dict):
    # Serialize before opening a file, then replace atomically so a failed write
    # does not truncate the user's existing configuration.
    content = json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False)
    output = root / "poc" / "emulator_run" / "config.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=output.parent, delete=False) as fp:
            temporary = Path(fp.name)
            fp.write(content)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _positive_number(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _speed_bounds(config: dict) -> tuple[float, float]:
    lo, hi = config.get("min_speed_mps"), config.get("max_speed_mps")
    if lo is None and hi is None:
        base = _positive_number(config.get("base_speed_mps", 4.0), "speed")
        amplitude = float(config.get("speed_amp", 0.0))
        if not math.isfinite(amplitude) or amplitude < 0:
            raise ValueError("speed_amp must be finite and nonnegative")
        lo, hi = base - amplitude, base + amplitude
    if lo is None or hi is None:
        raise ValueError("min_speed_mps and max_speed_mps must be set together")
    lo, hi = _positive_number(lo, "minimum speed"), _positive_number(hi, "maximum speed")
    if lo > hi:
        raise ValueError("minimum speed must not exceed maximum speed")
    return lo, hi


def _parse_speed(value: str) -> tuple[float, float]:
    try:
        speed = float(value)
    except ValueError:
        # Try each separator: this also permits scientific notation, e.g. 1e-1-2.
        for match in re.finditer("-", value):
            try:
                lo = float(value[:match.start()])
                hi = float(value[match.end():])
            except ValueError:
                continue
            lo, hi = _positive_number(lo, "speed"), _positive_number(hi, "speed")
            if lo > hi:
                raise ValueError("minimum speed must not exceed maximum speed")
            return lo, hi
        raise ValueError(f"invalid speed: {value} (expected a number or min-max)")
    speed = _positive_number(speed, "speed")
    return speed, speed


def _validate_route(points):
    if len(points) < 3 or len(set(map(tuple, points))) < 3:
        raise ValueError("route needs at least 3 distinct points")
    for index, (lon, lat) in enumerate(points):
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ValueError("coordinates must be finite")
        if not -180 <= lon <= 180 or not -90 < lat < 90:
            raise ValueError("longitude must be in [-180, 180] and latitude in (-90, 90)")
        if points[index] == points[(index + 1) % len(points)]:
            raise ValueError("consecutive route points must differ (including the closing edge)")
        if abs(lon - points[(index + 1) % len(points)][0]) > 180:
            raise ValueError("route crossing the antimeridian is unsupported")


def _set_route(root: Path, args: list) -> int:
    try:
        config = _read_config(root)
        if not args:
            print("Current route:")
            for i, (lon, lat) in enumerate(config.get("walk_path", [])):
                print(f"  {i+1}. {lon}, {lat}")
            print("Usage: lepao route lon,lat lon,lat lon,lat ...")
            return 0

        points = []
        for arg in args:
            parts = arg.split(",")
            if len(parts) != 2:
                raise ValueError(f"invalid coordinate: {arg} (expected lon,lat)")
            points.append([float(parts[0]), float(parts[1])])
        _validate_route(points)
        config["walk_path"] = points
        _write_config(root, config)
    except (OSError, ValueError, TypeError) as exc:
        return _config_error(exc)

    print(f"Route saved ({len(points)} points):")
    for lon, lat in points:
        print(f"  {lon}, {lat}")
    return 0


def _set_settings(root: Path, args: list) -> int:
    try:
        config = _read_config(root)
        if not args:
            speed_min, speed_max = _speed_bounds(config)
            distance = _positive_number(config.get("dist_limit_m", 3000), "distance")
            speed_str = f"{speed_min:g}-{speed_max:g}" if speed_min != speed_max else f"{speed_min:g}"
            print("Current settings:")
            print(f"  Speed:     {speed_str} m/s")
            print(f"  Distance:  {distance:g} m")
            print(f"  Route:     {len(config.get('walk_path', []))} points")
            print("Usage: lepao config set --speed <min-max|number> --distance <m>")
            return 0

        # Apply explicit replacements before validating their final values so
        # this command can also repair an invalid saved speed or distance.
        legacy_speed = config.get("min_speed_mps") is None and config.get("max_speed_mps") is None
        speed_changed = False
        index = 0
        while index < len(args):
            option = args[index]
            if option not in ("--speed", "--speed-min", "--speed-max", "--distance") or index + 1 == len(args):
                raise ValueError(f"unknown or incomplete option: {option}")
            value = args[index + 1]
            if option == "--speed":
                config["min_speed_mps"], config["max_speed_mps"] = _parse_speed(value)
                speed_changed = True
            elif option == "--speed-min":
                config["min_speed_mps"] = _positive_number(value, "minimum speed")
                speed_changed = True
            elif option == "--speed-max":
                config["max_speed_mps"] = _positive_number(value, "maximum speed")
                speed_changed = True
            else:
                config["dist_limit_m"] = _positive_number(value, "distance")
            index += 2

        if speed_changed and legacy_speed:
            for field, sign in (("min_speed_mps", -1), ("max_speed_mps", 1)):
                if config.get(field) is None:
                    config[field] = float(config.get("base_speed_mps", 4.0)) + sign * float(config.get("speed_amp", 0.0))
        speed_min, speed_max = _speed_bounds(config)
        distance = _positive_number(config.get("dist_limit_m", 3000), "distance")
        if speed_changed:
            config["min_speed_mps"] = speed_min
            config["max_speed_mps"] = speed_max
            config["base_speed_mps"] = speed_min / 2.0 + speed_max / 2.0
        config["dist_limit_m"] = distance
        _write_config(root, config)
    except (OSError, ValueError, TypeError) as exc:
        return _config_error(exc)

    speed_str = f"{speed_min:g}-{speed_max:g}" if speed_min != speed_max else f"{speed_min:g}"
    print("Settings saved:")
    print(f"  Speed:     {speed_str} m/s")
    print(f"  Distance:  {distance:g} m")
    return 0
