"""REST 客户端：参数转发、成功解析、以及各类失败的翻译。

全部走 ``httpx.MockTransport``——**任何测试都不访问真实 Go 服务**。
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.errors import ErrorCode, PetHospitalError
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.tools.list_pets import LIST_PETS_QUERY_PARAMS

Handler = Callable[[httpx.Request], httpx.Response]


async def call(client: PetHospitalClient, query: dict[str, Any] | None = None) -> Any:
    return await client.list_pets(query or {})


async def expect_error(client: PetHospitalClient, query: dict[str, Any] | None = None) -> PetHospitalError:
    with pytest.raises(PetHospitalError) as excinfo:
        await client.list_pets(query or {})
    return excinfo.value


class TestRequestForwarding:
    async def test_path_and_all_params_are_forwarded(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=ok_response())

        client = make_client(handler)
        query = {
            "q": "旺财",
            "name": "旺财",
            "ownerName": "张三",
            "ownerPhone": "13800000000",
            "species": "dog",
            "doctor": "李医生",
            "disease": "感冒",
            "status": "waiting",
            "min": 0,
            "max": 100,
            "sortBy": "name",
            "order": "asc",
            "page": 2,
            "pageSize": 50,
        }
        await call(client, query)

        assert len(seen) == 1
        request = seen[0]
        assert request.method == "GET"
        assert request.url.path == "/api/v1/pets"
        assert request.url.host == "go.pet-hospital.test"  # 绝不碰真实服务
        assert dict(request.url.params) == {k: str(v) for k, v in query.items()}
        assert set(request.url.params) == set(LIST_PETS_QUERY_PARAMS)

    async def test_no_params_when_query_is_empty(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=ok_response())

        await call(make_client(handler))
        assert seen[0].url.query == b""

    async def test_env_proxy_is_not_used(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        """本机 ``HTTP_PROXY`` 必须对本客户端无效，否则回环请求会被改成绝对 URL 形式。"""
        client = make_client(lambda request: httpx.Response(200, json={}))
        assert client.http.trust_env is False


class TestSuccessParsing:
    async def test_go_data_fields_are_mapped(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        client = make_client(lambda request: httpx.Response(200, json=ok_response()))
        page = await call(client)
        assert page.total == 1
        assert page.page == 1
        assert page.page_size == 20
        assert page.total_pages == 1
        assert page.total_cost == 88.5
        # 序列化回驼峰，和 Go 的 JSON 一致
        dumped = page.model_dump(by_alias=True, mode="json")
        assert set(dumped) >= {"items", "total", "page", "pageSize", "totalPages", "totalCost"}

    @pytest.mark.parametrize(
        "records, charges",
        [
            (None, None),
            ([], []),
            ([{"id": 1}], [{"id": 2, "amount": 10}]),
            (None, [{"id": 2}]),
        ],
    )
    async def test_records_and_charges_accept_null_or_array(
        self,
        make_client: Callable[[Handler], PetHospitalClient],
        ok_response: Callable[..., dict],
        records: Any,
        charges: Any,
    ) -> None:
        """Go 侧这两个字段可能返回 ``null`` 也可能返回数组，都必须兼容。"""
        payload = ok_response(items=[{"id": 1, "records": records, "charges": charges}])
        client = make_client(lambda request: httpx.Response(200, json=payload))
        page = await call(client)
        assert page.items[0].records == records
        assert page.items[0].charges == charges

    async def test_extra_backend_fields_are_preserved(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        """适配器不臆造字段，也不悄悄丢字段。"""
        payload = ok_response(
            items=[{"id": 7, "nickname": "小七", "weightKg": 4.2, "records": None}],
            extraTopLevel="keep-me",
        )
        client = make_client(lambda request: httpx.Response(200, json=payload))
        page = await call(client)
        item = page.model_dump(by_alias=True, mode="json")["items"][0]
        assert item["nickname"] == "小七"
        assert item["weightKg"] == 4.2
        assert page.model_dump(by_alias=True, mode="json")["extraTopLevel"] == "keep-me"

    async def test_bare_data_without_envelope_is_tolerated(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        body = ok_response()["data"]
        client = make_client(lambda request: httpx.Response(200, json=body))
        page = await call(client)
        assert page.total == 1


class TestStatusErrors:
    @pytest.mark.parametrize("status", [400, 401, 404, 422])
    async def test_client_errors_are_api_errors_and_not_retried(
        self, make_client: Callable[[Handler], PetHospitalClient], status: int
    ) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status, json={"message": "bad request"})

        client = make_client(handler)
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_API_ERROR
        assert error.details["status"] == status
        assert error.details["upstream_message"] == "bad request"
        assert len(attempts) == 1

    async def test_server_error_is_api_error(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        client = make_client(lambda request: httpx.Response(500, text="internal"))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_API_ERROR
        assert error.details["status"] == 500

    @pytest.mark.parametrize("status", [502, 503, 504])
    async def test_gateway_errors_become_unavailable_after_retries(
        self, make_client: Callable[[Handler], PetHospitalClient], status: int
    ) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status)

        error = await expect_error(make_client(handler))
        assert error.code is ErrorCode.BACKEND_UNAVAILABLE
        assert error.details["status"] == status


class TestRetries:
    async def test_transient_status_then_success(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503)
            return httpx.Response(200, json=ok_response())

        page = await call(make_client(handler))
        assert page.total == 1
        assert len(attempts) == 2

    async def test_retries_are_bounded(
        self, make_client: Callable[[Handler], PetHospitalClient], settings: Settings
    ) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ConnectError("refused", request=request)

        error = await expect_error(make_client(handler))
        assert error.code is ErrorCode.BACKEND_UNAVAILABLE
        assert len(attempts) == settings.backend_max_retries + 1

    async def test_max_retries_zero_means_single_attempt(
        self, settings: Settings, ok_response: Callable[..., dict]
    ) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503)

        strict = Settings(
            backend_base_url=settings.backend_base_url,
            backend_max_retries=0,
            backend_retry_backoff_seconds=0.0,
        )
        async with PetHospitalClient(strict, transport=httpx.MockTransport(handler)) as client:
            error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_UNAVAILABLE
        assert len(attempts) == 1


class TestTransportErrors:
    async def test_timeout(self, make_client: Callable[[Handler], PetHospitalClient]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        error = await expect_error(make_client(handler))
        assert error.code is ErrorCode.BACKEND_TIMEOUT
        assert error.details["attempts"] == 3

    async def test_connection_refused(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        error = await expect_error(make_client(handler))
        assert error.code is ErrorCode.BACKEND_UNAVAILABLE
        assert "PET_HOSPITAL_BASE_URL" in error.message

    async def test_timeout_then_success(
        self, make_client: Callable[[Handler], PetHospitalClient], ok_response: Callable[..., dict]
    ) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.ReadTimeout("too slow", request=request)
            return httpx.Response(200, json=ok_response())

        page = await call(make_client(handler))
        assert page.total == 1
        assert len(attempts) == 2


class TestInvalidResponses:
    async def test_non_json_body(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        client = make_client(
            lambda request: httpx.Response(
                200, text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"}
            )
        )
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE
        assert "502 Bad Gateway" not in json.dumps(error.details)

    async def test_top_level_array(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        client = make_client(lambda request: httpx.Response(200, json=[1, 2, 3]))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE
        assert error.details["received_type"] == "list"

    async def test_missing_data_key(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        client = make_client(lambda request: httpx.Response(200, json={"code": 0, "message": "ok"}))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE
        assert "data" in error.message

    async def test_data_is_not_an_object(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        client = make_client(lambda request: httpx.Response(200, json={"data": "nope"}))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE
        assert error.details["received_type"] == "str"

    async def test_data_missing_required_field(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        data = {"items": [], "total": 0, "page": 1, "pageSize": 20}  # 缺 totalPages
        client = make_client(lambda request: httpx.Response(200, json={"data": data}))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE
        assert error.details["errors"][0]["field"] == "totalPages"

    async def test_data_with_wrong_field_type(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        data = {
            "items": [],
            "total": "many",
            "page": 1,
            "pageSize": 20,
            "totalPages": 1,
            "totalCost": None,
        }
        client = make_client(lambda request: httpx.Response(200, json={"data": data}))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE
        assert error.details["errors"][0]["field"] == "total"

    async def test_records_as_object_is_invalid(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        """``records`` 只能是 null 或数组——对象是协议外的形状。"""
        data = {
            "items": [{"id": 1, "records": {"oops": True}}],
            "total": 1,
            "page": 1,
            "pageSize": 20,
            "totalPages": 1,
        }
        client = make_client(lambda request: httpx.Response(200, json={"data": data}))
        error = await expect_error(client)
        assert error.code is ErrorCode.BACKEND_INVALID_RESPONSE


class TestNoInternalsLeak:
    async def test_error_payloads_contain_no_internal_text(
        self, make_client: Callable[[Handler], PetHospitalClient]
    ) -> None:
        def raising(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("x", request=request)

        cases: list[Handler] = [
            lambda request: httpx.Response(500, text="boom"),
            lambda request: httpx.Response(200, text="not json"),
            raising,
        ]
        for handler in cases:
            error = await expect_error(make_client(handler))
            blob = json.dumps(
                {"code": str(error.code), "message": error.message, "details": error.details},
                ensure_ascii=False,
            )
            for leak in ("Traceback", "site-packages", "httpcore", "pydantic", "raise "):
                assert leak not in blob, f"{handler} 泄漏了 {leak}"
