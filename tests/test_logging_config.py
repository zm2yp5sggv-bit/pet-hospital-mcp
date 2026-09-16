"""JSON 日志字段，以及递归脱敏。

脱敏这条特别容易「测了个寂寞」：只测顶层 ``ownerPhone`` 会过，但 ``items[*]``
里的 ``chipNo`` 照样漏。所以这里全部按**嵌套结构**构造用例。
"""

from __future__ import annotations

import json
import logging

import pytest

from pet_hospital_mcp.logging_config import (
    MASK,
    JsonFormatter,
    is_sensitive_key,
    log_tool_call,
    normalize_key,
    redact,
)

REQUIRED_LOG_FIELDS = ("timestamp", "tool_name", "params", "status", "duration_ms")


class TestKeyMatching:
    @pytest.mark.parametrize(
        "key",
        [
            "ownerPhone",
            "owner_phone",
            "OWNERPHONE",
            "ownerPhoneNo",
            "owner-phone",
            "Owner Phone",
            "ownerAddr",
            "owner_addr",
            "OwnerAddress",
            "chipNo",
            "chip_no",
            "CHIPNO",
            "contactPhone",  # 片段命中：任何 *phone* 都脱
        ],
    )
    def test_sensitive(self, key: str) -> None:
        assert is_sensitive_key(key) is True

    @pytest.mark.parametrize("key", ["name", "species", "doctor", "id", "totalCost", "records"])
    def test_not_sensitive(self, key: str) -> None:
        assert is_sensitive_key(key) is False

    def test_normalize_key(self) -> None:
        assert normalize_key("Owner_Phone-No") == "ownerphoneno"
        assert normalize_key(123) == "123"


class TestRecursiveRedaction:
    def test_flat(self) -> None:
        redacted = redact({"ownerPhone": "13800000000", "name": "旺财"})
        assert redacted == {"ownerPhone": MASK, "name": "旺财"}

    def test_nested_dicts_and_lists(self) -> None:
        payload = {
            "items": [
                {
                    "id": 1,
                    "chipNo": "CHIP-1",
                    "owner": {"owner_phone": "139", "ownerAddr": "某路 2 号", "city": "上海"},
                    "records": [{"chip_no": "CHIP-1", "diagnosis": "感冒"}],
                }
            ],
            "total": 1,
        }
        redacted = redact(payload)
        assert redacted["items"][0]["chipNo"] == MASK
        assert redacted["items"][0]["owner"]["owner_phone"] == MASK
        assert redacted["items"][0]["owner"]["ownerAddr"] == MASK
        assert redacted["items"][0]["owner"]["city"] == "上海"
        assert redacted["items"][0]["records"][0]["chip_no"] == MASK
        assert redacted["items"][0]["records"][0]["diagnosis"] == "感冒"

    def test_sensitive_value_is_masked_whole(self) -> None:
        """命中敏感键就整体屏蔽，不保留内部任何片段。"""
        redacted = redact({"ownerPhone": {"home": "13800000000", "work": "13900000000"}})
        assert redacted["ownerPhone"] == MASK

    def test_does_not_mutate_input(self) -> None:
        payload = {"ownerPhone": "13800000000"}
        redact(payload)
        assert payload == {"ownerPhone": "13800000000"}

    def test_deeply_nested_is_bounded(self) -> None:
        payload: dict[str, object] = {}
        cursor: dict[str, object] = payload
        for _ in range(200):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        assert redact(payload) is not None  # 不递归爆栈即通过

    def test_tuples_are_preserved_as_tuples(self) -> None:
        redacted = redact(("a", {"chipNo": "x"}))
        assert isinstance(redacted, tuple)
        assert redacted[1]["chipNo"] == MASK


class TestJsonFormatter:
    def _format(self, record: logging.LogRecord) -> dict:
        return json.loads(JsonFormatter().format(record))

    def test_tool_call_log_has_all_required_fields(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="tool call finished",
            args=(),
            exc_info=None,
        )
        record.__dict__["structured"] = {
            "event": "tool_call",
            "tool_name": "list_pets",
            "params": {"species": "dog"},
            "status": "ok",
            "duration_ms": 12.5,
        }
        payload = self._format(record)
        for field in REQUIRED_LOG_FIELDS:
            assert field in payload, f"日志缺少 {field}"
        assert payload["tool_name"] == "list_pets"

    def test_params_in_log_are_redacted(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="tool call finished",
            args=(),
            exc_info=None,
        )
        record.__dict__["structured"] = {
            "tool_name": "list_pets",
            "params": {"ownerPhone": "13800000000", "items": [{"chipNo": "C1"}]},
            "status": "ok",
            "duration_ms": 1.0,
        }
        payload = self._format(record)
        assert payload["params"]["ownerPhone"] == MASK
        assert payload["params"]["items"][0]["chipNo"] == MASK
        assert "13800000000" not in json.dumps(payload)

    def test_exception_is_reduced_to_type_and_message(self) -> None:
        try:
            raise ValueError("底层炸了")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="t",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="boom",
                args=(),
                exc_info=sys.exc_info(),
            )
        payload = self._format(record)
        assert payload["exception"]["type"] == "ValueError"
        assert payload["exception"]["message"] == "底层炸了"
        assert "Traceback" not in json.dumps(payload)


class TestLogToolCall:
    def test_emits_single_json_line_with_required_fields(self) -> None:
        logger = logging.getLogger("pet_hospital_mcp.test")
        logger.handlers.clear()
        logger.propagate = True

        records: list[logging.LogRecord] = []

        class Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        collector = Collector()
        logger.addHandler(collector)
        logger.setLevel(logging.INFO)

        log_tool_call(
            logger,
            tool_name="list_pets",
            params={"ownerPhone": "13800000000", "species": "dog"},
            status="error",
            duration_ms=3.14159,
            error_code="BACKEND_TIMEOUT",
        )

        assert len(records) == 1
        # 断言**最终写出去的那行 JSON**，而不是内存里的 record：
        # timestamp 是 JsonFormatter 加的，只查 record 会漏掉它。
        payload = json.loads(JsonFormatter().format(records[0]))
        for field in REQUIRED_LOG_FIELDS:
            assert field in payload, f"日志缺少 {field}"
        assert payload["tool_name"] == "list_pets"
        assert payload["status"] == "error"
        assert payload["duration_ms"] == 3.142
        assert payload["params"]["ownerPhone"] == MASK
        assert payload["params"]["species"] == "dog"
        assert payload["error_code"] == "BACKEND_TIMEOUT"
        assert "13800000000" not in json.dumps(payload)
        logger.handlers.clear()

    def test_none_params_is_tolerated(self) -> None:
        logger = logging.getLogger("pet_hospital_mcp.test.none")
        records: list[logging.LogRecord] = []

        class Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger.addHandler(Collector())
        log_tool_call(logger, tool_name="list_pets", params=None, status="ok", duration_ms=0.0)
        assert records[0].__dict__["structured"]["params"] == {}
        logger.handlers.clear()
