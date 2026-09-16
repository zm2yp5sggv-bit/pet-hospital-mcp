"""``ListPetsInput`` 的严格校验与查询参数构造。

要求逐条对应：物种/状态/排序字段/排序方向用真实后端允许值；``page >= 1``；
``1 <= pageSize <= 500``；``min``/``max`` 非负且 ``min <= max``；拒绝未知字段、
NaN、Infinity 与类型不正确的输入。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pet_hospital_mcp.tools.list_pets import (
    LIST_PETS_QUERY_PARAMS,
    ORDER_VALUES,
    SORT_BY_VALUES,
    SPECIES_VALUES,
    STATUS_VALUES,
    ListPetsInput,
)


def errors_of(payload: dict) -> list[dict[str, str]]:
    from pet_hospital_mcp.errors import summarize_validation_error

    with pytest.raises(ValidationError) as excinfo:
        ListPetsInput.model_validate(payload)
    return summarize_validation_error(excinfo.value)


class TestUnknownFields:
    @pytest.mark.parametrize("field", ["bogus", "owner", "sortby", "PAGE"])
    def test_rejected(self, field: str) -> None:
        errors = errors_of({field: 1})
        assert any("Extra inputs are not permitted" in item["message"] for item in errors)

    @pytest.mark.parametrize(
        "field",
        ["page_size", "owner_name", "owner_phone", "sort_by", "min_cost", "max_cost"],
    )
    def test_python_side_snake_case_is_rejected(self, field: str) -> None:
        """只认后端的驼峰参数名。

        以前这里挂着 ``populate_by_name=True``，于是 ``page_size`` 被「顺手」接受，
        等于凭空多出一批后端不存在的私有参数。模型字段名是内部实现细节，
        不该漏到线缆上。
        """
        errors = errors_of({field: 1})
        assert errors, f"{field} 本应被拒绝"

    def test_all_documented_params_are_accepted(self) -> None:
        payload = {
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
            "page": 1,
            "pageSize": 20,
        }
        model = ListPetsInput.model_validate(payload)
        assert set(model.to_query()) == set(LIST_PETS_QUERY_PARAMS)


class TestPaging:
    @pytest.mark.parametrize("page", [0, -1])
    def test_page_must_be_at_least_one(self, page: int) -> None:
        assert errors_of({"page": page})

    def test_page_one_is_fine(self) -> None:
        assert ListPetsInput.model_validate({"page": 1}).page == 1

    @pytest.mark.parametrize("size", [0, -5, 501, 1000])
    def test_page_size_bounds(self, size: int) -> None:
        assert errors_of({"pageSize": size})

    @pytest.mark.parametrize("size", [1, 500])
    def test_page_size_boundaries_are_inclusive(self, size: int) -> None:
        assert ListPetsInput.model_validate({"pageSize": size}).page_size == size


class TestCostRange:
    @pytest.mark.parametrize("field", ["min", "max"])
    def test_negative_rejected(self, field: str) -> None:
        assert errors_of({field: -0.01})

    def test_min_greater_than_max_rejected(self) -> None:
        errors = errors_of({"min": 100, "max": 10})
        assert any("min 不能大于 max" in item["message"] for item in errors)

    def test_equal_bounds_allowed(self) -> None:
        model = ListPetsInput.model_validate({"min": 10, "max": 10})
        assert model.to_query() == {"min": 10, "max": 10}

    def test_int_bounds_are_normalised_for_the_wire(self) -> None:
        """``min=10.0`` 不该发成 ``min=10.0``——整值浮点转回整数。"""
        model = ListPetsInput.model_validate({"min": 10.0, "max": 20.5})
        assert model.to_query() == {"min": 10, "max": 20.5}

    def test_zero_is_allowed(self) -> None:
        assert ListPetsInput.model_validate({"min": 0, "max": 0}).min_cost == 0


class TestNonFiniteNumbers:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    @pytest.mark.parametrize("field", ["min", "max"])
    def test_rejected(self, field: str, bad: float) -> None:
        assert errors_of({field: bad})


class TestWrongTypes:
    @pytest.mark.parametrize("value", ["1", 1.5, True, False, [1], {"v": 1}, None])
    def test_page_rejects_non_integer(self, value: object) -> None:
        if value is None:
            assert ListPetsInput.model_validate({"page": None}).page is None
            return
        errors = errors_of({"page": value})
        assert any(item["field"] == "page" for item in errors)

    @pytest.mark.parametrize("value", [True, "10", [10]])
    def test_min_rejects_non_number(self, value: object) -> None:
        assert any(item["field"] == "min" for item in errors_of({"min": value}))

    @pytest.mark.parametrize("value", [1, True, ["dog"], {"species": "dog"}])
    def test_text_fields_reject_non_string(self, value: object) -> None:
        assert any(item["field"] == "name" for item in errors_of({"name": value}))


class TestEnums:
    @pytest.mark.parametrize("field,allowed", [
        ("species", SPECIES_VALUES),
        ("status", STATUS_VALUES),
        ("sortBy", SORT_BY_VALUES),
        ("order", ORDER_VALUES),
    ])
    def test_every_allowed_value_is_accepted(self, field: str, allowed: tuple[str, ...]) -> None:
        for value in allowed:
            assert ListPetsInput.model_validate({field: value})

    @pytest.mark.parametrize("field", ["species", "status", "sortBy", "order"])
    def test_unknown_value_rejected(self, field: str) -> None:
        assert errors_of({field: "definitely-not-allowed"})

    def test_case_sensitive(self) -> None:
        assert errors_of({"order": "ASC"})
        assert errors_of({"species": "Dog"})


class TestQueryConstruction:
    def test_only_provided_params_are_sent(self) -> None:
        """没给的参数不能瞎补默认值——后端有自己的默认。"""
        assert ListPetsInput.model_validate({}).to_query() == {}

    def test_aliases_are_used_for_the_wire(self) -> None:
        query = ListPetsInput.model_validate(
            {"ownerName": "张三", "sortBy": "name", "pageSize": 50, "max": 9}
        ).to_query()
        assert query == {"ownerName": "张三", "sortBy": "name", "pageSize": 50, "max": 9}
        assert "owner_name" not in query
        assert "page_size" not in query

    def test_none_is_dropped_not_sent_as_null(self) -> None:
        query = ListPetsInput.model_validate({"name": None, "q": "", "page": 2}).to_query()
        assert query == {"q": "", "page": 2}

    def test_no_private_adapter_params_leak(self) -> None:
        query = ListPetsInput.model_validate({"q": "x"}).to_query()
        assert set(query) <= set(LIST_PETS_QUERY_PARAMS)
