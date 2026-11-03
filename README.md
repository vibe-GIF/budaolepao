# budaolepao
步道乐跑 刷跑脚本 + 反作弊检测系统

本仓库包含 Windows / MuMu 模拟器 PoC，以及独立的本地检测与评估服务。
检测规则与合成数据评估由本项目实现，不代表步道乐跑官方的判定结果。
PoC 的使用范围及准备工作见 [模拟器说明](poc/emulator_run/README.md)。

## 环境与安装

- Python 3.10 或以上。
- 模拟器 PoC 依赖 Windows 版 MuMu 的 `MuMuManager.exe` 和 `adb.exe`。
- 当前应从完整源码目录以 editable 模式安装；CLI 会读取源码树里的 `poc/`、`detection/` 和 `eval/`。

建议在虚拟环境中安装，在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\lepao.exe -h
```

## 使用
- `lepao` — 打开 TUI 控制中心（所有功能都在菜单里选）
- `lepao run` — 启动模拟器 PoC，先阅读其准备要求
- `lepao -h` — 查看所有命令

使用上面的虚拟环境时，可将 `lepao` 替换为 `.\.venv\Scripts\lepao.exe`。
配置错误、模拟器操作失败或按钮识别失败时，应根据错误提示修正后再运行。
本地距离达到目标只表示模拟循环结束，程序不会确认平台成绩是否有效。

## 离线测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

运行器测试使用假的模拟器、时钟和 UI，不启动 MuMu、不登录账号，也不提交跑步记录。
测试覆盖配置校验、命令失败、速度边界和运行中断；检测/评估测试使用本地生成的数据。
这些结果不能替代真实设备和平台端的验证。
