# pet-hospital-mcp

把既有的 **Go 宠物医院 REST API** 暴露给 AI Agent 的 MCP 服务。

* **目录名**：`pet-hospital-mcp`（工作区绝对路径 `E:\dsh\pet-hospital-mcp`）
* **干什么用**：AI Agent 通过 MCP 协议调用本服务，本服务再用 HTTP 去问那台 Go 服务要数据。
  Go 服务本身**不改一行**——本仓库只是它前面的一层适配器。
* **阶段**：一（phase 1）。**只实现了一个工具 `list_pets`**，对应 `GET /api/v1/pets`。
  阶段二工具（详情、新增、更新、删除等）**没有实现**，也不在本仓库范围内。

---

## 1. 它到底做了什么（核心逻辑）

```
MCP 客户端 (Agent / Inspector / SDK)
        │  POST /mcp        JSON-RPC，无状态，无会话
        ▼
   ┌─────────────────────────────────────────┐
   │  pet-hospital-mcp  (Starlette + MCP)    │
   │   ├── /health   存活探针（不碰上游）      │
   │   └── /mcp      tools/list, tools/call   │
   │                                          │
   │   list_pets  工具实现：                   │
   │     1. 取原始 arguments（绕过 SDK 过滤）  │
   │     2. 拒绝未知字段                       │
   │     3. ListPetsInput 严格校验            │
   │     4. 转成 Go 侧查询参数                 │
   └──────────────┬──────────────────────────┘
                  │  GET /api/v1/pets?...
                  ▼
        Go 宠物医院服务 (默认 127.0.0.1:8080)
```

四件事值得单列，因为它们是**照着旧版 FastMCP 经验猜就会踩雷**的地方：

### 1.1 无状态 Streamable HTTP，协议 `2026-07-28`

* 传输：`mcp.streamable_http_app(stateless_http=True)` —— 每个请求一个全新 transport。
* **没有** `initialize` 握手，**没有** `Mcp-Session-Id`，**没有**会话存储/过期/`max_sessions`。
* 服务发现走 `server/discover`。
* 好处：可以随便水平扩、随便重启，不需要粘性会话。

#### ⚠️ 手搓客户端必须知道：`params._meta` 是硬要求

**每个请求的 `params._meta` 必须带两个「命名空间化」的信封键**，键名就是带斜杠的完整 URI：

```json
{
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {},
      "io.modelcontextprotocol/clientInfo": { "name": "my-client", "version": "1.0" }
    }
  }
}
```

写错就要吃 400，而且两种错法**报错文本还不一样**：

| 错误 | 服务端响应 |
| --- | --- |
| 完全没有 `_meta` | `params._meta must be an object carrying the required 'io.modelcontextprotocol/protocolVersion' and 'io.modelcontextprotocol/clientCapabilities' envelope keys` |
| 有 `_meta` 但键名是裸的 `protocolVersion` / `clientCapabilities` | `params._meta is missing the required envelope key(s): io.modelcontextprotocol/protocolVersion, io.modelcontextprotocol/clientCapabilities` |

用官方 SDK `Client` 时这些都是自动的，**只有自己拼 JSON 才会踩**。
`smoke_test.py` 里有两条反证测试专门盯着这两种错法。

#### `server/discover` 的返回形状

**没有**顶层 `protocolVersion` 字段。要看版本就读 `supportedVersions`（列表），
服务身份在 `_meta` 里：

```json
{
  "result": {
    "supportedVersions": ["2026-07-28"],
    "capabilities": { "tools": { "listChanged": true }, "prompts": {...}, "resources": {...} },
    "instructions": "宠物医院业务数据服务……",
    "resultType": "complete",
    "cacheScope": "private",
    "ttlMs": 0,
    "_meta": {
      "io.modelcontextprotocol/serverInfo": { "name": "pet-hospital-mcp", "version": "0.1.0" }
    }
  }
}
```

> 注意区分：`/health` 里有一个**我们自己的**顶层 `protocolVersion` 字段（方便运维肉眼核对），
> 那是本服务的探针约定，**不是**协议字段。两者别混。


### 1.2 严格校验必须自己做，不能交给 SDK 自动生成的参数模型

`@mcp.tool()` 或 `Tool.from_function` 会按函数类型注解生成一个参数模型，而它：

* `extra` 是**默认值**——未知字段会被**静默丢弃**，客户端传错参数名既不报错也不生效；
* 校验失败时抛的是 **Pydantic 原始文本**，直接漏出内部结构。

所以 `list_pets.py` 里的做法是：

1. 函数签名只写「什么都收的过路参数」（类型一律 `Any`），保证 SDK 那层永远不会先失败；
2. 从 `ctx.request_context.params["arguments"]` 取**未经 SDK 过滤的原始字典**；
3. 自己用 `ListPetsInput`（`extra="forbid"`）做严格校验，对外只吐统一错误形状；
4. 用 `ListPetsInput.model_json_schema(by_alias=True)` **覆盖** `tool.parameters`，
   于是客户端看到的 `inputSchema` 里是**真的** enum、上下界和
   `additionalProperties: false`，而不是一坨 `Any`。

> **形状细节（容易看漏）**：因为所有字段都是 `Optional`，Pydantic v2 会把每个属性包成
> `{"anyOf": [ {真实约束}, {"type": "null"} ]}` —— **约束在 `anyOf` 里面一层**。
> 顶层直接找 `properties.species.enum` 或 `properties.pageSize.maximum` 是**找不到**的：
>
> ```json
> "species": {
>   "anyOf": [
>     { "enum": ["犬","猫","兔","鸟","仓鼠","爬宠","其他"], "type": "string" },
>     { "type": "null" }
>   ],
>   "default": null, "description": "物种", "title": "Species"
> }
> "pageSize": {
>   "anyOf": [ { "maximum": 500, "minimum": 1, "type": "integer" }, { "type": "null" } ],
>   "default": null, "description": "每页条数", "title": "Pagesize"
> }
> ```
>
> 写客户端解析时记得往 `anyOf` 里钻一层。`tests/test_tool_registration.py` 的
> `test_input_schema_carries_real_constraints` 就是这么取的。

> `ListPetsInput` **故意不加** `populate_by_name=True`。加了会让 `page_size` / `owner_name`
> 这类蛇形写法也被接受，等于凭空变出一批后端根本不存在的「私有参数」。

### 1.3 失败用 `isError=true` 返回，不 `raise`

| 做法 | 后果 |
| --- | --- |
| `raise ToolError(...)` | SDK 包成 `Error executing tool list_pets: ...`，**破坏统一错误形状** |
| `raise MCPError(...)` | 变成 JSON-RPC 协议错误，**模型根本看不到** |
| ✅ `return CallToolResult(is_error=True, ...)` | 模型能读到错误 JSON，并按 `code` 决定重试还是改参数 |

统一错误形状（成功与失败都只有这一种失败形状）：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "存在不支持的查询参数：foo",
    "details": { "unknown_fields": ["foo"], "allowed_fields": ["q", "name", "..."] }
  }
}
```

错误码共 6 个：`VALIDATION_ERROR`、`BACKEND_TIMEOUT`、`BACKEND_UNAVAILABLE`、
`BACKEND_API_ERROR`、`BACKEND_INVALID_RESPONSE`、`INTERNAL_ERROR`。
前 1 个是「你的参数不对」，中间 4 个是「后端不给力」，最后 1 个是兜底（**不外泄任何内部细节**）。

### 1.4 输出 schema 是「手工挂上去」的

声明了 output schema 之后，SDK 会对**每一个**返回的 `CallToolResult` 做
`output_model.model_validate(result.structured_content)`。而失败结果按规范不带
`structuredContent`——于是错误路径会被判校验失败，整个塌成 `ToolError`。

解法：函数返回类型保持 `CallToolResult`（结果原样透传），再把
`PetsPage` 的 schema 往实例 `__dict__` 里塞，遮蔽那个 `cached_property`。
`tests/test_tool_registration.py` 把**这两点同时**锁住了。

---

## 2. 技术栈与依赖

| 项 | 值 |
| --- | --- |
| Python | **≥ 3.11** |
| MCP SDK | **`mcp==2.0.0`**（v2；`MCPServer` 是 v2 里 `FastMCP` 的新名字） |
| HTTP 客户端 | `httpx>=0.27,<1` |
| 数据校验 | `pydantic>=2.7,<3` |
| ASGI / 服务器 | `starlette`、`uvicorn`（由 `mcp` 带入） |
| 测试 | `pytest>=8`、`pytest-asyncio>=0.23` |

**`mcp.server.fastmcp` 这个模块在 2.0.0 里不存在**——那是 v1 的路径。
`tests/test_tool_registration.py` 用 AST 检查把它挡在门外。

### 一个必须记住的坑：`trust_env=False`

`PetHospitalClient` 构造 `httpx.AsyncClient` 时**固定** `trust_env=False`。

原因是一次真实踩坑：本机 `HTTP_PROXY=http://127.0.0.1:55830` 时，httpx 默认走代理，
于是发往 `127.0.0.1:8080` 的请求行变成绝对 URL 形式
（`GET http://127.0.0.1:8080/api/v1/pets HTTP/1.1`），中间件按普通路径匹配就 **404**。
上游是本地/内网服务时，环境代理是纯干扰源。

---

## 3. 文件组织

```
pet-hospital-mcp/
├── pyproject.toml                 # 元数据、依赖、pytest 配置（asyncio_mode=auto, pythonpath=src）
├── .gitignore                     # 排除 .venv / __pycache__ / .pytest_cache
├── README.md                      # 本文件
├── UPGRADE_PROMPT.md              # 阶段二的扩展指令（喂给 AI 用）
├── smoke_test.py                  # 「照着 README 做一遍」的可执行证据（真 HTTP，非 mock）
├── src/pet_hospital_mcp/
│   ├── __init__.py                # 版本号 + 阶段一范围说明
│   ├── __main__.py                # python -m pet_hospital_mcp
│   ├── config.py                  # Settings（环境变量 → 一次性解析并校验）
│   ├── errors.py                  # ErrorCode / ErrorEnvelope / error_result / 校验错误摘要
│   ├── logging_config.py          # JSON 日志 + 递归脱敏
│   ├── rest_client.py             # GET /api/v1/pets 异步客户端（超时 / 重试 / 错误翻译）
│   ├── server.py                  # 装配 MCPServer、/health、Starlette app、uvicorn 入口
│   └── tools/
│       ├── __init__.py            # TOOL_BUILDERS / build_all_tools
│       └── list_pets.py           # ★ 唯一一个工具：ListPetsInput + 严格校验 + schema
└── tests/
    ├── conftest.py                # 隔离代理环境变量、MockTransport、起真 uvicorn 的 fixture
    ├── test_config.py             # 环境变量解析与非法值
    ├── test_errors.py             # 错误形状与校验错误摘要
    ├── test_logging_config.py     # 日志脱敏（含嵌套结构）
    ├── test_list_pets_input.py    # ListPetsInput 严格性（含 page_size 必须被拒）
    ├── test_rest_client.py        # 成功 / 4xx / 5xx / 超时 / 连不上 / 非法 JSON / data 缺失
    ├── test_tool_registration.py  # AST 静态约束：只有一个工具、schema、无 fastmcp、stateless_http
    └── test_stateless_http.py     # 真的起 HTTP，端到端走协议
```

`TOOL_BUILDERS` 是个元组，加阶段二工具时**只往这里加一项**即可。

**注意 `tests/` 与 `smoke_test.py` 的分工**：`tests/` 是回归网（`pytest -q` 日常跑，
上游用 `httpx.MockTransport` 顶替，不碰网络）；`smoke_test.py` 是**可执行文档**
（自起一个假 Go 后端 + 真 uvicorn，把 README 里的流程走一遍，证明文档没在骗人）。
两者都用真 TCP，区别只在 mock 的粒度。

---

## 4. 运行

### 4.1 前置：先把 Go 服务跑起来

本服务只是适配器。**Go 服务不在本仓库里**，需要你自己把它起在
`http://127.0.0.1:8080`（或改 `PET_HOSPITAL_BASE_URL` 指向别处）。

自检一下它活着：

```bash
curl -i "http://127.0.0.1:8080/api/v1/pets?page=1&pageSize=5"
```

### 4.2 装依赖

```bash
cd pet-hospital-mcp
python -m venv .venv

# Windows (PowerShell / cmd)
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

### 4.3 起服务

```bash
# 方式 A：模块入口
python -m pet_hospital_mcp

# 方式 B：console script（pip install -e . 之后）
pet-hospital-mcp
```

启动后：

| 地址 | 说明 |
| --- | --- |
| `http://127.0.0.1:8000/mcp` | MCP 端点（只此一个，POST 即可） |
| `http://127.0.0.1:8000/health` | 存活探针 |

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","service":"pet-hospital-mcp","version":"0.1.0",
#  "protocolVersion":"2026-07-28","backendBaseUrl":"http://127.0.0.1:8080",
#  "transport":"streamable-http","stateful":false}
```

> `/health` **不**调用上游。上游宕机不该让本服务被判为不健康——否则编排系统会把
> 唯一能解释「上游为什么挂了」的那个服务也一起重启。

### 4.4 全部配置项

上游地址由 `PET_HOSPITAL_BASE_URL` 决定，监听地址由 `MCP_HOST` / `MCP_PORT` 决定；
其余键有安全默认值，只在需要调整时设置。**配置非法时启动即失败**（`ConfigError`），
不会半死不活地跑起来。

| 环境变量 | 默认值 | 校验 |
| --- | --- | --- |
| `PET_HOSPITAL_BASE_URL` | `http://127.0.0.1:8080` | 必须以 `http://` 或 `https://` 开头；尾部 `/` 自动去掉 |
| `PET_HOSPITAL_TIMEOUT_SECONDS` | `10.0` | 必须为正数 |
| `PET_HOSPITAL_MAX_RETRIES` | `2` | 不能为负数（总尝试次数 = 重试 + 1） |
| `PET_HOSPITAL_RETRY_BACKOFF_SECONDS` | `0.25` | 不能为负数（指数退避：`backoff * 2^attempt`） |
| `MCP_HOST` | `127.0.0.1` | 不能为空 |
| `MCP_PORT` | `8000` | 1..65535 |
| `MCP_HTTP_PATH` | `/mcp` | 必须以 `/` 开头 |
| `MCP_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |

可重试的上游状态码：`429 / 502 / 503 / 504`。重试耗尽后 502/503/504 归为
`BACKEND_UNAVAILABLE`，其余非 2xx 归为 `BACKEND_API_ERROR`。

### 4.5 日志

JSON 格式输出到 **stderr**（stdout 留给服务本身），每行一个对象：

```json
{"timestamp":"2026-09-16T02:11:03.412Z","level":"INFO","logger":"pet_hospital_mcp.tools.list_pets",
 "tool_name":"list_pets","params":{"ownerPhone":"***","page":1},"status":"ok",
 "duration_ms":12.7,"backend_base_url":"http://127.0.0.1:8080"}
```

**脱敏**：`ownerPhone` / `ownerAddr` / `chipNo`（含任何名字里带 `phone` 的片段）
会被**递归**替换成 `***`，键名先做归一化（小写、去掉非字母数字）再判定，
所以 `owner_phone`、`OwnerPhone`、`ownerPhone` 一视同仁。
`httpx` / `httpcore` 的日志被压到 `WARNING`，避免把上游 URL 和 body 刷进来。

---

## 5. 验证

### 5.1 跑测试

```bash
cd pet-hospital-mcp
pytest -q
```

**当前结果：178 passed**（`.venv`，`mcp==2.0.0`；72 + 72 + 34）。

测试分两层：

* **静态层**（`test_tool_registration.py`）：用 **AST 解析**而不是字符串搜索，
  因为 docstring 里就写着 "FastMCP" 和 "Mcp-Session-Id"，子串匹配会误报。
  它检查：只有一个工具、名字是蛇形、描述覆盖了关键内容、`inputSchema` 与文档里的
  参数完全一致且有真实 enum/上下界（**钻到 `anyOf` 里取**）、`outputSchema` 是驼峰、
  `Literal` 与常量一致、签名里有过路参数、
  **没有** `fastmcp` 导入、**没有**会话机制、`stateless_http=True` 是**字面量** `True`。
* **行为层**：`test_stateless_http.py` 用 `httpx.MockTransport` 假造 Go 后端，
  再用**真的 uvicorn** 起一个随机端口，走真实 HTTP 协议。覆盖：

  | 用例 | 断言 |
  | --- | --- |
  | `GET /health` | 返回 ok，且**没有**调用后端 |
  | `server/discover` | `supportedVersions == ["2026-07-28"]` |
  | `initialize` | **不存在**（不返回正常结果） |
  | 响应头 | **没有** `Mcp-Session-Id` |
  | 传一个乱造的 session 头 | 被忽略，不影响结果 |
  | tools/list | `list_pets` 的 schema 正确 |
  | 正常调用 | 拿到 Go 的数据（`records=null`、`charges=[]` 都吃下） |
  | 传未知参数 | `VALIDATION_ERROR` + `unknown_fields`，且**没打到后端** |
  | 传蛇形参数（`page_size`） | `VALIDATION_ERROR` |
  | 传错类型 | `VALIDATION_ERROR` |
  | 后端 500 | `BACKEND_API_ERROR`，不泄漏上游原文 |
  | 连不上后端 | `BACKEND_UNAVAILABLE` |
  | 不存在的工具 | JSON-RPC 协议错误 |
  | `DELETE /mcp` | 4xx（不靠它做会话拆除） |
  | SDK `Client` 全流程 | `client.protocol_version == "2026-07-28"`，且它走 `discover` 不走 `initialize` |

> `GET /mcp` 的用例**特意删掉了**：在 2026-07-28 之前，`GET /mcp` 会被当成
> 旧版 SSE 长连接而**挂住不返回**，不适合放进测试。

### 5.2 跑冒烟测试（真的起服务，照着文档走一遍）

```bash
python smoke_test.py
```

**当前结果：48/48 通过。**

它自己起一个假 Go 后端（:8081）和真 uvicorn（:8001），然后真发 HTTP 走完 9 组检查：
`/health`（含「没打后端」）→ `server/discover`（含**缺 `_meta` 必须 400**、
**裸键名必须 400**、**`initialize` 不存在**三条反证）→ `tools/list`（含 `anyOf`
里取 enum/上下界的形状检查）→ 正常调用（含字段名驼峰、参数真传到了后端）
→ 未知参数（含「没打后端」）→ 后端挂掉（含 details 不含 traceback）
→ 协议层无会话。

> 这个脚本是**可执行文档**：如果 README 里写的步骤哪天真跑不通了，它会红。

### 5.3 用官方 Inspector 亲眼看一下（推荐）

```bash
npx @modelcontextprotocol/inspector
```

在界面里填：

* Transport：`Streamable HTTP`
* URL：`http://127.0.0.1:8000/mcp`

然后：`Tools` → `list_pets` → 填参数 → `Run`。

### 5.4 用 Python SDK 直接调

```python
import asyncio
from mcp import Client

async def main() -> None:
    async with Client("http://127.0.0.1:8000/mcp") as client:
        tools = await client.list_tools()
        print([t.name for t in tools.tools])          # ['list_pets']

        result = await client.call_tool("list_pets", {"species": "犬", "page": 1})
        print(result.structured_content)              # 与 Go 的 data 一一对应

        bad = await client.call_tool("list_pets", {"noSuchParam": 1})
        print(bad.is_error, bad.content[0].text)      # True + VALIDATION_ERROR JSON

asyncio.run(main())
```

### 5.5 裸 curl（看协议层）

> ⚠️ **必须带 `params._meta`，且键名要带 `io.modelcontextprotocol/` 前缀**，
> 否则 400 —— 原因见 §1.1。这段是实测跑通的，别省。

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    "params": {
      "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "curl", "version": "0"}
      }
    }
  }'
```

调 `tools/call` 时再加 `Mcp-Name: list_pets` 头（`tools/call` / `resources/read` /
`prompts/get` 上它是必给的）：

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: list_pets' \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {
      "name": "list_pets",
      "arguments": {"species": "犬", "page": 1},
      "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "curl", "version": "0"}
      }
    }
  }'
```

### 5.6 手动改坏一次，确认检查真的会红

测试套件是判官，判官自己写错时会**静默报告「全部通过」**——比不检查更危险。
所以关键约束都做过 mutation test，确认改坏之后真的会 FAIL：

| 改坏什么 | 期望 | 结果 |
| --- | --- | --- |
| `stateless_http=True` → `False` | `test_stateless_http_is_explicitly_enabled` FAILED | ✅ 如预期变红 |
| `ListPetsInput` 去掉 `extra="forbid"` | 未知字段用例 FAILED | ✅ 如预期变红 |

（改完记得改回来。）

**这套办法确实抓到过东西**：`test_stateless_http_is_explicitly_enabled` 的第一版
只检查 `stateless_http` 这个关键字**在不在**，不看它的值——改成 `False` 照样通过。
已修正为用 AST 检查字面量必须是 `True`。

**另外，写文档时也栽过一次**（记在这里免得重犯）：README 5.5 的裸 curl 示例第一版
漏了 `params._meta`，照着敲必然 400。是 `smoke_test.py` 把「文档里的步骤」也当成
被测对象跑了一遍，才把它揪出来。**给别人的操作步骤，必须自己先跑通。**

---

## 6. 枚举真值（已核实）

**本仓库最重要的历史遗留项已于 2026-09-16 核实关闭。**

早期版本里 `src/pet_hospital_mcp/tools/list_pets.py` 的四组枚举值是**暂定占位值**
（英文假值 `dog` / `waiting` 等），因为当时手上没有 Go 服务的源码。现依据两条互相
印证的证据链全部替换为真实取值：

**证据一：Go 服务源码**（pet-hospital-mcp-teaching-main）

| 枚举 | 真实取值 | 源码位置 |
| --- | --- | --- |
| `SPECIES_VALUES` | `犬` / `猫` / `兔` / `鸟` / `仓鼠` / `爬宠` / `其他` | `internal/model/model.go` |
| `STATUS_VALUES` | `待就诊` / `就诊中` / `住院中` / `已康复` / `慢性病随访` | `internal/model/model.go` |
| `SORT_BY_VALUES` | `id` / `name` / `ownerName` / `species` / `doctor` / `disease` / `status` / `totalCost` / `visitCount` / `createdAt` / `updatedAt` | `internal/api/api.go`（handleMeta） |
| `ORDER_VALUES` | `asc` / `desc` | `internal/store/store.go`（sortPets） |

**证据二：运行实例**（pethospital.exe + 真实 pet.db）：
`GET /api/v1/meta` 返回的 `species` / `status` / `sortFields` 与源码一致；
`species=犬`（命中 446 条）、`status=住院中`、`sortBy=ownerName&order=asc` 均实测生效。

**顺带核实的后端行为**（都写进了工具描述）：

* `sortBy` 为空或未知 → 后端按 `id` 排序；`order` 为空 → 后端默认 `desc`，
  非 `desc`（忽略大小写）一律按 `asc` 处理。工具侧把 `order` 收紧为小写
  `asc`/`desc` 白名单，属客户端自律，不改变后端行为。
* `pageSize` 超过 500 会被后端钳回 500（与工具的 `MAX_PAGE_SIZE=500` 对齐）；
  后端默认 `pageSize=20`。
* 查询参数名 14 个、成功响应 `data` 结构（`items`/`total`/`page`/`pageSize`/
  `totalPages`/`totalCost`）、`records`/`charges` 可能为 `null`——全部与实现一致。

**改枚举时的同步点**（未来的自己请记好）：四个常量与四个 `Literal` 别名**两处必须
一致**，`tests/test_tool_registration.py` 有测试锁定，只改一处会红；README / smoke_test /
tests 里的示例值也要跟着改——本次核实就发现英文占位值散落在 6 个文件里。

---

## 7. 阶段一明确**没有**做的

* **没有** phase-2 工具：宠物详情、新增/修改/删除、病历记录、费用明细、医生/主人等
  资源，**一个都没有**。当前 `tools/list` 只会返回 `list_pets` 一条。
* **没有**鉴权 / 认证层（上游若需要，请放在反向代理或后续阶段）。
* **没有**缓存、限流、持久化——服务是无状态的。
* **没有**修改 Go 服务，也没有它的源码或构建产物。

阶段二的扩展方式见 [`UPGRADE_PROMPT.md`](./UPGRADE_PROMPT.md)。
