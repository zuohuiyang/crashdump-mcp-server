import asyncio

import pytest
from mcp.types import ListToolsRequest

from mcp_windbg import server


pytestmark = pytest.mark.usefixtures("restore_upload_runtime_state")


def _get_tool_map():
    app_server = server._create_server()
    handler = app_server.request_handlers[ListToolsRequest]
    result = asyncio.run(handler(ListToolsRequest(method="tools/list")))
    return {tool.name: tool for tool in result.root.tools}


def test_crashdump_server_exposes_only_dump_analysis_tools():
    tool_map = _get_tool_map()
    tool_names = set(tool_map)

    assert "create_upload_session" in tool_names
    assert "session_id" in tool_map["open_windbg_dump"].inputSchema["properties"]
    assert "session_id" in tool_map["run_windbg_cmd"].inputSchema["properties"]
    assert "session_id" in tool_map["close_windbg_dump"].inputSchema["properties"]
    assert "connection_string" not in tool_map["run_windbg_cmd"].inputSchema["properties"]
    assert "open_windbg_remote" not in tool_names
    assert "close_windbg_remote" not in tool_names
    assert "send_ctrl_break" not in tool_names
