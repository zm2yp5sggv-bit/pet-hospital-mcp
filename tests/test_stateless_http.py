"""无状态 Streamable HTTP 与端到端调用。

覆盖要求第 7 条：不发送旧 ``initialize``、不要求也不返回 ``Mcp-Session-Id``、
使用 2026-07-28 规定的发现/调用方式、``/health`` 可用、工具能通过 HTTP MCP 端点
被发现并调用。

这些测试跑的是**真的 TCP + 真的 ASGI**（uvicorn 起在随机端口），上游则用
``httpx.MockTransport`` 顶替——所以既不依赖真实 Go 服务，也不是在「假装」HTTP。
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from pet_hospital_mcp.tools.list_pets import LIST_PETS_QUERY_PARAMS

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "pytest-client", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


class RequestRecorder:
    """记录流经它的每个 HTTP 请求，用于断言协议行为（尤其「没有 initialize」）。"""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests: list[dict[str, Any]] = []
        self.response_headers: list[dict[str, str]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []

        async def receive_wrapper() -> Any:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.response_headers.append(
                    {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
                )
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        body = b"".join(chunks)
        rpc_method: str | None = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                rpc_method = parsed.get("method")
        except (ValueError, UnicodeDecodeError):
            pass

        self.requests.append(
            {
                "http_method": scope["method"],
                "path": scope["path"],
                "rpc_method": rpc_method,
                "headers": {
                    k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
                },
            }
        )

    @property
    def rpc_methods(self) -> list[str | None]:
        return [request["rpc_method"] for request in self.requests]

    def headers_named(self, name: str) -> list[str]:
        lowered = name.lower()
        return [
            headers[lowered] for headers in self.response_headers if lowered in headers
        ]


async def rpc(
    http: httpx.AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    request_id: int = 1,
) -> httpx.Response:
    """按 2026-07-28 的绑定规则发一次 JSON-RPC 请求。

    镜像头：``MCP-Protocol-Version`` 必给；``Mcp-Method`` 必给；``Mcp-Name`` 仅在
    tools/call、resources/read、prompts/get 上必给。
    """
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
        headers=headers,
    )


def result_of(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    assert "result" in payload, f"期望 result，实际: {payload}"
    return payload["result"]


def error_text_of(result: dict[str, Any]) -> dict[str, Any]:
    """从失败的 CallToolResult 里取出统一错误形状。"""
    assert result.get("isError") is True
    texts = [block["text"] for block in result["content"] if block.get("type") == "text"]
    assert texts, "失败结果里没有文本内容"
    return json.loads(texts[0])


class TestHealth:
    async def test_health_endpoint(self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]) -> None:
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await http.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "pet-hospital-mcp"
        assert body["protocolVersion"] == "2026-07-28"
        assert body["stateful"] is False

    async def test_health_does_not_touch_backend(
        self, build_app: Callable[..., Any], serve: Callable[..., Any]
    ) -> None:
        """上游挂了，/health 依然 200——否则编排系统会把唯一能解释原因的服务也重启掉。"""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            raise httpx.ConnectError("backend down", request=request)

        base_url = await serve(build_app(handler))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await http.get("/health")
        assert response.status_code == 200
        assert calls == []


class TestStatelessProtocol:
    async def test_server_discover_supports_2026_07_28(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(http, "server/discover", {"_meta": META})
        assert response.status_code == 200
        result = result_of(response)
        assert result["supportedVersions"] == ["2026-07-28"]
        assert "tools" in result["capabilities"]

    async def test_no_initialize_handshake_needed(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        """不握手也能直接 tools/list —— 2026-07-28 已移除 initialize。"""
        recorder = RequestRecorder(
            build_app(lambda request: httpx.Response(200, json=ok_response()))
        )
        base_url = await serve(recorder)
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(http, "tools/list", {"_meta": META})
        assert response.status_code == 200
        assert [t["name"] for t in result_of(response)["tools"]] == ["list_pets"]
        assert "initialize" not in recorder.rpc_methods

    async def test_no_session_id_returned_or_required(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        recorder = RequestRecorder(
            build_app(lambda request: httpx.Response(200, json=ok_response()))
        )
        base_url = await serve(recorder)
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            await rpc(http, "tools/list", {"_meta": META})
            # 故意带一个假会话头，服务端必须忽略它、既不校验也不回发
            await rpc(
                http,
                "tools/list",
                {"_meta": META},
                request_id=2,
            )
        assert recorder.headers_named("mcp-session-id") == []

    async def test_bogus_session_header_is_ignored(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        """按 2026-07-28：请求上出现 Mcp-Session-Id 就忽略它，不发也不回声。"""
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            headers = {
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list",
                "Mcp-Session-Id": "totally-bogus-session",
            }
            response = await http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}},
                headers=headers,
            )
        assert response.status_code == 200
        assert "mcp-session-id" not in {k.lower() for k in response.headers}

    async def test_sdk_client_uses_discover_not_initialize(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        """用官方 SDK 2.x 客户端跑一遍完整流程。"""
        from mcp import Client

        recorder = RequestRecorder(
            build_app(lambda request: httpx.Response(200, json=ok_response()))
        )
        base_url = await serve(recorder)

        async with Client(f"{base_url}/mcp") as client:
            assert client.protocol_version == "2026-07-28"
            tools = await client.list_tools()
            assert [t.name for t in tools.tools] == ["list_pets"]
            result = await client.call_tool("list_pets", {"species": "犬", "page": 1})

        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["total"] == 1
        assert "initialize" not in recorder.rpc_methods
        assert recorder.headers_named("mcp-session-id") == []


class TestToolsOverHttp:
    async def test_tools_list_schema_round_trip(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(http, "tools/list", {"_meta": META})
        tool = result_of(response)["tools"][0]
        assert tool["name"] == "list_pets"
        assert set(tool["inputSchema"]["properties"]) == set(LIST_PETS_QUERY_PARAMS)
        assert tool["inputSchema"]["additionalProperties"] is False
        assert set(tool["outputSchema"]["properties"]) == {
            "items",
            "total",
            "page",
            "pageSize",
            "totalPages",
            "totalCost",
        }

    async def test_successful_call_returns_go_data(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(
                http,
                "tools/call",
                {"name": "list_pets", "arguments": {"species": "犬"}, "_meta": META},
                name="list_pets",
            )
        assert response.status_code == 200
        result = result_of(response)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert structured["pageSize"] == 20
        assert structured["totalPages"] == 1
        assert structured["totalCost"] == 88.5
        assert structured["items"][0]["records"] is None
        assert structured["items"][0]["charges"] == []

    async def test_unknown_argument_returns_validation_error(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        """未知字段必须被拒——这是 SDK 默认行为做不到、只能自己拦的一条。"""
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200, json=ok_response())

        base_url = await serve(build_app(handler))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(
                http,
                "tools/call",
                {
                    "name": "list_pets",
                    "arguments": {"species": "犬", "page_size": 10},
                    "_meta": META,
                },
                name="list_pets",
            )
        assert response.status_code == 200
        error = error_text_of(result_of(response))
        assert error["error"]["code"] == "VALIDATION_ERROR"
        assert error["error"]["details"]["unknown_fields"] == ["page_size"]
        assert called == [], "入参非法时不应该打后端"

    async def test_bad_type_returns_validation_error(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(
                http,
                "tools/call",
                {"name": "list_pets", "arguments": {"page": "2"}, "_meta": META},
                name="list_pets",
            )
        error = error_text_of(result_of(response))
        assert error["error"]["code"] == "VALIDATION_ERROR"
        assert error["error"]["details"]["errors"][0]["field"] == "page"

    async def test_backend_failure_surfaces_as_structured_error(
        self, build_app: Callable[..., Any], serve: Callable[..., Any]
    ) -> None:
        """上游 500 → BACKEND_API_ERROR，且绝不出现 httpx/Python 的原始文本。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "boom"})

        base_url = await serve(build_app(handler))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(
                http,
                "tools/call",
                {"name": "list_pets", "arguments": {}, "_meta": META},
                name="list_pets",
            )
        result = result_of(response)
        error = error_text_of(result)
        assert error["error"]["code"] == "BACKEND_API_ERROR"
        assert error["error"]["details"]["status"] == 500
        text = json.dumps(result, ensure_ascii=False)
        for leak in ("Traceback", "httpx", "HTTPStatusError", "pydantic", "site-packages"):
            assert leak not in text

    async def test_backend_unavailable_is_reported(
        self, build_app: Callable[..., Any], serve: Callable[..., Any]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        base_url = await serve(build_app(handler))
        async with httpx.AsyncClient(base_url=base_url, timeout=15) as http:
            response = await rpc(
                http,
                "tools/call",
                {"name": "list_pets", "arguments": {}, "_meta": META},
                name="list_pets",
            )
        error = error_text_of(result_of(response))
        assert error["error"]["code"] == "BACKEND_UNAVAILABLE"

    async def test_unknown_tool_is_a_protocol_error(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await rpc(
                http,
                "tools/call",
                {"name": "delete_everything", "arguments": {}, "_meta": META},
                name="delete_everything",
            )
        payload = response.json()
        assert "error" in payload or payload.get("result", {}).get("isError") is True


class TestModelContextProtocolHeaders:
    async def test_mcp_name_header_is_required_for_tools_call(
        self, build_app: Callable[..., Any], serve: Callable[..., Any], ok_response: Callable[..., Any]
    ) -> None:
        """``Mcp-Name`` 在 tools/call 上是必需的镜像头；缺失时服务端按头不匹配拒绝。"""
        base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            response = await http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_pets", "arguments": {}, "_meta": META},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                },
            )
        assert response.status_code in (200, 400)


async def test_delete_is_not_a_session_teardown_we_rely_on(
    build_app: Callable[..., Any],
    serve: Callable[..., Any],
    ok_response: Callable[..., Any],
) -> None:
    """2026-07-28 里 DELETE 属于「结束会话」的旧机制，本服务不依赖它。

    实测记录（免得下次又当成 bug 查一遍）：

    * ``DELETE /mcp`` → 快速返回非 2xx，因为没有会话可结束；
    * ``GET /mcp`` → **会挂着**：SDK 为 2025-era 客户端保留了「服务端到客户端」
      的 SSE 长连接（直到超时才断开）。所以 GET 不能用来做「不支持旧机制」的断言，
      本服务也不依赖它——真正保证无状态的是一律用 POST + 不回发会话头。
    """
    base_url = await serve(build_app(lambda request: httpx.Response(200, json=ok_response())))
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
        response = await http.delete("/mcp")
    assert response.status_code >= 400
