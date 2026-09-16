"""工具注册与 schema 契约。

这一组测试是「防漂移」的：签名里的过路参数、``ListPetsInput`` 的字段、广告出去的
``inputSchema`` 三者必须始终一致；枚举常量与其 Literal 类型也必须一致。
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

import pytest

from pet_hospital_mcp import __file__ as package_init
from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.tools import build_all_tools
from pet_hospital_mcp.tools.list_pets import (
    LIST_PETS_DESCRIPTION,
    LIST_PETS_QUERY_PARAMS,
    LIST_PETS_TOOL_NAME,
    ORDER_VALUES,
    SORT_BY_VALUES,
    SPECIES_VALUES,
    STATUS_VALUES,
    ListPetsInput,
    build_list_pets_tool,
)

PACKAGE_DIR = Path(package_init).parent


@dataclass(frozen=True)
class CodeSurface:
    """一个模块「真正写了什么」的摘要，用来做源码级约束检查。"""

    identifiers: frozenset[str]
    keyword_names: frozenset[str]
    literals: frozenset[str]


def _code_surface(path: Path) -> CodeSurface:
    """解析模块，收集标识符、关键字参数名与非 docstring 字符串字面量。

    刻意**不**做全文子串匹配：注释与文档里解释「我们不用 FastMCP / 不做会话」是
    正常的，把它当成违规会让检查变成噪音，最终被无视——那比没有检查更糟。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    identifiers: set[str] = set()
    keyword_names: set[str] = set()
    literals: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                identifiers.add(alias.name)
                identifiers.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                identifiers.add(node.module)
                identifiers.add(node.module.split(".")[0])
            for alias in node.names:
                identifiers.add(alias.name)
        elif isinstance(node, ast.keyword) and node.arg:
            keyword_names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                literals.add(node.value)

    return CodeSurface(
        identifiers=frozenset(identifiers),
        keyword_names=frozenset(keyword_names),
        literals=frozenset(literals),
    )


def _all_code_surfaces() -> list[tuple[Path, CodeSurface]]:
    return [(path, _code_surface(path)) for path in sorted(PACKAGE_DIR.rglob("*.py"))]


@pytest.fixture
async def tool(settings: Settings):
    async with PetHospitalClient(settings) as client:
        yield build_list_pets_tool(client, settings)


async def test_only_one_tool_is_registered(settings: Settings) -> None:
    """阶段一：只有一个 MCP 工具。"""
    async with PetHospitalClient(settings) as client:
        tools = build_all_tools(client, settings)
    assert [t.name for t in tools] == [LIST_PETS_TOOL_NAME]


def test_tool_name_is_snake_case(tool: Any) -> None:
    assert tool.name == "list_pets"
    assert tool.name.islower()
    assert " " not in tool.name


def test_description_documents_purpose_params_and_return(tool: Any) -> None:
    """工具描述必须说明用途、参数、适用场景、返回值。"""
    description = LIST_PETS_DESCRIPTION
    assert tool.description == description

    for marker in ("用途", "适用场景", "参数", "返回值"):
        assert marker in description, f"描述缺少「{marker}」章节"

    for param in LIST_PETS_QUERY_PARAMS:
        assert f"`{param}`" in description, f"描述缺少参数 {param}"

    for field in ("items", "total", "page", "pageSize", "totalPages", "totalCost"):
        assert f"`{field}`" in description, f"描述缺少返回字段 {field}"

    for code in (
        "VALIDATION_ERROR",
        "BACKEND_TIMEOUT",
        "BACKEND_UNAVAILABLE",
        "BACKEND_API_ERROR",
        "BACKEND_INVALID_RESPONSE",
        "INTERNAL_ERROR",
    ):
        assert code in description, f"描述缺少错误码 {code}"


def test_input_schema_advertises_exactly_the_documented_params(tool: Any) -> None:
    schema = tool.parameters
    assert set(schema["properties"]) == set(LIST_PETS_QUERY_PARAMS)
    assert schema["additionalProperties"] is False
    assert schema["type"] == "object"
    # 全部可选
    assert schema.get("required", []) == []


def test_input_schema_carries_real_constraints(tool: Any) -> None:
    props = tool.parameters["properties"]

    def enum_of(name: str) -> list[str]:
        for branch in props[name]["anyOf"]:
            if "enum" in branch:
                return list(branch["enum"])
        raise AssertionError(f"{name} 没有 enum 约束")

    assert enum_of("species") == list(SPECIES_VALUES)
    assert enum_of("status") == list(STATUS_VALUES)
    assert enum_of("sortBy") == list(SORT_BY_VALUES)
    assert enum_of("order") == list(ORDER_VALUES)

    page_bounds = next(b for b in props["page"]["anyOf"] if "minimum" in b)
    assert page_bounds["minimum"] == 1

    size_bounds = next(b for b in props["pageSize"]["anyOf"] if "maximum" in b)
    assert size_bounds["minimum"] == 1
    assert size_bounds["maximum"] == 500

    for name in ("min", "max"):
        bounds = next(b for b in props[name]["anyOf"] if "minimum" in b)
        assert bounds["minimum"] == 0


def test_output_schema_matches_go_data_fields(tool: Any) -> None:
    """``outputSchema`` 必须广告 Go 侧的驼峰字段名。"""
    schema = tool.output_schema
    assert schema is not None
    assert set(schema["properties"]) == {
        "items",
        "total",
        "page",
        "pageSize",
        "totalPages",
        "totalCost",
    }


def test_literal_types_match_enum_constants() -> None:
    """``Literal[...]`` 与 ``*_VALUES`` 元组是两份数据，必须一模一样。"""
    model = ListPetsInput
    cases = {
        "species": SPECIES_VALUES,
        "status": STATUS_VALUES,
        "sort_by": SORT_BY_VALUES,
        "order": ORDER_VALUES,
    }
    for field_name, expected in cases.items():
        annotation = model.model_fields[field_name].annotation
        literal_args = None
        for arg in get_args(annotation):
            if get_args(arg):
                literal_args = get_args(arg)
                break
        assert literal_args is not None, f"{field_name} 不是 Literal 类型"
        assert set(literal_args) == set(expected), f"{field_name} 的 Literal 与常量不一致"


async def test_signature_passthrough_params_mirror_wire_names(settings: Settings) -> None:
    """签名里的过路参数必须与线缆参数名一一对应（顺序无关，集合相等）。

    签名只用来让 SDK 生成的参数模型接受这些键；真正的契约在 ``ListPetsInput``。
    两者一旦漂移，「未知字段」判定就会误报。
    """
    async with PetHospitalClient(settings) as client:
        tool = build_list_pets_tool(client, settings)
    signature = inspect.signature(tool.fn)
    declared = {
        name
        for name, param in signature.parameters.items()
        if name not in {"ctx"} and param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    }
    assert declared == set(LIST_PETS_QUERY_PARAMS)


def test_no_fastmcp_import_anywhere() -> None:
    """阶段一明确不实现、不兼容、不迁移 SDK 1.x 的 FastMCP。

    按 **AST 识别符**判定，而不是全文搜字符串——文档里为了解释「不走 FastMCP 老路」
    本来就会提到这个词，全文搜会误报自己。
    """
    offenders = [
        f"{path.name}: {sorted(name for name in surface.identifiers if 'fastmcp' in name.lower())}"
        for path, surface in _all_code_surfaces()
        if any("fastmcp" in name.lower() for name in surface.identifiers)
    ]
    assert offenders == [], f"发现 FastMCP 真实引用：{offenders}"


def test_no_session_machinery_in_source() -> None:
    """无状态：代码里不得出现会话存储、会话过期、可恢复 SSE 等旧机制。

    同样按 AST 判定（关键字参数 + 非 docstring 字符串字面量），避免把说明性文字
    当成实现。
    """
    forbidden_kwargs = {
        "max_sessions",
        "session_idle_timeout",
        "event_store",
        "retry_interval",
    }
    forbidden_literals = {"mcp-session-id"}

    hits: list[str] = []
    for path, surface in _all_code_surfaces():
        hits += [
            f"{path.name}: 传入了 {name}" for name in sorted(surface.keyword_names & forbidden_kwargs)
        ]
        hits += [
            f"{path.name}: 出现字面量 {value!r}"
            for value in sorted(surface.literals)
            if value.lower() in forbidden_literals
        ]
    assert hits == [], f"发现会话机制痕迹：{hits}"


def test_stateless_http_is_explicitly_enabled() -> None:
    """不是「恰好没配」，而是**显式**开启无状态——且值必须是字面量 ``True``。

    只断言「出现了这个关键字」是不够的：把 ``stateless_http=True`` 改成
    ``False`` 关键字依然在，检查照样绿。所以这里连值一起验。
    """
    path = PACKAGE_DIR / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "stateless_http"
    ]
    assert values, "server.py 必须显式传入 stateless_http"
    for value in values:
        assert isinstance(value, ast.Constant) and value.value is True, (
            f"stateless_http 必须是字面量 True，实际为 {ast.dump(value)}"
        )


def test_stateless_http_is_enabled(settings: Settings) -> None:
    """应用装配必须使用无状态 Streamable HTTP。"""
    from pet_hospital_mcp.server import create_app

    app = create_app(settings)
    # Mount("/", app=<StreamableHTTPASGIApp route>)
    inner = app.routes[0]
    inner_paths = [getattr(route, "path", None) for route in inner.app.routes]
    assert "/mcp" in inner_paths
    assert "/health" in inner_paths


def test_error_shape_matches_contract() -> None:
    """统一错误形状：顶层只有 error.{code,message,details}。"""
    from pet_hospital_mcp.errors import ErrorCode, build_error_envelope

    envelope = build_error_envelope(ErrorCode.VALIDATION_ERROR, "坏输入", {"a": 1})
    assert set(envelope) == {"error"}
    assert set(envelope["error"]) == {"code", "message", "details"}
    assert json.loads(json.dumps(envelope)) == envelope
