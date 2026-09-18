"""
Modular RAG MCP Server - Main Entry Point

This is the entry point for the MCP Server. It validates the configuration,
then serves the MCP tools over stdio.

Note: stdout is reserved for JSON-RPC protocol messages, so every
human-readable message here goes to stderr.
"""

import sys
from pathlib import Path

from src.core.settings import SettingsError, load_settings


def main() -> int:
    """
    Main entry point for the MCP Server.

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    print("Modular RAG MCP Server - Starting...", file=sys.stderr)

    settings_path = Path("config/settings.yaml")
    try:
        load_settings(settings_path)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    from src.mcp_server.server import run_stdio_server

    return run_stdio_server()


if __name__ == "__main__":
    sys.exit(main())
