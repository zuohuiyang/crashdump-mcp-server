from __future__ import annotations

import re

import pytest

from dump_analyzer_mcp_server.tests.e2e.client import MCPHTTPClient
from dump_analyzer_mcp_server.tests.e2e.conftest import parse_tool_text_payload, upload_dump
from dump_analyzer_mcp_server.tests.e2e.config import E2EConfig


pytestmark = [pytest.mark.e2e, pytest.mark.e2e_symbol_heavy]


def _require_symbol_heavy_asset(config: E2EConfig) -> bytes:
    if config.symbol_heavy_dump_path is None:
        pytest.fail("缺少 symbol_heavy dump 路径，symbol_heavy 场景不允许 skip")
    if not config.symbol_heavy_dump_path.exists():
        pytest.fail(f"重符号场景 dump 不存在: {config.symbol_heavy_dump_path}")
    return config.symbol_heavy_dump_path.read_bytes()


def _prepare_and_start(client: MCPHTTPClient, config: E2EConfig, dump_payload: bytes) -> str:
    file_name = config.symbol_heavy_dump_path.name if config.symbol_heavy_dump_path else "symbol-heavy.dmp"
    prep_result = client.call_tool(
        "prepare_dump_upload",
        {"file_name": file_name, "file_size": len(dump_payload)},
    )
    prep_error, prep_payload = parse_tool_text_payload(prep_result)
    assert prep_error is False
    assert isinstance(prep_payload, dict)

    status_code, upload_resp = upload_dump(prep_payload["upload_url"], dump_payload, config.timeout_seconds)
    assert status_code == 201
    assert upload_resp["status"] == "uploaded"

    start_result = client.call_tool("start_analysis_session", {"file_id": prep_payload["file_id"]})
    start_error, start_payload = parse_tool_text_payload(start_result)
    assert start_error is False
    assert isinstance(start_payload, dict)
    return start_payload["session_id"]


def _execute(client: MCPHTTPClient, session_id: str, command: str, timeout: int) -> dict:
    print(f"[symbol_heavy] run command: {command}")
    result = client.call_tool(
        "execute_windbg_command",
        {"session_id": session_id, "command": command, "timeout": timeout},
    )
    is_error, payload = parse_tool_text_payload(result)
    assert is_error is False
    assert isinstance(payload, dict)
    assert payload["success"] is True
    print(f"[symbol_heavy] done command: {command}, cost_ms={payload.get('execution_time_ms')}")
    return payload


def _execute_with_progress(
    client: MCPHTTPClient, session_id: str, command: str, timeout: int
) -> tuple[dict, list[dict]]:
    print(f"[symbol_heavy] run command (stream): {command}")
    result, progress_events = client.call_tool_with_progress(
        "execute_windbg_command",
        {"session_id": session_id, "command": command, "timeout": timeout},
    )
    is_error, payload = parse_tool_text_payload(result)
    assert is_error is False
    assert isinstance(payload, dict)
    assert payload["success"] is True
    print(
        f"[symbol_heavy] done command (stream): {command}, cost_ms={payload.get('execution_time_ms')}, "
        f"progress_events={len(progress_events)}"
    )
    return payload, progress_events


def test_e2e_symbol_heavy_cold_cache(mcp_client: MCPHTTPClient, e2e_config: E2EConfig):
    dump_payload = _require_symbol_heavy_asset(e2e_config)
    session_id = _prepare_and_start(mcp_client, e2e_config, dump_payload)
    try:
        _execute(mcp_client, session_id, "!sym noisy", timeout=e2e_config.timeout_seconds)
        reload_result, reload_progress = _execute_with_progress(
            mcp_client, session_id, ".reload /f", timeout=e2e_config.timeout_seconds
        )
        _execute(mcp_client, session_id, "!sym quiet", timeout=e2e_config.timeout_seconds)
        module_result = _execute(mcp_client, session_id, "lmv m electron*", timeout=e2e_config.timeout_seconds)
        kv_result = _execute(mcp_client, session_id, ".ecxr;kv", timeout=e2e_config.timeout_seconds)

        assert reload_result["execution_time_ms"] >= 0
        progress_text = "\n".join(str(e.get("message", "")) for e in reload_progress)
        assert any(str(e.get("phase", "")).lower() == "running" for e in reload_progress)
        assert any(str(e.get("phase", "")).lower() == "completed" for e in reload_progress)
        assert any(token in progress_text.upper() for token in ("SYMSRV", "DBGHELP", "PDB", "SYMBOL"))
        assert module_result["output"]
        assert "electron" in module_result["output"].lower()

        stack_output = kv_result["output"]
        assert stack_output

        # 1) 必须看到 electron 模块符号（等价于 PDB 解析到函数名）
        assert "electron!electron::ElectronBindings::Crash" in stack_output

        # 2) 栈帧第一行（00 帧）必须命中 ElectronBindings::Crash
        frame0_line = next(
            (line for line in stack_output.splitlines() if re.match(r"^\s*00\s", line)),
            "",
        )
        assert "electron!electron::ElectronBindings::Crash" in frame0_line
        print(f"[symbol_heavy] frame0={frame0_line}")

    finally:
        mcp_client.call_tool("close_analysis_session", {"session_id": session_id})


def test_e2e_symbol_heavy_warm_cache(mcp_client: MCPHTTPClient, e2e_config: E2EConfig):
    dump_payload = _require_symbol_heavy_asset(e2e_config)
    session_id = _prepare_and_start(mcp_client, e2e_config, dump_payload)
    try:
        first = _execute(mcp_client, session_id, "!analyze -v", timeout=e2e_config.timeout_seconds)
        second = _execute(mcp_client, session_id, "!analyze -v", timeout=e2e_config.timeout_seconds)

        first_ms = int(first["execution_time_ms"])
        second_ms = int(second["execution_time_ms"])
        # 热缓存通常更快；允许一定波动，避免环境噪声导致误报。
        assert second_ms <= int(first_ms * 1.5)
    finally:
        mcp_client.call_tool("close_analysis_session", {"session_id": session_id})
