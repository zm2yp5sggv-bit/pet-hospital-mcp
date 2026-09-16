"""公共夹具。

⚠️ 第一件事是**清掉代理环境变量**——这不是洁癖，是本项目踩过的真实坑：

本机 ``HTTP_PROXY=http://127.0.0.1:55830`` 时，httpx 默认 ``trust_env=True``，
会把 ``http://127.0.0.1:<port>/mcp`` 的请求发成**绝对 URL 形式的请求行**
（``POST http://127.0.0.1:8951/mcp HTTP/1.1``），Starlette 按路径匹配就 404。
表现是「同一个客户端，第一个请求 200、第二个请求 404」这种最难查的假故障。
测试里必须隔离掉，否则结果依赖跑测试这台机器的环境变量。
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any, AsyncIterator, Callable, Iterator

import httpx
import pytest
import uvicorn

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.server import create_app

PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture(autouse=True, scope="session")
def _isolate_proxy_env() -> Iterator[None]:
    """会话级：临时移除代理环境变量，结束后原样还原。"""
    saved: dict[str, str | None] = {}
    for key in list(os.environ):
        if key.upper() in PROXY_ENV_KEYS:
            saved[key] = os.environ.pop(key)
    # 显式声明回环地址不走代理，双保险。
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def settings() -> Settings:
    """测试用配置：假上游地址、零退避（让重试测试跑得快）。"""
    return Settings(
        backend_base_url="http://go.pet-hospital.test",
        backend_timeout_seconds=1.0,
        backend_max_retries=2,
        backend_retry_backoff_seconds=0.0,
    )


# --------------------------------------------------------------------- 上游假数据


def backend_item(**overrides: Any) -> dict[str, Any]:
    """``data.items[*]`` 的最小真实形状。"""
    item: dict[str, Any] = {
        "id": 1,
        "name": "旺财",
        "species": "犬",
        "ownerName": "张三",
        "ownerPhone": "13800000000",
        "ownerAddr": "上海市某路 1 号",
        "chipNo": "CHIP-0001",
        "status": "待就诊",
        "records": None,
        "charges": [],
    }
    item.update(overrides)
    return item


def backend_data(**overrides: Any) -> dict[str, Any]:
    """``GET /api/v1/pets`` 成功响应里的 ``data``。"""
    data: dict[str, Any] = {
        "items": [backend_item()],
        "total": 1,
        "page": 1,
        "pageSize": 20,
        "totalPages": 1,
        "totalCost": 88.5,
    }
    data.update(overrides)
    return data


def backend_ok(**overrides: Any) -> dict[str, Any]:
    """完整成功响应（带 data 信封）。"""
    return {"code": 0, "message": "ok", "data": backend_data(**overrides)}


@pytest.fixture
def ok_response() -> Callable[..., dict[str, Any]]:
    return backend_ok


@pytest.fixture
def make_transport() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.MockTransport]:
    """把「请求 -> 响应」的函数包成 MockTransport。"""

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    return _make


@pytest.fixture
async def make_client(
    settings: Settings,
) -> AsyncIterator[Callable[[Callable[[httpx.Request], httpx.Response]], PetHospitalClient]]:
    """构造注入了 MockTransport 的 REST 客户端；退出时统一关闭。"""
    created: list[PetHospitalClient] = []

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> PetHospitalClient:
        client = PetHospitalClient(settings, transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    try:
        yield _make
    finally:
        for client in created:
            await client.aclose()


# ------------------------------------------------------------------ HTTP 真服务


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def build_app(
    settings: Settings,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], Any]:
    """构造完整的 ASGI 应用（上游用 MockTransport 顶替）。"""

    def _build(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
        return create_app(settings, transport=httpx.MockTransport(handler))

    return _build


@pytest.fixture
async def serve() -> AsyncIterator[Callable[[Any], str]]:
    """在真实端口上跑一个 uvicorn，返回 base_url。

    接收任意 ASGI 应用，因此可以在外层再包一层「请求记录器」来断言协议行为。
    """
    running: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []

    async def _serve(app: Any) -> str:
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        task = asyncio.create_task(server.serve())
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - 启动失败
            raise RuntimeError("uvicorn 未能在超时内启动")
        running.append((server, task))
        return f"http://127.0.0.1:{port}"

    try:
        yield _serve
    finally:
        for server, task in running:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                pass
