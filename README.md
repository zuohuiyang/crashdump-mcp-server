# CrashDump MCP Server

用于**远程 Windows Crash Dump 分析**的 MCP Server，基于 WinDbg/CDB 提供上传、打开、分析和后续命令执行能力。

<!-- mcp-name: io.github.zuohuiyang/crashdump-mcp-server -->

## 项目定位

- 这是一个 **server-first** 的 MCP 服务，不再把自己定位为本地 WinDbg 工具集合。
- 核心场景只有一条主链路：`上传 dump -> 打开分析 -> 执行补充命令 -> 关闭会话`。
- `dump_path` 永远按**服务端机器**的文件系统解释，不按 MCP 客户端所在机器解释。

## 能力范围

- `create_upload_session`：为客户端本地 dump 创建上传会话
- `open_windbg_dump`：打开服务端本地 dump 或已上传 dump
- `run_windbg_cmd`：对当前 dump 会话执行 WinDbg 命令
- `close_windbg_dump`：关闭 dump 会话并清理资源
- `list_windbg_dumps`：列出服务端本地目录中的 dump 文件

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

`--public-base-url` 用于返回完整 `upload_url`。如果不传，默认使用 `http://<host>:<port>`。

## 调用语义

- 如果 dump 文件已经在**服务端机器**上，直接使用 `open_windbg_dump(dump_path=...)`。
- 如果 dump 文件只在**客户端机器**上，先调用 `create_upload_session`，再对返回的 `upload_url` 发起 `PUT` 上传，最后用 `session_id` 打开。
- 当 `dump_path` 或 `directory_path` 不存在时，错误含义也是“服务端机器上不存在”，不是客户端路径错误。

## 典型流程

1. 调用 `create_upload_session(file_name="crash.dmp")`
2. 读取结果中的 `upload_url`
3. 对 `upload_url` 发送原始 dump 二进制 `PUT`
4. 调用 `open_windbg_dump(session_id="...")`
5. 按需调用 `run_windbg_cmd(session_id="...", command="kb")`
6. 完成后调用 `close_windbg_dump(session_id="...")`

## VS Code 配置示例

```json
{
  "servers": {
    "crashdump_mcp_server": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `CRASHDUMP_MCP_SERVER_BASE_URL` | 返回 `upload_url` 时使用的对外基址 | `http://127.0.0.1:8000` |
| `CRASHDUMP_MCP_UPLOAD_DIR` | 上传文件落盘目录 | `%PROGRAMDATA%\crashdump-mcp-server\uploads` 或系统临时目录 |
| `CRASHDUMP_MCP_MAX_UPLOAD_MB` | 最大上传大小（MB） | `100` |
| `CRASHDUMP_MCP_SESSION_TTL_SECONDS` | 空闲上传会话 TTL | `1800` |
| `CRASHDUMP_MCP_MAX_ACTIVE_SESSIONS` | 最大活跃上传会话数 | `10` |

为兼容旧部署，当前仍接受旧版 `WINDBG_*` 上传环境变量。

## 归因说明

- 本项目基于上游 `svnscha/mcp-windbg` fork 并持续修改。
- 上游项目采用 MIT License，本项目继续沿用 MIT License。
- 当前维护仓库：`https://github.com/zuohuiyang/mcp-windbg`
- 上游仓库：`https://github.com/svnscha/mcp-windbg`

## License

MIT
