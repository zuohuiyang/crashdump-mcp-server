import asyncio
import atexit
import errno
import glob
import json
import logging
import os
import traceback
import winreg
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from . import upload_sessions
from .cdb_session import CDBSession
from .prompts import load_prompt
from .upload_sessions import UploadSessionMetadata, UploadSessionStatus

from mcp.shared.exceptions import McpError
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    ErrorData,
    TextContent,
    Tool,
    Prompt,
    PromptArgument,
    PromptMessage,
    GetPromptResult,
    INVALID_PARAMS,
    INTERNAL_ERROR,
)
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_MB = upload_sessions.DEFAULT_MAX_UPLOAD_MB
DEFAULT_SESSION_TTL_SECONDS = upload_sessions.DEFAULT_SESSION_TTL_SECONDS
DEFAULT_MAX_ACTIVE_SESSIONS = upload_sessions.DEFAULT_MAX_ACTIVE_SESSIONS
DEFAULT_UPLOAD_CLEANUP_INTERVAL_SECONDS = 30
UPLOAD_ROUTE_PATH = "/uploads/dumps/{session_id}"
SERVER_NAME = "crashdump-mcp-server"
UPLOAD_ERROR_TOO_LARGE = "UPLOAD_TOO_LARGE"
UPLOAD_ERROR_INVALID_FORMAT = "INVALID_DUMP_FORMAT"
UPLOAD_ERROR_INSUFFICIENT_STORAGE = "INSUFFICIENT_STORAGE"
UPLOAD_ERROR_WRITE_FAILED = "UPLOAD_WRITE_FAILED"
UPLOAD_ERROR_SESSION_NOT_FOUND = "UPLOAD_SESSION_NOT_FOUND"
UPLOAD_ERROR_SESSION_EXPIRED = "UPLOAD_SESSION_EXPIRED"
UPLOAD_ERROR_INVALID_STATE = "UPLOAD_SESSION_INVALID_STATE"
UPLOAD_ERROR_TOO_MANY_SESSIONS = "UPLOAD_TOO_MANY_SESSIONS"
UPLOAD_ERROR_UPLOAD_FAILED = "UPLOAD_FAILED"
UPLOAD_ERROR_URL_UNAVAILABLE = "UPLOAD_URL_UNAVAILABLE"


class UploadWorkflowError(RuntimeError):
    """Structured upload workflow error."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        remediation: str,
        details: Optional[Dict[str, object]] = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.details = details or {}
        self.http_status = http_status

    def to_payload(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def _build_session_id(
    dump_path: Optional[str] = None,
    connection_string: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    if dump_path:
        return os.path.abspath(dump_path)
    if connection_string:
        return f"remote:{connection_string}"
    if session_id:
        return upload_sessions.build_upload_cdb_session_key(session_id)
    raise ValueError("One target must be provided")


session_registry = upload_sessions.session_registry
upload_runtime_config = upload_sessions.upload_runtime_config
public_base_url = os.getenv("CRASHDUMP_MCP_SERVER_BASE_URL", "").strip().rstrip("/")

def get_local_dumps_path() -> Optional[str]:
    """Get the local dumps path from the Windows registry."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
        ) as key:
            dump_folder, _ = winreg.QueryValueEx(key, "DumpFolder")
            if os.path.exists(dump_folder) and os.path.isdir(dump_folder):
                return dump_folder
    except (OSError, WindowsError):
        # Registry key might not exist or other issues
        pass

    # Default Windows dump location
    default_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CrashDumps")
    if os.path.exists(default_path) and os.path.isdir(default_path):
        return default_path

    return None

class OpenWindbgDump(BaseModel):
    """Parameters for analyzing an uploaded crash dump."""
    session_id: str = Field(description="Upload session identifier returned by create_upload_session")
    include_stack_trace: bool = Field(description="Whether to include stack traces in the analysis")
    include_modules: bool = Field(description="Whether to include loaded module information")
    include_threads: bool = Field(description="Whether to include thread information")


class CreateUploadSessionParams(BaseModel):
    """Parameters for creating an upload session."""
    file_name: str = Field(
        description="Original dump filename. Must use a supported dump extension such as .dmp, .mdmp, or .hdmp"
    )

    @model_validator(mode='after')
    def validate_file_name(self):
        file_name = self.file_name.strip()
        if not file_name:
            raise ValueError("file_name must not be empty")
        if not upload_sessions.is_supported_dump_filename(file_name):
            raise ValueError("Only .dmp, .mdmp, and .hdmp files are supported")
        return self


class RunWindbgCmdParams(BaseModel):
    """Parameters for executing a WinDbg command."""
    session_id: str = Field(description="Upload session identifier returned by create_upload_session")
    command: str = Field(description="WinDbg command to execute")


class CloseWindbgDumpParams(BaseModel):
    """Parameters for unloading an uploaded crash dump session."""

    session_id: str = Field(description="Upload session identifier returned by create_upload_session")


class ListWindbgDumpsParams(BaseModel):
    """Parameters for listing crash dumps in a directory."""
    directory_path: Optional[str] = Field(
        default=None,
        description="Server-local directory path to search for dump files. If not specified, uses the server crash dump directory from the registry."
    )
    recursive: bool = Field(
        default=False,
        description="Whether to search recursively in subdirectories"
    )


def cleanup_expired_upload_sessions(now=None) -> int:
    return upload_sessions.cleanup_expired_upload_sessions(now=now)


async def upload_session_cleanup_loop(interval_seconds: int = DEFAULT_UPLOAD_CLEANUP_INTERVAL_SECONDS) -> None:
    """Background task that periodically cleans expired upload sessions."""
    safe_interval = max(1, interval_seconds)
    while True:
        try:
            cleanup_expired_upload_sessions()
        except Exception:
            logger.exception("Unexpected error during upload session cleanup loop")
        await asyncio.sleep(safe_interval)


def create_upload_session(file_name: str) -> Dict[str, object]:
    """Create a new upload session and reserve a temp path."""
    try:
        payload = upload_sessions.create_upload_session(file_name)
    except upload_sessions.UploadSessionLimitError as exc:
        raise UploadWorkflowError(
            code=UPLOAD_ERROR_TOO_MANY_SESSIONS,
            message=str(exc),
            remediation="Close or wait for existing upload sessions before creating another one.",
            http_status=409,
        ) from exc
    except ValueError as exc:
        raise UploadWorkflowError(
            code=UPLOAD_ERROR_INVALID_FORMAT,
            message=str(exc),
            remediation="Use a supported dump filename with .dmp, .mdmp, or .hdmp extension.",
        ) from exc

    try:
        payload["upload_url"] = build_upload_url(payload["session_id"])
    except UploadWorkflowError:
        upload_sessions.close_upload_session(payload["session_id"])
        raise
    payload["next_steps"] = [
        "PUT upload_url with raw dump bytes",
        f"call open_windbg_dump(session_id={payload['session_id']})",
    ]
    payload["upload_instructions"] = (
        "Upload the raw dump bytes to upload_url with HTTP PUT, "
        "then call open_windbg_dump(session_id=...) to analyze it."
    )
    return payload


def acquire_uploaded_session_for_tool(session_id: str) -> UploadSessionMetadata:
    metadata, error_message = upload_sessions.acquire_uploaded_session(
        session_id,
        upload_runtime_config.session_ttl_seconds,
        for_analysis=True,
    )
    if metadata is None:
        if error_message == "Upload session not found":
            raise UploadWorkflowError(
                code=UPLOAD_ERROR_SESSION_NOT_FOUND,
                message=error_message,
                remediation="Create a new upload session and upload the dump again.",
                details={"session_id": session_id},
                http_status=404,
            )
        if error_message == "Upload session has expired":
            raise UploadWorkflowError(
                code=UPLOAD_ERROR_SESSION_EXPIRED,
                message=error_message,
                remediation="Create a new upload session because the previous one expired.",
                details={"session_id": session_id},
                http_status=409,
            )
        if error_message == "Upload session is currently being analyzed":
            raise UploadWorkflowError(
                code=UPLOAD_ERROR_INVALID_STATE,
                message=error_message,
                remediation="Wait for the current analysis to finish, then retry the command.",
                details={"session_id": session_id, "current_status": "analyzing"},
                http_status=409,
            )
        if "expected uploaded" in error_message:
            current_status = "unknown"
            if "state is " in error_message:
                current_status = error_message.split("state is ", 1)[1].split(",", 1)[0].strip()
            raise UploadWorkflowError(
                code=UPLOAD_ERROR_INVALID_STATE,
                message="Upload not completed yet",
                remediation="Finish PUT upload to upload_url, then retry open_windbg_dump(session_id=...).",
                details={"session_id": session_id, "current_status": current_status},
                http_status=409,
            )
        raise UploadWorkflowError(
            code=UPLOAD_ERROR_INVALID_STATE,
            message=error_message,
            remediation="Retry after the upload session becomes available.",
            details={"session_id": session_id},
            http_status=409,
        )
    return metadata


def build_upload_path(session_id: str) -> str:
    return UPLOAD_ROUTE_PATH.format(session_id=session_id)


def configure_public_base_url(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    explicit_base_url: Optional[str] = None,
) -> str:
    global public_base_url

    configured = (explicit_base_url or os.getenv("CRASHDUMP_MCP_SERVER_BASE_URL", "").strip()).rstrip("/")
    public_base_url = configured
    return public_base_url


def build_upload_url(session_id: str) -> str:
    parsed = urlparse(public_base_url)
    hostname = (parsed.hostname or "").strip().lower()
    if not parsed.scheme or not parsed.netloc or hostname in {"0.0.0.0", "::", "localhost", "127.0.0.1"}:
        raise UploadWorkflowError(
            code=UPLOAD_ERROR_URL_UNAVAILABLE,
            message="upload URL cannot be derived from missing or non-routable public base URL",
            remediation="Configure CRASHDUMP_MCP_SERVER_BASE_URL or --public-base-url with a client-reachable IP or hostname.",
            details={"public_base_url": public_base_url},
            http_status=500,
        )
    return f"{public_base_url}{build_upload_path(session_id)}"


def _upload_error_payload(
    code: str,
    message: str,
    *,
    remediation: str,
    details: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "code": code,
        "message": message,
        "remediation": remediation,
    }
    if details:
        payload["details"] = details
    return payload


async def _stream_upload_to_file(
    request,
    target_path: str,
    max_bytes: int,
    expected_signatures: Tuple[bytes, ...],
) -> int:
    total_size = 0
    header = b""
    pending = bytearray()

    with open(target_path, "wb") as f:
        async for chunk in request.stream():
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > max_bytes:
                raise ValueError(UPLOAD_ERROR_TOO_LARGE)
            if len(header) < 4:
                pending.extend(chunk)
                if len(pending) < 4:
                    continue
                header = bytes(pending[:4])
                if header not in expected_signatures:
                    raise ValueError(UPLOAD_ERROR_INVALID_FORMAT)
                f.write(pending)
                pending.clear()
                continue
            f.write(chunk)

    if len(header) < 4:
        raise ValueError(UPLOAD_ERROR_INVALID_FORMAT)

    return total_size


def get_or_create_session(
    dump_path: Optional[str] = None,
    connection_string: Optional[str] = None,
    cdb_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = False
) -> CDBSession:
    """Get an existing CDB session or create a new one."""
    target_count = int(bool(dump_path)) + int(bool(connection_string))
    if target_count == 0:
        raise ValueError("One of dump_path or connection_string must be provided")
    if target_count > 1:
        raise ValueError("dump_path and connection_string are mutually exclusive")

    cdb_session_id = _build_session_id(
        dump_path=dump_path,
        connection_string=connection_string,
    )

    try:
        return upload_sessions.get_or_create_cdb_session(
            cdb_session_id,
            lambda: CDBSession(
                dump_path=dump_path,
                remote_connection=connection_string,
                cdb_path=cdb_path,
                symbols_path=symbols_path,
                timeout=timeout,
                verbose=verbose,
            ),
        )
    except FileNotFoundError as e:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Dump file not found on the crashdump server host: {dump_path}. "
                "Client-local paths are not readable by this MCP server. "
                "Upload the dump with create_upload_session and open it with session_id."
            ),
        )) from e
    except Exception as e:
        raise McpError(ErrorData(
            code=INTERNAL_ERROR,
            message=f"Failed to create CDB session: {str(e)}"
        ))


def get_or_create_uploaded_session(
    metadata: UploadSessionMetadata,
    cdb_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = False,
) -> CDBSession:
    """Get or create a CDB session for an uploaded dump."""
    try:
        return upload_sessions.get_or_create_cdb_session(
            _build_session_id(session_id=metadata.session_id),
            lambda: CDBSession(
                dump_path=metadata.temp_file_path,
                cdb_path=cdb_path,
                symbols_path=symbols_path,
                timeout=timeout,
                verbose=verbose,
            ),
        )
    except Exception as e:
        raise McpError(ErrorData(
            code=INTERNAL_ERROR,
            message=f"Failed to create CDB session: {str(e)}"
        ))


@contextmanager
def debugger_session_for_tool(
    *,
    dump_path: Optional[str] = None,
    connection_string: Optional[str] = None,
    session_id: Optional[str] = None,
    cdb_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = False,
):
    """Resolve the requested target into a reusable CDB session."""
    analysis_metadata = None
    try:
        if session_id:
            analysis_metadata = acquire_uploaded_session_for_tool(session_id)
            session = get_or_create_uploaded_session(
                analysis_metadata,
                cdb_path=cdb_path,
                symbols_path=symbols_path,
                timeout=timeout,
                verbose=verbose,
            )
        else:
            session = get_or_create_session(
                dump_path=dump_path,
                connection_string=connection_string,
                cdb_path=cdb_path,
                symbols_path=symbols_path,
                timeout=timeout,
                verbose=verbose,
            )
        yield session
    finally:
        upload_sessions.release_uploaded_session_after_analysis(
            analysis_metadata,
            upload_runtime_config.session_ttl_seconds,
        )


def unload_session(
    dump_path: Optional[str] = None,
    connection_string: Optional[str] = None,
) -> bool:
    """Unload and clean up a CDB session."""
    target_count = int(bool(dump_path)) + int(bool(connection_string))
    if target_count == 0:
        return False
    if target_count > 1:
        return False

    cdb_session_id = _build_session_id(
        dump_path=dump_path,
        connection_string=connection_string,
    )

    session = upload_sessions.pop_cdb_session(cdb_session_id)
    if session is not None:
        try:
            session.shutdown()
        except Exception:
            pass
        return True

    return False


def close_upload_session(session_id: str) -> Dict[str, object]:
    """Close upload session, shutdown CDB session and remove temp file."""
    payload, error_kind, error_message = upload_sessions.close_upload_session(session_id)
    if payload is None:
        if error_kind == "not_found":
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message=f"{UPLOAD_ERROR_SESSION_NOT_FOUND}: {error_message}",
                )
            )
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message=f"{UPLOAD_ERROR_INVALID_STATE}: {error_message}",
            )
        )
    return payload


def close_windbg_dump(
    session_id: str,
) -> Dict[str, object]:
    """Close an uploaded dump session."""
    return close_upload_session(session_id)


async def serve_http(
    host: str = "127.0.0.1",
    port: int = 8000,
    cdb_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = False,
    public_base_url_override: Optional[str] = None,
) -> None:
    """Run the crash dump MCP server with Streamable HTTP transport.

    Args:
        host: Host to bind the HTTP server to
        port: Port to bind the HTTP server to
        cdb_path: Optional custom path to cdb.exe
        symbols_path: Optional custom symbols path
        timeout: Command timeout in seconds
        verbose: Whether to enable verbose output
    """
    import uvicorn

    configure_public_base_url(host=host, port=port, explicit_base_url=public_base_url_override)
    app = create_http_app(
        cdb_path=cdb_path,
        symbols_path=symbols_path,
        timeout=timeout,
        verbose=verbose,
        public_base_url_override=public_base_url_override,
    )

    logger.info(f"Starting {SERVER_NAME} on {host}:{port}")
    print(f"{SERVER_NAME} running on http://{host}:{port}")
    print(f"  MCP endpoint: http://{host}:{port}/mcp")
    print(f"  Upload base URL: {public_base_url}")

    config = uvicorn.Config(app, host=host, port=port, log_level="info" if verbose else "warning")
    server_instance = uvicorn.Server(config)
    await server_instance.serve()


def create_http_app(
    cdb_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = False,
    public_base_url_override: Optional[str] = None,
):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from starlette.types import Receive, Scope, Send

    if public_base_url_override:
        configure_public_base_url(explicit_base_url=public_base_url_override)

    server = _create_server(
        cdb_path,
        symbols_path,
        timeout,
        verbose,
    )

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
    )

    async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    def upload_error(
        status_code: int,
        code: str,
        message: str,
        *,
        remediation: str,
        details: Optional[Dict[str, object]] = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"error": _upload_error_payload(code, message, remediation=remediation, details=details)},
        )

    async def upload_dump(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        metadata, error_kind, error_message = upload_sessions.prepare_upload_session_for_upload(
            session_id, upload_runtime_config.session_ttl_seconds
        )
        if metadata is None:
            if error_kind == "expired":
                return upload_error(
                    409,
                    UPLOAD_ERROR_SESSION_EXPIRED,
                    error_message,
                    remediation="Create a new upload session and retry the upload.",
                    details={"session_id": session_id},
                )
            if error_kind in {"busy", "invalid_state"}:
                return upload_error(
                    409,
                    UPLOAD_ERROR_INVALID_STATE,
                    error_message,
                    remediation="Create a new upload session if the previous upload is stuck, or wait and retry.",
                    details={"session_id": session_id},
                )
            return upload_error(
                404,
                UPLOAD_ERROR_SESSION_NOT_FOUND,
                error_message,
                remediation="Create a new upload session before uploading.",
                details={"session_id": session_id},
            )

        def fail_upload(
            status_code: int,
            code: str,
            message: str,
            *,
            remediation: str,
            details: Optional[Dict[str, object]] = None,
            log_unexpected: bool = False,
        ) -> JSONResponse:
            upload_sessions.mark_upload_failed(metadata)
            if log_unexpected:
                logger.exception("Unexpected upload failure for session %s", session_id)
            return upload_error(status_code, code, message, remediation=remediation, details=details)

        try:
            max_bytes = upload_runtime_config.max_upload_mb * 1024 * 1024
            expected_signatures = upload_sessions.get_expected_dump_signatures(metadata.original_file_name)
            total_size = await _stream_upload_to_file(
                request,
                metadata.temp_file_path,
                max_bytes,
                expected_signatures,
            )
            upload_sessions.mark_upload_completed(metadata, upload_runtime_config.session_ttl_seconds)
        except ValueError as exc:
            if str(exc) == UPLOAD_ERROR_TOO_LARGE:
                return fail_upload(
                    413,
                    UPLOAD_ERROR_TOO_LARGE,
                    f"Upload exceeds limit ({upload_runtime_config.max_upload_mb}MB)",
                    remediation="Use a smaller dump file or increase CRASHDUMP_MCP_MAX_UPLOAD_MB on the server.",
                    details={"session_id": session_id, "max_upload_mb": upload_runtime_config.max_upload_mb},
                )
            return fail_upload(
                400,
                UPLOAD_ERROR_INVALID_FORMAT,
                "Invalid dump upload payload",
                remediation="Upload the raw bytes of a supported .dmp, .mdmp, or .hdmp file.",
                details={"session_id": session_id},
            )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                return fail_upload(
                    507,
                    UPLOAD_ERROR_INSUFFICIENT_STORAGE,
                    "Insufficient storage space",
                    remediation="Free disk space on the server upload directory and retry.",
                    details={"session_id": session_id},
                )
            return fail_upload(
                500,
                UPLOAD_ERROR_WRITE_FAILED,
                f"Upload write failure: {exc}",
                remediation="Check server upload directory permissions and storage health, then retry.",
                details={"session_id": session_id},
            )
        except asyncio.CancelledError:
            upload_sessions.mark_upload_failed(metadata)
            raise
        except Exception:
            return fail_upload(
                500,
                UPLOAD_ERROR_UPLOAD_FAILED,
                "Unexpected upload failure",
                remediation="Retry the upload. If the problem persists, inspect server logs.",
                details={"session_id": session_id},
                log_unexpected=True,
            )
        finally:
            upload_sessions.release_upload_lock(metadata)

        return JSONResponse(
            status_code=201,
            content={
                "session_id": session_id,
                "status": UploadSessionStatus.UPLOADED.value,
                "size_bytes": total_size,
            },
        )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        cleanup_task = asyncio.create_task(
            upload_session_cleanup_loop(DEFAULT_UPLOAD_CLEANUP_INTERVAL_SECONDS)
        )
        try:
            async with session_manager.run():
                yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            cleanup_sessions()

    return Starlette(
        debug=verbose,
        routes=[
            Mount("/mcp", app=handle_streamable_http),
            Route("/uploads/dumps/{session_id}", endpoint=upload_dump, methods=["PUT"]),
        ],
        lifespan=lifespan,
    )


def _create_server(
    cdb_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = False,
) -> Server:
    """Create and configure the MCP server with all tools and prompts.

    Args:
        cdb_path: Optional custom path to cdb.exe
        symbols_path: Optional custom symbols path
        timeout: Command timeout in seconds
        verbose: Whether to enable verbose output

    Returns:
        Configured Server instance
    """
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="create_upload_session",
                description="""
                Create a server-side upload session for a supported crash dump file (*.dmp, *.mdmp, *.hdmp).
                This is the required first step for analyzing a client-local dump.
                Returns a client-reachable upload_url for an HTTP PUT upload.
                After upload, use session_id for every follow-up call.
                """,
                inputSchema=CreateUploadSessionParams.model_json_schema(),
            ),
            Tool(
                name="open_windbg_dump",
                description="""
                Analyze an uploaded Windows crash dump identified by session_id.
                Use create_upload_session first, upload the raw dump bytes to upload_url, then call this tool.
                """,
                inputSchema=OpenWindbgDump.model_json_schema(),
            ),
            Tool(
                name="run_windbg_cmd",
                description="""
                Execute a specific WinDbg command against an uploaded dump session identified by session_id.
                """,
                inputSchema=RunWindbgCmdParams.model_json_schema(),
            ),
            Tool(
                name="close_windbg_dump",
                description="""
                Close an uploaded dump session identified by session_id and release resources.
                """,
                inputSchema=CloseWindbgDumpParams.model_json_schema(),
            ),
            Tool(
                name="list_windbg_dumps",
                description="""
                List Windows crash dump files in a server-local directory.
                Use this tool to discover dump files that already exist on the crashdump server host.
                """,
                inputSchema=ListWindbgDumpsParams.model_json_schema(),
            ),
        ]

    @server.call_tool()
    async def call_tool(name, arguments: dict) -> list[TextContent]:
        try:
            if name == "open_windbg_dump":
                if not arguments.get("session_id"):
                    local_dumps_path = get_local_dumps_path()
                    local_hint = ""
                    if local_dumps_path:
                        local_hint = (
                            f"\n\nServer-local dumps may still exist under {local_dumps_path}, "
                            "but this crashdump server requires session_id for analysis calls."
                        )

                    return [TextContent(
                        type="text",
                        text=(
                            "Use 'create_upload_session' first, upload the raw dump bytes to the returned "
                            "'upload_url', and then call 'open_windbg_dump' with 'session_id'."
                            f"{local_hint}"
                        )
                    )]

                args = OpenWindbgDump(**arguments)
                with debugger_session_for_tool(
                    session_id=args.session_id,
                    cdb_path=cdb_path,
                    symbols_path=symbols_path,
                    timeout=timeout,
                    verbose=verbose,
                ) as session:
                    results = []

                    crash_info = session.send_command(".lastevent")
                    results.append("### Crash Information\n```\n" + "\n".join(crash_info) + "\n```\n\n")

                    # Run !analyze -v
                    analysis = session.send_command("!analyze -v")
                    results.append("### Crash Analysis\n```\n" + "\n".join(analysis) + "\n```\n\n")

                    # Optional
                    if args.include_stack_trace:
                        stack = session.send_command("kb")
                        results.append("### Stack Trace\n```\n" + "\n".join(stack) + "\n```\n\n")

                    if args.include_modules:
                        modules = session.send_command("lm")
                        results.append("### Loaded Modules\n```\n" + "\n".join(modules) + "\n```\n\n")

                    if args.include_threads:
                        threads = session.send_command("~")
                        results.append("### Threads\n```\n" + "\n".join(threads) + "\n```\n\n")

                    return [TextContent(type="text", text="".join(results))]

            elif name == "run_windbg_cmd":
                args = RunWindbgCmdParams(**arguments)
                with debugger_session_for_tool(
                    session_id=args.session_id,
                    cdb_path=cdb_path,
                    symbols_path=symbols_path,
                    timeout=timeout,
                    verbose=verbose,
                ) as session:
                    output = session.send_command(args.command)

                    return [TextContent(
                        type="text",
                        text=f"Command: {args.command}\n\nOutput:\n```\n" + "\n".join(output) + "\n```"
                    )]

            elif name == "create_upload_session":
                args = CreateUploadSessionParams(**arguments)
                payload = create_upload_session(args.file_name)
                return [TextContent(type="text", text=json.dumps(payload))]

            elif name == "close_windbg_dump":
                args = CloseWindbgDumpParams(**arguments)
                payload = close_upload_session(args.session_id)
                return [TextContent(type="text", text=json.dumps(payload))]

            elif name == "list_windbg_dumps":
                args = ListWindbgDumpsParams(**arguments)

                if args.directory_path is None:
                    args.directory_path = get_local_dumps_path()
                    if args.directory_path is None:
                        raise McpError(ErrorData(
                            code=INVALID_PARAMS,
                            message="No directory path specified and no default dump path found in registry."
                        ))

                if not os.path.exists(args.directory_path) or not os.path.isdir(args.directory_path):
                    raise McpError(ErrorData(
                        code=INVALID_PARAMS,
                        message=(
                            f"Directory not found on the crashdump server host: {args.directory_path}. "
                            "Client-local directories are not readable by this MCP server."
                        )
                    ))

                # Determine search pattern based on recursion flag
                search_pattern = os.path.join(args.directory_path, "**", "*.*dmp") if args.recursive else os.path.join(args.directory_path, "*.*dmp")

                # Find all dump files
                dump_files = glob.glob(search_pattern, recursive=args.recursive)

                # Sort alphabetically for consistent results
                dump_files.sort()

                if not dump_files:
                    return [TextContent(
                        type="text",
                        text=f"No crash dump files (*.*dmp) found in {args.directory_path}"
                    )]

                # Format the results
                result_text = f"Found {len(dump_files)} crash dump file(s) in {args.directory_path}:\n\n"
                for i, dump_file in enumerate(dump_files):
                    # Get file size in MB
                    try:
                        size_mb = round(os.path.getsize(dump_file) / (1024 * 1024), 2)
                    except (OSError, IOError):
                        size_mb = "unknown"

                    result_text += f"{i+1}. {dump_file} ({size_mb} MB)\n"

                return [TextContent(
                    type="text",
                    text=result_text
                )]

            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool: {name}"
            ))

        except UploadWorkflowError as exc:
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message=json.dumps({"error": exc.to_payload()}),
                )
            ) from exc
        except McpError:
            raise
        except Exception as e:
            traceback_str = traceback.format_exc()
            raise McpError(ErrorData(
                code=INTERNAL_ERROR,
                message=f"Error executing tool {name}: {str(e)}\n{traceback_str}"
            ))

    # Prompt constants
    DUMP_TRIAGE_PROMPT_NAME = "dump-triage"
    DUMP_TRIAGE_PROMPT_TITLE = "Crash Dump Triage Analysis"
    DUMP_TRIAGE_PROMPT_DESCRIPTION = "Comprehensive single crash dump analysis with detailed metadata extraction and structured reporting"

    # Define available prompts for triage analysis
    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return [
            Prompt(
                name=DUMP_TRIAGE_PROMPT_NAME,
                title=DUMP_TRIAGE_PROMPT_TITLE,
                description=DUMP_TRIAGE_PROMPT_DESCRIPTION,
                arguments=[
                    PromptArgument(
                        name="session_id",
                        description="Upload session identifier returned by create_upload_session",
                        required=False,
                    ),
                ],
            ),
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
        if arguments is None:
            arguments = {}

        if name == DUMP_TRIAGE_PROMPT_NAME:
            session_id = arguments.get("session_id", "")
            try:
                prompt_content = load_prompt("dump-triage")
            except FileNotFoundError as e:
                raise McpError(ErrorData(
                    code=INTERNAL_ERROR,
                    message=f"Prompt file not found: {e}"
                ))

            if session_id:
                prompt_text = f"**Uploaded dump session:** {session_id}\n\n{prompt_content}"
            else:
                prompt_text = prompt_content

            return GetPromptResult(
                description=DUMP_TRIAGE_PROMPT_DESCRIPTION,
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(
                            type="text",
                            text=prompt_text
                        ),
                    ),
                ],
            )

        else:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown prompt: {name}"
            ))

    return server


# Clean up function to ensure all sessions are closed when the server exits
def cleanup_sessions():
    """Close all active CDB sessions."""
    upload_sessions.cleanup_sessions()


# Register cleanup on module exit
atexit.register(cleanup_sessions)
