# Dump Analyzer MCP Server

用于远程 Windows Crash Dump 分析的 MCP 服务：支持上传 dump、创建分析会话、执行 CDB 命令，并返回执行状态。

项目目标是让 AI 侧脱离对 Windows 操作系统的运行依赖：通过标准 MCP 接口调用部署在 Windows 服务端的 CDB 指令完成 dump 分析。

<!-- mcp-name: io.github.zuohuiyang/dump-analyzer-mcp-server -->

## 核心功能

- 提供 4 个核心工具：`prepare_dump_upload`、`start_analysis_session`、`execute_windbg_command`、`close_analysis_session`
- 命令执行状态统一为三阶段：`queued -> running -> completed`
- MCP 通道采用 `streamable-http` 的 SSE 实时事件语义（执行中持续推送进度事件）
- CDB 输出原样透传，不做语义解析
- 命令长时间无输出时自动发送心跳，避免误判为卡死
- 进度展示采用“阶段+文本”模型，不对符号加载等场景做百分比估算
- 默认拒绝危险命令（如 `.shell`、重定向、`.create/.attach/.kill` 等）

## 前置条件

- 操作系统：Windows
- Python：3.10 及以上
- 调试器：建议使用 Windows SDK `26100` 及以上版本（含 WinDbg/CDB），且服务端可访问 `cdb.exe`
- 网络：客户端可访问 `--public-base-url` 对应地址

## 安全边界（重要）

- 本服务默认面向内网/受信任环境，当前不内置用户鉴权与权限体系，请勿直接暴露公网；如需跨网络访问，请在前置网关或反向代理层提供鉴权、访问控制与 TLS，并结合网络隔离、白名单和防火墙限制访问来源。

## 使用流程

1. 调用 `prepare_dump_upload(file_size, file_name)` 获取 `file_id` 和 `upload_url`
2. 对 `upload_url` 发送 HTTP `PUT`（原始 dump 字节）
3. 调用 `start_analysis_session(file_id)` 获取 `session_id`
4. 调用 `execute_windbg_command(session_id, command, timeout)` 执行任意 CDB 命令
5. 完成后调用 `close_analysis_session(session_id)` 释放资源

## 启动

```powershell
uv sync
uv run dump-analyzer-mcp-server --host 0.0.0.0 --port 8000 --public-base-url http://your-host:8000
```

- MCP 入口：`http://your-host:8000/mcp`
- 上传入口：`http://your-host:8000/uploads/dumps/{file_id}`

## MCP 客户端配置示例

```json
{
  "mcpServers": {
    "dump-analyzer": {
      "url": "http://your-host:8000/mcp"
    }
  }
}
```

## 命令行参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--host` | 否 | `127.0.0.1` | 服务监听地址 |
| `--port` | 否 | `8000` | 服务监听端口 |
| `--public-base-url` | 是（必填） | 无 | 返回给客户端的可访问基址 |
| `--cdb-path` | 否 | 自动探测 | `cdb.exe` 路径 |
| `--symbols-path` | 否 | `srv*c:\symbols*https://msdl.microsoft.com/download/symbols` | 服务端符号路径（调用方不可覆盖） |
| `--timeout` | 否 | `30` | 命令执行超时（秒） |
| `--upload-dir` | 否 | `%PROGRAMDATA%\dump-analyzer-mcp-server\uploads` 或系统临时目录 | 上传临时目录 |
| `--max-upload-mb` | 否 | `100` | 最大上传大小（MB） |
| `--session-ttl-seconds` | 否 | `1800` | 空闲会话 TTL（秒） |
| `--max-active-sessions` | 否 | `10` | 最大活跃上传会话数 |
| `--verbose` | 否 | `false` | 输出详细日志 |

说明：
- `--public-base-url` 必须是客户端可访问地址，否则 `prepare_dump_upload` 会返回 `UPLOAD_URL_UNAVAILABLE`
- `--symbols-path` 仅服务端管理员可配置；调用方工具参数不可覆盖符号路径

## 错误与状态说明

- 命令执行状态固定为：`queued`、`running`、`completed`
- 工具调用失败时返回结构化错误（含错误码与错误信息），便于客户端处理
- `prepare_dump_upload` 在无法生成可访问上传地址时返回 `UPLOAD_URL_UNAVAILABLE`

## 开发与测试

```bash
uv run pytest src/dump_analyzer_mcp_server/tests/ -v
```

### E2E 测试（新增）

E2E 统一按远程用户视角执行：测试代码作为客户端调用已部署服务。

一键执行（部署 + 启动 + 全量 E2E + 前后清理）：

```powershell
.\scripts\e2e-deploy-start-run.ps1
```

脚本特性：

- 零参数执行：自动确定本机可用 IPv4 并启动服务
- 固定执行全量 E2E：`src/dump_analyzer_mcp_server/tests/e2e`
- symbol_heavy 样本路径由测试配置负责（默认：`tests/e2e/assets/electron.dmp`）
- E2E 客户端默认使用实际局域网 IP（与服务端 `public-base-url` 一致）
- 运行时使用 `pytest -s -vv`，会实时输出测试 stdout 与详细进度
- 执行前后都会清理临时 symbols 目录，保证下次仍为冷加载
- 服务 symbols 源同时包含：
  - `https://msdl.microsoft.com/download/symbols`
  - `https://symbols.electronjs.org`

常用环境变量：

- `DUMP_E2E_BASE_URL`：已部署服务地址（示例：`http://your-host:8000`）
- `DUMP_E2E_DUMP_PATH`：核心闭环用例 dump 路径（默认仓库自带 DemoCrash）

脚本会打印并保存以下日志：

- 关键参数：清理目录、`publicBaseUrl`、E2E 客户端地址、`symbolsPath`、`cdbPath`、`dump` 路径、服务启动命令
- pytest 日志文件：`%TEMP%\dump-analyzer-e2e-pytest.log`
- server stdout/stderr：`%TEMP%\dump-analyzer-e2e-server.out.log` / `%TEMP%\dump-analyzer-e2e-server.err.log`
- 失败时会额外输出：pytest 尾部、server 尾部、端口/进程快照、防火墙手动放行命令模板（不自动修改系统规则）

常见失败快速定位：

- 端口冲突：`Get-NetTCPConnection -LocalPort 8000`
- 进程占用：`Get-CimInstance Win32_Process | ? { $_.CommandLine -match "dump_analyzer_mcp_server|pytest|uv run" }`
- 防火墙手动放行（管理员 PowerShell）：
  - `New-NetFirewallRule -DisplayName "DumpAnalyzer-E2E-8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000`
  - `New-NetFirewallRule -DisplayName "DumpAnalyzer-E2E-Python" -Direction Inbound -Action Allow -Program "<venvPython路径>"`

`e2e_symbol_heavy` 场景验收标准（硬性）：

- 执行 `.ecxr;kv` 后，调用栈必须出现 `electron!electron::ElectronBindings::Crash`
- `00` 栈帧（第一帧）必须命中 `electron::ElectronBindings::Crash`

只跑核心闭环 E2E（不含 Electron）：

```bash
uv run pytest src/dump_analyzer_mcp_server/tests/e2e -m "e2e and not e2e_symbol_heavy" -v
```

跑大 PDB 长时间加载场景用例：

```bash
uv run pytest src/dump_analyzer_mcp_server/tests/e2e -m "e2e_symbol_heavy" -v
```

## Fork 说明

- 本项目基于上游 `svnscha/mcp-windbg` fork 并持续演进，当前定位已调整为远程 Windows Crash Dump 分析 MCP 服务。

## License

MIT
