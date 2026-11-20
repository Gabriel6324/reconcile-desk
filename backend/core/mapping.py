"""Explicit, versioned mappings from immutable parsed tables to money records.

User mappings refer to exact original header names. ``validate_mapping`` resolves
them to immutable parser column ids; callers persist both the user's definition
and the compiled result against the same dataset snapshot. ``map_rows`` consumes
that compiled result and never infers a column, currency, sign, or missing amount.

Version 1 trims only ASCII space, tab, CR and LF at cell edges. It preserves case,
leading zeroes, internal whitespace, Unicode, signs and decimal lexemes. Native
XLSX date/boolean cells become invalid business values rather than numeric money
or identifiers. Original cells and physical locations remain in the source table;
the returned record id resolves directly to that dataset and parsed row.
"""
import re

from .business import MAX_KEY_PARTS, MAX_RECORDS
from .errors import CoreError
from .parsers import MAX_COLUMNS, MAX_CELL_CHARS, TRIM


MAPPING_VERSION = "collection-mapping-1.0.0"
_FIELDS = {"key_columns", "amount_column", "event_id_column", "currency", "source_namespace"}
_COLUMN_ID = re.compile(r"c[1-9][0-9]?\Z")
_DATASET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ROW_ID = re.compile(r"r[1-9][0-9]{0,9}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_TAGS = {"literal_text", "decimal_lexeme", "date_formatted_value", "boolean", "null"}


def _fail(code, message, **details):
    raise CoreError(code, message, details)


def _valid_text(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159
                        or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _definition(mapping, compiled=False):
    expected = _FIELDS | {"mapping_version"} if compiled else _FIELDS
    if not isinstance(mapping, dict) or set(mapping) != expected:
        _fail("INVALID_MAPPING", "字段映射缺失必填项或包含不支持的属性。")
    if compiled and mapping["mapping_version"] != MAPPING_VERSION:
        _fail("MAPPING_VERSION_UNSUPPORTED", "字段映射版本不受支持。")
    namespace = mapping["source_namespace"]
    if not _valid_text(namespace, 128) or namespace != namespace.strip() or not namespace.strip():
        _fail("INVALID_MAPPING", "来源命名空间须为 1 至 128 个字符的明确文本。")
    keys = mapping["key_columns"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEY_PARTS:
        _fail("INVALID_MAPPING", "业务键须明确选择 1 至 10 列。")
    currency = mapping["currency"]
    if not isinstance(currency, dict) or set(currency) not in ({"column"}, {"constant"}):
        _fail("INVALID_MAPPING", "币种须选择一列或明确指定一个固定币种。")
    if "constant" in currency and (not isinstance(currency["constant"], str)
                                    or not _CURRENCY.fullmatch(currency["constant"])):
        _fail("INVALID_MAPPING", "固定币种须为三个大写英文字母，含义须由规则确认。")
    references = keys + [mapping["amount_column"], mapping["event_id_column"]]
    if "column" in currency:
        references.append(currency["column"])
    if any(not isinstance(value, str) or not value or len(value) > MAX_CELL_CHARS for value in references):
        _fail("INVALID_MAPPING", "字段引用必须为非空列名。")
    if compiled and any(not _COLUMN_ID.fullmatch(value) or int(value[1:]) > MAX_COLUMNS for value in references):
        _fail("INVALID_MAPPING", "已编译映射必须引用有效列标识。")
    if len(set(keys)) != len(keys):
        _fail("MAPPING_COLUMN_CONFLICT", "业务键不能重复选择同一列。")
    # An event id can also be an order/business key. Money and currency have
    # distinct semantics and cannot silently double as another mapped field.
    amount, event = mapping["amount_column"], mapping["event_id_column"]
    if amount in keys or amount == event or ("column" in currency and currency["column"] in keys + [amount, event]):
        _fail("MAPPING_COLUMN_CONFLICT", "金额和币种列不能同时用作其他字段。")


def validate_mapping(mapping, columns):
    """Return a new compiled definition using parser column ids.

    ``columns`` is the immutable parser's ``headers`` (also named ``columns``)
    list, with ``id`` and exact original ``name`` for each header. Names are not
    fuzzy matched or trimmed here: the user confirms precisely the shown header.
    """
    _definition(mapping)
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        _fail("INVALID_MAPPING_HEADERS", "资料表头无效。")
    names, identifiers = {}, set()
    for column in columns:
        if not isinstance(column, dict):
            _fail("INVALID_MAPPING_HEADERS", "资料表头无效。")
        name, identifier = column.get("name"), column.get("id")
        if (not isinstance(name, str) or not name.strip(TRIM) or len(name) > MAX_CELL_CHARS
                or not isinstance(identifier, str) or not _COLUMN_ID.fullmatch(identifier)
                or int(identifier[1:]) > MAX_COLUMNS or name in names or identifier in identifiers):
            _fail("INVALID_MAPPING_HEADERS", "资料表头含无效或重复的列名、列标识。")
        names[name] = identifier
        identifiers.add(identifier)

    def resolve(name):
        if name not in names:
            _fail("MAPPING_COLUMN_NOT_FOUND", "所选列不存在，列名须完全匹配。", column=name)
        return names[name]

    currency = mapping["currency"]
    result = {
        "mapping_version": MAPPING_VERSION,
        "key_columns": [resolve(name) for name in mapping["key_columns"]],
        "amount_column": resolve(mapping["amount_column"]),
        "event_id_column": resolve(mapping["event_id_column"]),
        "currency": {"column": resolve(currency["column"])} if "column" in currency else dict(currency),
        "source_namespace": mapping["source_namespace"],
    }
    _definition(result, compiled=True)
    return result


def _value(cells, column, row_id):
    # Absent mapped cells are structural corruption. A real blank XLSX cell is
    # present as {tag: 'null', raw: None}; CSV blanks are literal empty strings.
    cell = cells.get(column)
    if (not isinstance(cell, dict) or set(cell) != {"tag", "raw"}
            or not isinstance(cell["tag"], str) or cell["tag"] not in _TAGS):
        _fail("INVALID_MAPPING_ROWS", "规范资料单元格结构无效。", row_id=row_id, column=column)
    raw = cell["raw"]
    if cell["tag"] == "null":
        if raw is not None:
            _fail("INVALID_MAPPING_ROWS", "空单元格包含非空值。", row_id=row_id, column=column)
        return None
    # Do not stringify floats, booleans, ints, containers or Decimal objects.
    # The parser contract stores even native numeric values as exact strings.
    if not isinstance(raw, str) or len(raw) > MAX_CELL_CHARS:
        _fail("INVALID_MAPPING_ROWS", "单元格须保留原始文本，不能转换浮点数。", row_id=row_id, column=column)
    if cell["tag"] in ("date_formatted_value", "boolean"):
        return None
    return raw.strip(TRIM)


def map_rows(dataset_id, rows, mapping):
    """Map parser row dictionaries to the business engine's six-field records.

    Invalid/blank cell *values* survive as empty strings or None for explicit
    business exceptions and complete row coverage. Invalid structural input
    fails the whole operation; it can never produce a partially mapped result.
    """
    _definition(mapping, compiled=True)
    if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
        _fail("INVALID_MAPPING_ROWS", "资料标识无效。")
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        _fail("INVALID_MAPPING_ROWS", "资料记录列表无效或超过数量限制。")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("INVALID_MAPPING_ROWS", "资料记录结构无效。")
        row_id, cells = row.get("id"), row.get("cells")
        if (not isinstance(row_id, str) or not _ROW_ID.fullmatch(row_id)
                or not isinstance(cells, dict)):
            _fail("INVALID_MAPPING_ROWS", "资料行标识或单元格结构无效。")
        if row_id in seen:
            _fail("DUPLICATE_RECORD_ID", "资料行标识重复，无法建立唯一来源覆盖。", row_id=row_id)
        seen.add(row_id)
        currency = mapping["currency"]
        result.append({
            "id": f"{dataset_id}:{row_id}",
            "key": [_value(cells, column, row_id) for column in mapping["key_columns"]],
            "amount": _value(cells, mapping["amount_column"], row_id),
            "event_id": _value(cells, mapping["event_id_column"], row_id),
            "currency": _value(cells, currency["column"], row_id) if "column" in currency else currency["constant"],
            "source_namespace": mapping["source_namespace"],
        })
    return result
