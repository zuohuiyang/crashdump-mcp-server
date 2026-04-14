# CrashDump MCP Server

用于远程 Windows Crash Dump 分析的 MCP 服务：支持上传 dump、创建分析会话、执行 CDB 命令，并返回执行状态。

<!-- mcp-name: io.github.zuohuiyang/crashdump-mcp-server -->

## 核心功能

- 提供 4 个核心工具：`prepare_dump_upload`、`start_analysis_session`、`execute_windbg_command`、`close_analysis_session`
- 命令执行状态统一为三阶段：`queued -> running -> completed`
- CDB 输出原样透传，不做语义解析
- 命令长时间无输出时自动发送心跳，避免误判为卡死
- 默认拒绝危险命令（如 `.shell`、重定向、`.create/.attach/.kill` 等）

## 前置条件

- 操作系统：Windows
- Python：3.10 及以上
- 调试器：已安装 WinDbg/CDB，且服务端可访问 `cdb.exe`
- 网络：客户端可访问 `--public-base-url` 对应地址

## 使用流程

1. 调用 `prepare_dump_upload(file_size, file_name)` 获取 `file_id` 和 `upload_url`
2. 对 `upload_url` 发送 HTTP `PUT`（原始 dump 字节）
3. 调用 `start_analysis_session(file_id)` 获取 `session_id`
4. 调用 `execute_windbg_command(session_id, command, timeout)` 执行任意 CDB 命令
5. 完成后调用 `close_analysis_session(session_id)` 释放资源

## 启动

```powershell
uv sync
uv run crashdump-mcp-server --host 0.0.0.0 --port 8000 --public-base-url http://your-host:8000
```

- MCP 入口：`http://your-host:8000/mcp`
- 上传入口：`http://your-host:8000/uploads/dumps/{file_id}`

## MCP 客户端配置示例

```json
{
  "mcpServers": {
    "crashdump": {
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
| `--public-base-url` | 远端部署时是 | `http://<host>:<port>` | 返回给客户端的可访问基址 |
| `--cdb-path` | 否 | 自动探测 | `cdb.exe` 路径 |
| `--symbols-path` | 否 | `_NT_SYMBOL_PATH` 或 `srv*c:\symbols*https://msdl.microsoft.com/download/symbols` | 服务端符号路径（调用方不可覆盖） |
| `--timeout` | 否 | `30` | 命令执行超时（秒） |
| `--verbose` | 否 | `false` | 输出详细日志 |

说明：
- `--public-base-url` 必须是客户端可访问地址，否则 `prepare_dump_upload` 会返回 `UPLOAD_URL_UNAVAILABLE`
- `--symbols-path` 仅服务端管理员可配置；调用方工具参数不可覆盖符号路径

## 错误与状态说明

- 命令执行状态固定为：`queued`、`running`、`completed`
- 工具调用失败时返回结构化错误（含错误码与错误信息），便于客户端处理
- `prepare_dump_upload` 在无法生成可访问上传地址时返回 `UPLOAD_URL_UNAVAILABLE`

## 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `CRASHDUMP_MCP_SERVER_BASE_URL` | 返回 `upload_url` 的对外基址 | 无默认值，远端部署需显式配置 |
| `CRASHDUMP_MCP_UPLOAD_DIR` | 上传临时目录 | `%PROGRAMDATA%\\crashdump-mcp-server\\uploads` 或系统临时目录 |
| `CRASHDUMP_MCP_MAX_UPLOAD_MB` | 最大上传大小（MB） | `100` |
| `CRASHDUMP_MCP_SESSION_TTL_SECONDS` | 空闲会话 TTL（秒） | `1800` |
| `CRASHDUMP_MCP_MAX_ACTIVE_SESSIONS` | 最大活跃上传会话数 | `10` |

## 开发与测试

```bash
uv run pytest src/crashdump_mcp_server/tests/ -v
```

## Fork 说明

- 本项目基于上游 `svnscha/mcp-windbg` fork 并持续演进，当前定位已调整为远程 Windows Crash Dump 分析 MCP 服务。

## License

MIT
