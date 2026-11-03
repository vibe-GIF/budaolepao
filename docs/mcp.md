# MCP 接口

MCP 通过 HTTP 连接已启动的 `lepao web`，网页和 MCP 共用同一个控制器、运行任务、配置和日志。MCP 自身不创建控制器，不会另启一个运行器。

## 安装与启动

从完整源码目录安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,test]"
.\.venv\Scripts\lepao.exe web
```

保持网页服务运行，然后让 MCP 客户端启动下面的 stdio 服务。这里使用官方 Python SDK v1 的 `FastMCP`，依赖限制为 `mcp>=1.12,<2`，保留 Python 3.10 兼容性。SDK v2 的入口和 Python 版本要求不同，不能直接替换。

以下是通用 MCP 客户端配置。把示例中的绝对 Python 路径替换为本机已安装此项目的虚拟环境；JSON 中 Windows 反斜杠需要双写。

```json
{
  "mcpServers": {
    "lepao": {
      "command": "C:\\projects\\budaolepao\\.venv\\Scripts\\python.exe",
      "args": ["-m", "budaolepao_cli.mcp_server"]
    }
  }
}
```

同一入口也可以通过 `lepao mcp` 或 `lepao-mcp` 启动。服务使用 stdio，标准输出只用于 MCP 协议；不要用普通终端输出判断它是否卡住。停止 MCP 客户端不会停止网页服务。

默认读取源码根目录下 `.lepao/controller.json` 中的 `url`、`token` 和 `pid`。网页服务负责写入该文件；MCP 每次请求重新读取，可连接网页服务重启后的地址。必要时可在 `args` 中追加 `--controller-file` 和另一个本地发现文件的绝对路径。

不要把该文件内容、令牌或请求头放进 MCP 客户端配置。桥接器只接受带显式端口的 `http://127.0.0.1:<port>`，禁用 HTTP 代理和重定向，不接受远程地址、`localhost`、URL 中的密码、路径或查询参数。令牌只通过 `X-Lepao-Token` 发送，不包含在工具结果或日志中。

## 工具

| 工具 | 作用 |
| --- | --- |
| `get_status` | 读取运行状态、模式、进度与退出信息 |
| `read_config` | 读取当前配置、配置来源和版本标识 `revision` |
| `update_config(config, expected_revision=null)` | 提交完整配置对象，可传版本标识避免覆盖其他客户端的新修改 |
| `check_readiness` | 读取配置、模拟器和模板检查结果 |
| `get_logs(after=0)` | 读取游标之后的日志，下一次传入返回的 `cursor` |
| `start_run(mode="preview")` | 启动预演，或明确指定 `emulator` 启动模拟器模式 |
| `stop_run` | 请求停止当前任务 |
| `get_emulator` / `set_emulator` | 读取或保存 MuMu 路径 |
| `list_templates` | 读取五个按钮模板的存在与可读状态 |
| `upload_template(name, content_base64)` | 写入一个不超过 2 MiB 的 PNG 模板 |

允许的模板名为 `begin_run.png`、`free_run.png`、`pause_run.png`、`end_run.png`、`confirm_end.png`。输入是 PNG 文件本身的 base64，不是带 `data:image/png;...` 前缀的 URL。

成功结果直接保留网页 API 的 JSON 对象。失败结果为 `{"error":{"code":"...","message":"..."}}`，HTTP 错误还会带 `status_code`。应先检查 `error`，再读取业务字段。超时、认证失败、服务未启动或数据无效会返回明确错误，不会自动启动服务或重试写操作。

编辑配置时，先调用 `read_config`，修改返回的完整 `config`，再把同次返回的 `revision` 作为 `update_config` 的 `expected_revision`。如果网页或其他客户端已保存新版本，服务返回 409；重新读取并合并需要的修改后再提交。省略 `expected_revision` 保留脚本调用兼容性，但不会检查是否覆盖了其他客户端的修改。

`preview` 仅运行本地预演，不操作设备。`emulator` 会操作 MuMu，仍需准备 App、配置和模板。停止操作保留 MuMu 窗口；请人工检查 App 状态，必要时结束 App 内的活动。`platform_accepted` 为 `null`，本地完成不证明官方平台已接收、认可或计入任务。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_mcp.py -q
```

测试使用假的 HTTP 控制器，覆盖请求格式、版本冲突、URL 限制、令牌保护和错误返回，并用官方 SDK 通过 stdio 实际执行 `initialize`、`list_tools`、`call_tool`。这些测试不会启动 MuMu 或登录账号，也不代表真实设备验证。

API 参考：[官方 SDK v1.12.4 文档](https://github.com/modelcontextprotocol/python-sdk/blob/v1.12.4/README.md)。
