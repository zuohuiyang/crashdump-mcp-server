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
        response = self._post(payload, include_session=False)
        result = self._extract_result(response)
        self._notify_initialized()
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        response = self._post(
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
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._counter),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self._extract_result(response)

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

    def _post(self, payload: dict[str, Any], *, include_session: bool = True) -> dict[str, Any]:
        headers = {
            "accept": "application/json",
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
                body = resp.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MCPHTTPError(f"POST /mcp/ failed: {exc.code} {detail}") from exc

        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _extract_result(response: dict[str, Any]) -> dict[str, Any]:
        if "error" in response:
            raise MCPHTTPError(f"MCP error: {json.dumps(response['error'], ensure_ascii=False)}")
        result = response.get("result")
        if not isinstance(result, dict):
            return {}
        return result
