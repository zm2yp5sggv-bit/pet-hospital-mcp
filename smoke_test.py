"""一次性冒烟测试：用真 HTTP 把服务跑起来，验证 README 里的步骤确实可用。

不是测试套件的一部分（测试套件在 tests/ 下）；这个脚本只是「照着 README 做一遍」的
可执行证据。跑法：

    .venv/Scripts/python.exe smoke_test.py

它会：
  1. 起一个假的 Go 后端（8081），返回一段真实形状的 data；
  2. 起本 MCP 服务（8001，指向假后端）；
  3. 真发 HTTP：/health、server/discover、tools/list、tools/call（成功 / 未知参数）；
  4. 停掉假后端，再发一次 tools/call，验证 BACKEND_UNAVAILABLE；
  5. 打勾/打叉，退出码非 0 表示有失败。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import httpx
import uvicorn

sys.path.insert(0, "src")

from pet_hospital_mcp.config import Settings  # noqa: E402
from pet_hospital_mcp.server import create_app  # noqa: E402

BACKEND_PORT = 8081
MCP_PORT = 8001

# 故意混入 records=null 与 charges=[]，两种「可能为空」的形态都覆盖
FAKE_DATA = {
    "total": 2,
    "page": 1,
    "pageSize": 20,
    "totalPages": 1,
    "totalCost": 3420.5,
    "items": [
        {
            "id": 1,
            "name": "旺财",
            "species": "犬",
            "ownerName": "张三",
            "ownerPhone": "13800000000",
            "doctor": "李医生",
            "disease": "犬瘟热",
            "status": "就诊中",
            "totalCost": 2100,
            "records": None,          # ← Go 侧可能是 null
            "charges": [{"item": "输液", "amount": 800}],
        },
        {
            "id": 2,
            "name": "咪咪",
            "species": "猫",
            "ownerName": "李四",
            "ownerPhone": "13900000001",
            "doctor": "王医生",
            "disease": "肠胃炎",
            "status": "已康复",
            "totalCost": 1320.5,
            "records": [],
            "charges": None,          # ← 这个反过来，charges 是 null
        },
    ],
}

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    _results.append((ok, label))
    print(f"  {'[PASS]' if ok else '[FAIL]'} {label}" + (f"  -- {extra}" if extra else ""))


# --------------------------------------------------------------- 假 Go 后端
class FakeBackend(BaseHTTPRequestHandler):
    seen_queries: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        FakeBackend.seen_queries.append(self.path)
        if self.path.startswith("/api/v1/pets"):
            payload = json.dumps({"code": 0, "data": FAKE_DATA}, ensure_ascii=False)
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args) -> None:  # 闭嘴
        pass


def start_backend() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", BACKEND_PORT), FakeBackend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


async def main() -> int:
    print("=" * 70)
    print("pet-hospital-mcp 冒烟测试（真 HTTP，非 mock）")
    print("=" * 70)

    # ---------------------------------------------------------- 1. 起假后端
    print(f"\n[1] 起假 Go 后端 :{BACKEND_PORT}")
    backend = start_backend()
    check(wait_port(BACKEND_PORT), "假后端已监听")

    # ------------------------------------------------------------ 2. 起服务
    print(f"\n[2] 起 MCP 服务 :{MCP_PORT}（PET_HOSPITAL_BASE_URL 指向假后端）")
    settings = Settings.from_env(
        {
            "PET_HOSPITAL_BASE_URL": f"http://127.0.0.1:{BACKEND_PORT}",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(MCP_PORT),
            "MCP_LOG_LEVEL": "WARNING",
        }
    )
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    check(wait_port(MCP_PORT), "MCP 服务已监听")

    base = f"http://127.0.0.1:{MCP_PORT}"
    # trust_env=False 同样必须：本机 HTTP_PROXY 会把 127.0.0.1 请求变成绝对 URL
    async with httpx.AsyncClient(base_url=base, timeout=30.0, trust_env=False) as http:

        # ------------------------------------------------------- 3. /health
        print("\n[3] GET /health（不应调用后端）")
        before = len(FakeBackend.seen_queries)
        r = await http.get("/health")
        health = r.json()
        check(r.status_code == 200, "HTTP 200", str(r.status_code))
        check(health.get("status") == "ok", "status == ok", health.get("status"))
        check(
            health.get("protocolVersion") == "2026-07-28",
            "protocolVersion == 2026-07-28",
            str(health.get("protocolVersion")),
        )
        check(health.get("stateful") is False, "stateful == false", str(health.get("stateful")))
        check(
            len(FakeBackend.seen_queries) == before,
            "/health 没有打到后端",
            f"后端收到 {len(FakeBackend.seen_queries) - before} 次请求",
        )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
        }

        # 2026-07-28 起，每个请求的 params._meta 必须带**命名空间化**的信封键。
        # 键名就是带斜杠的完整 URI，写成裸的 protocolVersion / clientCapabilities
        # 会被拒 400（"params._meta is missing the required envelope key(s)"）。
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "smoke-test", "version": "0"},
        }

        async def rpc(method: str, params: dict | None = None, name: str | None = None):
            h = dict(headers)
            h["Mcp-Method"] = method
            if name:
                h["Mcp-Name"] = name
            p = dict(params or {})
            p["_meta"] = meta
            body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}
            resp = await http.post("/mcp", json=body, headers=h)
            text = resp.text
            if "text/event-stream" in resp.headers.get("content-type", ""):
                for line in text.splitlines():
                    if line.startswith("data:"):
                        text = line[5:].strip()
                        break
            return resp, (json.loads(text) if text.strip() else None)

        # ------------------------------------------------- 4. server/discover
        print("\n[4] server/discover")
        r, body = await rpc("server/discover")
        check(r.status_code == 200, "HTTP 200", str(r.status_code))
        check("mcp-session-id" not in {k.lower() for k in r.headers}, "响应头没有 Mcp-Session-Id")
        disc = (body or {}).get("result", {})
        # 注意：discover 的结果里**没有**顶层 protocolVersion，
        # 而是 supportedVersions 列表 + _meta 里的 serverInfo。
        versions = disc.get("supportedVersions") or []
        check(
            "2026-07-28" in versions,
            "supportedVersions 含 2026-07-28",
            str(versions),
        )
        check(
            disc.get("_meta", {})
            .get("io.modelcontextprotocol/serverInfo", {})
            .get("name")
            == "pet-hospital-mcp",
            "serverInfo 在 _meta 里给出服务名",
            json.dumps(disc.get("_meta", {}), ensure_ascii=False)[:120],
        )

        # 反证：缺 _meta 必须被拒，说明这个约束是真的在生效（而不是我们猜的）
        r_bad = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**headers, "Mcp-Method": "tools/list"},
        )
        check(
            r_bad.status_code == 400 and "_meta" in r_bad.text,
            "缺 params._meta 被拒 400（说明信封键确实是硬要求）",
            f"{r_bad.status_code}: {r_bad.text[:120]}",
        )
        # 反证：键名不带命名空间也必须被拒（这正是本脚本第一版犯的错）
        r_plain = await http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {"protocolVersion": "2026-07-28", "clientCapabilities": {}}},
            },
            headers={**headers, "Mcp-Method": "tools/list"},
        )
        check(
            r_plain.status_code == 400,
            "裸键名 protocolVersion / clientCapabilities 被拒（必须带 io.modelcontextprotocol/ 前缀）",
            str(r_plain.status_code),
        )
        # 反证：没有 initialize 这个握手方法
        r_init = await http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"_meta": meta},
            },
            headers={**headers, "Mcp-Method": "initialize"},
        )
        check(
            not (r_init.status_code == 200 and "result" in r_init.text),
            "initialize 不存在（2026-07-28 已移除握手）",
            f"HTTP {r_init.status_code}",
        )

        # ---------------------------------------------------- 5. tools/list
        print("\n[5] tools/list")
        r, body = await rpc("tools/list")
        check(r.status_code == 200, "HTTP 200", str(r.status_code))
        tools = (body or {}).get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        check(names == ["list_pets"], "有且仅有 list_pets", str(names))
        if tools:
            schema = tools[0].get("inputSchema", {})
            props = schema.get("properties", {})
            check(
                schema.get("additionalProperties") is False,
                "inputSchema.additionalProperties == false",
            )
            check(
                set(props) == {
                    "q", "name", "ownerName", "ownerPhone", "species", "doctor",
                    "disease", "status", "min", "max", "sortBy", "order", "page", "pageSize",
                },
                "inputSchema 参数集与文档一致",
                str(sorted(props)),
            )
            # ⚠️ 形状细节：字段都是 Optional，所以 Pydantic 把它们包成
            # {"anyOf": [{真实约束}, {"type": "null"}]}——约束在 **anyOf 里面一层**，
            # 顶层直接找 enum / maximum 是找不到的。（本脚本第一版就栽在这。）
            def branch(prop: str, key: str) -> dict:
                node = props.get(prop, {})
                for b in node.get("anyOf", [node]):
                    if key in b:
                        return b
                return {}

            check(
                branch("species", "enum").get("enum") == [
                    "犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他",
                ],
                "species 的 enum 在 anyOf 内（真实约束）",
                str(branch("species", "enum").get("enum")),
            )
            check(
                branch("status", "enum").get("enum")
                == ["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"],
                "status 的 enum 在 anyOf 内",
                str(branch("status", "enum").get("enum")),
            )
            check(
                branch("order", "enum").get("enum") == ["asc", "desc"],
                "order 的 enum 在 anyOf 内",
                str(branch("order", "enum").get("enum")),
            )
            check(
                branch("pageSize", "maximum").get("maximum") == 500
                and branch("pageSize", "maximum").get("minimum") == 1,
                "pageSize 上下界 1..500 在 anyOf 内",
                json.dumps(branch("pageSize", "maximum"), ensure_ascii=False),
            )
            check(
                branch("page", "minimum").get("minimum") == 1,
                "page 下界 1 在 anyOf 内",
                json.dumps(branch("page", "minimum"), ensure_ascii=False),
            )
            check(
                branch("min", "minimum").get("minimum") == 0,
                "min 下界 0 在 anyOf 内",
                json.dumps(branch("min", "minimum"), ensure_ascii=False),
            )
            check(
                "outputSchema" in tools[0],
                "声明了 outputSchema",
                "有" if "outputSchema" in tools[0] else "缺失",
            )
            out_schema = tools[0].get("outputSchema", {})
            check(
                {"pageSize", "totalPages", "totalCost"} <= set(out_schema.get("properties", {})),
                "outputSchema 用驼峰字段名",
                str(sorted(out_schema.get("properties", {}))),
            )

        # ------------------------------------------------- 6. 正常调用
        print("\n[6] tools/call list_pets 正常路径")
        r, body = await rpc("tools/call", {"name": "list_pets", "arguments": {"species": "犬", "page": 1}}, "list_pets")
        result = (body or {}).get("result", {})
        check(result.get("isError") is not True, "isError 不是 true", str(result.get("isError")))
        sc = result.get("structuredContent") or {}
        check(sc.get("total") == 2, "structuredContent.total == 2", str(sc.get("total")))
        check(len(sc.get("items", [])) == 2, "拿到 2 条 items", str(len(sc.get("items", []))))
        check(
            sc.get("items", [{}])[0].get("records") is None,
            "records=null 被正确透传",
            str(sc.get("items", [{}])[0].get("records")),
        )
        check("pageSize" in sc, "用驼峰 pageSize", str(list(sc)))
        check(
            # 中文枚举在查询串里是百分号编码（犬 → %E7%8A%AC），不能直接匹配「犬」。
            any(f"species={quote('犬')}" in q for q in FakeBackend.seen_queries),
            "查询参数确实传到了后端",
            str(FakeBackend.seen_queries[-1] if FakeBackend.seen_queries else ""),
        )

        # ----------------------------------------- 7. 未知参数 → 校验失败
        print("\n[7] tools/call 未知参数（应 VALIDATION_ERROR 且不打后端）")
        before = len(FakeBackend.seen_queries)
        r, body = await rpc("tools/call", {"name": "list_pets", "arguments": {"noSuchParam": 1}}, "list_pets")
        result = (body or {}).get("result", {})
        check(result.get("isError") is True, "isError == true", str(result.get("isError")))
        text = (result.get("content") or [{}])[0].get("text", "")
        try:
            err = json.loads(text)["error"]
        except Exception:
            err = {}
        check(err.get("code") == "VALIDATION_ERROR", "code == VALIDATION_ERROR", str(err.get("code")))
        check(
            err.get("details", {}).get("unknown_fields") == ["noSuchParam"],
            "details.unknown_fields 列出真凶",
            str(err.get("details", {}).get("unknown_fields")),
        )
        check(
            len(FakeBackend.seen_queries) == before,
            "校验失败时没有打到后端",
            f"后端收到 {len(FakeBackend.seen_queries) - before} 次请求",
        )
        check("Error executing tool" not in text, "没有被 SDK 包一层错误前缀")

        # -------------------------------------- 8. 后端挂了 → UNAVAILABLE
        print("\n[8] 停掉后端，再调一次（应 BACKEND_UNAVAILABLE）")
        backend.shutdown()
        backend.server_close()
        await asyncio.sleep(0.3)
        r, body = await rpc("tools/call", {"name": "list_pets", "arguments": {}}, "list_pets")
        result = (body or {}).get("result", {})
        check(result.get("isError") is True, "isError == true", str(result.get("isError")))
        text = (result.get("content") or [{}])[0].get("text", "")
        try:
            err = json.loads(text)["error"]
        except Exception:
            err = {}
        check(
            err.get("code") == "BACKEND_UNAVAILABLE",
            "code == BACKEND_UNAVAILABLE",
            str(err.get("code")),
        )
        # 反证：details 里不许出现 traceback / 上游原文
        details = json.dumps(err.get("details", {}), ensure_ascii=False)
        check(
            "Traceback" not in details and 'File "' not in details,
            "details 不含 Python traceback",
            details[:120],
        )
        check(
            "Connection refused" not in details,
            "details 不含 httpx 原始异常消息",
            details[:120],
        )
        # reason 是**有意**带上的异常类名（够诊断、又不含 URL/堆栈），
        # 所以这里只要求它是个短标识符，不是「不许出现」。
        reason = str(err.get("details", {}).get("reason", ""))
        check(
            reason.isidentifier() and len(reason) < 40,
            "details.reason 是短类名（有意保留，非泄漏）",
            reason,
        )
        check(
            set(err.get("details", {})) <= {"url", "attempts", "reason", "status"},
            "details 键在预期集合内",
            str(sorted(err.get("details", {}))),
        )
        check(
            err.get("message", "") != "",
            "message 是一句人话",
            str(err.get("message"))[:100],
        )

        # -------------------------------------------------- 9. 没有会话头
        print("\n[9] 协议层：无状态 / 无会话机制")
        r, _ = await rpc("tools/list", name="list_pets")
        hdrs = {k.lower() for k in r.headers}
        check("mcp-session-id" not in hdrs, "不返回 Mcp-Session-Id")
        r_ok, _ = await rpc("tools/list")
        check(r_ok.status_code == 200, "（基线）不带 session 头也能正常调用", str(r_ok.status_code))
        r2 = await http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/list",
                "params": {"_meta": meta},
            },
            headers={**headers, "Mcp-Method": "tools/list", "Mcp-Session-Id": "bogus-session-id"},
        )
        check(
            r2.status_code == 200,
            "乱造的 Mcp-Session-Id 被忽略（不参与任何会话查找）",
            str(r2.status_code),
        )
        # 注意：这里**不测** GET /mcp —— 它会被当成旧版 SSE 长连接挂住不返回，
        # 加进来等于让本脚本自己卡死。tests/ 里同样刻意没有这一条。
        r4 = await http.delete("/mcp")
        check(r4.status_code >= 400, "DELETE /mcp 不承担会话拆除", f"HTTP {r4.status_code}")

    server.should_exit = True
    await asyncio.sleep(0.5)

    # ------------------------------------------------------------ 汇总
    passed = sum(1 for ok, _ in _results if ok)
    failed = [label for ok, label in _results if not ok]
    print("\n" + "=" * 70)
    print(f"结果：{passed}/{len(_results)} 通过")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
