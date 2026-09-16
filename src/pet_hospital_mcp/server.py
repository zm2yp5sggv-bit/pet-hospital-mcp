"""服务装配与启动入口。

装配方式（与 SDK 文档一致）
--------------------------
``mcp.streamable_http_app()`` 返回一个 Starlette 应用，里面只有 ``/mcp`` 一条路由，
以及一个启动 ``mcp.session_manager`` 的 lifespan。我们把它 ``Mount("/")`` 进自己的
Starlette 应用，于是：

* ``/health``（由 ``@mcp.custom_route`` 注册）与 ``/mcp`` 同处一个应用；
* **挂载会废掉子应用自带的 lifespan**，所以必须在宿主应用自己的 lifespan 里
  ``async with mcp.session_manager.run()``，否则第一个请求会以
  ``RuntimeError: Task group is not initialized`` 收场；
* 顺带在这里关闭上游 HTTP 连接池，进程退出时不留悬挂连接。

无状态 Streamable HTTP
----------------------
``streamable_http_app(stateless_http=True)``：每个请求一个全新 transport，不做会话
跟踪，因此**没有** ``Mcp-Session-Id``、没有会话存储/过期、没有 ``max_sessions``。
协议修订版由 SDK 决定（``mcp.types.LATEST_PROTOCOL_VERSION``，本版本为 2026-07-28）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Final

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from .config import Settings
from .logging_config import configure_logging
from .rest_client import PetHospitalClient
from .tools import build_all_tools

__all__ = ["HEALTH_PATH", "create_mcp_server", "create_app", "main"]

HEALTH_PATH: Final = "/health"

SERVER_INSTRUCTIONS: Final = """\
宠物医院业务数据服务。当前只提供 list_pets 一个工具，用于按条件查询宠物病例列表。

调用要点：全部参数可选；物种/状态/排序字段/排序方向使用后端约定的英文取值；
费用区间用 min/max 且 min 不得大于 max；分页用 page（从 1 开始）与 pageSize（1..500）。

失败时返回 isError=true 与统一错误形状 {"error": {"code", "message", "details"}}，
请据 code 判断是入参问题（VALIDATION_ERROR）还是后端问题（BACKEND_*）。
"""


def create_mcp_server(settings: Settings, client: PetHospitalClient) -> MCPServer:
    """构造 ``MCPServer``：注册工具、``/health``，并声明服务身份。"""
    mcp: MCPServer = MCPServer(
        name=settings.service_name,
        version=settings.service_version,
        instructions=SERVER_INSTRUCTIONS,
        log_level=settings.mcp_log_level,
        tools=build_all_tools(client, settings),
    )

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(request: Request) -> Response:  # noqa: ARG001 - Starlette handler 签名
        """存活探针。

        只报告进程自身状态，**不**调用上游：上游宕机不应该让本服务被判为不健康
        （否则编排系统会把唯一能解释「上游为什么挂了」的服务也一起重启）。
        """
        return JSONResponse(
            {
                "status": "ok",
                "service": settings.service_name,
                "version": settings.service_version,
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "backendBaseUrl": settings.backend_base_url,
                "transport": "streamable-http",
                "stateful": False,
            }
        )

    return mcp


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """构造完整的 ASGI 应用（``/mcp`` + ``/health``）。

    :param transport: 仅测试使用，注入 ``httpx.MockTransport`` 之类，使 HTTP 层
        测试不必启动真实 Go 服务。
    """
    configure_logging(settings.mcp_log_level)

    client = PetHospitalClient(settings, transport=transport)
    mcp = create_mcp_server(settings, client)

    # 注意：必须在 Mount 之前调用，session_manager 只有在这之后才存在。
    inner_app = mcp.streamable_http_app(
        stateless_http=True,
        streamable_http_path=settings.mcp_http_path,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:  # noqa: ARG001
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await client.aclose()

    return Starlette(routes=[Mount("/", app=inner_app)], lifespan=lifespan)


def main() -> None:
    """``python -m pet_hospital_mcp`` / ``pet-hospital-mcp`` 的入口。"""
    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.mcp_log_level.lower(),
    )
