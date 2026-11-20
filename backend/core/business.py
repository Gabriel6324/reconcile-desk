"""Bounded, deterministic whole-group reconciliation of normalized money records.

This module performs the collection comparison only. Source completeness, the
meaning of amounts, signs, currencies and any normalization must be confirmed
by the calling business profile; row coverage alone cannot attest completeness.
It neither searches arbitrary amount combinations nor allocates partial rows.
"""

from collections import Counter, defaultdict
from decimal import Decimal, FloatOperation, Inexact, InvalidOperation, Rounded, localcontext
import hashlib
import json
import re

from .errors import CoreError


ENGINE_VERSION = "business-collection-1.0.0"
MAX_RECORDS = 100_000
MAX_GROUP_RECORDS = 100_000
MAX_INTEGER_DIGITS = 30
MAX_SCALE = 8
MAX_KEY_PARTS = 10
MAX_KEY_PART_LENGTH = 256
STATUSES = (
    "exact_equal", "within_tolerance", "amount_difference", "missing_counterpart",
    "ambiguous", "duplicate_event", "invalid_value", "missing_key",
)
RECORD_FIELDS = {"id", "key", "currency", "amount", "event_id", "source_namespace"}
MONEY = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")
CURRENCY = re.compile(r"[A-Z]{3}\Z")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fail(code, message, **details):
    raise CoreError(code, message, details)


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(ch) < 32 or 127 <= ord(ch) <= 159
                        or 0xD800 <= ord(ch) <= 0xDFFF for ch in value))


def _plain(number):
    if number == 0:
        return "0"
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _money(value):
    """Parse only bounded, plain decimal strings, without float or rounding."""
    if not isinstance(value, str) or len(value) > 64 or not MONEY.fullmatch(value):
        return None, "INVALID_DECIMAL_SYNTAX"
    integer, _, fraction = value.lstrip("+-").partition(".")
    if len(integer.lstrip("0")) > MAX_INTEGER_DIGITS or len(fraction) > MAX_SCALE:
        return None, "DECIMAL_OUT_OF_RANGE"
    # Decimal(string) is exact and does not apply the ambient precision.
    return Decimal(value), None


def _rule(rule):
    if not isinstance(rule, dict) or set(rule) != {"stage_id", "aggregation", "tolerance"}:
        _fail("INVALID_BUSINESS_RULE", "规则必须明确指定阶段、聚合方式和金额容差。")
    if rule["stage_id"] != "collection":
        _fail("UNSUPPORTED_BUSINESS_STAGE", "当前引擎仅支持 collection 阶段。")
    if not isinstance(rule["aggregation"], str) or rule["aggregation"] not in ("1:1", "1:N", "N:1"):
        _fail("INVALID_BUSINESS_RULE", "聚合方式只支持 1:1、1:N 或 N:1。")
    tolerance, error = _money(rule["tolerance"])
    if error or tolerance < 0:
        _fail("INVALID_BUSINESS_RULE", "金额容差必须为有效的非负十进制字符串。")
    return {"stage_id": "collection", "aggregation": rule["aggregation"],
            "tolerance": _plain(tolerance)}, tolerance


def _records(records, side):
    checked, identifiers = [], set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            _fail("INVALID_BUSINESS_INPUT", "规范记录字段缺失或含不支持的属性。", side=side, index=index)
        identifier = record["id"]
        if not _text(identifier, 128) or not identifier.strip():
            _fail("INVALID_BUSINESS_INPUT", "记录 ID 必须为有效的短文本。", side=side, index=index)
        if identifier in identifiers:
            _fail("DUPLICATE_RECORD_ID", "同一侧记录 ID 重复，无法建立唯一来源覆盖。", side=side, row_id=identifier)
        identifiers.add(identifier)
        issues = []
        key = record["key"]
        if not isinstance(key, list) or not 1 <= len(key) <= MAX_KEY_PARTS:
            key = None
            issues.append("MISSING_OR_INVALID_KEY")
        elif not all(_text(part, MAX_KEY_PART_LENGTH) and part.strip() for part in key):
            key = None
            issues.append("MISSING_OR_INVALID_KEY")
        else:
            # Tuples distinguish e.g. ['ab', 'c'] from ['a', 'bc'].
            key = tuple(key)
        currency = record["currency"]
        if not isinstance(currency, str) or not CURRENCY.fullmatch(currency):
            currency = None
            issues.append("MISSING_OR_INVALID_CURRENCY")
        amount, error = _money(record["amount"])
        if error:
            issues.append(error)
        namespace, event = record["source_namespace"], record["event_id"]
        identity = None
        if not _text(namespace, 128) or not namespace.strip() or not _text(event, 128) or not event.strip():
            issues.append("MISSING_OR_INVALID_EVENT_IDENTITY")
        else:
            identity = (namespace, event)
        checked.append({"id": identifier, "side": side, "key": key, "currency": currency,
                        "amount": amount, "identity": identity, "issues": issues})
    identities = Counter(r["identity"] for r in checked if r["identity"] is not None)
    for record in checked:
        if record["identity"] is not None and identities[record["identity"]] > 1:
            record["issues"].append("DUPLICATE_EVENT")
    return checked


def _calculate(left, right, tolerance):
    count = len(left) + len(right)
    if count > MAX_GROUP_RECORDS:
        _fail("BUSINESS_GROUP_LIMIT", "同组记录数量超过执行上限。")
    # ceil(log10(n)) without floating point; two guard digits also cover a
    # subtraction between opposite-signed totals and a tolerance comparison.
    count_digits = len(str(max(1, count) - 1)) if count > 1 else 0
    precision = MAX_INTEGER_DIGITS + MAX_SCALE + count_digits + 2
    with localcontext() as context:
        context.prec = precision
        context.Emax, context.Emin = 999, -999
        for trap in (Inexact, Rounded, FloatOperation, InvalidOperation):
            context.traps[trap] = True
        expected = sum((r["amount"] for r in left), Decimal(0))
        observed = sum((r["amount"] for r in right), Decimal(0))
        delta = observed - expected
        for value in (expected, observed, delta):
            integer, _, fraction = format(value, "f").lstrip("+-").partition(".")
            if len(integer.lstrip("0")) > 35 or len(fraction) > MAX_SCALE:
                _fail("BUSINESS_RESULT_LIMIT", "聚合金额超出结果范围，未发布部分结果。")
        status = "exact_equal" if delta == 0 else "within_tolerance" if abs(delta) <= tolerance else "amount_difference"
        return status, {"expected_amount": _plain(expected), "observed_amount": _plain(observed),
                        "delta": _plain(delta), "absolute_delta": _plain(abs(delta))}


def reconcile_groups(left_records, right_records, rule):
    """Return stable groups and complete one-row/one-group coverage per side.

    Record schema: id, key (list of exact text components), currency (explicit
    three-uppercase-letter code), amount (plain decimal text), event_id and
    source_namespace. Currency code *semantics* must be checked by the profile.
    Ids are unique per side; event identities are checked across all records of
    the same side, including different keys/currencies. The two sides represent
    different roles, so their identity namespaces are not compared to each other.

    Any invalid member poisons its entire key/currency group. An unknown currency
    with a known key also blocks numerical conclusions for that key's other
    currency groups, because the missing currency cannot safely be inferred.
    """
    definition, tolerance = _rule(rule)
    if not isinstance(left_records, list) or not isinstance(right_records, list):
        _fail("INVALID_BUSINESS_INPUT", "左右资料必须是规范记录列表。")
    if len(left_records) + len(right_records) > MAX_RECORDS:
        _fail("BUSINESS_INPUT_LIMIT", "输入记录总数超过执行上限。")
    all_records = _records(left_records, "left") + _records(right_records, "right")
    buckets = defaultdict(list)
    unknown_currency_keys = {r["key"] for r in all_records if r["key"] is not None and r["currency"] is None}
    for record in all_records:
        if record["key"] is None or record["currency"] is None:
            bucket = ("invalid", record["side"], record["id"])
        else:
            bucket = ("key", record["key"], record["currency"])
        buckets[bucket].append(record)
    groups, coverage = [], []
    counts = {status: 0 for status in STATUSES}
    for bucket in sorted(buckets, key=_canonical):
        members = sorted(buckets[bucket], key=lambda r: (r["side"], r["id"]))
        left = [r for r in members if r["side"] == "left"]
        right = [r for r in members if r["side"] == "right"]
        issues = [{"side": r["side"], "row_id": r["id"], "codes": sorted(r["issues"])}
                  for r in members if r["issues"]]
        key, currency = members[0]["key"], members[0]["currency"]
        if key is not None and currency is not None and key in unknown_currency_keys:
            issues.append({"side": None, "row_id": None, "codes": ["RELATED_CURRENCY_UNKNOWN"]})
        codes = {code for item in issues for code in item["codes"]}
        amounts = {"expected_amount": None, "observed_amount": None, "delta": None, "absolute_delta": None}
        if "MISSING_OR_INVALID_KEY" in codes:
            status = "missing_key"
        elif "DUPLICATE_EVENT" in codes:
            status = "duplicate_event"
        elif codes:
            status = "invalid_value"
        elif not left or not right:
            status = "missing_counterpart"
        elif ((definition["aggregation"] in ("1:1", "1:N") and len(left) != 1)
              or (definition["aggregation"] in ("1:1", "N:1") and len(right) != 1)):
            status = "ambiguous"
        else:
            status, amounts = _calculate(left, right, tolerance)
        # This is a deterministic locator *within a run*, not a global evidence
        # fingerprint. Callers must scope it by run_id and must not carry
        # approvals to another run merely because a group id is unchanged.
        identity = {"engine": ENGINE_VERSION, "rule": definition,
                    "key": list(key) if key is not None else None, "currency": currency,
                    "members": [(r["side"], r["id"]) for r in members]}
        group_id = "g_" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
        groups.append({"id": group_id, "stage_id": "collection", "key": identity["key"],
                       "currency": currency, "status": status,
                       "left_rows": [r["id"] for r in left], "right_rows": [r["id"] for r in right],
                       "tolerance": definition["tolerance"], **amounts, "issues": issues})
        for record in members:
            coverage.append({"side": record["side"], "row_id": record["id"],
                             "group_id": group_id, "disposition": status})
        counts[status] += 1
    coverage.sort(key=lambda item: (item["side"], item["row_id"]))
    return {"engine_version": ENGINE_VERSION, "stage_id": "collection", "rule": definition,
            "groups": groups, "coverage": coverage,
            "summary": {"left_rows": len(left_records), "right_rows": len(right_records),
                        "input_rows": len(all_records), "covered_rows": len(coverage),
                        "total_groups": len(groups), "counts": counts,
                        "technical_coverage": "complete", "source_completeness": "not_attested",
                        "requires_review": any(g["status"] != "exact_equal" for g in groups)}}
