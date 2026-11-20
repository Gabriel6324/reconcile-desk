"""Independent expectations for business time boundaries and declarations."""
import copy
from datetime import datetime

import pytest

from core.errors import CoreError
from core.period_policy import assess_completeness, validate_attestations, validate_period_definition


def definition(**changes):
    return {"name": "November collection", "start_date": "2026-11-01", "end_date": "2026-11-30",
            "timezone": "Asia/Shanghai", "cutoff_at": "2026-12-01T00:00:00+08:00", **changes}


def declarations(orders="complete", payments="complete"):
    return {"orders": {"status": orders, "note": "Order export checked through the cutoff."},
            "payments": {"status": payments, "note": "Payment export checked through the cutoff."}}


def assert_error(code, operation, value):
    with pytest.raises(CoreError) as exc:
        operation(value)
    assert exc.value.code == code


def test_inclusive_business_dates_freeze_exact_utc_boundary_evidence():
    raw = definition(name="  十一月核对  ")
    original = copy.deepcopy(raw)
    result = validate_period_definition(raw)
    assert result == {"name": "十一月核对", "start_date": "2026-11-01", "end_date": "2026-11-30",
                      "timezone": "Asia/Shanghai", "cutoff_at": "2026-11-30T16:00:00+00:00",
                      "start_inclusive": "2026-10-31T16:00:00+00:00",
                      "end_exclusive": "2026-11-30T16:00:00+00:00"}
    assert raw == original


def test_cutoff_equivalent_offset_and_microsecond_boundary():
    expected = validate_period_definition(definition())
    assert validate_period_definition(definition(cutoff_at="2026-11-30T16:00:00Z")) == expected
    later = validate_period_definition(definition(cutoff_at="2026-11-30T16:00:00.000001+00:00"))
    assert later["cutoff_at"] == "2026-11-30T16:00:00.000001+00:00"
    assert_error("PERIOD_CUTOFF_BEFORE_END", validate_period_definition,
                 definition(cutoff_at="2026-11-30T15:59:59.999999Z"))


@pytest.mark.parametrize("start,end", [("2000-02-29", "2000-02-29"), ("2200-03-01", "2200-03-01")])
def test_one_day_past_and_future_periods_do_not_depend_on_current_clock(start, end):
    result = validate_period_definition(definition(start_date=start, end_date=end, timezone="UTC",
                                                  cutoff_at="2200-03-02T00:00:00Z"))
    assert result["start_inclusive"] == start + "T00:00:00+00:00"
    delta = datetime.fromisoformat(result["end_exclusive"]) - datetime.fromisoformat(result["start_inclusive"])
    assert delta.total_seconds() == 86400


def test_leap_year_366_days_accepted_and_367_days_rejected():
    raw = definition(start_date="2024-01-01", end_date="2024-12-31", cutoff_at="2025-01-01T00:00:00+08:00")
    assert validate_period_definition(raw)["end_date"] == "2024-12-31"
    assert_error("INVALID_PERIOD_RANGE", validate_period_definition,
                 {**raw, "end_date": "2025-01-01", "cutoff_at": "2025-01-02T00:00:00+08:00"})


@pytest.mark.parametrize("day,cutoff,hours", [
    ("2026-03-08", "2026-03-09T00:00:00-04:00", 23),
    ("2026-11-01", "2026-11-02T00:00:00-05:00", 25),
])
def test_dst_days_use_calendar_midnights_not_24_hour_offsets(day, cutoff, hours):
    result = validate_period_definition(definition(start_date=day, end_date=day,
        timezone="America/New_York", cutoff_at=cutoff))
    delta = datetime.fromisoformat(result["end_exclusive"]) - datetime.fromisoformat(result["start_inclusive"])
    assert delta.total_seconds() == hours * 3600


def test_half_hour_timezone_and_45_minute_timezone_are_supported():
    for zone, cutoff, expected in [
        ("Asia/Kolkata", "2026-12-01T00:00:00+05:30", "2026-11-30T18:30:00+00:00"),
        ("Asia/Kathmandu", "2026-12-01T00:00:00+05:45", "2026-11-30T18:15:00+00:00"),
    ]:
        result = validate_period_definition(definition(timezone=zone, cutoff_at=cutoff))
        assert result["cutoff_at"] == result["end_exclusive"] == expected


@pytest.mark.parametrize("start,end,zone,cutoff", [
    ("2011-12-30", "2011-12-30", "Pacific/Apia", "2012-01-01T00:00:00Z"),
    ("2011-12-29", "2011-12-29", "Pacific/Apia", "2012-01-01T00:00:00Z"),
    ("2020-11-01", "2020-11-01", "America/Havana", "2020-11-03T00:00:00Z"),
    ("2018-11-04", "2018-11-04", "America/Sao_Paulo", "2018-11-06T00:00:00Z"),
])
def test_nonexistent_or_ambiguous_midnight_is_not_silently_chosen(start, end, zone, cutoff):
    assert_error("AMBIGUOUS_PERIOD_BOUNDARY", validate_period_definition,
        definition(start_date=start, end_date=end, timezone=zone, cutoff_at=cutoff))


@pytest.mark.parametrize("value", ["2026-2-01", "20260201", "2026-W01-1", "2026-02-29", "0000-01-01",
                                  "2026-11-01T00:00:00", None, 20261101, " 2026-11-01"])
@pytest.mark.parametrize("field", ["start_date", "end_date"])
def test_dates_have_strict_calendar_shape(value, field):
    assert_error("INVALID_PERIOD_DATE", validate_period_definition, definition(**{field: value}))


def test_date_overflow_and_reversed_range_return_contract_errors():
    assert_error("INVALID_PERIOD_RANGE", validate_period_definition, definition(start_date="2026-12-01"))
    assert_error("INVALID_PERIOD_DATE", validate_period_definition, definition(
        start_date="9999-12-31", end_date="9999-12-31", cutoff_at="9999-12-31T23:59:59Z"))
    assert_error("INVALID_PERIOD_DATE", validate_period_definition, definition(
        start_date="0001-01-01", end_date="0001-01-01", cutoff_at="0001-01-02T00:00:00Z"))


@pytest.mark.parametrize("value", ["2026-12-01", "2026-12-01T00:00:00", "2026-12-01 00:00:00Z",
    "2026-12-01T00:00Z", "20261201T000000Z", "2026-12-01T00:00:00+08:60",
    "2026-12-01T00:00:00+24:00", "2026-12-01T00:00:00+0800", "2026-12-01T24:00:00Z",
    "2026-12-01T00:00:60Z", "2026-12-01T00:00:00.1234567Z", "2026-12-01T00:00:00z",
    "2026-12-01T00:00:00+08:00:00", "2026-12-01T00:00:00+08:00 ", True, None,
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_naive_or_noncanonical_or_out_of_range_cutoff_rejected(value):
    assert_error("INVALID_PERIOD_CUTOFF", validate_period_definition, definition(cutoff_at=value))


@pytest.mark.parametrize("value", [None, "", " Asia/Shanghai", "../Asia/Shanghai", "/Asia/Shanghai",
    "Mars/Olympus", "localtime", "posixrules", "right/UTC", "posix/UTC", "UTC+08:00", "a" * 129])
def test_timezone_must_be_explicit_resolvable_and_not_local_environment_alias(value):
    assert_error("INVALID_PERIOD_TIMEZONE", validate_period_definition, definition(timezone=value))


@pytest.mark.parametrize("value", [None, [], {}, {**definition(), "unexpected": True},
    {key: value for key, value in definition().items() if key != "cutoff_at"}])
def test_period_schema_is_exact(value):
    assert_error("INVALID_PERIOD_DEFINITION", validate_period_definition, value)


@pytest.mark.parametrize("value", ["", " ", "　", "a" * 161, "bad\nname", "\ud800", 1, None])
def test_invalid_names_rejected(value):
    assert_error("INVALID_PERIOD_DEFINITION", validate_period_definition, definition(name=value))


@pytest.mark.parametrize("orders,payments,expected", [
    ("complete", "complete", "attested_complete"),
    ("complete", "not_attested", "not_attested"),
    ("not_attested", "complete", "not_attested"),
    ("not_attested", "not_attested", "not_attested"),
    ("incomplete", "complete", "incomplete"),
    ("complete", "incomplete", "incomplete"),
    ("incomplete", "not_attested", "incomplete"),
    ("not_attested", "incomplete", "incomplete"),
    ("incomplete", "incomplete", "incomplete"),
])
def test_all_attestation_combinations_never_silently_upgrade(orders, payments, expected):
    raw = declarations(orders, payments)
    original = copy.deepcopy(raw)
    assert assess_completeness(raw) == {"status": expected, "roles": raw}
    assert raw == original


def test_notes_normalized_without_mutating_and_allow_empty_unattested():
    raw = {"orders": {"status": "not_attested", "note": " \t\r\n"},
           "payments": {"status": "complete", "note": " 已核对导出范围。\n文件为空，确认没有交易。 "}}
    expected = {"orders": {"status": "not_attested", "note": ""},
                "payments": {"status": "complete", "note": "已核对导出范围。\n文件为空，确认没有交易。"}}
    normalized = validate_attestations(raw)
    assert normalized == expected
    assert assess_completeness(raw)["status"] == "not_attested"
    normalized["orders"]["status"] = "complete"
    assert raw["orders"]["status"] == "not_attested"


@pytest.mark.parametrize("status", ["complete", "incomplete"])
@pytest.mark.parametrize("note", ["", " \t\n", "　"])
def test_definite_declaration_requires_meaningful_nonempty_note(status, note):
    raw = declarations()
    raw["orders"] = {"status": status, "note": note}
    assert_error("INVALID_SOURCE_ATTESTATION", validate_attestations, raw)


@pytest.mark.parametrize("note", ["a" * 1001, "note\x00", "note\x7f", "\ud800", None, 123, {}])
def test_all_notes_require_bounded_safe_text(note):
    raw = declarations("not_attested")
    raw["orders"]["note"] = note
    assert_error("INVALID_SOURCE_ATTESTATION", validate_attestations, raw)


def test_1000_character_note_is_allowed_without_using_row_count_as_evidence():
    raw = declarations()
    raw["orders"]["note"] = "x" * 1000
    assert assess_completeness(raw)["status"] == "attested_complete"
    raw["orders"]["row_count"] = 0
    assert_error("INVALID_SOURCE_ATTESTATION", validate_attestations, raw)


@pytest.mark.parametrize("value", [None, [], {}, {"orders": {"status": "complete", "note": "all"}},
    {**declarations(), "refunds": {"status": "complete", "note": "all"}},
    {**declarations(), "orders": {"status": "complete"}},
    {**declarations(), "orders": {"status": "attested_complete", "note": "all"}},
    {**declarations(), "orders": {"status": [], "note": "all"}},
    {**declarations(), "orders": None}])
def test_attestation_schema_rejects_missing_roles_unknown_fields_and_statuses(value):
    assert_error("INVALID_SOURCE_ATTESTATION", validate_attestations, value)
    assert_error("INVALID_SOURCE_ATTESTATION", assess_completeness, value)
