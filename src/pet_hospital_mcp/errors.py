"""统一结构化错误：定义、构造，以及「什么异常映射成哪个 code」。

对外（MCP 客户端）只会有一种错误形状：

.. code-block:: json

    {"error": {"code": "ERROR_CODE", "message": "可读错误信息", "details": {}}}

任何 HTTPX / Pydantic / SDK / Python 异常都必须在这一层被翻译成上面的形状，
**不允许把原始异常文本、类型名或 traceback 透给客户端**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ErrorCode",
    "ErrorBody",
    "ErrorEnvelope",
    "PetHospitalError",
    "INTERNAL_ERROR_MESSAGE",
    "build_error_envelope",
    "error_result",
    "summarize_validation_error",
]


class ErrorCode(StrEnum):
    """对外错误码。阶段一固定为这六个。"""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    """工具入参不合法：未知字段、类型错误、越界、NaN/Infinity 等。"""

    BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
    """调用 Go 服务超时（重试已用尽）。"""

    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    """连不上 Go 服务：连接被拒、DNS 解析失败、网络不可达。"""

    BACKEND_API_ERROR = "BACKEND_API_ERROR"
    """Go 服务返回了非 2xx。"""

    BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"
    """Go 服务返回了非法 JSON，或 JSON 结构不符合约定。"""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """其余未归类异常——兜底，绝不外泄细节。"""


class ErrorBody(BaseModel):
    """错误体的三元组。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="错误码，取值见 ErrorCode")
    message: str = Field(description="可读错误信息，面向调用方的人/模型")
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """统一的对外错误信封。"""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


@dataclass(slots=True)
class PetHospitalError(Exception):
    """服务内部异常：携带对外错误码。

    上层（工具实现）只需 ``except PetHospitalError``，把它转成 MCP 工具错误结果；
    其余异常统一兜底为 ``INTERNAL_ERROR``。
    """

    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


def build_error_envelope(
    code: ErrorCode | str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """构造统一错误 dict（已用 Pydantic 模型校验过形状）。"""
    envelope = ErrorEnvelope(
        error=ErrorBody(code=str(code), message=message, details=details or {})
    )
    return envelope.model_dump(mode="json")


def error_result(
    code: ErrorCode | str, message: str, details: dict[str, Any] | None = None
) -> CallToolResult:
    """把统一错误形状包装成**失败的**工具调用结果。

    这是 SDK 2.x 里让调用方模型「看得见」错误的方式：返回
    ``CallToolResult(is_error=True, ...)``。注意

    * Python 侧属性名是 ``is_error``（线缆上序列化为 ``isError``）；
    * 不要走 ``raise ToolError``——SDK 会给消息加上 ``Error executing tool ...``
      前缀，破坏统一错误形状；
    * 更不要 ``raise MCPError``——那会变成 JSON-RPC 协议错误，客户端直接抛异常、
      模型看不到内容。
    """
    envelope = build_error_envelope(code, message, details)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(envelope, ensure_ascii=False),
            )
        ],
        is_error=True,
    )


def summarize_validation_error(exc: BaseException) -> list[dict[str, str]]:
    """把 Pydantic 的 ``ValidationError`` 压成安全的 ``[{field, message}]``。

    只保留字段位置与一句短消息，**丢掉**原始输入、异常类型名、``errors.pydantic.dev``
    链接等内部信息。非 Pydantic 异常返回空列表。
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return []
    summarized: list[dict[str, str]] = []
    try:
        raw_errors = errors()
    except Exception:  # pragma: no cover - 防御性
        return []
    for item in raw_errors:
        location = item.get("loc") or ()
        field_path = ".".join(str(part) for part in location) or "<body>"
        summarized.append({"field": field_path, "message": str(item.get("msg", ""))})
    return summarized


INTERNAL_ERROR_MESSAGE: Final = "服务内部错误，请稍后重试或联系维护者。"
"""兜底错误文案：不含任何异常类型名、路径或堆栈。"""
