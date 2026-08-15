"""
Minimal MCP Streamable-HTTP client.

The Book of Business is exposed as a read-only MCP server. We do not want a
heavyweight agent framework sitting between us and the data, because the
whole point of the 4:30 test is that ingestion has to be boring, fast and
reliable. So: a small JSON-RPC client that speaks Streamable HTTP, handles
both application/json and text/event-stream replies, and carries the session
id. Nothing else.
"""

from __future__ import annotations

import json
import threading
import time
import httpx
from typing import Any


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, endpoint: str, token: str, timeout: float = 60.0):
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 0
        # The crawl fans out across threads, so request ids must not collide.
        # httpx.Client is itself thread-safe, so one connection pool is shared.
        self._lock = threading.Lock()
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=True,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=12))
        self.server_info: dict = {}
        self._initialized = False

    # ------------------------------------------------------------ plumbing
    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    @staticmethod
    def _parse_sse(text: str) -> dict | None:
        """Pull the last JSON-RPC payload out of an SSE stream."""
        last = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    last = json.loads(raw)
                except json.JSONDecodeError:
                    continue
        return last

    def _post(self, payload: dict, expect_reply: bool = True) -> dict | None:
        r = self._client.post(self.endpoint, headers=self._headers(),
                              content=json.dumps(payload))
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if r.status_code == 202 or not expect_reply:
            return None
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code} from MCP: {r.text[:600]}")

        ctype = r.headers.get("content-type", "")
        data = self._parse_sse(r.text) if "text/event-stream" in ctype else r.json()
        if data is None:
            raise MCPError(f"No JSON-RPC payload in reply: {r.text[:400]}")
        if isinstance(data, dict) and "error" in data:
            raise MCPError(json.dumps(data["error"])[:600])
        return data

    # -------------------------------------------------------------- public
    def initialize(self) -> dict:
        if self._initialized:
            return self.server_info
        res = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tower-gtm-control-tower", "version": "1.0.0"},
            },
        })
        self.server_info = (res or {}).get("result", {})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                       expect_reply=False)
        except Exception:
            pass
        self._initialized = True
        return self.server_info

    def list_tools(self) -> list[dict]:
        self.initialize()
        out: list[dict] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            res = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                              "method": "tools/list", "params": params})
            result = (res or {}).get("result", {})
            out.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

    def list_resources(self) -> list[dict]:
        self.initialize()
        try:
            res = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                              "method": "resources/list", "params": {}})
            return (res or {}).get("result", {}).get("resources", [])
        except MCPError:
            return []

    def call(self, name: str, arguments: dict | None = None,
             retries: int = 3) -> Any:
        """Call a tool and return the unwrapped payload (parsed JSON if possible)."""
        self.initialize()
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                res = self._post({
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                })
                return self._unwrap((res or {}).get("result", {}))
            except Exception as e:  # transient network / 5xx
                last_err = e
                if attempt == retries - 1:
                    break
                time.sleep(1.5 * (attempt + 1))
        raise MCPError(f"tool {name} failed after {retries} tries: {last_err}")

    @staticmethod
    def _unwrap(result: dict) -> Any:
        """MCP wraps payloads in content blocks. Give callers the actual data."""
        if not isinstance(result, dict):
            return result
        if "structuredContent" in result and result["structuredContent"] is not None:
            sc = result["structuredContent"]
            if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
                return sc["result"]
            return sc
        content = result.get("content")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            joined = "\n".join(t for t in texts if t)
            if joined:
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return joined
        return result

    def close(self) -> None:
        self._client.close()
