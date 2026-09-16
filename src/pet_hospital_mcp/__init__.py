"""宠物医院 MCP 服务（阶段一）。

本包只做一件事：把现有 Go 宠物医院 REST API 的能力，以 MCP 工具的形式暴露给 AI Agent。

设计边界（阶段一，见 ``UPGRADE_PROMPT.md``）：

* Go 服务是唯一业务后端，本包**只通过 HTTP 调用它**，不碰它的代码、数据库或配置。
* 本服务是独立的 Python 进程，对外只暴露一个 MCP 端点（默认 ``/mcp``）和一个 ``/health``。
* 只实现 ``list_pets`` 一个工具，不做阶段二。

它构建在官方 MCP Python SDK v2（``mcp==2.0.0``）之上，使用 ``MCPServer`` 与
**无状态 Streamable HTTP**（协议修订版 ``2026-07-28``）：没有 ``initialize`` 握手、
没有 ``Mcp-Session-Id``、没有会话存储与过期。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
