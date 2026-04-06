from .server import serve, serve_http

def main():
    """Crash dump MCP server entry point."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="Run the crashdump-mcp-server for remote Windows crash dump analysis."
    )
    parser.add_argument("--cdb-path", type=str, help="Custom path to cdb.exe")
    parser.add_argument("--symbols-path", type=str, help="Custom symbols path")
    parser.add_argument("--timeout", type=int, default=30, help="Command timeout in seconds")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use (default: stdio)",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind HTTP server to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind HTTP server to (default: 8000)")
    parser.add_argument(
        "--public-base-url",
        type=str,
        default=None,
        help="Externally reachable base URL used when returning upload_url (default: http://<host>:<port>)",
    )

    args = parser.parse_args()

    if args.transport == "stdio":
        asyncio.run(serve(
            cdb_path=args.cdb_path,
            symbols_path=args.symbols_path,
            timeout=args.timeout,
            verbose=args.verbose,
        ))
    else:
        asyncio.run(serve_http(
            host=args.host,
            port=args.port,
            cdb_path=args.cdb_path,
            symbols_path=args.symbols_path,
            timeout=args.timeout,
            verbose=args.verbose,
            public_base_url_override=args.public_base_url,
        ))


if __name__ == "__main__":
    main()
