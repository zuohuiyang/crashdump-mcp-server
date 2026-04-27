import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

COMMAND_MARKER_TEXT = "COMMAND_COMPLETED_MARKER"
COMMAND_MARKER = f".echo {COMMAND_MARKER_TEXT}"
MAX_COMMAND_WALL_TIME_SECONDS = 30 * 60

# Default paths where cdb.exe might be located
DEFAULT_CDB_PATHS = [
    # Traditional Windows SDK locations
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x64)\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x86)\cdb.exe",

    # Microsoft Store WinDbg Preview locations (architecture-specific)
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX64.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX86.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbARM64.exe")
]

class CDBError(Exception):
    """Custom exception for CDB-related errors"""
    pass


@dataclass
class CommandExecution:
    request_id: str
    command: str
    started_at: float
    first_output_at: Optional[float] = None
    last_output_at: Optional[float] = None
    completed: bool = False
    cancelled: bool = False
    output_lines: list[str] = field(default_factory=list)
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    line_queue: queue.Queue[str] = field(default_factory=queue.Queue, repr=False)


class CDBSession:
    def __init__(
        self,
        dump_path: Optional[str] = None,
        remote_connection: Optional[str] = None,
        cdb_path: Optional[str] = None,
        symbols_path: Optional[str] = None,
        initial_commands: Optional[List[str]] = None,
        timeout: int = 10,
        verbose: bool = False,
        additional_args: Optional[List[str]] = None
    ):
        """
        Initialize a new CDB debugging session.

        Args:
            dump_path: Path to the crash dump file (mutually exclusive with remote_connection)
            remote_connection: Remote debugging connection string (e.g., "tcp:Port=5005,Server=192.168.0.100")
            cdb_path: Custom path to cdb.exe. If None, will try to find it automatically
            symbols_path: Custom symbols path. If None, uses default Windows symbols
            initial_commands: List of commands to run when CDB starts
            timeout: Timeout in seconds for waiting for CDB responses
            verbose: Whether to print additional debug information
            additional_args: Additional arguments to pass to cdb.exe

        Raises:
            CDBError: If cdb.exe cannot be found or started
            FileNotFoundError: If the dump file cannot be found
            ValueError: If invalid parameters are provided
        """
        # Validate that exactly one of dump_path or remote_connection is provided
        if not dump_path and not remote_connection:
            raise ValueError("Either dump_path or remote_connection must be provided")
        if dump_path and remote_connection:
            raise ValueError("dump_path and remote_connection are mutually exclusive")

        if dump_path and not os.path.isfile(dump_path):
            raise FileNotFoundError(f"Dump file not found: {dump_path}")

        self.dump_path = dump_path
        self.remote_connection = remote_connection
        self.timeout = timeout
        self.verbose = verbose

        # Find cdb executable
        self.cdb_path = self._find_cdb_executable(cdb_path)
        if not self.cdb_path:
            raise CDBError("Could not find cdb.exe. Please provide a valid path.")

        # Prepare command args
        cmd_args = [self.cdb_path]

        # Add connection type specific arguments
        if self.dump_path:
            cmd_args.extend(["-z", self.dump_path])
        elif self.remote_connection:
            cmd_args.extend(["-remote", self.remote_connection])

        # Add symbols path if provided
        if symbols_path:
            cmd_args.extend(["-y", symbols_path])

        # Add any additional arguments
        if additional_args:
            cmd_args.extend(additional_args)

        try:
            # Create a process group so CTRL+BREAK can be delivered for cancellation.
            creationflags = 0
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            self.process = subprocess.Popen(
                cmd_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                creationflags=creationflags,
            )
        except Exception as e:
            raise CDBError(f"Failed to start CDB process: {str(e)}")

        self.command_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_execution: Optional[CommandExecution] = None
        self._request_counter = 0
        self._reader_thread = threading.Thread(target=self._read_output_bytes, daemon=True)
        self._reader_thread.start()

        # Avoid blocking startup on an initialization probe command.
        # For symbol-heavy dumps this phase can be very slow and opaque to clients.
        # We only verify the process did not exit immediately; readiness is handled
        # by the first real command where progress/heartbeat can be surfaced.
        time.sleep(0.2)
        if self.process.poll() is not None:
            self.shutdown()
            raise CDBError("CDB process exited during initialization")

        # Run initial commands if provided
        if initial_commands:
            for cmd in initial_commands:
                self.send_command(cmd)

    def _find_cdb_executable(self, custom_path: Optional[str] = None) -> Optional[str]:
        """Find the cdb.exe executable"""
        if custom_path and os.path.isfile(custom_path):
            return custom_path

        for path in DEFAULT_CDB_PATHS:
            if os.path.isfile(path):
                return path

        return None

    def _next_request_id(self) -> str:
        with self._state_lock:
            self._request_counter += 1
            return str(self._request_counter)

    def _emit_line(self, text: str) -> None:
        with self._state_lock:
            execution = self._active_execution
            if execution is None:
                return

            if COMMAND_MARKER_TEXT in text:
                execution.completed = True
                execution.done_event.set()
                return

            now = time.time()
            if execution.first_output_at is None:
                execution.first_output_at = now
            execution.last_output_at = now
            execution.output_lines.append(text)
            execution.line_queue.put(text)

    def _read_output_bytes(self) -> None:
        """Thread function to continuously read raw CDB output bytes."""
        if not self.process or not self.process.stdout:
            return

        buffer = bytearray()
        skip_next_lf = False
        try:
            while True:
                chunk = self.process.stdout.read(1)
                if not chunk:
                    break

                if skip_next_lf and chunk == b"\n":
                    skip_next_lf = False
                    continue
                skip_next_lf = False

                if chunk in (b"\r", b"\n"):
                    if chunk == b"\r":
                        skip_next_lf = True
                    line = buffer.decode("utf-8", errors="replace")
                    buffer.clear()
                    if self.verbose:
                        print(f"CDB > {line}")
                    self._emit_line(line)
                    continue

                buffer.extend(chunk)
        except (IOError, ValueError) as e:
            if self.verbose:
                print(f"CDB output reader error: {e}")
        finally:
            if buffer:
                line = buffer.decode("utf-8", errors="replace")
                if self.verbose:
                    print(f"CDB > {line}")
                self._emit_line(line)

    def send_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
        """
        Send a command to CDB and return the output

        Args:
            command: The command to send
            timeout: Custom timeout for this command (overrides instance timeout)

        Returns:
            List of output lines from CDB

        Raises:
            CDBError: If the command times out or CDB is not responsive
        """
        if not self.process:
            raise CDBError("CDB process is not running")
        result = self.execute_command(command, timeout=timeout)
        return result["output_lines"]

    def execute_command(
        self,
        command: str,
        timeout: Optional[int] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
        heartbeat_interval: float = 5.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict:
        if not self.process or not self.process.stdin:
            raise CDBError("CDB process is not running")

        # `timeout` means idle timeout (no output/heartbeat activity).
        # A separate hard wall-time limit prevents unbounded command execution.
        cmd_idle_timeout = timeout or self.timeout
        request_id = self._next_request_id()
        execution = CommandExecution(
            request_id=request_id,
            command=command,
            started_at=time.time(),
        )
        last_activity_at = execution.started_at
        interrupt_deadline: Optional[float] = None

        with self.command_lock:
            try:
                with self._state_lock:
                    self._active_execution = execution

                self.process.stdin.write(f"{command}\n{COMMAND_MARKER}\n".encode("utf-8"))
                self.process.stdin.flush()
            except IOError as e:
                raise CDBError(f"Failed to send command: {str(e)}")

            try:
                while True:
                    now = time.time()
                    if now - execution.started_at > MAX_COMMAND_WALL_TIME_SECONDS:
                        raise CDBError(
                            f"Command exceeded max wall time after {MAX_COMMAND_WALL_TIME_SECONDS} seconds: {command}"
                        )
                    if now - last_activity_at > cmd_idle_timeout:
                        raise CDBError(f"Command idle timed out after {cmd_idle_timeout} seconds: {command}")

                    if cancel_event and cancel_event.is_set() and not execution.cancelled:
                        execution.cancelled = True
                        try:
                            self.send_ctrl_break()
                        except CDBError:
                            # Preserve cancellation state even if signal delivery fails.
                            pass
                        interrupt_deadline = time.time() + 5.0

                    if (
                        execution.cancelled
                        and interrupt_deadline is not None
                        and time.time() > interrupt_deadline
                        and not execution.completed
                    ):
                        execution.completed = True
                        execution.done_event.set()

                    try:
                        line = execution.line_queue.get(timeout=0.2)
                        last_activity_at = time.time()
                        if on_output:
                            on_output(line)
                    except queue.Empty:
                        if (
                            on_heartbeat
                            and heartbeat_interval > 0
                            and not execution.completed
                            and (time.time() - last_activity_at) >= heartbeat_interval
                        ):
                            on_heartbeat()
                            last_activity_at = time.time()

                    if execution.completed and execution.line_queue.empty():
                        break
            finally:
                with self._state_lock:
                    self._active_execution = None

        return {
            "request_id": execution.request_id,
            "command": execution.command,
            "output_lines": execution.output_lines.copy(),
            "cancelled": execution.cancelled,
            "execution_time_ms": int((time.time() - execution.started_at) * 1000),
        }

    def shutdown(self):
        """Clean up and terminate the CDB process"""
        try:
            if self.process and self.process.poll() is None:
                try:
                    if self.remote_connection:
                        # For remote connections, send CTRL+B to detach
                        self.process.stdin.write("\x02")  # CTRL+B
                        self.process.stdin.flush()
                    else:
                        # For dump files, send 'q' to quit
                        self.process.stdin.write("q\n")
                        self.process.stdin.flush()
                    self.process.wait(timeout=1)
                except Exception:
                    pass

                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=3)
        except Exception as e:
            if self.verbose:
                print(f"Error during shutdown: {e}")
        finally:
            self.process = None

    def send_ctrl_break(self) -> None:
        """Send a CTRL+BREAK event to the CDB process to break in.

        Raises:
            CDBError: If the signal cannot be delivered or the process is not running.
        """
        if not self.process or self.process.poll() is not None:
            raise CDBError("CDB process is not running")

        try:
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception as e:
            raise CDBError(f"Failed to send CTRL+BREAK: {str(e)}")

    def get_session_id(self) -> str:
        """Get a unique identifier for this CDB session."""
        if self.dump_path:
            return os.path.abspath(self.dump_path)
        elif self.remote_connection:
            return f"remote:{self.remote_connection}"
        else:
            raise CDBError("Session has no valid identifier")

    def __enter__(self):
        """Support for context manager protocol"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up when exiting context manager"""
        self.shutdown()
