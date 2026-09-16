"""工具注册表。

**扩展方式（阶段二及以后）**：在 ``tools/`` 下新增一个模块，实现一个
``build_xxx_tool(client, settings) -> Tool``，然后把它加进 :data:`TOOL_BUILDERS`。
REST 客户端、日志约定、统一错误形状、入参校验套路都在本包之外，直接复用即可。

阶段一只有一个工具：``list_pets``。
"""

from __future__ import annotations

from typing import Callable, Final

from mcp.server.mcpserver.tools.base import Tool

from ..config import Settings
from ..rest_client import PetHospitalClient
from .list_pets import build_list_pets_tool

__all__ = ["TOOL_BUILDERS", "build_all_tools"]

ToolBuilder = Callable[[PetHospitalClient, Settings], Tool]

#: 每个条目：``(client, settings) -> Tool``。
TOOL_BUILDERS: Final[tuple[ToolBuilder, ...]] = (build_list_pets_tool,)


def build_all_tools(client: PetHospitalClient, settings: Settings) -> list[Tool]:
    """构造全部工具。"""
    return [builder(client, settings) for builder in TOOL_BUILDERS]
