"""JSON 日志与递归脱敏。

约定
----
* 每次工具调用至少打一条 JSON 日志，字段包含 ``timestamp`` / ``tool_name`` /
  ``params`` / ``status`` / ``duration_ms``。
* ``ownerPhone`` / ``ownerAddr`` / ``chipNo`` 及其 snake_case（以及大小写变体）写法
  必须在**任意嵌套深度**被脱敏——它们在工具入参里可能出现在顶层，也可能藏在
  ``items[*]``、``records[*].ownerInfo`` 这类位置。
* 日志只写 stderr。stdio 传输下 stdout 是协议线缆，绝不能往里写东西。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Final, Mapping

__all__ = [
    "MASK",
    "JsonFormatter",
    "configure_logging",
    "log_tool_call",
    "redact",
    "normalize_key",
    "is_sensitive_key",
]

MASK: Final = "***REDACTED***"
"""脱敏后的占位符。"""

SENSITIVE_KEYS: Final[frozenset[str]] = frozenset({"ownerphone", "owneraddr", "chipno"})
"""归一化后的敏感字段名。"""

SENSITIVE_KEY_FRAGMENTS: Final[tuple[str, ...]] = ("phone", "chipno", "owneraddr")
"""归一化后包含这些片段的字段名同样脱敏（覆盖面更稳，例如 ``contactPhone``）。"""

_MAX_DEPTH: Final = 32
"""递归深度上限：防御恶意/异常的深层嵌套结构。"""


def normalize_key(key: Any) -> str:
    """把字段名归一化后比较：小写并去掉下划线、连字符、空格。

    ``ownerPhone`` / ``owner_phone`` / ``OWNER-PHONE`` / ``owner phone`` 归一化后都是
    ``ownerphone``。
    """
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def is_sensitive_key(key: Any) -> bool:
    """字段名是否属于需要脱敏的敏感字段。"""
    normalized = normalize_key(key)
    if not normalized:
        return False
    if normalized in SENSITIVE_KEYS:
        return True
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏：字典按键名判定，列表/元组/集合逐元素处理。

    返回值是新对象，不改动入参。命中敏感键的值一律替换为 :data:`MASK`——
    即使它是嵌套结构，也整体屏蔽，不保留任何部分。
    """
    if depth > _MAX_DEPTH:
        return "<max-depth-exceeded>"

    if isinstance(value, Mapping):
        return {
            key: (MASK if is_sensitive_key(key) else redact(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted_items = [redact(item, depth=depth + 1) for item in value]
        return tuple(redacted_items) if isinstance(value, tuple) else redacted_items
    if isinstance(value, (set, frozenset)):
        return {redact(item, depth=depth + 1) for item in value}
    return value


class JsonFormatter(logging.Formatter):
    """把日志记录渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }

        # 结构化字段：由 log_tool_call 通过 extra= 挂到 record 上。
        for key, value in getattr(record, "structured", {}).items():
            payload[key] = redact(value)

        payload["message"] = record.getMessage()

        if record.exc_info:
            # 只在服务端日志里留异常类型与消息；traceback 也留在本地，不外发。
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
            }

        for reserved in ("structured",):
            payload.pop(reserved, None)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """配置根 logger：JSON 格式、输出到 stderr。可重复调用（幂等）。"""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx / httpcore 的 INFO 会把每一次上游请求刷成一行「HTTP Request: ...」，
    # 淹掉真正有用的工具调用日志。压到 WARNING 留错误，去掉噪音。
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_tool_call(
    logger: logging.Logger,
    *,
    tool_name: str,
    params: Mapping[str, Any] | None,
    status: str,
    duration_ms: float,
    error_code: str | None = None,
    extra: Mapping[str, Any] | None = None,
    level: int = logging.INFO,
) -> None:
    """打一条工具调用日志。

    ``status`` 取 ``"ok"`` 或 ``"error"``。``params`` 会先脱敏再落盘。
    """
    structured: dict[str, Any] = {
        "event": "tool_call",
        "tool_name": tool_name,
        "params": redact(dict(params or {})),
        "status": status,
        "duration_ms": round(float(duration_ms), 3),
    }
    if error_code is not None:
        structured["error_code"] = str(error_code)
    if extra:
        for key, value in extra.items():
            structured.setdefault(key, value)

    logger.log(level, "tool call finished", extra={"structured": structured})
