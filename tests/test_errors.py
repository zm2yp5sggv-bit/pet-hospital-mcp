"""统一错误形状，以及「绝不外泄内部细节」这条硬约束。"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field, ValidationError

from pet_hospital_mcp.errors import (
    ErrorCode,
    ErrorEnvelope,
    PetHospitalError,
    build_error_envelope,
    error_result,
    summarize_validation_error,
)


def test_error_codes_are_the_documented_six() -> None:
    assert {code.value for code in ErrorCode} == {
        "VALIDATION_ERROR",
        "BACKEND_TIMEOUT",
        "BACKEND_UNAVAILABLE",
        "BACKEND_API_ERROR",
        "BACKEND_INVALID_RESPONSE",
        "INTERNAL_ERROR",
    }


def test_envelope_shape_and_round_trip() -> None:
    envelope = build_error_envelope(ErrorCode.BACKEND_TIMEOUT, "超时了", {"attempts": 3})
    assert set(envelope) == {"error"}
    assert envelope["error"] == {
        "code": "BACKEND_TIMEOUT",
        "message": "超时了",
        "details": {"attempts": 3},
    }
    # 可以被模型往返校验，说明形状是稳定的
    assert ErrorEnvelope.model_validate(envelope).error.code == "BACKEND_TIMEOUT"


def test_details_defaults_to_empty_object() -> None:
    envelope = build_error_envelope(ErrorCode.INTERNAL_ERROR, "崩了")
    assert envelope["error"]["details"] == {}


def test_error_result_marks_failure_the_sdk_2x_way() -> None:
    """用 ``is_error``（线缆上的 ``isError``），不是 1.x 的 ``isError`` 属性。"""
    result = error_result(ErrorCode.VALIDATION_ERROR, "参数不对", {"unknown_fields": ["x"]})

    assert result.is_error is True
    assert hasattr(result, "is_error")
    assert not hasattr(result, "isError")

    text = result.content[0].text
    assert json.loads(text)["error"]["code"] == "VALIDATION_ERROR"
    assert result.structured_content is None


def test_error_result_message_is_not_prefixed_or_wrapped() -> None:
    """失败结果里的文本必须**就是**统一错误形状，不能带任何前缀。

    如果改成 ``raise ToolError(...)``，SDK 会包成
    ``Error executing tool list_pets: {...}``，JSON 就不再是合法 JSON 了。
    """
    result = error_result(ErrorCode.BACKEND_UNAVAILABLE, "上游不可用")
    text = result.content[0].text
    assert text.startswith("{")
    assert "Error executing tool" not in text
    json.loads(text)


class _Payload(BaseModel):
    page: int = Field(ge=1)
    name: str


def test_summarize_validation_error_keeps_only_field_and_message() -> None:
    try:
        _Payload.model_validate({"page": 0, "name": 123, "extra": "drop-me"})
    except ValidationError as exc:
        summarized = summarize_validation_error(exc)
    else:  # pragma: no cover
        raise AssertionError("应当校验失败")

    assert {item["field"] for item in summarized} == {"page", "name"}
    blob = json.dumps(summarized, ensure_ascii=False)
    for leak in ("errors.pydantic.dev", "input_value", "int_parsing", "_Payload", "Traceback"):
        assert leak not in blob


def test_summarize_validation_error_ignores_non_pydantic_exceptions() -> None:
    assert summarize_validation_error(ValueError("nope")) == []
    assert summarize_validation_error(httpx.ConnectError("boom")) == []


def test_pet_hospital_error_carries_code_and_details() -> None:
    error = PetHospitalError(ErrorCode.BACKEND_API_ERROR, "坏了", {"status": 500})
    assert error.code is ErrorCode.BACKEND_API_ERROR
    assert error.details == {"status": 500}
    assert str(error) == "坏了"
    assert isinstance(error, Exception)


def test_internal_fallback_message_has_no_internals() -> None:
    from pet_hospital_mcp.errors import INTERNAL_ERROR_MESSAGE

    for leak in ("Traceback", "Error", "Exception", "/", "\\"):
        assert leak not in INTERNAL_ERROR_MESSAGE
