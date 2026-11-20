"""Pure collection-period boundaries and explicitly attested source completeness.

Dates are inclusive business dates in a named IANA zone. Persisted UTC boundaries
are computed when the period is created, so a later timezone database update cannot silently
change a confirmed interval. Ambiguous or nonexistent midnight boundaries require
a different explicit period definition; this module never guesses a DST fold.

Completeness describes a person's source declaration, not a verified financial
conclusion. Successful parsing, nonempty files and complete row coverage cannot
upgrade a declaration. The only supported source roles are orders and payments.
"""
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import CoreError


PERIOD_POLICY_VERSION = "collection-period-1.0.0"
REQUIRED_ROLES = ("orders", "payments")
MAX_PERIOD_DAYS = 366
MAX_NAME_CHARS = 160
MAX_NOTE_CHARS = 1000
_TRIM = " \t\n\r"
_FIELDS = {"name", "start_date", "end_date", "timezone", "cutoff_at"}
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
_ZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z")
_STATUSES = {"complete", "incomplete", "not_attested"}


def _fail(code, message, **details):
    raise CoreError(code, message, details)


def _text(value, limit, allow_lines=False):
    if not isinstance(value, str) or len(value) > limit:
        return None
    if any((ord(char) < 32 and not (allow_lines and char in "\t\n\r"))
           or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value.strip(_TRIM)


def _day(value, field):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        _fail("INVALID_PERIOD_DATE", "周期日期须为 YYYY-MM-DD。", field=field)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CoreError("INVALID_PERIOD_DATE", "周期日期无效。", {"field": field}) from exc


def _zone(value):
    if (not isinstance(value, str) or not value or len(value) > 128 or not _ZONE.fullmatch(value)
            or value in ("localtime", "posixrules") or value.split("/")[0] in ("posix", "right")):
        _fail("INVALID_PERIOD_TIMEZONE", "周期时区须为明确的 IANA 时区名称。")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CoreError("INVALID_PERIOD_TIMEZONE", "周期时区不存在或时区数据库未安装。") from exc


def _midnight(day, zone, field):
    local = datetime.combine(day, time.min)
    instants = set()
    try:
        for fold in (0, 1):
            candidate = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if candidate.astimezone(zone).replace(tzinfo=None) == local:
                instants.add(candidate)
    except (ValueError, OverflowError) as exc:
        raise CoreError("INVALID_PERIOD_DATE", "周期边界超出可表示范围。", {"field": field}) from exc
    if len(instants) != 1:
        _fail("AMBIGUOUS_PERIOD_BOUNDARY", "周期午夜边界在指定时区不存在或对应多个时刻。", field=field)
    return next(iter(instants))


def _cutoff(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        _fail("INVALID_PERIOD_CUTOFF", "资料截止时间须为包含时区偏移的 ISO 时间。")
    # datetime.fromisoformat normalizes e.g. +08:60; the contract must not.
    if value[-1:] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        _fail("INVALID_PERIOD_CUTOFF", "资料截止时间的时区偏移无效。")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise CoreError("INVALID_PERIOD_CUTOFF", "资料截止时间无效或超出可表示范围。") from exc


def validate_period_definition(definition):
    """Normalize five input fields and freeze the corresponding UTC interval.

    Returns the five fields plus ``start_inclusive`` and ``end_exclusive`` UTC
    timestamps. ``cutoff_at`` is UTC as well. No system clock or current date is
    consulted: backfills and deliberately future periods have the same policy.
    """
    if not isinstance(definition, dict) or set(definition) != _FIELDS:
        _fail("INVALID_PERIOD_DEFINITION", "周期定义缺失必填项或包含不支持的属性。")
    name = _text(definition["name"], MAX_NAME_CHARS)
    if not name or not name.strip():
        _fail("INVALID_PERIOD_DEFINITION", "周期名称须为 1 至 160 个字符的有效文本。")
    start, end = _day(definition["start_date"], "start_date"), _day(definition["end_date"], "end_date")
    days = (end - start).days + 1
    if not 1 <= days <= MAX_PERIOD_DAYS:
        _fail("INVALID_PERIOD_RANGE", "周期须按日期顺序覆盖 1 至 366 个自然日。")
    zone = _zone(definition["timezone"])
    try:
        exclusive_day = end + timedelta(days=1)
    except OverflowError as exc:
        raise CoreError("INVALID_PERIOD_DATE", "周期结束日期超出可表示范围。", {"field": "end_date"}) from exc
    start_at = _midnight(start, zone, "start_date")
    end_at = _midnight(exclusive_day, zone, "end_date")
    if end_at <= start_at:
        _fail("INVALID_PERIOD_RANGE", "周期在指定时区须具有正的时间跨度。")
    cutoff = _cutoff(definition["cutoff_at"])
    if cutoff < end_at:
        _fail("PERIOD_CUTOFF_BEFORE_END", "资料截止时间须覆盖周期最后一天的全部时间。")
    return {"name": name, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "timezone": zone.key, "cutoff_at": cutoff.isoformat(),
            "start_inclusive": start_at.isoformat(), "end_exclusive": end_at.isoformat()}


def validate_attestations(attestations):
    """Normalize explicit declarations for both fixed collection source roles."""
    if not isinstance(attestations, dict) or set(attestations) != set(REQUIRED_ROLES):
        _fail("INVALID_SOURCE_ATTESTATION", "完整性声明须明确包含订单和支付两个来源角色。")
    result = {}
    for role in REQUIRED_ROLES:
        value = attestations[role]
        if not isinstance(value, dict) or set(value) != {"status", "note"}:
            _fail("INVALID_SOURCE_ATTESTATION", "来源声明须包含状态和说明。", role=role)
        status = value["status"]
        if not isinstance(status, str) or status not in _STATUSES:
            _fail("INVALID_SOURCE_ATTESTATION", "来源声明状态无效。", role=role)
        note = _text(value["note"], MAX_NOTE_CHARS, allow_lines=True)
        if note is None or (status != "not_attested" and not note.strip()):
            _fail("INVALID_SOURCE_ATTESTATION", "已声明完整或不完整的来源须填写说明，最多 1000 个字符。", role=role)
        result[role] = {"status": status, "note": note}
    return result


def assess_completeness(attestations):
    """Return an attestation assessment without inferring data completeness."""
    roles = validate_attestations(attestations)
    statuses = {value["status"] for value in roles.values()}
    status = ("incomplete" if "incomplete" in statuses else
              "attested_complete" if statuses == {"complete"} else "not_attested")
    return {"status": status, "roles": roles}
