"""配置：默认值、环境变量覆盖、非法值立即失败。"""

from __future__ import annotations

import pytest

from pet_hospital_mcp.config import (
    DEFAULT_BACKEND_BASE_URL,
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    ConfigError,
    Settings,
)


class TestDefaults:
    def test_documented_defaults(self) -> None:
        settings = Settings.from_env({})
        assert settings.backend_base_url == "http://127.0.0.1:8080"
        assert settings.backend_base_url == DEFAULT_BACKEND_BASE_URL
        # MCP 默认只监听回环地址
        assert settings.mcp_host == DEFAULT_MCP_HOST == "127.0.0.1"
        assert settings.mcp_port == DEFAULT_MCP_PORT == 8000
        assert settings.mcp_http_path == "/mcp"

    def test_list_pets_url_matches_go_api(self) -> None:
        settings = Settings.from_env({})
        assert settings.backend_list_pets_url == "http://127.0.0.1:8080/api/v1/pets"

    def test_trailing_slash_is_normalised(self) -> None:
        settings = Settings.from_env({"PET_HOSPITAL_BASE_URL": "http://127.0.0.1:8080/"})
        assert settings.backend_list_pets_url == "http://127.0.0.1:8080/api/v1/pets"


class TestEnvOverrides:
    def test_required_knobs(self) -> None:
        settings = Settings.from_env(
            {
                "PET_HOSPITAL_BASE_URL": "https://pets.example.com",
                "MCP_HOST": "0.0.0.0",
                "MCP_PORT": "9123",
            }
        )
        assert settings.backend_base_url == "https://pets.example.com"
        assert settings.mcp_host == "0.0.0.0"
        assert settings.mcp_port == 9123

    def test_backend_tuning_knobs(self) -> None:
        settings = Settings.from_env(
            {
                "PET_HOSPITAL_TIMEOUT_SECONDS": "3.5",
                "PET_HOSPITAL_MAX_RETRIES": "0",
                "PET_HOSPITAL_RETRY_BACKOFF_SECONDS": "0.1",
                "MCP_HTTP_PATH": "/rpc",
                "MCP_LOG_LEVEL": "debug",
            }
        )
        assert settings.backend_timeout_seconds == 3.5
        assert settings.backend_max_retries == 0
        assert settings.backend_retry_backoff_seconds == 0.1
        assert settings.mcp_http_path == "/rpc"
        assert settings.mcp_log_level == "DEBUG"

    def test_blank_value_falls_back_to_default(self) -> None:
        settings = Settings.from_env({"MCP_HOST": "   "})
        assert settings.mcp_host == DEFAULT_MCP_HOST


class TestInvalidValues:
    @pytest.mark.parametrize(
        ("env", "hint"),
        [
            ({"PET_HOSPITAL_BASE_URL": "127.0.0.1:8080"}, "http"),
            ({"PET_HOSPITAL_BASE_URL": "ftp://x"}, "http"),
            ({"PET_HOSPITAL_TIMEOUT_SECONDS": "0"}, "正数"),
            ({"PET_HOSPITAL_TIMEOUT_SECONDS": "abc"}, "必须是数字"),
            ({"PET_HOSPITAL_MAX_RETRIES": "-1"}, "不能为负数"),
            ({"PET_HOSPITAL_MAX_RETRIES": "x"}, "必须是整数"),
            ({"MCP_PORT": "0"}, "MCP_PORT"),
            ({"MCP_PORT": "70000"}, "MCP_PORT"),
            ({"MCP_PORT": "abc"}, "必须是整数"),
            ({"MCP_HTTP_PATH": "mcp"}, "MCP_HTTP_PATH"),
            ({"MCP_LOG_LEVEL": "CHATTY"}, "MCP_LOG_LEVEL"),
        ],
    )
    def test_config_error(self, env: dict[str, str], hint: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            Settings.from_env(env)
        assert hint in str(excinfo.value)

    def test_constructing_settings_directly_is_also_validated(self) -> None:
        with pytest.raises(ConfigError):
            Settings(mcp_port=0)
