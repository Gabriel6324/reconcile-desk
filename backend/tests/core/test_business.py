"""Independent expected outcomes for the new whole-group money engine."""
import copy
from decimal import getcontext, setcontext, ROUND_FLOOR
import random

import pytest

from core.business import MAX_RECORDS, reconcile_groups
from core.errors import CoreError


def record(identifier, amount, key=None, currency="CNY", event=None, namespace="source"):
    return {"id": identifier, "key": ["order-1"] if key is None else key,
            "currency": currency, "amount": amount, "event_id": identifier if event is None else event,
            "source_namespace": namespace}


def rule(aggregation="1:1", tolerance="0", stage="collection"):
    return {"stage_id": stage, "aggregation": aggregation, "tolerance": tolerance}


def group(left, right, **kwargs):
    result = reconcile_groups(left, right, rule(**kwargs))
    assert len(result["groups"]) == 1
    return result["groups"][0]


def test_one_to_many_whole_group_payments_with_exact_large_decimal():
    actual = group([record("order", "9007199254740993.03")],
                   [record("p1", "9007199254740993.01"), record("p2", "0.02")],
                   aggregation="1:N")
    assert actual["status"] == "exact_equal"
    assert actual["observed_amount"] == "9007199254740993.03"
    assert actual["delta"] == "0"


def test_many_to_one_and_negative_values_remain_exact():
    actual = group([record("a", "-1.25"), record("b", "-2.75")],
                   [record("payment", "-4")], aggregation="N:1")
    assert actual["status"] == "exact_equal"
    assert actual["expected_amount"] == "-4"


def test_large_value_small_difference_is_not_float_equal():
    actual = group([record("a", "9007199254740993.01")], [record("b", "9007199254740993.02")])
    assert (actual["status"], actual["delta"]) == ("amount_difference", "0.01")


@pytest.mark.parametrize("delta,tolerance,status", [("10.01", "0.01", "within_tolerance"),
    ("9.99", "0.01", "within_tolerance"), ("10.01000001", "0.01", "amount_difference"),
    ("10", "0.01", "exact_equal")])
def test_absolute_tolerance_boundary_is_inclusive(delta, tolerance, status):
    actual = group([record("a", "10")], [record("b", delta)], tolerance=tolerance)
    assert actual["status"] == status
    assert actual["delta"] is not None


def test_precision_independent_of_ambient_decimal_context():
    saved = getcontext().copy()
    try:
        getcontext().prec = 2
        getcontext().Emax = 2
        amount = "9" * 30
        actual = group([record("a", amount), record("b", "0.00000001")],
                       [record("c", amount + ".00000001")], aggregation="N:1")
        assert actual["status"] == "exact_equal"
        assert actual["expected_amount"] == amount + ".00000001"
    finally:
        setcontext(saved)


@pytest.mark.parametrize("left,right,tolerance", [("-0", "0.00000000", "-0.00000000"),
                                                  ("+000.00", "-00.00000000", "0")])
def test_negative_zero_is_equal_and_serialized_as_canonical_zero(left,right,tolerance):
    actual = group([record("a", left)], [record("b", right)], tolerance=tolerance)
    assert actual["status"] == "exact_equal"
    assert {actual[k] for k in ("expected_amount", "observed_amount", "delta", "absolute_delta", "tolerance")} == {"0"}


def test_non_default_rounding_and_clamp_do_not_affect_exact_results():
    saved = getcontext().copy()
    try:
        getcontext().prec = 1
        getcontext().rounding = ROUND_FLOOR
        getcontext().clamp = 1
        getcontext().Emax = 1
        getcontext().Emin = -1
        actual = group([record("a", "999999999999999999999999999999.00000001")],
                       [record("b", "999999999999999999999999999999.00000002")])
        assert actual["delta"] == "0.00000001"
    finally:
        setcontext(saved)


def test_large_cancelling_terms_retain_tiny_residual():
    amount = "9" * 30 + ".99999999"
    actual = group([record("a", "0.00000001")],
                   [record("b", amount), record("c", "-" + amount), record("d", "0.00000001")],
                   aggregation="1:N")
    assert actual["status"] == "exact_equal"
    assert actual["observed_amount"] == "0.00000001"


def test_maximum_whole_group_accumulation_is_exact_and_has_full_coverage():
    value = "9" * 30
    payments = [record(str(i), value) for i in range(MAX_RECORDS - 1)]
    result = reconcile_groups([record("order", "0")], payments, rule("1:N"))
    actual = result["groups"][0]
    assert actual["observed_amount"] == str(int(value) * (MAX_RECORDS - 1))
    assert actual["delta"] == actual["observed_amount"]
    assert result["summary"]["covered_rows"] == MAX_RECORDS


@pytest.mark.parametrize("value", [None, "", " ", 1, 0.01, True, "NaN", "Infinity", "1e2", "1,000", ".1", "1.",
                                   "1 " , "1" * 31, "0.000000001", "0" * 65])
def test_invalid_amount_is_never_silently_zero(value):
    actual = group([record("a", value)], [record("b", "0")])
    assert actual["status"] == "invalid_value"
    assert actual["delta"] is None


def test_invalid_member_blocks_sum_for_entire_matching_bucket():
    actual = group([record("a", "10")], [record("b", "10"), record("c", None)], aggregation="1:N")
    assert actual["status"] == "invalid_value"
    assert actual["observed_amount"] is None
    assert actual["right_rows"] == ["b", "c"]


def test_currency_codes_never_cancel_each_other():
    result = reconcile_groups([record("a", "10", currency="CNY")],
                              [record("b", "10", currency="USD")], rule())
    assert len(result["groups"]) == 2
    assert {g["status"] for g in result["groups"]} == {"missing_counterpart"}


def test_unknown_currency_blocks_false_equal_for_known_key():
    result = reconcile_groups([record("a", "10")],
                              [record("b", "10"), record("c", "2", currency=None)], rule("1:N"))
    assert {g["status"] for g in result["groups"]} == {"invalid_value"}
    assert result["summary"]["covered_rows"] == 3


@pytest.mark.parametrize("key", [[], [""], [" "], [None], "order-1", ["a"] * 11, ["a" * 257]])
def test_missing_or_invalid_keys_have_explicit_coverage(key):
    result = reconcile_groups([record("a", "1", key=key)], [record("b", "1")], rule())
    assert {g["status"] for g in result["groups"]} == {"missing_key", "missing_counterpart"}
    assert result["summary"]["covered_rows"] == 2


def test_composite_key_components_cannot_collide_by_concatenation():
    result = reconcile_groups([record("a", "1", key=["a|b", "c"])],
                              [record("b", "1", key=["a", "b|c"])], rule())
    assert len(result["groups"]) == 2
    assert {g["status"] for g in result["groups"]} == {"missing_counterpart"}


def test_exact_keys_preserve_case_leading_zeros_and_space():
    result = reconcile_groups([record("a", "1", key=["001", " X"])],
                              [record("b", "1", key=["1", "x"])], rule())
    assert result["summary"]["counts"]["missing_counterpart"] == 2


@pytest.mark.parametrize("aggregation,left_count,right_count", [("1:1", 2, 1), ("1:N", 2, 1), ("N:1", 1, 2), ("1:N", 2, 2)])
def test_disallowed_cardinality_is_ambiguous_without_subset_search(aggregation,left_count,right_count):
    actual = group([record("l"+str(i), "1") for i in range(left_count)],
                   [record("r"+str(i), "1") for i in range(right_count)], aggregation=aggregation)
    assert actual["status"] == "ambiguous"
    assert actual["expected_amount"] is None


def test_duplicate_event_same_namespace_blocks_all_affected_groups():
    result = reconcile_groups([record("l1", "1", key=["A"]), record("l2", "2", key=["B"])],
                              [record("r1", "1", key=["A"], event="duplicate"),
                               record("r2", "2", key=["B"], event="duplicate")], rule())
    assert {g["status"] for g in result["groups"]} == {"duplicate_event"}
    assert result["summary"]["covered_rows"] == 4


def test_duplicate_event_cannot_evade_detection_by_currency():
    result = reconcile_groups([record("l1", "1", currency="CNY"), record("l2", "1", currency="USD")],
                              [record("r1", "1", event="same", currency="CNY"),
                               record("r2", "1", event="same", currency="USD")], rule())
    assert {g["status"] for g in result["groups"]} == {"duplicate_event"}
    assert len(result["coverage"]) == 4


def test_unknown_currency_only_blocks_groups_with_same_business_key():
    result = reconcile_groups([record("a", "1", key=["A"]), record("b", "1", key=["B"])],
                              [record("c", "1", key=["A"], currency=None),
                               record("d", "1", key=["B"])], rule())
    assert result["summary"]["counts"]["exact_equal"] == 1
    assert next(g for g in result["groups"] if g["key"] == ["B"])["status"] == "exact_equal"


def test_same_external_id_in_distinct_namespaces_is_not_duplicate():
    actual = group([record("a", "3")], [record("b", "1", event="1", namespace="channel-a"),
                                        record("c", "2", event="1", namespace="channel-b")], aggregation="1:N")
    assert actual["status"] == "exact_equal"


def test_event_identity_is_compared_within_side_not_between_roles():
    actual = group([record("same", "1")], [record("same", "1")])
    assert actual["status"] == "exact_equal"


def test_empty_event_identity_cannot_silently_bypass_dedup():
    actual = group([record("a", "1", event="")], [record("b", "1")])
    assert actual["status"] == "invalid_value"


def test_stable_output_under_input_reordering_and_no_input_mutation():
    left = [record("L"+str(i), str(i), key=[str(i)]) for i in range(20)]
    right = [record("R"+str(i), str(i+1), key=[str(i)]) for i in range(20)]
    definition = rule()
    before = copy.deepcopy((left, right, definition))
    expected = reconcile_groups(left, right, definition)
    assert (left, right, definition) == before
    random.Random(13).shuffle(left)
    random.Random(17).shuffle(right)
    assert reconcile_groups(left, right, definition) == expected


def test_coverage_once_for_each_side_and_record_with_mixed_issues():
    left = [record("a", "1"), record("b", None, key=["B"]), record("c", "1", key=[])]
    right = [record("x", "1"), record("y", "3", key=["Y"]), record("z", "2", key=["Z"], currency="")]
    result = reconcile_groups(left, right, rule())
    pairs = [(r["side"], r["row_id"]) for r in result["coverage"]]
    assert len(pairs) == len(set(pairs)) == 6
    assert set(pairs) == {("left", r["id"]) for r in left} | {("right",r["id"]) for r in right}
    assert {c["group_id"] for c in result["coverage"]} == {g["id"] for g in result["groups"]}
    assert result["summary"]["source_completeness"] == "not_attested"


def test_record_limit_is_enforced_before_processing():
    with pytest.raises(CoreError) as error:
        reconcile_groups([{}] * (MAX_RECORDS + 1), [], rule())
    assert error.value.code == "BUSINESS_INPUT_LIMIT"


def test_duplicate_row_id_rejected_before_coverage_can_be_corrupted():
    with pytest.raises(CoreError) as error:
        reconcile_groups([record("same", "1"), record("same", "2")], [], rule())
    assert error.value.code == "DUPLICATE_RECORD_ID"


@pytest.mark.parametrize("definition", [{}, rule(tolerance="-1"), rule(tolerance=1), rule(aggregation="N:M"),
                                        {**rule(), "execute": "anything"}])
def test_rule_rejection(definition):
    with pytest.raises(CoreError) as error:
        reconcile_groups([], [], definition)
    assert error.value.code == "INVALID_BUSINESS_RULE"


def test_refund_or_settlement_does_not_claim_implementation():
    with pytest.raises(CoreError) as error:
        reconcile_groups([], [], rule(stage="refund"))
    assert error.value.code == "UNSUPPORTED_BUSINESS_STAGE"


def test_unknown_record_fields_and_non_list_input_rejected():
    with pytest.raises(CoreError):
        reconcile_groups([{**record("a", "1"), "sql": "select"}], [], rule())
    with pytest.raises(CoreError):
        reconcile_groups(iter([]), [], rule())


def test_non_serializable_record_id_is_rejected_as_input_error():
    with pytest.raises(CoreError) as error:
        reconcile_groups([record("\ud800", "1")], [], rule())
    assert error.value.code == "INVALID_BUSINESS_INPUT"


@pytest.mark.parametrize("identifier", ["", " ", "\u3000"])
def test_blank_record_id_is_rejected(identifier):
    with pytest.raises(CoreError) as error:
        reconcile_groups([record(identifier, "1")], [], rule())
    assert error.value.code == "INVALID_BUSINESS_INPUT"


def test_random_signed_sums_match_independent_integer_arithmetic():
    rng = random.Random(24)
    def money(units):
        sign = "-" if units < 0 else ""
        whole, fraction = divmod(abs(units), 100_000_000)
        return f"{sign}{whole}.{fraction:08d}"
    left, right, expected_deltas = [], [], {}
    for key in range(70):
        parts = [rng.randrange(-(10**35), 10**35) for _ in range(rng.randrange(1, 20))]
        difference = rng.choice([0, -1, 1, 19, 20, 21, -21])
        expected_deltas[str(key)] = difference
        left.append(record(f"l{key}", money(sum(parts) - difference), key=[str(key)]))
        right.extend(record(f"r{key}-{n}", money(value), key=[str(key)]) for n,value in enumerate(parts))
    result = reconcile_groups(left,right,rule("1:N", "0.00000020"))
    assert len(result["groups"]) == 70
    for actual in result["groups"]:
        delta = expected_deltas[actual["key"][0]]
        expected = "exact_equal" if delta == 0 else "within_tolerance" if abs(delta) <= 20 else "amount_difference"
        assert actual["status"] == expected
        # Avoid Decimal in the oracle: parse the emitted string into integer
        # units and compare to arithmetic independent of the implementation.
        raw = actual["delta"]
        whole, _, fraction = raw.lstrip("-").partition(".")
        emitted_units = (int(whole) * 100_000_000 + int(fraction.ljust(8,"0"))) * (-1 if raw.startswith("-") else 1)
        assert emitted_units == delta


def test_empty_input_has_no_business_completeness_claim():
    result = reconcile_groups([], [], rule())
    assert result["groups"] == result["coverage"] == []
    assert result["summary"]["source_completeness"] == "not_attested"
