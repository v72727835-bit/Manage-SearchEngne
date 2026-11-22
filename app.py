"""ASGI entrypoint for serverless hosts (Vercel). Each request runs its own
stateless MCP session manager, so it works without a long-lived process."""
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

import server


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    path = scope.get("path", "").rstrip("/") or "/"
    if path == server.MCP_PATH.rstrip("/"):
        mgr = StreamableHTTPSessionManager(app=server.mcp._mcp_server, json_response=True, stateless=True)
        async with mgr.run():
            await mgr.handle_request(scope, receive, send)
        return
    if path == "/":
        resp = JSONResponse({"status": "ok", "service": "freelance-lead-finder-mcp"})
    else:
        resp = JSONResponse({"error": "not found"}, status_code=404)
    await resp(scope, receive, send)
