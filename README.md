# CrashDump MCP Server

用于**远程 Windows Crash Dump 分析**的 MCP Server，基于 WinDbg/CDB 提供上传、打开、分析和后续命令执行能力。

<!-- mcp-name: io.github.zuohuiyang/crashdump-mcp-server -->

## 项目定位

- 这是一个 **server-first** 的 MCP 服务，不再把自己定位为本地 WinDbg 工具集合。
- 核心场景只有一条主链路：`上传 dump -> 打开分析 -> 执行补充命令 -> 关闭会话`。
- `session_id` 是上传分析链路的唯一主键。

## 目标与约束

- 调用者只需要配置一个 MCP server 即可使用，不要求额外安装 SDK、helper、companion client 或本地桥接程序。
- 服务端不能假设能直接读取调用者本地文件；如果文件只存在于调用者本机，仍需通过服务端可接收的上传机制进入服务端。
- 不走 base64 分片上传方案，避免把大文件传输塞进 JSON 或 MCP 工具参数。
- 上传返回值必须提供对调用者可用的地址或明确的地址语义，不能继续把 `0.0.0.0` 当作默认可用访问地址。
- 上传失败时必须提供结构化状态和可操作错误，而不是仅返回粗粒度 `503`。

## 当前优化路线

- 延续现有 HTTP 上传方案，不引入依赖调用侧宿主能力的本地 helper。
- 优先把上传链路做成更闭环、更可观测、更可诊断，而不是改变纯 server 的产品边界。
- 后续优化优先级依次为：修正上传地址语义、增加上传状态与健康检查、降低调用方对三段式流程的感知。

## 能力范围

- `create_upload_session`：为客户端本地 dump 创建上传会话
- `open_windbg_dump`：分析已上传 dump 的会话
- `run_windbg_cmd`：对当前上传 dump 会话执行 WinDbg 命令
- `close_windbg_dump`：关闭上传 dump 会话并清理资源
- `list_windbg_dumps`：可选工具，用于查看服务端本地目录中的 dump 文件，但不作为主分析入口

## 安装

```bash
pip install crashdump-mcp-server
```

## 启动

```bash
crashdump-mcp-server --host 0.0.0.0 --port 8000 --public-base-url http://your-host:8000
```

- MCP 入口：`http://your-host:8000/mcp`
- 上传入口：`http://your-host:8000/uploads/dumps/{session_id}`

### 命令行参数

```text
--host HOST
--port PORT
--public-base-url URL
--cdb-path PATH
--symbols-path PATH
--timeout SECONDS
--verbose
```

`--public-base-url` 用于返回完整 `upload_url`。远端部署时必须显式配置为调用者可访问的 IP 或域名，否则 `create_upload_session` 会直接失败。

## 调用语义

- `create_upload_session` 是调用者唯一的起始步骤。
- `open_windbg_dump`、`run_windbg_cmd`、`close_windbg_dump` 全部只接受 `session_id`。
- 如果服务端没有显式配置 `--public-base-url` 或 `CRASHDUMP_MCP_SERVER_BASE_URL`，`create_upload_session` 会直接失败并返回 `UPLOAD_URL_UNAVAILABLE`。
- 返回给调用者的 `upload_url` 必须是可直接访问的完整地址，例如 `http://192.168.253.128:8080/uploads/dumps/<session_id>`。

## 典型流程

1. 调用 `create_upload_session(file_name="crash.dmp")`
2. 读取结果中的 `upload_url`、`session_id`、`next_steps`
3. 对 `upload_url` 发送原始 dump 二进制 `PUT`
4. 调用 `open_windbg_dump(session_id="...")`
5. 按需调用 `run_windbg_cmd(session_id="...", command="kb")`
6. 完成后调用 `close_windbg_dump(session_id="...")`

### 失败恢复

- 如果 `create_upload_session` 返回 `UPLOAD_URL_UNAVAILABLE`，说明当前部署没有配置对客户端可达的对外地址；先修正 `--public-base-url` 或 `CRASHDUMP_MCP_SERVER_BASE_URL`，再重新创建会话。
- 如果 `open_windbg_dump(session_id=...)` 返回 `UPLOAD_SESSION_INVALID_STATE`，说明上传还没完成；先完成 `PUT upload_url`，再重试打开分析。

## VS Code 配置示例

```json
{
  "servers": {
    "crashdump_mcp_server": {
      "type": "http",
      "url": "http://192.168.253.128:8080/mcp"
    }
  }
}
```

## 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `CRASHDUMP_MCP_SERVER_BASE_URL` | 返回 `upload_url` 时使用的对外基址 | 无默认值，远端部署时必须显式配置 |
| `CRASHDUMP_MCP_UPLOAD_DIR` | 上传文件落盘目录 | `%PROGRAMDATA%\crashdump-mcp-server\uploads` 或系统临时目录 |
| `CRASHDUMP_MCP_MAX_UPLOAD_MB` | 最大上传大小（MB） | `100` |
| `CRASHDUMP_MCP_SESSION_TTL_SECONDS` | 空闲上传会话 TTL | `1800` |
| `CRASHDUMP_MCP_MAX_ACTIVE_SESSIONS` | 最大活跃上传会话数 | `10` |

## 归因说明

- 本项目基于上游 `svnscha/mcp-windbg` fork 并持续修改。
- 上游项目采用 MIT License，本项目继续沿用 MIT License。
- 当前维护仓库：`https://github.com/zuohuiyang/crashdump-mcp-server`
- 上游仓库：`https://github.com/svnscha/mcp-windbg`

## License

MIT
