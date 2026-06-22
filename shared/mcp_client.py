"""
RouxYou — MCP Client (Phase 38)
================================
JSON-RPC 2.0 client for consuming external MCP (Model Context Protocol) servers.
Lets RouxYou connect to external tool servers as a consumer.

Supports:
- Stdio transport (subprocess-based MCP servers)
- MCP initialize handshake (protocol version negotiation)
- Tool discovery (tools/list)
- Tool execution (tools/call)
- Resource listing (resources/list)
- Graceful shutdown

Reference: claw-code/rust/crates/runtime/src/mcp_stdio.rs (1720 lines)

Usage:
    from shared.mcp_client import McpClient

    client = McpClient(command=["npx", "@anthropic/mcp-filesystem"], args=["--root", "/path"])
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("read_file", {"path": "/some/file.txt"})
    await client.shutdown()

With context manager:
    async with McpClient(command=["python", "-m", "my_mcp_server"]) as client:
        tools = await client.list_tools()
        result = await client.call_tool("search", {"query": "hello"})
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Any
from pathlib import Path

logger = logging.getLogger("roux.mcp")


# ============================================================
# MCP Protocol Types
# ============================================================

MCP_PROTOCOL_VERSION = "2024-11-05"  # Latest MCP spec version

@dataclass
class McpTool:
    """A tool exposed by an MCP server."""
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    server_name: str = ""  # Which server provides this tool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "server_name": self.server_name,
        }


@dataclass
class McpResource:
    """A resource exposed by an MCP server."""
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mime_type": self.mime_type,
        }


@dataclass
class McpToolResult:
    """Result from calling an MCP tool."""
    content: list[dict] = field(default_factory=list)  # [{type: "text", text: "..."}, ...]
    is_error: bool = False

    @property
    def text(self) -> str:
        """Extract plain text from content blocks."""
        parts = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {"content": self.content, "is_error": self.is_error}


@dataclass
class McpServerCapabilities:
    """Capabilities reported by an MCP server during initialization."""
    tools: bool = False
    resources: bool = False
    prompts: bool = False
    server_name: str = ""
    server_version: str = ""
    protocol_version: str = ""

    def to_dict(self) -> dict:
        return {
            "tools": self.tools,
            "resources": self.resources,
            "prompts": self.prompts,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
        }


# ============================================================
# JSON-RPC 2.0 Transport
# ============================================================

class JsonRpcError(Exception):
    """Error from a JSON-RPC response."""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC Error {code}: {message}")


class StdioTransport:
    """
    JSON-RPC 2.0 transport over subprocess stdio.
    Messages are newline-delimited JSON.
    """

    def __init__(self):
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None

    async def start(self, command: list[str], env: Optional[dict] = None):
        """Start the subprocess and begin reading responses."""
        import os
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)

        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(f"MCP subprocess started: {' '.join(command)} (pid={self._process.pid})")

    async def _read_loop(self):
        """Background task: read JSON-RPC responses from stdout."""
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning(f"MCP: Non-JSON line from server: {line_str[:100]}")
                    continue

                # Match response to pending request
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        if "error" in msg:
                            err = msg["error"]
                            future.set_exception(JsonRpcError(
                                err.get("code", -1),
                                err.get("message", "Unknown error"),
                                err.get("data"),
                            ))
                        else:
                            future.set_result(msg.get("result"))
                elif "method" in msg:
                    # Server-initiated notification (no id) — log and ignore
                    logger.debug(f"MCP notification: {msg.get('method')}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"MCP read loop error: {e}")

    async def send_request(self, method: str, params: Optional[dict] = None,
                           timeout: float = 30) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP transport not connected")

        self._request_id += 1
        req_id = self._request_id

        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        # Register pending future
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[req_id] = future

        # Send request
        line = json.dumps(request) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

        # Wait for response
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP request '{method}' timed out after {timeout}s")

    async def send_notification(self, method: str, params: Optional[dict] = None):
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or not self._process.stdin:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            notification["params"] = params

        line = json.dumps(notification) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def stop(self):
        """Stop the subprocess."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._process:
            try:
                self._process.stdin.close() if self._process.stdin else None
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._process.kill()
            except Exception:
                pass
            logger.info(f"MCP subprocess stopped (pid={self._process.pid})")
            self._process = None

        # Cancel any pending requests
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None


# ============================================================
# MCP Client
# ============================================================

class McpClient:
    """
    MCP protocol client. Connects to an MCP server via stdio,
    performs initialization handshake, and provides tool/resource access.
    """

    def __init__(self, command: list[str], args: list[str] = None,
                 name: str = "", env: Optional[dict] = None):
        """
        Args:
            command: Command to start the MCP server (e.g., ["npx", "@anthropic/mcp-filesystem"])
            args: Additional arguments for the server
            name: Human-readable name for this server
            env: Additional environment variables for the subprocess
        """
        self.command = command + (args or [])
        self.name = name or command[0]
        self.env = env
        self._transport = StdioTransport()
        self._capabilities: Optional[McpServerCapabilities] = None
        self._tools: list[McpTool] = []
        self._initialized = False

    async def connect(self, timeout: float = 30) -> McpServerCapabilities:
        """Start the server and perform MCP initialization handshake."""
        await self._transport.start(self.command, env=self.env)

        # MCP initialize request
        result = await self._transport.send_request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "RouxYou",
                "version": "1.0.0",
            }
        }, timeout=timeout)

        # Parse server capabilities
        server_info = result.get("serverInfo", {})
        capabilities = result.get("capabilities", {})

        self._capabilities = McpServerCapabilities(
            tools="tools" in capabilities,
            resources="resources" in capabilities,
            prompts="prompts" in capabilities,
            server_name=server_info.get("name", self.name),
            server_version=server_info.get("version", ""),
            protocol_version=result.get("protocolVersion", ""),
        )

        # Send initialized notification
        await self._transport.send_notification("notifications/initialized")

        self._initialized = True
        logger.info(
            f"MCP server '{self._capabilities.server_name}' initialized "
            f"(tools={self._capabilities.tools}, resources={self._capabilities.resources})"
        )

        return self._capabilities

    async def list_tools(self) -> list[McpTool]:
        """Discover tools provided by the server."""
        if not self._initialized:
            raise RuntimeError("MCP client not initialized. Call connect() first.")

        if not self._capabilities.tools:
            return []

        result = await self._transport.send_request("tools/list", {})
        tools = []
        for t in result.get("tools", []):
            tool = McpTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self.name,
            )
            tools.append(tool)

        self._tools = tools
        logger.info(f"MCP '{self.name}': discovered {len(tools)} tools")
        return tools

    async def call_tool(self, name: str, arguments: Optional[dict] = None,
                        timeout: float = 60) -> McpToolResult:
        """Execute a tool on the server."""
        if not self._initialized:
            raise RuntimeError("MCP client not initialized. Call connect() first.")

        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments

        result = await self._transport.send_request("tools/call", params, timeout=timeout)

        return McpToolResult(
            content=result.get("content", []),
            is_error=result.get("isError", False),
        )

    async def list_resources(self) -> list[McpResource]:
        """List resources provided by the server."""
        if not self._initialized:
            raise RuntimeError("MCP client not initialized. Call connect() first.")

        if not self._capabilities.resources:
            return []

        result = await self._transport.send_request("resources/list", {})
        resources = []
        for r in result.get("resources", []):
            resources.append(McpResource(
                uri=r.get("uri", ""),
                name=r.get("name", ""),
                description=r.get("description", ""),
                mime_type=r.get("mimeType", ""),
            ))

        return resources

    async def shutdown(self):
        """Gracefully shut down the MCP server."""
        if self._initialized:
            try:
                await self._transport.send_notification("notifications/cancelled")
            except Exception:
                pass

        await self._transport.stop()
        self._initialized = False
        self._tools = []
        logger.info(f"MCP '{self.name}': shut down")

    @property
    def is_connected(self) -> bool:
        return self._initialized and self._transport.is_running

    @property
    def capabilities(self) -> Optional[McpServerCapabilities]:
        return self._capabilities

    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools)

    def status(self) -> dict:
        return {
            "name": self.name,
            "connected": self.is_connected,
            "capabilities": self._capabilities.to_dict() if self._capabilities else None,
            "tools_count": len(self._tools),
        }

    # Context manager support
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.shutdown()


# ============================================================
# MCP Server Manager (Multi-Server)
# ============================================================

@dataclass
class McpServerConfig:
    """Configuration for an MCP server."""
    name: str
    command: list[str]
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    enabled: bool = True


class McpServerManager:
    """
    Manages multiple MCP server connections.
    Provides unified tool discovery and execution across all servers.

    Usage:
        manager = McpServerManager()
        manager.add_server(McpServerConfig(
            name="filesystem",
            command=["npx", "@anthropic/mcp-filesystem"],
            args=["--root", "/path"],
        ))
        await manager.start_all()
        tools = manager.all_tools()
        result = await manager.call_tool("filesystem", "read_file", {"path": "/file.txt"})
        await manager.stop_all()
    """

    def __init__(self):
        self._clients: dict[str, McpClient] = {}
        self._configs: dict[str, McpServerConfig] = {}

    def add_server(self, config: McpServerConfig):
        """Register a server configuration."""
        self._configs[config.name] = config

    async def start_all(self):
        """Start all enabled servers."""
        for name, config in self._configs.items():
            if not config.enabled:
                logger.info(f"MCP server '{name}': disabled, skipping")
                continue

            try:
                client = McpClient(
                    command=config.command,
                    args=config.args,
                    name=config.name,
                    env=config.env or None,
                )
                await client.connect()
                await client.list_tools()
                self._clients[name] = client
            except Exception as e:
                logger.error(f"MCP server '{name}' failed to start: {e}")

    async def stop_all(self):
        """Stop all running servers."""
        for name, client in list(self._clients.items()):
            try:
                await client.shutdown()
            except Exception as e:
                logger.error(f"MCP server '{name}' failed to stop: {e}")
        self._clients.clear()

    def all_tools(self) -> list[McpTool]:
        """Get all tools from all connected servers."""
        tools = []
        for client in self._clients.values():
            tools.extend(client.tools)
        return tools

    async def call_tool(self, server_name: str, tool_name: str,
                        arguments: Optional[dict] = None,
                        timeout: float = 60) -> McpToolResult:
        """Call a tool on a specific server."""
        client = self._clients.get(server_name)
        if not client:
            raise ValueError(f"MCP server '{server_name}' not connected")
        return await client.call_tool(tool_name, arguments, timeout=timeout)

    async def call_tool_by_name(self, tool_name: str,
                                arguments: Optional[dict] = None,
                                timeout: float = 60) -> McpToolResult:
        """Call a tool by name, auto-routing to the correct server."""
        for client in self._clients.values():
            for tool in client.tools:
                if tool.name == tool_name:
                    return await client.call_tool(tool_name, arguments, timeout=timeout)
        raise ValueError(f"Tool '{tool_name}' not found on any connected MCP server")

    def get_client(self, name: str) -> Optional[McpClient]:
        return self._clients.get(name)

    def status(self) -> dict:
        return {
            "servers": {
                name: client.status()
                for name, client in self._clients.items()
            },
            "total_tools": len(self.all_tools()),
            "configured": list(self._configs.keys()),
            "connected": list(self._clients.keys()),
        }


# Module-level singleton
mcp_manager = McpServerManager()
