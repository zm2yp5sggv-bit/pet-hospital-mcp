"""Go 宠物医院 REST API 的客户端。

职责边界
--------
* 只通过 HTTP 调用现有 Go 服务，不触碰它的代码、数据或配置。
* 负责超时、有限重试，并把「传输层/协议层/业务层」的各种失败**翻译**成
  :class:`~pet_hospital_mcp.errors.PetHospitalError`（带统一错误码）。
* 绝不把 HTTPX / Pydantic 的原始异常或文本抛给上层。

关于 ``trust_env``
----------------
本客户端固定使用 ``trust_env=False``。原因是一次真实踩坑：本机 ``HTTP_PROXY``
指向一个本地代理时，httpx 默认会走该代理，于是发往 ``127.0.0.1:8080`` 的请求
变成绝对 URL 形式的请求行（``GET http://127.0.0.1:8080/api/v1/pets HTTP/1.1``），
上游/中间件按普通路径匹配就会 404。上游是本地/内网服务时，环境代理是纯粹的
干扰源，因此这里显式关掉。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final, Mapping

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .errors import ErrorCode, PetHospitalError, summarize_validation_error

__all__ = ["PetItem", "PetsPage", "PetHospitalClient"]

RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 502, 503, 504})
"""值得重试的上游状态码：限流与「上游暂时不可用」。"""

UNAVAILABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({502, 503, 504})
"""重试耗尽后归为 BACKEND_UNAVAILABLE 的状态码。"""

_UPSTREAM_MESSAGE_KEYS: Final[tuple[str, ...]] = ("message", "msg", "error")
_MAX_UPSTREAM_MESSAGE: Final = 300

_MISSING: Final = object()


class PetItem(BaseModel):
    """``data.items[*]`` 里的单个宠物。

    只显式声明 ``records`` / ``charges``——因为 Go 的这两个字段**可能是 ``null``、
    也可能是数组**，必须显式兼容。其余字段（``id`` / ``name`` / ``ownerName`` /
    ``chipNo`` ……）由 ``extra="allow"`` 原样透传：本适配器不臆造业务字段，也不
    悄悄丢掉后端多给的字段。
    """

    model_config = ConfigDict(extra="allow")

    records: list[Any] | None = Field(
        default=None, description="病历记录；Go 侧可能是 null 或数组"
    )
    charges: list[Any] | None = Field(
        default=None, description="费用明细；Go 侧可能是 null 或数组"
    )


class PetsPage(BaseModel):
    """``GET /api/v1/pets`` 成功响应里的 ``data``。

    字段名与 Go 的 JSON 完全对应（``pageSize`` / ``totalPages`` / ``totalCost``），
    序列化时用 ``by_alias=True`` 还原成驼峰。
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    items: list[PetItem] = Field(description="当前页的宠物列表")
    total: int = Field(description="满足筛选条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(alias="pageSize", description="每页条数")
    total_pages: int = Field(alias="totalPages", description="总页数")
    total_cost: float | None = Field(
        default=None, alias="totalCost", description="费用合计；Go 侧可能为 null"
    )


class PetHospitalClient:
    """``GET /api/v1/pets`` 的异步客户端（带超时与有限重试）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        #: 暴露底层 httpx 客户端，便于测试断言与诊断。
        self.http = httpx.AsyncClient(
            base_url=settings.backend_base_url,
            timeout=httpx.Timeout(settings.backend_timeout_seconds),
            headers={"Accept": "application/json"},
            transport=transport,
            trust_env=False,
        )

    @property
    def settings(self) -> Settings:
        return self._settings

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> PetHospitalClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 调用

    async def list_pets(self, query: Mapping[str, Any] | None = None) -> PetsPage:
        """调用 ``GET /api/v1/pets``。

        :param query: 已按 Go 侧命名（``pageSize`` / ``min`` / ``max`` ……）构造好的
            查询参数；``None`` 值必须由调用方剔除。
        :raises PetHospitalError: 任何失败，都带统一错误码。
        """
        params = dict(query or {})
        settings = self._settings
        attempts = settings.backend_max_retries + 1

        last_exc: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                response = await self.http.get("/api/v1/pets", params=params)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await self._backoff(attempt)
                    continue
                raise PetHospitalError(
                    ErrorCode.BACKEND_TIMEOUT,
                    f"调用宠物医院服务超时（已重试 {attempts - 1} 次，"
                    f"超时上限 {settings.backend_timeout_seconds} 秒）。",
                    {
                        "url": settings.backend_list_pets_url,
                        "timeout_seconds": settings.backend_timeout_seconds,
                        "attempts": attempts,
                    },
                ) from exc
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await self._backoff(attempt)
                    continue
                raise PetHospitalError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "无法连接宠物医院服务，请确认 Go 服务已启动且 "
                    f"PET_HOSPITAL_BASE_URL（当前 {settings.backend_base_url}）正确。",
                    {
                        "url": settings.backend_list_pets_url,
                        "attempts": attempts,
                        "reason": type(exc).__name__,
                    },
                ) from exc

            if response.status_code in RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                last_exc = None
                await self._backoff(attempt)
                continue

            return self._parse_response(response)

        # 理论不可达：循环内每个分支要么 return 要么 raise。
        raise PetHospitalError(  # pragma: no cover - 防御性
            ErrorCode.INTERNAL_ERROR,
            "调用宠物医院服务失败。",
            {"reason": type(last_exc).__name__ if last_exc else "unknown"},
        )

    async def _backoff(self, attempt: int) -> None:
        delay = self._settings.backend_retry_backoff_seconds * (2**attempt)
        if delay > 0:
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ 解析

    def _parse_response(self, response: httpx.Response) -> PetsPage:
        status = response.status_code

        if not 200 <= status < 300:
            raise self._status_error(response)

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PetHospitalError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "宠物医院服务返回的不是合法 JSON。",
                {"status": status, "content_type": response.headers.get("content-type", "")},
            ) from exc

        data = self._extract_data(body, status)

        try:
            return PetsPage.model_validate(data)
        except ValidationError as exc:
            raise PetHospitalError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "宠物医院服务的响应结构与约定不符（data 字段不完整或类型不对）。",
                {"status": status, "errors": summarize_validation_error(exc)},
            ) from exc

    @staticmethod
    def _extract_data(body: Any, status: int) -> Mapping[str, Any]:
        """取出成功响应里的 ``data``。"""
        if not isinstance(body, dict):
            raise PetHospitalError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "宠物医院服务的响应顶层不是 JSON 对象。",
                {"status": status, "received_type": type(body).__name__},
            )

        data = body.get("data", _MISSING)
        if data is _MISSING:
            # 容错：直接把 data 平铺在顶层的实现，同样接受。
            if "items" in body:
                return body
            raise PetHospitalError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "宠物医院服务的响应缺少 data 字段。",
                {"status": status, "keys": sorted(str(k) for k in body)[:20]},
            )

        if not isinstance(data, dict):
            raise PetHospitalError(
                ErrorCode.BACKEND_INVALID_RESPONSE,
                "宠物医院服务的 data 字段不是 JSON 对象。",
                {"status": status, "received_type": type(data).__name__},
            )
        return data

    def _status_error(self, response: httpx.Response) -> PetHospitalError:
        status = response.status_code
        details: dict[str, Any] = {"status": status}
        upstream_message = self._upstream_message(response)
        if upstream_message:
            details["upstream_message"] = upstream_message

        if status in UNAVAILABLE_STATUS_CODES:
            return PetHospitalError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"宠物医院服务暂时不可用（HTTP {status}）。",
                details,
            )

        return PetHospitalError(
            ErrorCode.BACKEND_API_ERROR,
            f"宠物医院服务返回错误状态 HTTP {status}。",
            details,
        )

    @staticmethod
    def _upstream_message(response: httpx.Response) -> str | None:
        """尽力从上游错误体里抠一句可读信息；抠不到就算了，绝不拼原文。"""
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        for key in _UPSTREAM_MESSAGE_KEYS:
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_UPSTREAM_MESSAGE]
            if isinstance(value, dict):
                nested = value.get("message")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()[:_MAX_UPSTREAM_MESSAGE]
        return None
