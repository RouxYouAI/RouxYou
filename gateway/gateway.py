"""
GATEWAY — Reverse Proxy for Zero-Downtime Deployment
------------------------------------------------------
Lightweight async reverse proxy. All traffic flows through here,
enabling instant backend swaps without any client-side awareness.

Routes:
  /orch/*    → orchestrator
  /coder/*   → coder
  /worker/*  → worker
  /watch/*   → watchtower
"""

import asyncio
import time
from pathlib import Path
from typing import Optional, Dict

import aiohttp
from aiohttp import web, ClientTimeout

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.lifecycle import register_process
from shared.logger import get_logger
from config import CONFIG

logger = get_logger("gateway")

GATEWAY_PORT = CONFIG.PORT_GATEWAY

DEFAULT_ROUTES = {
    "/orch":   {"host": "127.0.0.1", "port": CONFIG.PORT_ORCHESTRATOR, "name": "orchestrator"},
    "/coder":  {"host": "127.0.0.1", "port": CONFIG.PORT_CODER,        "name": "coder"},
    "/worker": {"host": "127.0.0.1", "port": CONFIG.PORT_WORKER,       "name": "worker"},
    "/watch":  {"host": "127.0.0.1", "port": CONFIG.PORT_WATCHTOWER,   "name": "watchtower"},
}

PROXY_TIMEOUT = ClientTimeout(total=300, connect=5)


class RouteTable:
    def __init__(self):
        self._routes: Dict[str, dict] = {}
        self._swap_history: list = []
        self._load()

    def _load(self):
        self._routes = {k: dict(v) for k, v in DEFAULT_ROUTES.items()}
        logger.info("Initialized default route table")

    def resolve(self, path: str) -> Optional[tuple]:
        for prefix, backend in self._routes.items():
            if path == prefix or path.startswith(prefix + "/"):
                stripped = path[len(prefix):] or "/"
                url = f"http://{backend['host']}:{backend['port']}"
                return url, stripped
        return None

    def swap(self, service_name: str, new_port: int, new_host: str = "127.0.0.1") -> dict:
        for prefix, backend in self._routes.items():
            if backend["name"] == service_name:
                old_port = backend["port"]
                backend["port"] = new_port
                backend["host"] = new_host
                self._swap_history.append({
                    "service": service_name, "prefix": prefix,
                    "old_port": old_port, "new_port": new_port,
                    "timestamp": time.time(),
                })
                logger.info(f"SWAP: {service_name} :{old_port} → :{new_port}")
                return {"success": True, "service": service_name,
                        "old_port": old_port, "new_port": new_port}
        return {"success": False, "error": f"No route found for service: {service_name}"}

    def get_routes(self) -> dict:
        return {"routes": {k: dict(v) for k, v in self._routes.items()},
                "swap_history": self._swap_history[-10:]}

    def get_backends(self) -> list:
        return [{"prefix": prefix, **backend} for prefix, backend in self._routes.items()]


routes = RouteTable()
_client_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global _client_session
    if _client_session is None or _client_session.closed:
        _client_session = aiohttp.ClientSession(timeout=PROXY_TIMEOUT)
    return _client_session


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    path = request.path
    query_string = request.query_string

    resolved = routes.resolve(path)
    if not resolved:
        return web.json_response(
            {"error": f"No route for path: {path}",
             "available_prefixes": list(routes._routes.keys())},
            status=404,
        )

    backend_url, stripped_path = resolved
    target = f"{backend_url}{stripped_path}"
    if query_string:
        target += f"?{query_string}"

    skip_headers = {
        "host", "transfer-encoding", "connection", "keep-alive",
        "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade",
    }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip_headers}
    headers["X-Forwarded-For"] = request.remote or "unknown"
    headers["X-Forwarded-Host"] = request.host

    body = await request.read() if request.can_read_body else None

    session = await get_session()
    try:
        async with session.request(
            method=request.method, url=target, headers=headers,
            data=body, allow_redirects=False,
        ) as backend_resp:
            response = web.StreamResponse(
                status=backend_resp.status,
                headers={k: v for k, v in backend_resp.headers.items()
                         if k.lower() not in skip_headers},
            )
            await response.prepare(request)
            async for chunk in backend_resp.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response

    except aiohttp.ClientConnectorError:
        svc = path.split("/")[1] if "/" in path else "unknown"
        return web.json_response({"error": f"Backend unreachable: {svc}", "target": target}, status=502)
    except asyncio.TimeoutError:
        return web.json_response({"error": "Backend timeout", "target": target}, status=504)
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return web.json_response({"error": f"Proxy error: {str(e)}"}, status=500)


async def gateway_health(request: web.Request) -> web.Response:
    backends = routes.get_backends()
    session = await get_session()
    health = {"gateway": "ok", "backends": {}}
    for b in backends:
        name = b["name"]
        url = f"http://{b['host']}:{b['port']}/health"
        try:
            async with session.get(url, timeout=ClientTimeout(total=2)) as resp:
                health["backends"][name] = {
                    "status": "ok" if resp.status == 200 else "degraded",
                    "port": b["port"], "http_status": resp.status,
                }
        except Exception:
            health["backends"][name] = {"status": "down", "port": b["port"]}
    return web.json_response(health)


async def gateway_routes(request: web.Request) -> web.Response:
    return web.json_response(routes.get_routes())


async def gateway_swap(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    service = data.get("service")
    port = data.get("port")
    host = data.get("host", "127.0.0.1")
    if not service or not port:
        return web.json_response({"error": "Required: service, port"}, status=400)
    result = routes.swap(service, int(port), host)
    return web.json_response(result, status=200 if result.get("success") else 400)


async def gateway_backends(request: web.Request) -> web.Response:
    return web.json_response({"backends": routes.get_backends()})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/gateway/health", gateway_health)
    app.router.add_get("/gateway/routes", gateway_routes)
    app.router.add_post("/gateway/swap", gateway_swap)
    app.router.add_get("/gateway/backends", gateway_backends)
    app.router.add_get("/health", gateway_health)
    app.router.add_route("*", "/{path_info:.*}", proxy_handler)

    async def cleanup(app):
        if _client_session and not _client_session.closed:
            await _client_session.close()
    app.on_cleanup.append(cleanup)
    return app


def main():
    logger.info(f"GATEWAY — listening on port {GATEWAY_PORT}")
    for prefix, backend in DEFAULT_ROUTES.items():
        logger.info(f"  {prefix:10s} → :{backend['port']} ({backend['name']})")
    register_process("gateway")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=GATEWAY_PORT, print=None)


if __name__ == "__main__":
    main()
