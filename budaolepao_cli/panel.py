"""
budaolepao panel — 终端控制中心
================================
把所有命令收进门户菜单，选中即跑（子进程调用，与命令行行为完全一致），
跑完自动回菜单。用于替代一条条敲命令。

用法：lepao panel
"""

import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

console = Console()

_root = Path(__file__).parent.parent


def _spawn(args: list[str]) -> int:
    """以子进程运行 CLI 命令，行为等价于终端敲 budaolepao <args>。"""
    return subprocess.run(
        [sys.executable, "-m", "budaolepao_cli"] + args,
        cwd=str(_root),
    ).returncode


def _header() -> Panel:
    title = Text(" Budao Lepao 控制中心", style="bold white")
    sub = Text(" 刷跑 · 检测 · 评估 · 配置   |   q 退出", style="grey58")
    return Panel(title.append("\n").append(sub), border_style="cyan")


def _menu() -> Table:
    t = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
    t.add_column("No.", style="bold cyan", width=4)
    t.add_column("Action", style="white")
    t.add_column("Desc", style="grey58")
    rows = [
        ("1", "刷跑", "启动 MuMu 模拟器刷跑脚本 (run)"),
        ("2", "检测服务", "启动 FastAPI 检测服务 (detect)"),
        ("3", "TUI 看板", "实时检测看板 (dashboard, 可加 --demo)"),
        ("4", "评估", "跑召回/误报 + 回归 (eval)"),
        ("5", "配置-显示", "查看当前 config.json"),
        ("6", "配置-设置", "设置配速/里程 (config set)"),
        ("7", "配置-路线", "设置路线 (config route)"),
        ("8", "扫描 MuMu", "扫描所有磁盘找 MuMu (scan)"),
        ("9", "采集按钮", "截图选区存按钮图 (capture)"),
    ]
    for no, act, desc in rows:
        t.add_row(no, act, desc)
    return t


def _ask_demo() -> bool:
    return Prompt.ask("看板是否用 demo 模式? (y/n)", default="n").strip().lower() == "y"


def _config_set():
    speed = Prompt.ask("速度 (m/s)，范围如 4-5 或单点 4.5", default="4-5")
    distance = Prompt.ask("目标里程 (m)", default="3000")
    rc = _spawn(["config", "set", "--speed", speed, "--distance", distance])
    if rc == 0:
        console.print("[green]配置已保存[/]")
    else:
        console.print("[red]配置未保存，请检查上方错误信息[/]")
    Prompt.ask("按回车返回")


def _config_route():
    use_map = Prompt.ask("用高德拾取器选点吗? (y/n)", default="y").strip().lower() == "y"
    if use_map:
        # 打开高德坐标拾取器：右键地图 → 这是哪儿 → 复制坐标 → 粘贴
        rc = _spawn(["config", "map"])
        if rc == 0:
            console.print("[green]路线已保存[/]")
        else:
            console.print("[red]路线未保存，请检查上方错误信息[/]")
    else:
        pts = Prompt.ask(
            "路线经纬度 (lon,lat lon,lat ...，至少 3 个)", default=""
        ).strip()
        if not pts:
            console.print("[yellow]未输入，取消[/]")
        else:
            rc = _spawn(["config", "route"] + pts.split())
            if rc == 0:
                console.print("[green]路线已保存[/]")
            else:
                console.print("[red]路线未保存，请检查上方错误信息[/]")
    Prompt.ask("按回车返回")


def main():
    while True:
        console.clear()
        console.print(_header())
        console.print(_menu())
        choice = Prompt.ask("请选择", default="1")
        if choice in ("q", "Q", "quit"):
            console.print("[grey58]退出[/]")
            break

        if choice == "1":
            rc = _spawn(["run"])
            if rc != 0:
                console.print("[red]模拟已停止，请检查上方错误信息[/]")
            Prompt.ask("按回车返回")
        elif choice == "2":
            _spawn(["detect"])
        elif choice == "3":
            if _ask_demo():
                _spawn(["dashboard", "--demo"])
            else:
                _spawn(["dashboard"])
        elif choice == "4":
            _spawn(["eval"])
        elif choice == "5":
            _spawn(["config", "show"])
            Prompt.ask("按回车返回")
        elif choice == "6":
            _config_set()
        elif choice == "7":
            _config_route()
        elif choice == "8":
            _spawn(["scan"])
            Prompt.ask("按回车返回")
        elif choice == "9":
            _spawn(["capture"])
            Prompt.ask("按回车返回")
        else:
            console.print("[red]无效输入[/]")


if __name__ == "__main__":
    main()
