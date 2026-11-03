"""
lepao map - 打开地图拾取器，从剪贴板循环读取坐标

流程：
  1. 打开高德拾取器
  2. 右键地图 -> 这是哪儿 -> 复制坐标
  3. 切回终端，按回车
  4. 脚本从剪贴板读取坐标
  5. 重复直到输入 done 结束，保存全部坐标

依赖：pip install pyperclip
"""

import sys
import webbrowser
import re
from pathlib import Path


def get_clipboard() -> str:
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def parse_coords(text: str) -> list:
    coords = []
    number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
    pattern = rf'(?<![\w.+-])({number})\s*,\s*({number})(?![\w.+-])'
    for match in re.findall(pattern, text):
        try:
            lon, lat = float(match[0]), float(match[1])
            if -180 <= lon <= 180 and -90 < lat < 90:
                coords.append([lon, lat])
        except ValueError:
            continue
    return coords


def main():
    # Use the same validation and destination as `lepao config route`, including
    # when run directly without an editable installation.
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from budaolepao_cli.cli import _set_route

    # 打开地图
    webbrowser.open("https://lbs.amap.com/tools/picker")
    print("地图已打开")
    print("操作: 右键地图 -> 这是哪儿 -> 复制坐标")

    all_coords = []
    print("\n循环收集坐标：复制坐标后切回终端按回车；全部完成后输入 done 结束")
    while True:
        print("\n复制坐标后，切回终端按回车 (或输入 done 结束)...")
        try:
            line = input().strip().lower()
        except EOFError:
            break
        if line == "done":
            break

        text = get_clipboard()
        coords = parse_coords(text)

        if coords:
            all_coords.extend(coords)
            print(f"OK +{len(coords)} coords: {coords}")
            print(f"   共 {len(all_coords)} 个坐标")
        else:
            print(f"FAIL no coords (clipboard: {text[:60]})")

    if all_coords:
        return _set_route(root, [f"{lon},{lat}" for lon, lat in all_coords])
    else:
        print("\nNo coords saved")
        return 2


if __name__ == "__main__":
    sys.exit(main())
