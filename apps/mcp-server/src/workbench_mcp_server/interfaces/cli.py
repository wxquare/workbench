import argparse
import asyncio
import json
import logging
import os

from workbench_mcp_server.infrastructure.auth.bearer import BearerAuthenticator
from workbench_mcp_server.infrastructure.config.secrets import EnvSecrets
from workbench_mcp_server.infrastructure.config.settings import load_settings


async def serve(settings, transport):
    import uvicorn

    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.interfaces.mcp.server import create_http_app, create_server

    if transport == "stdio" and settings.server.stdio_principal is None:
        raise ValueError("stdio principal must be configured")
    runtime = Runtime(settings)
    try:
        if transport == "stdio":
            await create_server(runtime, transport).run_stdio_async()
        else:
            server = uvicorn.Server(
                uvicorn.Config(
                    create_http_app(runtime),
                    host=settings.server.host,
                    port=settings.server.port,
                    log_level="warning",
                    access_log=False,
                )
            )
            await server.serve()
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description="Scoped read-only diagnostic MCP server")
    parser.add_argument("command", choices=["check", "serve"])
    parser.add_argument("--config", default=os.environ.get("WORKBENCH_MCP_CONFIG"))
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    args = parser.parse_args()
    if not args.config:
        parser.error("--config or WORKBENCH_MCP_CONFIG is required")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # SDK/HTTP libraries must not emit full request arguments or credentials.
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = load_settings(args.config)
        secrets = EnvSecrets()
        BearerAuthenticator(settings, secrets)
        for source in settings.sources:
            secrets.resolve(source.auth_secret_ref)
        if settings.cursor_secret_ref:
            from workbench_mcp_server.infrastructure.clock import SystemClock
            from workbench_mcp_server.infrastructure.cursor import SignedCursor

            SignedCursor(secrets.resolve(settings.cursor_secret_ref), SystemClock())
        if args.command == "check":
            print(
                json.dumps(
                    {
                        "configuration": "ok",
                        "discovery": "not_checked",
                        "business_query": "not_checked",
                    }
                )
            )
        else:
            asyncio.run(serve(settings, args.transport))
    except (ValueError, OSError, StopIteration):
        logging.error("Configuration or startup failed; check bindings and secret references")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
