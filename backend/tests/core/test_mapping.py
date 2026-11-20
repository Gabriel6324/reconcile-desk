"""Contract tests for explicit mapping, exact money and source row identity."""
import copy
import io
from decimal import Decimal

import openpyxl
import pytest

from core.business import reconcile_groups
from core.dataset_parser import parse_dataset
from core.errors import CoreError
from core.mapping import MAPPING_VERSION, map_rows, validate_mapping


RULE = {"stage_id": "collection", "aggregation": "1:1", "tolerance": "0"}


def definition(**changes):
    return {"key_columns": ["order"], "amount_column": "amount",
            "event_id_column": "event", "currency": {"column": "currency"},
            "source_namespace": "payment-system", **changes}


def table(data=b"order,amount,event,currency\n001,9007199254740993.01,P1,CNY\n"):
    return parse_dataset(data, "data.csv")


def mapped(parsed=None, ident="dataset", mapping=None):
    parsed = table() if parsed is None else parsed
    return map_rows(ident, parsed["rows"], validate_mapping(mapping or definition(), parsed["headers"]))


def assert_error(code, function, *args):
    with pytest.raises(CoreError) as exc:
        function(*args)
    assert exc.value.code == code


def test_exact_decimal_and_scoped_row_ids_preserve_original_evidence():
    source = table()
    original = copy.deepcopy(source)
    compiled = validate_mapping(definition(), source["headers"])
    assert compiled == {
        "mapping_version": MAPPING_VERSION, "key_columns": ["c1"], "amount_column": "c2",
        "event_id_column": "c3", "currency": {"column": "c4"}, "source_namespace": "payment-system",
    }
    left, right = mapped(source, "orders"), mapped(source, "payments")
    assert left[0] == {"id": "orders:r1", "key": ["001"], "amount": "9007199254740993.01",
                       "event_id": "P1", "currency": "CNY", "source_namespace": "payment-system"}
    result = reconcile_groups(left, right, RULE)
    assert result["groups"][0]["status"] == "exact_equal"
    assert result["groups"][0]["expected_amount"] == "9007199254740993.01"
    assert {(item["side"], item["row_id"]) for item in result["coverage"]} == {
        ("left", "orders:r1"), ("right", "payments:r1")}
    assert source == original


def test_one_to_many_mapping_handles_real_parser_tables():
    left = table(b"order,amount,event,currency\nA,9007199254740993.03,order-A,CNY\n")
    right = table(b"order,amount,event,currency\nA,9007199254740993.01,p1,CNY\nA,0.02,p2,CNY\n")
    result = reconcile_groups(mapped(left, "orders"), mapped(right, "payments"), {**RULE, "aggregation": "1:N"})
    assert result["groups"][0]["status"] == "exact_equal"
    assert result["groups"][0]["observed_amount"] == "9007199254740993.03"
    assert result["summary"]["covered_rows"] == 3
    assert result["summary"]["source_completeness"] == "not_attested"


def test_key_can_intentionally_be_event_identity_and_currency_is_explicit():
    source = table(b"order,amount\n001,10.00\n")
    mapping = definition(event_id_column="order", currency={"constant": "CNY"})
    result = mapped(source, mapping=mapping)
    assert result[0]["event_id"] == result[0]["key"][0] == "001"
    assert result[0]["currency"] == "CNY"


def test_composite_keys_remain_separate_components():
    source = table(b"order,branch,amount,event,currency\nab,c,10,p1,CNY\na,bc,20,p2,CNY\n")
    records = mapped(source, mapping=definition(key_columns=["order", "branch"]))
    assert [r["key"] for r in records] == [["ab", "c"], ["a", "bc"]]
    result = reconcile_groups(records, [], RULE)
    assert result["summary"]["total_groups"] == 2


def test_ascii_edge_trim_preserves_case_unicode_internal_spaces_and_zeroes():
    source = table("order,amount,event,currency\n 00Ab 中　, +001.200 , P 01 , CNY \n".encode())
    record = mapped(source)[0]
    assert record["key"] == ["00Ab 中　"]
    assert record["amount"] == "+001.200"
    assert record["event_id"] == "P 01"
    assert record["currency"] == "CNY"
    source["rows"][0]["cells"]["c1"]["raw"] = "\t\n00Ab\r "
    assert mapped(source)[0]["key"] == ["00Ab"]


def test_header_names_are_resolved_exactly_even_when_they_look_like_ids():
    source = table(b"c2,c1, amount ,currency\nA,P1,10,CNY\n")
    mapping = definition(key_columns=["c2"], amount_column=" amount ", event_id_column="c1")
    compiled = validate_mapping(mapping, source["headers"])
    assert compiled["key_columns"] == ["c1"]
    assert compiled["event_id_column"] == "c2"
    assert compiled["amount_column"] == "c3"
    assert_error("MAPPING_COLUMN_NOT_FOUND", validate_mapping, {**mapping, "amount_column": "amount"}, source["headers"])


@pytest.mark.parametrize("raw", ["", " ", "NaN", "1e3", "1,000.00", "¥10", "1.000000001", "9" * 31])
def test_invalid_amount_is_covered_and_cannot_match_zero(raw):
    source = table()
    source["rows"][0]["cells"]["c2"]["raw"] = raw
    left = mapped(source)
    right = [{**left[0], "id": "right:r1", "event_id": "payment", "amount": "0"}]
    result = reconcile_groups(left, right, RULE)
    assert result["groups"][0]["status"] == "invalid_value"
    assert result["groups"][0]["delta"] is None
    assert result["summary"]["covered_rows"] == 2


@pytest.mark.parametrize("tag,raw", [("null", None), ("date_formatted_value", "45678"), ("boolean", "1")])
@pytest.mark.parametrize("column", ["c1", "c2", "c3", "c4"])
def test_typed_non_business_values_stay_invalid_in_each_role(tag, raw, column):
    source = table()
    source["rows"][0]["cells"][column] = {"tag": tag, "raw": raw}
    record = mapped(source)[0]
    result = reconcile_groups([record], [], RULE)
    assert result["groups"][0]["status"] == ("missing_key" if column == "c1" else "invalid_value")
    assert result["summary"]["covered_rows"] == 1


def test_native_xlsx_boolean_and_scientific_lexeme_are_not_silently_money():
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["order", "amount", "event", "currency"])
    sheet.append(["A", True, "P1", "CNY"])
    sheet.append(["B", 1e-10, "P2", "CNY"])
    stream = io.BytesIO()
    workbook.save(stream)
    source = parse_dataset(stream.getvalue(), "data.xlsx")
    records = mapped(source)
    assert records[0]["amount"] is None
    assert records[1]["amount"] == source["rows"][1]["cells"]["c2"]["raw"] == "1e-10"
    result = reconcile_groups(records, [], RULE)
    assert result["summary"]["counts"]["invalid_value"] == 2


def test_currency_case_is_not_guessed_and_blocks_other_same_key_conclusions():
    source = table(b"order,amount,event,currency\nA,10,P1,cny\nA,10,P2,CNY\n")
    records = mapped(source)
    assert records[0]["currency"] == "cny"
    result = reconcile_groups(records, [], RULE)
    assert result["summary"]["counts"]["invalid_value"] == 2
    assert all(group["delta"] is None for group in result["groups"])


def test_duplicate_event_after_declared_trim_is_flagged_and_not_dropped():
    source = table(b"order,amount,event,currency\nA,10, P1 ,CNY\nB,10,P1,CNY\n")
    result = reconcile_groups(mapped(source), [], RULE)
    assert result["summary"]["counts"]["duplicate_event"] == 2
    assert result["summary"]["covered_rows"] == 2


@pytest.mark.parametrize("change", [
    {"key_columns": []}, {"key_columns": "order"}, {"key_columns": [None]},
    {"key_columns": ["order"] * 11}, {"amount_column": 1}, {"event_id_column": ""},
    {"currency": {}}, {"currency": {"constant": "cny"}}, {"currency": {"constant": " CNY"}},
    {"currency": {"constant": 123}}, {"currency": {"column": "currency", "constant": "CNY"}},
    {"source_namespace": ""}, {"source_namespace": " padded "}, {"source_namespace": "a" * 129},
    {"source_namespace": "a\n"}, {"source_namespace": "\ud800"}, {"unexpected": True},
])
def test_invalid_mapping_schema_is_rejected_before_rows(change):
    assert_error("INVALID_MAPPING", validate_mapping, definition(**change), table()["headers"])


@pytest.mark.parametrize("change", [
    {"key_columns": ["order", "order"]}, {"amount_column": "order"},
    {"amount_column": "event"}, {"currency": {"column": "order"}},
    {"currency": {"column": "amount"}}, {"currency": {"column": "event"}},
])
def test_conflicting_roles_are_rejected(change):
    assert_error("MAPPING_COLUMN_CONFLICT", validate_mapping, definition(**change), table()["headers"])


@pytest.mark.parametrize("columns", [None, [], [{"id": "c0", "name": "x"}],
    [{"id": "c51", "name": "x"}], [{"id": "c1", "name": "x"}, {"id": "c1", "name": "y"}],
    [{"id": "c1", "name": "x"}, {"id": "c2", "name": "x"}], [{"id": "c1", "name": " "}]])
def test_ambiguous_or_malformed_header_contract_is_rejected(columns):
    assert_error("INVALID_MAPPING_HEADERS", validate_mapping, definition(), columns)


def test_mapping_version_and_compiled_column_contract_cannot_be_bypassed():
    source = table()
    compiled = validate_mapping(definition(), source["headers"])
    assert_error("MAPPING_VERSION_UNSUPPORTED", map_rows, "d", source["rows"], {**compiled, "mapping_version": "future"})
    assert_error("INVALID_MAPPING", map_rows, "d", source["rows"], definition())
    assert_error("INVALID_MAPPING", map_rows, "d", source["rows"], {**compiled, "amount_column": "amount"})


@pytest.mark.parametrize("raw", [1.2, 1, True, Decimal("1.2"), [], {}, None])
def test_non_text_numeric_or_container_values_are_structural_corruption(raw):
    source = table()
    source["rows"][0]["cells"]["c2"]["raw"] = raw
    assert_error("INVALID_MAPPING_ROWS", mapped, source)


@pytest.mark.parametrize("cell", [{"tag": "null", "raw": "0"}, {"tag": "formula", "raw": "=1"},
    {"tag": "literal_text"}, {"tag": "literal_text", "raw": "0", "extra": True},
    {"tag": [], "raw": "0"}])
def test_corrupt_cells_fail_instead_of_creating_partial_results(cell):
    source = table()
    source["rows"][0]["cells"]["c2"] = cell
    assert_error("INVALID_MAPPING_ROWS", mapped, source)
    del source["rows"][0]["cells"]["c2"]
    assert_error("INVALID_MAPPING_ROWS", mapped, source)


def test_row_identity_is_unique_and_cannot_collide_with_dataset_separator():
    source = table()
    source["rows"].append(copy.deepcopy(source["rows"][0]))
    assert_error("DUPLICATE_RECORD_ID", mapped, source)
    assert_error("INVALID_MAPPING_ROWS", mapped, table(), "unsafe:dataset")
    source["rows"][1]["id"] = "r01"
    assert_error("INVALID_MAPPING_ROWS", mapped, source)


def test_empty_table_is_preserved_without_inventing_records():
    source = table(b"order,amount,event,currency\n")
    assert mapped(source) == []


def test_compilation_does_not_mutate_or_alias_confirmation_input():
    source, raw = table(), definition()
    original = copy.deepcopy(raw)
    compiled = validate_mapping(raw, source["headers"])
    compiled["key_columns"][0] = "c2"
    compiled["currency"]["column"] = "c1"
    assert raw == original
