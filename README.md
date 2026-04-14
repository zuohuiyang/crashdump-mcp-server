# CrashDump MCP Server

用于远程 Windows Crash Dump 分析的 MCP 服务，采用 `CDB` 执行命令并通过进度通知持续回传执行状态。

<!-- mcp-name: io.github.zuohuiyang/crashdump-mcp-server -->

## 核心特性

- 仅保留 4 个核心工具：`prepare_dump_upload`、`start_analysis_session`、`execute_windbg_command`、`close_analysis_session`
- 命令执行统一三阶段：`queued -> running -> completed`
- CDB 输出原样透传（不做语义解析）
- 命令长时间无输出时自动发送心跳，避免“假卡死”
- 危险命令严格拒绝（如 `.shell`、重定向、`.create/.attach/.kill` 等）

## 使用流程

1. 调用 `prepare_dump_upload(file_size, file_name)` 获取 `file_id` 和 `upload_url`
2. 对 `upload_url` 发送 HTTP `PUT`（原始 dump 字节）
3. 调用 `start_analysis_session(file_id)` 获取 `session_id`
4. 调用 `execute_windbg_command(session_id, command, timeout)` 执行任意 CDB 命令
5. 完成后调用 `close_analysis_session(session_id)` 释放资源

## 启动

```bash
uv sync
uv run crashdump-mcp-server --host 0.0.0.0 --port 8000 --public-base-url http://your-host:8000
```

- MCP 入口：`http://your-host:8000/mcp`
- 上传入口：`http://your-host:8000/uploads/dumps/{file_id}`

## 命令行参数

```text
--host HOST
--port PORT
--public-base-url URL
--cdb-path PATH
--symbols-path PATH
--timeout SECONDS
--verbose
```

说明：
- `--public-base-url` 必须是客户端可访问地址，否则 `prepare_dump_upload` 会返回 `UPLOAD_URL_UNAVAILABLE`
- `--symbols-path` 仅服务端管理员可配置；调用方工具参数不可覆盖符号路径

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

## License

MIT
