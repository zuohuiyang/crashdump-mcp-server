from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import count
from typing import Any
from urllib import error, request

from mcp.types import DEFAULT_NEGOTIATED_VERSION


class MCPHTTPError(RuntimeError):
    pass


@dataclass
class MCPHTTPClient:
    endpoint: str
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        # Starlette mount endpoint is "/mcp/" and may 307-redirect "/mcp".
        # Use the canonical trailing-slash endpoint to avoid redirect handling issues.
        self._endpoint = self.endpoint.rstrip("/") + "/mcp/"
        self._counter = count(1)
        self._session_id: str | None = None
        self._protocol_version = DEFAULT_NEGOTIATED_VERSION
        self.last_progress_events: list[dict[str, Any]] = []

    def initialize(self) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._counter),
            "method": "initialize",
            "params": {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "dump-analyzer-e2e", "version": "0.1.0"},
            },
        }
        response, _events = self._post(payload, include_session=False)
        result = self._extract_result(response)
        self._notify_initialized()
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        response, _events = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._counter),
                "method": "tools/list",
                "params": {},
            }
        )
        result = self._extract_result(response)
        return list(result.get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response, _events = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._counter),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self._extract_result(response)

    def call_tool_with_progress(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        response, events = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._counter),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self._extract_result(response), events

    def delete_session(self) -> None:
        if not self._session_id:
            return
        req = request.Request(
            self._endpoint,
            method="DELETE",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "mcp-session-id": self._session_id,
                "mcp-protocol-version": self._protocol_version,
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds):
                return
        except error.HTTPError as exc:
            raise MCPHTTPError(f"DELETE /mcp/ failed: {exc.code} {exc.reason}") from exc

    def _notify_initialized(self) -> None:
        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

    def _post(self, payload: dict[str, Any], *, include_session: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        headers = {
            "accept": "text/event-stream, application/json",
            "content-type": "application/json",
            "mcp-protocol-version": self._protocol_version,
        }
        if include_session and self._session_id:
            headers["mcp-session-id"] = self._session_id

        req = request.Request(
            self._endpoint,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if not self._session_id:
                    new_sid = resp.headers.get("mcp-session-id")
                    if new_sid:
                        self._session_id = new_sid
                content_type = (resp.headers.get("content-type") or "").lower()
                body = resp.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MCPHTTPError(f"POST /mcp/ failed: {exc.code} {detail}") from exc

        if not body:
            self.last_progress_events = []
            return {}, []

        decoded = body.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type:
            events = self._parse_sse_payload(decoded)
            response: dict[str, Any] | None = None
            progress_events: list[dict[str, Any]] = []
            for event in events:
                method = event.get("method")
                if method == "$/progress":
                    params = event.get("params")
                    if isinstance(params, dict):
                        value = params.get("value")
                        if isinstance(value, dict):
                            progress_events.append(value)
                    continue
                if "id" in event and ("result" in event or "error" in event):
                    response = event
            if response is None:
                raise MCPHTTPError(f"SSE stream missing terminal JSON-RPC response: {decoded[:300]}")
            self.last_progress_events = progress_events
            return response, progress_events

        parsed = json.loads(decoded)
        if not isinstance(parsed, dict):
            raise MCPHTTPError(f"Unexpected JSON-RPC response payload: {type(parsed).__name__}")
        self.last_progress_events = []
        return parsed, []

    @staticmethod
    def _parse_sse_payload(payload: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        data_lines: list[str] = []
        for raw_line in payload.splitlines():
            line = raw_line.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if not line.strip():
                if data_lines:
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        if data_lines:
            data = "\n".join(data_lines)
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                return events
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _extract_result(response: dict[str, Any]) -> dict[str, Any]:
        if "error" in response:
            raise MCPHTTPError(f"MCP error: {json.dumps(response['error'], ensure_ascii=False)}")
        result = response.get("result")
        if not isinstance(result, dict):
            return {}
        return result
