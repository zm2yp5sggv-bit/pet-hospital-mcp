"""``list_pets`` 工具：把 ``GET /api/v1/pets`` 暴露给 MCP 客户端。

三件事必须说清楚，因为它们都是「照着旧版 FastMCP 经验猜」会踩雷的地方：

1. **入参校验不在函数签名里做。** ``@mcp.tool()`` / ``Tool.from_function`` 会按类型
   注解生成一个 ``extra`` 为默认（即静默丢弃未知键）的参数模型，且它抛出的校验
   错误是 Pydantic 原始文本。所以这里把签名写成「什么都收的过路参数」，把
   **未知字段判定 + 严格校验**搬进 :class:`ListPetsInput`，对外只吐统一错误形状。
   广告出去的 ``inputSchema`` 也换成 :class:`ListPetsInput` 的 schema，因此客户端
   看到的 enum / 上下界是真的（而不是 ``Any``）。
2. **失败用 ``CallToolResult(is_error=True)`` 返回，不 raise。** raise 会被 SDK 包成
   ``Error executing tool list_pets: ...``，破坏统一错误形状；raise ``MCPError``
   则会变成 JSON-RPC 协议错误，模型根本看不到。
3. **成功同时给 ``content`` 与 ``structuredContent``**，字段名与 Go 的 ``data`` 一致。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Final, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.tools.base import Tool
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..config import Settings
from ..errors import (
    INTERNAL_ERROR_MESSAGE,
    ErrorCode,
    PetHospitalError,
    error_result,
    summarize_validation_error,
)
from ..logging_config import log_tool_call
from ..rest_client import PetHospitalClient, PetsPage

__all__ = [
    "LIST_PETS_TOOL_NAME",
    "LIST_PETS_QUERY_PARAMS",
    "ListPetsInput",
    "build_list_pets_tool",
]

logger = logging.getLogger("pet_hospital_mcp.tools.list_pets")

LIST_PETS_TOOL_NAME: Final = "list_pets"

#: 允许且仅允许的查询参数（顺序即文档顺序）。
LIST_PETS_QUERY_PARAMS: Final[tuple[str, ...]] = (
    "q",
    "name",
    "ownerName",
    "ownerPhone",
    "species",
    "doctor",
    "disease",
    "status",
    "min",
    "max",
    "sortBy",
    "order",
    "page",
    "pageSize",
)

MAX_PAGE_SIZE: Final = 500

# ---------------------------------------------------------------------------
# 枚举值：已核实（2026-09-16），证据链两条且互相印证：
#
# 1. Go 服务源码 pet-hospital-mcp-teaching-main：
#    - internal/model/model.go  SpeciesDog..SpeciesOther / StatusWaiting..StatusChronic
#    - internal/api/api.go      handleMeta 的 sortFields 清单
#    - internal/store/store.go  sortPets：order 为空默认 desc、非 "desc"（忽略
#      大小写）一律按 asc；sortBy 为空或未知时按 id 排序；pageSize>500 钳到 500
# 2. 运行实例（pethospital.exe）：GET /api/v1/meta 返回的四组取值与源码一致，
#    且 species=犬 / status=住院中 / sortBy=ownerName&order=asc 实测生效。
#
# 注意：工具侧的 ``order`` 比 Go 收紧——后端大小写不敏感，这里只放行小写
# ``asc``/``desc``（白名单校验，属客户端自律，不改变后端行为）。
# 后端枚举若变更，同步本段四个常量与下方四个 ``Literal`` 别名（两处必须一致，
# tests/test_tool_registration.py 有测试锁定，只改一处会红）。
# ---------------------------------------------------------------------------

#: 与 Go 后端 ``GET /api/v1/meta`` 的 ``species`` 一致。
SPECIES_VALUES: Final[tuple[str, ...]] = (
    "犬",
    "猫",
    "兔",
    "鸟",
    "仓鼠",
    "爬宠",
    "其他",
)

#: 与 Go 后端 ``GET /api/v1/meta`` 的 ``status`` 一致。
STATUS_VALUES: Final[tuple[str, ...]] = (
    "待就诊",
    "就诊中",
    "住院中",
    "已康复",
    "慢性病随访",
)

#: 与 Go 后端 ``GET /api/v1/meta`` 的 ``sortFields`` 一致。
SORT_BY_VALUES: Final[tuple[str, ...]] = (
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
)

#: ``asc`` / ``desc``（工具侧收紧为小写；后端本身大小写不敏感）。
ORDER_VALUES: Final[tuple[str, ...]] = ("asc", "desc")

SpeciesValue = Literal["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
StatusValue = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SortByValue = Literal[
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
OrderValue = Literal["asc", "desc"]


LIST_PETS_DESCRIPTION: Final = f"""\
按条件查询宠物医院里的宠物病例列表。

**用途**：这是访问宠物医院业务数据的入口。可以按宠物信息、主人信息、主治医生、
疾病名称、就诊状态以及费用区间做组合筛选，并指定排序与分页。

**适用场景**：
- 用户问「有哪些宠物」「某位主人的宠物」「某医生在看哪些病例」；
- 需要按状态（如待就诊/住院中/已康复）或费用区间筛选；
- 需要一页一页翻看大量病例数据。

**参数**（全部可选，全部为「同时满足」的与条件）：
- `q`：模糊搜索关键词，匹配宠物名/主人/疾病等；
- `name`：宠物名称；
- `ownerName`：主人姓名；
- `ownerPhone`：主人手机号；
- `species`：物种，取值 {'/'.join(SPECIES_VALUES)}；
- `doctor`：主治医生姓名；
- `disease`：疾病名称；
- `status`：就诊状态，取值 {'/'.join(STATUS_VALUES)}；
- `min` / `max`：费用区间下界/上界，均为非负数字，且要求 `min <= max`；
- `sortBy`：排序字段，取值 {'/'.join(SORT_BY_VALUES)}；
- `order`：排序方向，取值 {'/'.join(ORDER_VALUES)}；
- `page`：页码，从 1 开始，默认 1；
- `pageSize`：每页条数，1..{MAX_PAGE_SIZE}，默认 20（由后端决定，超过 500 会被后端钳回 500）。

**返回值**：与后端 `data` 一一对应的一页数据 —— `items`（宠物列表，其中
`records`/`charges` 可能是 `null` 也可能是数组）、`total`、`page`、`pageSize`、
`totalPages`、`totalCost`。同时以 `structuredContent` 给出同一份结构化数据。

**失败时**：返回 `isError=true` 与统一错误形状
`{{"error": {{"code": ..., "message": ..., "details": {{}}}}}}`；`code` 可能是
VALIDATION_ERROR / BACKEND_TIMEOUT / BACKEND_UNAVAILABLE / BACKEND_API_ERROR /
BACKEND_INVALID_RESPONSE / INTERNAL_ERROR。
"""


class ListPetsInput(BaseModel):
    """``list_pets`` 的输入模型：严格校验，拒绝未知字段。

    **不要加 ``populate_by_name=True``**。那会让 ``page_size`` / ``owner_name`` 这类
    蛇形写法也被接受，等于凭空多出一批后端并不存在的「私有参数」。只认别名，
    与 ``GET /api/v1/pets`` 完全一致。
    """

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, description="模糊搜索关键词")
    name: str | None = Field(default=None, description="宠物名称")
    owner_name: str | None = Field(default=None, alias="ownerName", description="主人姓名")
    owner_phone: str | None = Field(default=None, alias="ownerPhone", description="主人手机号")
    species: SpeciesValue | None = Field(default=None, description="物种")
    doctor: str | None = Field(default=None, description="主治医生")
    disease: str | None = Field(default=None, description="疾病名称")
    status: StatusValue | None = Field(default=None, description="就诊状态")
    min_cost: float | None = Field(default=None, alias="min", ge=0, description="费用下界")
    max_cost: float | None = Field(default=None, alias="max", ge=0, description="费用上界")
    sort_by: SortByValue | None = Field(default=None, alias="sortBy", description="排序字段")
    order: OrderValue | None = Field(default=None, description="排序方向")
    page: int | None = Field(default=None, ge=1, description="页码，从 1 开始")
    page_size: int | None = Field(
        default=None, alias="pageSize", ge=1, le=MAX_PAGE_SIZE, description="每页条数"
    )

    # ------------------------------------------------------------- 严格类型

    @field_validator("page", "page_size", mode="before")
    @classmethod
    def _require_plain_integer(cls, value: Any) -> Any:
        """整数参数只接受 JSON 整数：布尔、字符串、小数一律拒绝。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("必须是整数（不接受字符串、小数或布尔值）")
        return value

    @field_validator("min_cost", "max_cost", mode="before")
    @classmethod
    def _require_finite_number(cls, value: Any) -> Any:
        """费用上下界只接受有限数字：拒绝 NaN / Infinity / 字符串 / 布尔。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("必须是数字（不接受字符串或布尔值）")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("必须是有限数字（不接受 NaN 或 Infinity）")
        return value

    @model_validator(mode="after")
    def _check_cost_range(self) -> ListPetsInput:
        if self.min_cost is not None and self.max_cost is not None and self.min_cost > self.max_cost:
            raise ValueError("min 不能大于 max")
        return self

    # --------------------------------------------------------------- 转查询

    def to_query(self) -> dict[str, Any]:
        """转成 Go 侧的查询参数：只用别名、剔除 ``None``、整数化整值浮点。"""
        dumped = self.model_dump(by_alias=True, exclude_none=True)
        return {
            key: int(value) if isinstance(value, float) and value.is_integer() else value
            for key, value in dumped.items()
        }


def _raw_arguments(ctx: Context, declared: dict[str, Any]) -> dict[str, Any]:
    """取**未经 SDK 过滤**的原始 ``arguments``。

    这是「拒绝未知字段」能成立的前提：SDK 生成的参数模型不带 ``extra="forbid"``，
    未知键在进入函数体之前就被静默丢掉了，所以必须回到 ``request_context.params``
    里拿原始字典。

    进程内直调（``MCPServer.call_tool``）没有请求上下文，此时退化为使用已声明的
    参数值——功能等价，只是检测不到未知字段。
    """
    try:
        params = ctx.request_context.params
    except (AttributeError, ValueError):
        params = None

    if params is None:
        return {key: value for key, value in declared.items() if value is not None}

    arguments = params.get("arguments")
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise PetHospitalError(
            ErrorCode.VALIDATION_ERROR,
            "arguments 必须是 JSON 对象。",
            {"received_type": type(arguments).__name__},
        )
    return dict(arguments)


def _declare_output_schema(tool: Tool, model: type[BaseModel]) -> None:
    """把 ``model`` 广告成工具的 ``outputSchema``。

    ``Tool.output_schema`` 是个读 ``fn_metadata`` 的 ``cached_property``（非 data
    descriptor），因此往实例 ``__dict__`` 里塞一个值即可遮蔽它。

    **为什么不直接写返回类型注解**：一旦声明了 output schema，SDK 会对**每一个**
    返回的 ``CallToolResult`` 做 ``output_model.model_validate(result.structured_content)``
    （见 ``mcp/server/mcpserver/utilities/func_metadata.py`` 的 ``convert_result``）。
    失败结果按规范不带 ``structuredContent``，于是会被判为校验失败、整个错误路径
    塌成 ``ToolError``。所以：签名上不声明输出模型（保证结果原样透传），schema
    通过这里单独广告。``tests/test_tool_registration.py`` 同时锁住了这两点。
    """
    tool.__dict__["output_schema"] = model.model_json_schema(by_alias=True)


def _success_result(page: PetsPage) -> CallToolResult:
    """成功结果：``content`` 给人看，``structuredContent`` 给程序用，字段名对齐 Go。"""
    return CallToolResult(
        content=[
            TextContent(type="text", text=page.model_dump_json(by_alias=True, indent=2))
        ],
        structured_content=page.model_dump(by_alias=True, mode="json"),
    )


async def _run_list_pets(client: PetHospitalClient, raw: dict[str, Any]) -> CallToolResult:
    unknown = sorted(set(raw) - set(LIST_PETS_QUERY_PARAMS))
    if unknown:
        raise PetHospitalError(
            ErrorCode.VALIDATION_ERROR,
            "存在不支持的查询参数：" + "、".join(unknown),
            {"unknown_fields": unknown, "allowed_fields": list(LIST_PETS_QUERY_PARAMS)},
        )

    try:
        payload = ListPetsInput.model_validate(raw)
    except ValidationError as exc:
        raise PetHospitalError(
            ErrorCode.VALIDATION_ERROR,
            "输入参数校验失败。",
            {"errors": summarize_validation_error(exc)},
        ) from exc

    page = await client.list_pets(payload.to_query())
    return _success_result(page)


def build_list_pets_tool(client: PetHospitalClient, settings: Settings) -> Tool:
    """构造 ``list_pets`` 工具。

    签名里的参数只是「过路槽位」：类型一律 ``Any``，使得 SDK 生成的参数模型永远
    不会先于本模块的严格校验失败。真正的线缆契约来自 :class:`ListPetsInput`，
    其 JSON Schema 会覆盖掉自动生成的 ``parameters``。
    """

    async def list_pets(
        ctx: Context,
        q: Any = None,
        name: Any = None,
        ownerName: Any = None,  # noqa: N803 - 与线缆参数名一致，便于对照
        ownerPhone: Any = None,  # noqa: N803
        species: Any = None,
        doctor: Any = None,
        disease: Any = None,
        status: Any = None,
        min: Any = None,  # noqa: A002 - 与线缆参数名一致
        max: Any = None,  # noqa: A002
        sortBy: Any = None,  # noqa: N803
        order: Any = None,
        page: Any = None,
        pageSize: Any = None,  # noqa: N803
    ) -> CallToolResult:
        declared = {
            "q": q,
            "name": name,
            "ownerName": ownerName,
            "ownerPhone": ownerPhone,
            "species": species,
            "doctor": doctor,
            "disease": disease,
            "status": status,
            "min": min,
            "max": max,
            "sortBy": sortBy,
            "order": order,
            "page": page,
            "pageSize": pageSize,
        }

        started = time.perf_counter()
        status_text = "ok"
        error_code: str | None = None
        try:
            try:
                raw = _raw_arguments(ctx, declared)
                result = await _run_list_pets(client, raw)
            except PetHospitalError as exc:
                status_text = "error"
                error_code = str(exc.code)
                result = error_result(exc.code, exc.message, exc.details)
            except Exception:  # noqa: BLE001 - 兜底：绝不外泄内部细节
                status_text = "error"
                error_code = str(ErrorCode.INTERNAL_ERROR)
                logger.exception("unhandled error in %s", LIST_PETS_TOOL_NAME)
                result = error_result(ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE)
            return result
        finally:
            log_tool_call(
                logger,
                tool_name=LIST_PETS_TOOL_NAME,
                params=declared,
                status=status_text,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error_code=error_code,
                extra={"backend_base_url": settings.backend_base_url},
                level=logging.WARNING if status_text == "error" else logging.INFO,
            )

    tool = Tool.from_function(
        list_pets,
        name=LIST_PETS_TOOL_NAME,
        description=LIST_PETS_DESCRIPTION,
    )
    # 用 ListPetsInput 的 schema 覆盖自动生成的宽松 schema：
    # 客户端因此能看到真实的 enum、上下界与 additionalProperties: false。
    tool.parameters = ListPetsInput.model_json_schema(by_alias=True)
    _declare_output_schema(tool, PetsPage)
    return tool
