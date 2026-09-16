"""运行期配置：全部来自环境变量，启动时一次性解析并校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Mapping

__all__ = [
    "ConfigError",
    "Settings",
    "DEFAULT_BACKEND_BASE_URL",
    "DEFAULT_MCP_HOST",
    "DEFAULT_MCP_PORT",
]

DEFAULT_BACKEND_BASE_URL: Final = "http://127.0.0.1:8080"
DEFAULT_MCP_HOST: Final = "127.0.0.1"
DEFAULT_MCP_PORT: Final = 8000
DEFAULT_MCP_HTTP_PATH: Final = "/mcp"
DEFAULT_BACKEND_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_BACKEND_MAX_RETRIES: Final = 2
DEFAULT_BACKEND_RETRY_BACKOFF_SECONDS: Final = 0.25
DEFAULT_LOG_LEVEL: Final = "INFO"

_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_MAX_PAGE_SIZE: Final = 500


class ConfigError(ValueError):
    """环境变量非法。启动即失败——配置错了就不该半死不活地跑起来。"""


def _read(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_str(env: Mapping[str, str], key: str, default: str) -> str:
    return _read(env, key) or default


def _read_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _read(env, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前为 {raw!r}") from exc


def _read_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _read(env, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是数字，当前为 {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """服务的全部运行期配置。

    上游地址只由 ``PET_HOSPITAL_BASE_URL`` 决定；监听地址与端口由 ``MCP_HOST`` /
    ``MCP_PORT`` 决定。其余键有安全默认值，仅在需要调整时设置。
    """

    backend_base_url: str = DEFAULT_BACKEND_BASE_URL
    backend_timeout_seconds: float = DEFAULT_BACKEND_TIMEOUT_SECONDS
    backend_max_retries: int = DEFAULT_BACKEND_MAX_RETRIES
    backend_retry_backoff_seconds: float = DEFAULT_BACKEND_RETRY_BACKOFF_SECONDS

    mcp_host: str = DEFAULT_MCP_HOST
    mcp_port: int = DEFAULT_MCP_PORT
    mcp_http_path: str = DEFAULT_MCP_HTTP_PATH
    mcp_log_level: str = DEFAULT_LOG_LEVEL

    service_name: str = "pet-hospital-mcp"
    service_version: str = "0.1.0"

    def __post_init__(self) -> None:
        if not self.backend_base_url.startswith(("http://", "https://")):
            raise ConfigError(
                "PET_HOSPITAL_BASE_URL 必须以 http:// 或 https:// 开头，"
                f"当前为 {self.backend_base_url!r}"
            )
        object.__setattr__(self, "backend_base_url", self.backend_base_url.rstrip("/"))

        if self.backend_timeout_seconds <= 0:
            raise ConfigError(
                f"PET_HOSPITAL_TIMEOUT_SECONDS 必须为正数，当前为 {self.backend_timeout_seconds}"
            )
        if self.backend_max_retries < 0:
            raise ConfigError(
                f"PET_HOSPITAL_MAX_RETRIES 不能为负数，当前为 {self.backend_max_retries}"
            )
        if self.backend_retry_backoff_seconds < 0:
            raise ConfigError(
                "PET_HOSPITAL_RETRY_BACKOFF_SECONDS 不能为负数，"
                f"当前为 {self.backend_retry_backoff_seconds}"
            )
        if not 1 <= self.mcp_port <= 65535:
            raise ConfigError(f"MCP_PORT 必须在 1..65535 之间，当前为 {self.mcp_port}")
        if not self.mcp_host:
            raise ConfigError("MCP_HOST 不能为空")
        if not self.mcp_http_path.startswith("/"):
            raise ConfigError(f"MCP_HTTP_PATH 必须以 / 开头，当前为 {self.mcp_http_path!r}")
        if self.mcp_log_level.upper() not in _LOG_LEVELS:
            raise ConfigError(
                f"MCP_LOG_LEVEL 必须是 {sorted(_LOG_LEVELS)} 之一，当前为 {self.mcp_log_level!r}"
            )

    @property
    def backend_list_pets_url(self) -> str:
        """``GET /api/v1/pets`` 的完整地址。"""
        return f"{self.backend_base_url}/api/v1/pets"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """从环境变量构造配置；``env`` 为空时读取 ``os.environ``。"""
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            backend_base_url=_read_str(
                source, "PET_HOSPITAL_BASE_URL", DEFAULT_BACKEND_BASE_URL
            ),
            backend_timeout_seconds=_read_float(
                source, "PET_HOSPITAL_TIMEOUT_SECONDS", DEFAULT_BACKEND_TIMEOUT_SECONDS
            ),
            backend_max_retries=_read_int(
                source, "PET_HOSPITAL_MAX_RETRIES", DEFAULT_BACKEND_MAX_RETRIES
            ),
            backend_retry_backoff_seconds=_read_float(
                source,
                "PET_HOSPITAL_RETRY_BACKOFF_SECONDS",
                DEFAULT_BACKEND_RETRY_BACKOFF_SECONDS,
            ),
            mcp_host=_read_str(source, "MCP_HOST", DEFAULT_MCP_HOST),
            mcp_port=_read_int(source, "MCP_PORT", DEFAULT_MCP_PORT),
            mcp_http_path=_read_str(source, "MCP_HTTP_PATH", DEFAULT_MCP_HTTP_PATH),
            mcp_log_level=_read_str(source, "MCP_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        )
