# UPGRADE_PROMPT.md — 阶段二扩展指令

> 用途：把这个文件**整份**作为提示词喂给 AI 编码助手（或人），即可在**不返工**的前提下
> 给 `pet-hospital-mcp` 加上阶段二的工具。
>
> 阶段一已交付并验证：**只有** `list_pets` 一个工具，178 个测试全绿。
> 下面每一条「必须 / 禁止」都是从阶段一真金白银踩出来的，别凭旧版 FastMCP 经验改。

---

## 0. 先把事实核实清楚，再动手

**禁止在没核实的情况下写任何断言。** 阶段一之所以留下四组「暂定枚举值」，
就是因为当时拿不到 Go 源码。阶段二开始前，**第一件事**是把下面这些都变成有证据的事实：

```bash
# 1) 找到目标 handler
grep -rn -A30 "api/v1/<resource>" <go-repo>

# 2) 抄出真实的枚举取值（这是阶段一唯一的未核实项）
grep -rn 'case "' <go-repo>/internal/...

# 3) 抄出真实的响应结构（字段名、是否可能为 null、是否可能为数组）
grep -rn -A20 'json:"' <go-repo>/internal/... | head -100

# 4) 确认端点方法、路径、查询参数全集
grep -rn 'router\.\|\.GET(\|\.POST(\|\.PUT(\|\.DELETE(' <go-repo>
```

拿不到源码时：**直接打一次真实接口看响应**，把 `curl -s` 的原始输出贴进笔记里当证据。
**不要猜，不要「应该差不多」。**

---

## 1. 要交付什么

按需选择（**一次只加一个工具，加完跑测试，再下一个**）：

| 工具名 | 对应端点 | 输入 | 典型分组 |
| --- | --- | --- | --- |
| `list_pets` | `GET /api/v1/pets` | 已实现 | ✅ 阶段一 |
| `get_pet` | `GET /api/v1/pets/{id}` | `id` | 详情 |
| `create_pet` | `POST /api/v1/pets` | 宠物字段 + 主人字段 | 写入 |
| `update_pet` | `PUT/PATCH /api/v1/pets/{id}` | `id` + 待改字段 | 写入 |
| `delete_pet` | `DELETE /api/v1/pets/{id}` | `id` | 写入（**高风险，见 §6**） |
| `list_records` | 病历记录端点 | 视后端而定 | 子资源 |
| `list_charges` | 费用明细端点 | 视后端而定 | 子资源 |

**每加一个工具就是一次完整的走查**：枚举核实 → 输入模型 → 错误映射 → 注册 →
静态测试 → HTTP 端到端测试 → mutation test。

---

## 2. 唯一正确的加工具姿势

### 2.1 文件

新建 `src/pet_hospital_mcp/tools/<tool_name>.py`，**照抄 `list_pets.py` 的骨架**。
不要另起炉灶，不要「顺手重构一下」——阶段一的骨架是逐条对着 SDK 源码验证出来的。

### 2.2 必须是这五件事

```python
# ① 输入模型：extra="forbid"，严格类型校验，别名对齐 Go 的线缆参数名
class GetPetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")   # ← 不许省
    # ... 字段，payload 用 alias

# ② 只接受别名，不许加 populate_by_name=True
#    ← 加了会让 page_size / owner_name 这类蛇形写法被接受，
#      等于凭空变出一批后端不存在的「私有参数」

# ③ 签名是「过路槽位」：类型一律 Any，保证 SDK 生成的参数模型永远不会先失败
async def get_pet(ctx: Context, id: Any = None) -> CallToolResult: ...

# ④ 原始 arguments 从 request_context.params 里取
raw = _raw_arguments(ctx, declared)      # ← 必须，否则未知字段被 SDK 静默丢弃

# ⑤ 覆盖 parameters 与手工挂 outputSchema
tool = Tool.from_function(fn, name=NAME, description=DESC)
tool.parameters = InputModel.model_json_schema(by_alias=True)
_declare_output_schema(tool, "<对应输出模型>")
```

> `_raw_arguments` 与 `_declare_output_schema` 目前是 `list_pets.py` 里的私有函数。
> 加到第三个工具时，可以把它们上提到一个新模块
> `src/pet_hospital_mcp/tools/_support.py`——**但这是重构，必须单独一次提交，
> 且重构前后 `pytest -q` 必须同样全绿。**

### 2.3 注册

只改 `src/pet_hospital_mcp/tools/__init__.py`：

```python
TOOL_BUILDERS: Final[tuple[ToolBuilder, ...]] = (
    build_list_pets_tool,
    build_get_pet_tool,          # ← 只加这一行
)
```

`build_all_tools` 会自动带上。**不要**去 `server.py` 里手工注册。

---

## 3. 五条硬约束（违反任何一条，测试会红）

| # | 约束 | 为什么 |
| --- | --- | --- |
| 1 | **不 `raise ToolError`** | SDK 会包成 `Error executing tool ...: `，破坏统一错误形状 |
| 2 | **不 `raise MCPError`** | 变成 JSON-RPC 协议错误，**模型根本看不到** |
| 3 | **返回值类型写 `CallToolResult`**，输出 schema 手工挂 | 一旦声明返回类型，SDK 会对**每个**结果（含失败）做校验，错误路径会塌成 `ToolError` |
| 4 | **错误一律 `return error_result(...)`** | 唯一的失败形状：`{"error": {"code", "message", "details"}}` |
| 5 | **`INTERNAL_ERROR` 只回固定文案** | `INTERNAL_ERROR_MESSAGE`，绝不上抛 traceback / 内部路径 / 上游原文 |

错误码**复用** `errors.ErrorCode`，**不要新造一套**。现有 6 个：

* `VALIDATION_ERROR` —— 参数问题，模型该改参数
* `BACKEND_TIMEOUT` / `BACKEND_UNAVAILABLE` / `BACKEND_API_ERROR` / `BACKEND_INVALID_RESPONSE` —— 后端问题
* `INTERNAL_ERROR` —— 兜底

确实需要新码时（比如写操作的 `BACKEND_CONFLICT`），在 `ErrorCode` 里加，并**同步更新
`tests/test_errors.py` 与 README 的错误码表**。

---

## 4. `rest_client.py` 的扩展规矩

阶段一只实现了 `list_pets`。加新端点时：

1. **保持 `trust_env=False`。** 这不是洁癖——本机有 `HTTP_PROXY` 时，httpx 会把
   `127.0.0.1` 的请求写成绝对 URL 请求行，中间件按普通路径匹配就是 **404**。
2. **复用重试/翻译逻辑**，别复制粘贴。把「发请求 + 超时/传输错误翻译 + 状态码归类」
   提炼成一个私有 helper（如 `_request(method, path, **kw)`），各端点调它。
   重试状态码仍然是 `429 / 502 / 503 / 504`。
3. **输出模型 `extra="allow"`**，只显式声明「可能是 `null` 或数组」这类怪字段。
   本适配器**不臆造业务字段，也不悄悄丢掉后端多给的字段**。
4. **裸数据容错保留**：`_extract_data` 既认 `{"data": {...}}`，也认把字段平铺在顶层的实现。
5. **绝不把 httpx / Pydantic 的原始异常或文本抛给上层**——一律翻译成 `PetHospitalError`。
   上游错误体里只允许抠 `message` / `msg` / `error` 一句话（截断到 300 字符），
   **不要拼原文**，否则可能把上游的堆栈或连接串漏出去。

---

## 5. 日志与脱敏

新增工具**必须**走 `log_tool_call`，字段沿用：

```
timestamp / level / logger / tool_name / params / status / duration_ms
```

* `status` 只有 `"ok"` / `"error"`；出错时附 `error_code`。
* 出错用 `level=WARNING`，成功用 `INFO`，`logger.exception` 只用于兜底分支。
* **`params` 直接传原始 dict 即可**——`redact()` 会在序列化时递归脱敏。
  但如果你新增了敏感字段（身份证、银行卡、地址……），**必须在
  `logging_config.py` 的敏感键集合里登记**，否则它会以明文进日志。
  `normalize_key` 会先小写并去掉非字母数字，所以 `owner_phone` / `OwnerPhone`
  一视同仁。
* **往 `log_tool_call` 传的是「客户端给的原始值」**，不是校验后的模型——
  这样非法输入也能在日志里看到（脱敏后）。

---

## 6. 写操作（POST / PUT / DELETE）的额外要求

阶段一全是只读。**写操作的风险等级完全不同**，必须：

1. **工具的 `description` 里明确写「这会修改后端数据」**，让模型知道这不是查询。
2. **`delete_pet` 默认不要加。** 真要加，先跟人确认；并且 id 不存在时
   后端返回 404 要映射成 `BACKEND_API_ERROR` 而不是静默「成功」。
3. **不做自动重试的幂等假设。** 当前 `list_pets` 会重试 `429/502/503/504`——
   这对 GET 是安全的。**POST 重试可能造成重复写入**：写操作要么不重试，
   要么先核实后端是否有幂等键，并在代码注释里写清楚依据。
4. **请求体字段用 `extra="forbid"` 的模型严格校验**，「用户没传」和「用户传了 null」
   在 PATCH 语义下是两件事，别用 `exclude_unset=False` 混过去。
5. **给写操作补一条「后端返回非 2xx 时绝不报成功」的测试。**
6. 阶段一**没有**任何鉴权层。若写操作需要凭证，**先在方案里说清楚怎么注入**
   （环境变量？反向代理？），不要硬编码，也不要写进日志或仓库。

---

## 7. 测试要求（一个都不能少）

新工具至少要有这四类测试：

```python
# ① 静态约束（加到 tests/test_tool_registration.py）
#    - tools/list 里工具数量与名字集合正确
#    - inputSchema 与文档一致，enum / 上下界是真的
#    - outputSchema 是驼峰
#    - Literal 别名与常量元组一致
#    - 仍然没有 fastmcp 导入、没有会话机制

# ② 输入模型单测（新建 tests/test_<tool>_input.py）
#    - 未知字段被拒
#    - 蛇形写法被拒（page_size 这种）
#    - 类型不对被拒（字符串数字、bool、NaN/Inf）
#    - 边界值

# ③ REST 客户端单测（加到 tests/test_rest_client.py，全走 MockTransport）
#    - 成功 / 4xx / 5xx / 超时 / 连不上 / 非法 JSON / data 缺失或类型错

# ④ HTTP 端到端（加到 tests/test_stateless_http.py，真 uvicorn + 随机端口）
#    - 正常调用拿到后端数据
#    - 未知参数 → VALIDATION_ERROR，且断言「后端没被调用」
#    - 后端 500 → BACKEND_API_ERROR，且断言不泄漏上游原文
#    - 连不上 → BACKEND_UNAVAILABLE
```

**写完必须做 mutation test**：把每条关键约束**改坏一次**，确认测试真的会 FAIL，
再改回来。判官自己写错时会静默报告「全部通过」，比没有检查更危险。阶段一为此
发现自己的一条断言只检查了关键字存在、不检查值，已修正为 AST 检查字面量 `True`。

**另外记得更新 `smoke_test.py`**：它是「可执行文档」，真起一个假 Go 后端 + 真 uvicorn，
把 README 里的流程走一遍。**新增工具的端到端检查也加到这里**（它和 `tests/` 的分工：
`tests/` 是回归网，上游 mock 在 HTTP 层；`smoke_test.py` 是照着文档走的演示）。
阶段一正是靠它发现 README 的裸 curl 示例漏了 `params._meta`——**给别人的操作步骤，
必须自己先跑通**。

**测试写法注意事项**（踩过的坑）：

* 用 **AST 解析**，不要字符串搜索——docstring 里就有 "FastMCP" / "Mcp-Session-Id"，
  子串匹配必误报。
* **不要测 `GET /mcp`**：在 2026-07-28 之前它会被当成旧版 SSE 长连接而**挂住不返回**。
* 隔离代理环境变量（`tests/conftest.py` 里的 `_isolate_proxy_env` 已 autouse，别删）。
* **写文件用 Python 工具，不要用 PowerShell `Set-Content -Encoding UTF8`**——
  PowerShell 5.1 会写入 UTF-8 BOM，`ast.parse` 直接报
  `SyntaxError: invalid non-printable character U+FEFF`。（阶段一踩过。）

---

## 8. 协议层不许碰的东西

阶段一的传输层已被测试锁死，**新增工具不需要也不应该动它**：

* `streamable_http_app(stateless_http=True)` —— 必须是字面量 `True`，有测试盯着。
* **不加** `initialize`，**不加** `Mcp-Session-Id`，**不加**会话存储 / `max_sessions`。
* 协议版本由 `mcp.types.LATEST_PROTOCOL_VERSION` 决定（当前 `2026-07-28`）。
* `Mount("/", app=inner_app)` 会**废掉子应用自带的 lifespan**，所以
  `async with mcp.session_manager.run()` 必须留在宿主应用的 lifespan 里，
  否则第一个请求会以 `RuntimeError: Task group is not initialized` 收场。
* `/health` **不**调上游，保持现状。

**手搓请求时 `params._meta` 是硬要求**（只有自己拼 JSON 才需要管；用 SDK `Client` 时自动）：

```json
"params": { "_meta": {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {}
} }
```

键名必须是**带斜杠的完整 URI**。写成裸的 `protocolVersion` / `clientCapabilities`
会 400（报错是 `params._meta is missing the required envelope key(s): ...`）。
`tests/test_stateless_http.py` 顶部的 `META` 常量就是标准写法，直接复用。

**`server/discover` 返回 `supportedVersions`（列表），没有顶层 `protocolVersion`。**
服务身份在 `result._meta["io.modelcontextprotocol/serverInfo"]`。
（别和 `/health` 混：`/health` 里那个顶层 `protocolVersion` 是我们自己的探针约定，不是协议字段。）

**`inputSchema` 里 Optional 字段的约束在 `anyOf` 里一层**：

```json
"species": { "anyOf": [ { "enum": ["犬", "..."], "type": "string" }, { "type": "null" } ], ... }
```

顶层直接找 `.enum` / `.maximum` 是**找不到**的。写测试或客户端解析时记得钻进去
（`tests/test_tool_registration.py` 的 `test_input_schema_carries_real_constraints` 有现成写法）。

`mcp` 版本锁在 `==2.0.0`。**升级 SDK 是独立的一次变更**，要重跑全部测试并重点复核：
`MCPServer` 是否改名、`streamable_http_app` 签名、`CallToolResult` 字段、
`Tool.output_schema` 是否还是 `cached_property`、`request_context.params` 结构，
以及 `_meta` 信封键的要求有没有变。

---

## 9. 顺手要做的清理

* 对齐 §0 核实到的枚举值，**同步改** `SPECIES_VALUES` 等四组常量**与**
  对应的四个 `Literal` 别名（两处不同步会有测试红）。改完把 README
  的「已知未核实项」那一节更新成「已核实（日期 + 证据来源）」。
* 更新 README 里所有会变的东西：工具清单、`tools/list` 示例输出、测试数量、
  错误码表、`TOOL_BUILDERS` 的说明。
* 阶段一明确**没有**做的部分（鉴权、缓存、限流、持久化）如果被加上了，
  README 的「阶段一明确没有做的」那一节要改写，别留过时描述。

---

## 10. 交付要求

提交前逐条自查：

- [ ] `pytest -q` **全绿**，并把实际数字写进汇报（不要写「测试通过」就完事）
- [ ] 每个新工具都做过 mutation test，且能说出「改坏什么 → 哪条测试会红」
- [ ] 所有**未核实**的假设都在 README 里显式标出来，并给出核实方法
- [ ] 说清楚**改动了哪些文件**（给路径）、**没有做**什么
- [ ] 工具数量、描述、schema 与实际线缆行为一致（用 Inspector 或 SDK 真调一次确认）
- [ ] 提交信息能说清本次变更的范围（加/删了哪些工具、有没有动传输层）

**两道红线**（源于阶段一）：

1. **先反驳再实现。** 拿到需求先检查它和现有实现/协议是否冲突；有冲突就说，
   不要闷头照做。
2. **禁止声称「已验证」。** 没跑过就不许说跑过；没核实过的取值就标成未核实。
   说自己「已完成」之前，先把命令真跑一遍。

---

## 附：阶段一的关键坑速查

| 现象 | 真实原因 | 解法 |
| --- | --- | --- |
| 未知参数传了没反应 | SDK 自动生成的参数模型 `extra` 是默认值，静默丢弃 | 从 `request_context.params["arguments"]` 取原始字典 |
| 错误信息变成 `Error executing tool ...` | `raise ToolError` 被 SDK 包装 | `return CallToolResult(is_error=True)` |
| 错误整个消失，客户端收到协议错误 | `raise MCPError` | 同上 |
| 成功路径好好的，错误路径塌成 `ToolError` | 声明了返回类型 → SDK 对**每个**结果做校验，而失败结果不带 `structuredContent` | 返回类型写 `CallToolResult`，schema 手工塞进 `tool.__dict__["output_schema"]` |
| 发往 `127.0.0.1` 的请求 404 | 本机 `HTTP_PROXY` 让 httpx 发绝对 URL 请求行 | `httpx.AsyncClient(trust_env=False)` |
| 第一个请求 `RuntimeError: Task group is not initialized` | `Mount` 废掉了子应用 lifespan | 在宿主 lifespan 里 `async with mcp.session_manager.run()` |
| `import fastmcp` 失败 | v2 里没有 `mcp.server.fastmcp`，`FastMCP` 已改名 `MCPServer` | 用 `from mcp.server import MCPServer` |
| `ast.parse` 报 `U+FEFF` | PowerShell 5.1 写了 UTF-8 BOM | 用 Python 工具写文件 |
