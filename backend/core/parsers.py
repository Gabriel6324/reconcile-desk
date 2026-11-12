"""Parse accepted CSV/XLSX without converting numeric cells through float."""

import csv
import hashlib
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from defusedxml.ElementTree import iterparse
from defusedxml.common import DefusedXmlException
from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format

from .errors import CoreError

PARSER_VERSION = "strict-tables-1.0.1"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 50
MAX_CELL_CHARS = 4000
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
MAX_XML_NODES = 1100000
MAX_XML_DEPTH = 64
TRIM = "\t\n\r "
_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
_NATIVE_NUMERIC = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee]([+-]?[0-9]+))?\Z")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def validate_text(value, location=None):
    if not isinstance(value, str):
        raise CoreError("INVALID_TEXT", "单元格必须是文本。", location)
    if len(value) > MAX_CELL_CHARS:
        raise CoreError("CELL_TOO_LONG", "单元格内容超过 4000 个字符。", location)
    for ch in value:
        n = ord(ch)
        if not (n in (9, 10, 13) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF):
            raise CoreError("INVALID_CHARACTER", "内容包含无法安全保存的控制字符。", {**(location or {}), "codepoint": f"U+{n:04X}"})
    return value


def _cell(tag, raw):
    return {"tag": tag, "raw": raw}


def _headers(values):
    if not values or len(values) > MAX_COLUMNS:
        raise CoreError("INVALID_HEADER", "需要 1 至 50 个连续的非空表头。")
    seen = set()
    result = []
    for ordinal, value in enumerate(values, 1):
        validate_text(value, {"row": 1, "column": ordinal})
        name = value.strip(TRIM)
        if not name or name in seen:
            raise CoreError("INVALID_HEADER", "表头不能为空或重复。", {"column": ordinal, "name": value})
        seen.add(name)
        result.append({"id": f"c{ordinal}", "name": value, "normalized_name": name, "ordinal": ordinal})
    return result


def _result(data, filename, fmt, headers, rows, sheet=None, skipped=0):
    total = 0
    for h in headers:
        total += len(h["name"].encode("utf-8"))
    for row in rows:
        for cell in row["cells"].values():
            if cell["raw"] is not None:
                total += len(str(cell["raw"]).encode("utf-8"))
    if total > MAX_TEXT_BYTES:
        raise CoreError("PARSED_DATA_TOO_LARGE", "解析后的表格文本超过 16 MiB。")
    profiles = []
    for header in headers:
        counts = {}
        for row in rows:
            tag = row["cells"][header["id"]]["tag"]
            counts[tag] = counts.get(tag, 0) + 1
        profiles.append({"column_id": header["id"], "types": counts})
    return {
        "filename": filename,
        "format": fmt,
        "sha256": hashlib.sha256(data).hexdigest(),
        "parser_version": PARSER_VERSION,
        "parse_options": {"header_row": 1, "encoding": "UTF-8" if fmt == "csv" else "OOXML", "delimiter": "," if fmt == "csv" else None},
        "sheet": sheet,
        "headers": headers,
        "columns": headers,
        "rows": rows,
        "row_count": len(rows),
        "profiles": profiles,
        "skipped_blank_records": skipped,
    }


def _parse_csv(data, filename):
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CoreError("UNSUPPORTED_ENCODING", "CSV 需使用 UTF-8 编码。") from exc
    _validate_csv_quotes(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",", strict=True)
    headers = None
    rows = []
    record = 0
    skipped = 0
    total_bytes = 0
    try:
        while True:
            line_start = reader.line_num + 1
            try:
                fields = next(reader)
            except StopIteration:
                break
            record += 1
            if headers is None:
                if not fields:
                    raise CoreError("INVALID_HEADER", "第一条记录必须是表头。")
                headers = _headers(fields)
                continue
            if not fields:
                skipped += 1
                continue
            if len(fields) != len(headers):
                raise CoreError("RAGGED_CSV", "数据记录的列数与表头不一致。", {"record": record, "line_start": line_start, "expected": len(headers), "actual": len(fields)})
            if len(rows) >= MAX_ROWS:
                raise CoreError("TOO_MANY_ROWS", "每份表格最多导入 10000 行数据。")
            cells = {}
            for header, raw in zip(headers, fields):
                validate_text(raw, {"record": record, "column": header["ordinal"]})
                total_bytes += len(raw.encode("utf-8"))
                if total_bytes > MAX_TEXT_BYTES:
                    raise CoreError("PARSED_DATA_TOO_LARGE", "解析后的表格文本超过 16 MiB。")
                cells[header["id"]] = _cell("literal_text", raw)
            ordinal = len(rows) + 1
            rows.append({"id": f"r{ordinal}", "ordinal": ordinal, "source": {"row": record, "record": record, "sheet": None, "line_start": line_start, "line_end": reader.line_num}, "cells": cells})
    except csv.Error as exc:
        raise CoreError("INVALID_CSV", "CSV 格式错误或单元格过长。", {"line": reader.line_num}) from exc
    if headers is None:
        raise CoreError("INVALID_HEADER", "文件为空，未找到表头。")
    return _result(data, filename, "csv", headers, rows, skipped=skipped)


def _validate_csv_quotes(text):
    """csv.reader(strict=True) still accepts quotes inside unquoted fields."""
    state = "start"
    line = 1
    for ch in text:
        if state == "quoted":
            if ch == '"':
                state = "after_quote"
        elif state == "after_quote":
            if ch == '"':
                state = "quoted"
            elif ch in ",\r\n":
                state = "start"
            else:
                raise CoreError("INVALID_CSV", "闭合引号后只能出现分隔符或换行。", {"line": line})
        elif ch == '"':
            if state != "start":
                raise CoreError("INVALID_CSV", "未加引号的字段中不能包含引号。", {"line": line})
            state = "quoted"
        elif ch in ",\r\n":
            state = "start"
        else:
            state = "unquoted"
        if ch == "\n":
            line += 1
    if state == "quoted":
        raise CoreError("INVALID_CSV", "CSV 字段的引号未闭合。", {"line": line})


def _xml(data, part):
    try:
        depth = 0
        nodes = 0
        root = None
        for event, elem in iterparse(io.BytesIO(data), events=("start", "end"), forbid_dtd=True, forbid_entities=True, forbid_external=True):
            if event == "start":
                depth += 1
                nodes += 1
                if root is None:
                    root = elem
                if depth > MAX_XML_DEPTH or nodes > MAX_XML_NODES:
                    raise CoreError("XML_LIMIT", "工作簿 XML 结构超过限制。", {"part": part})
            else:
                depth -= 1
        if root is None:
            raise CoreError("INVALID_XLSX", "工作簿 XML 为空。", {"part": part})
        return root
    except DefusedXmlException as exc:
        raise CoreError("UNSAFE_XML", "工作簿不允许 XML 文档类型或实体声明。", {"part": part}) from exc
    except (ET.ParseError, UnicodeError) as exc:
        raise CoreError("INVALID_XLSX", "工作簿包含无效 XML。", {"part": part}) from exc


def _zip_parts(data):
    result = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise CoreError("XLSX_LIMIT", "工作簿内部文件数量超过限制。")
            if sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
                raise CoreError("XLSX_LIMIT", "工作簿解压大小超过 50 MiB。")
            total = 0
            for info in infos:
                name = info.filename
                if name in result or name.startswith("/") or "\\" in name or ".." in name.split("/"):
                    raise CoreError("INVALID_XLSX", "工作簿存在重复或无效部件路径。")
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise CoreError("UNSUPPORTED_XLSX", "不支持加密或特殊压缩工作簿。")
                chunks = []
                with archive.open(info) as stream:
                    while chunk := stream.read(65536):
                        total += len(chunk)
                        if total > MAX_ZIP_BYTES:
                            raise CoreError("XLSX_LIMIT", "工作簿实际解压大小超过 50 MiB。")
                        chunks.append(chunk)
                result[name] = b"".join(chunks)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise CoreError("INVALID_XLSX", "文件不是有效的 XLSX 工作簿。") from exc
    if "[Content_Types].xml" not in result or "xl/workbook.xml" not in result:
        raise CoreError("INVALID_XLSX", "文件缺少必要的 XLSX 部件。")
    if any("vbaproject" in name.lower() or name.startswith("xl/externalLinks/") for name in result):
        raise CoreError("UNSUPPORTED_XLSX", "不支持宏或外部工作簿链接。")
    return result


def _reference(ref):
    match = _REF.fullmatch(ref or "")
    if not match:
        raise CoreError("INVALID_XLSX", "单元格坐标无效。", {"cell": ref})
    col = 0
    for ch in match[1]:
        col = col * 26 + ord(ch) - 64
    row = int(match[2])
    if col > 16384 or row > 1048576:
        raise CoreError("INVALID_XLSX", "单元格坐标超出工作表边界。", {"cell": ref})
    return col, row


def _inline_text(elem):
    parts = []
    for child in elem:
        if _local(child.tag) == "t":
            parts.append(child.text or "")
        elif _local(child.tag) == "r":
            parts.extend(x.text or "" for x in child if _local(x.tag) == "t")
    # OOXML escapes controls and literal escape prefixes using UTF-16 units.
    # Decode exactly once: _x005F_x0000_ is the literal text _x0000_.
    value = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m[1], 16)), "".join(parts))
    try:
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError as exc:
        raise CoreError("INVALID_CHARACTER", "工作簿文本包含非法 Unicode 标量。") from exc


def _parse_xlsx(data, filename):
    parts = _zip_parts(data)
    xmls = {}
    for name, content in parts.items():
        if name.endswith((".xml", ".rels")):
            xmls[name] = _xml(content, name)
    for name, root in xmls.items():
        if name.endswith(".rels"):
            for rel in root:
                if "externallink" in rel.attrib.get("Type", "").lower():
                    raise CoreError("EXTERNAL_LINK", "不支持包含外部工作簿关系的文件。")
    types = xmls["[Content_Types].xml"]
    if any("macroenabled" in elem.attrib.get("ContentType", "").lower() for elem in types):
        raise CoreError("UNSUPPORTED_XLSX", "不支持包含宏的工作簿。")
    shared = []
    shared_bytes = 0
    shared_root = xmls.get("xl/sharedStrings.xml")
    if shared_root is not None:
        for elem in shared_root:
            value = _inline_text(elem)
            validate_text(value)
            shared_bytes += len(value.encode("utf-8"))
            if len(shared) >= 500000 or shared_bytes > MAX_TEXT_BYTES:
                raise CoreError("XLSX_LIMIT", "工作簿共享字符串超过限制。")
            shared.append(value)
    date_styles = set()
    style_count = 0
    style_root = xmls.get("xl/styles.xml")
    if style_root is not None:
        formats = dict(BUILTIN_FORMATS)
        for elem in style_root.iter():
            if _local(elem.tag) == "numFmt":
                formats[int(elem.attrib["numFmtId"])] = elem.attrib.get("formatCode", "")
        for elem in style_root:
            if _local(elem.tag) == "cellXfs":
                for i, xf in enumerate(elem):
                    style_count += 1
                    if is_date_format(formats.get(int(xf.attrib.get("numFmtId", "0")), "")):
                        date_styles.add(i)
    rels = xmls.get("xl/_rels/workbook.xml.rels")
    if rels is None:
        raise CoreError("INVALID_XLSX", "工作簿缺少工作表关系。")
    targets = {}
    for rel in rels:
        target = rel.attrib.get("Target", "")
        if rel.attrib.get("TargetMode") == "External":
            continue
        normalized = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if normalized.startswith("../"):
            raise CoreError("INVALID_XLSX", "工作表关系路径无效。")
        targets[rel.attrib.get("Id")] = normalized
    candidates = []
    for sheet in xmls["xl/workbook.xml"].iter():
        if _local(sheet.tag) != "sheet":
            continue
        relation = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
        path = targets.get(relation)
        root = xmls.get(path)
        if root is None:
            raise CoreError("INVALID_XLSX", "工作表关系目标不存在。")
        values = {}
        coordinates = set()
        for elem in root.iter():
            if _local(elem.tag) == "mergeCell":
                raise CoreError("MERGED_CELLS", "首版不支持合并单元格。", {"range": elem.attrib.get("ref")})
            if _local(elem.tag) != "c":
                continue
            ref = elem.attrib.get("r")
            col, row = _reference(ref)
            if (row, col) in coordinates:
                raise CoreError("INVALID_XLSX", "工作表存在重复单元格坐标。", {"cell": ref})
            coordinates.add((row, col))
            children = {_local(child.tag): child for child in elem}
            if len(children) != len(elem):
                raise CoreError("INVALID_XLSX", "单元格存在重复内容节点。", {"cell": ref})
            if "f" in children:
                raise CoreError("FORMULA_NOT_SUPPORTED", "请将公式转为值后导入。", {"sheet": sheet.attrib.get("name"), "cell": ref})
            v = children.get("v")
            inline = children.get("is")
            tag = elem.attrib.get("t", "n")
            if v is None and inline is None and tag == "n":
                # Formatting-only / missing cells do not expand the table.
                continue
            if tag not in ("inlineStr", "str", "n") and v is None:
                raise CoreError("INVALID_XLSX", "单元格类型已声明但缺少值。", {"cell": ref})
            if inline is not None and tag != "inlineStr" or v is not None and tag == "inlineStr":
                raise CoreError("INVALID_XLSX", "单元格内容与声明类型不一致。", {"cell": ref})
            try:
                style = int(elem.attrib.get("s", "0"))
            except ValueError as exc:
                raise CoreError("INVALID_XLSX", "单元格样式无效。", {"cell": ref}) from exc
            if style < 0 or ("s" in elem.attrib and style >= style_count):
                raise CoreError("INVALID_XLSX", "单元格引用不存在的样式。", {"cell": ref})
            raw = v.text if v is not None else None
            if tag == "inlineStr":
                cell = _cell("literal_text", _inline_text(inline) if inline is not None else "")
            elif tag == "s":
                try:
                    index = int(raw)
                    if index < 0:
                        raise ValueError()
                    cell = _cell("literal_text", shared[index])
                except (TypeError, ValueError, IndexError) as exc:
                    raise CoreError("INVALID_XLSX", "共享字符串索引无效。", {"cell": ref}) from exc
            elif tag == "str":
                cell = _cell("literal_text", raw or "")
            elif tag == "b":
                if raw not in ("0", "1"):
                    raise CoreError("INVALID_XLSX", "布尔单元格值无效。", {"cell": ref})
                cell = _cell("boolean", raw)
            elif tag == "d":
                cell = _cell("date_formatted_value", raw or "")
            elif tag == "e":
                raise CoreError("CELL_ERROR", "工作簿包含错误单元格。", {"cell": ref, "value": raw})
            elif tag == "n":
                if raw is None:
                    continue
                number = _NATIVE_NUMERIC.fullmatch(raw)
                if number is None or (number[1] is not None and (len(number[1].lstrip("+-")) > 3 or abs(int(number[1])) > 100)):
                    raise CoreError("INVALID_XLSX_NUMBER", "数值单元格词法或指数超出限制。", {"cell": ref})
                cell = _cell("date_formatted_value" if style in date_styles else "decimal_lexeme", raw)
            else:
                raise CoreError("UNSUPPORTED_CELL_TYPE", "工作簿包含不支持的单元格类型。", {"cell": ref, "type": tag})
            validate_text(cell["raw"], {"sheet": sheet.attrib.get("name"), "cell": ref})
            values[(row, col)] = cell
            if len(values) > (MAX_ROWS + 1) * MAX_COLUMNS:
                raise CoreError("TOO_MANY_CELLS", "工作簿单元格数量超过限制。")
        if values:
            if sheet.attrib.get("state", "visible") != "visible":
                raise CoreError("HIDDEN_WORKSHEET", "不支持包含非空隐藏工作表的文件。")
            candidates.append((sheet.attrib.get("name", "Sheet1"), values))
    if len(candidates) != 1:
        raise CoreError("WORKSHEET_COUNT", "请导入只有一个非空可见工作表的文件。")
    sheet_name, values = candidates[0]
    header_columns = sorted(col for row, col in values if row == 1)
    if not header_columns or header_columns != list(range(1, len(header_columns) + 1)):
        raise CoreError("INVALID_HEADER", "第一行需要连续的非空表头。")
    if any(values[(1, col)]["tag"] != "literal_text" for col in header_columns):
        raise CoreError("INVALID_HEADER", "表头必须是文本。")
    headers = _headers([values[(1, col)]["raw"] for col in header_columns])
    for row, col in values:
        if col > len(headers):
            raise CoreError("VALUE_OUTSIDE_TABLE", "数据位于表头定义的列范围之外。", {"row": row, "column": col})
    source_rows = sorted({row for row, _ in values if row > 1})
    if len(source_rows) > MAX_ROWS:
        raise CoreError("TOO_MANY_ROWS", "每份表格最多导入 10000 行数据。")
    rows = []
    for ordinal, row in enumerate(source_rows, 1):
        cells = {h["id"]: values.get((row, h["ordinal"]), _cell("null", None)) for h in headers}
        rows.append({"id": f"r{ordinal}", "ordinal": ordinal, "source": {"row": row, "sheet": sheet_name, "record": None, "line_start": None, "line_end": None}, "cells": cells})
    return _result(data, filename, "xlsx", headers, rows, sheet_name)


def parse_file(data: bytes, filename: str) -> dict:
    if not isinstance(data, bytes):
        raise CoreError("INVALID_FILE", "上传内容必须是字节数据。")
    if len(data) > MAX_FILE_BYTES:
        raise CoreError("FILE_TOO_LARGE", "单个文件不得超过 10 MiB。")
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise CoreError("INVALID_FILENAME", "文件名需为 1 至 255 个字符。")
    validate_text(filename)
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "csv":
        if data.startswith(b"PK\x03\x04"):
            raise CoreError("INVALID_CSV", "文件内容与 CSV 扩展名不符。")
        return _parse_csv(data, filename)
    if suffix == "xlsx":
        if not data.startswith(b"PK\x03\x04"):
            raise CoreError("INVALID_XLSX", "文件内容与 XLSX 扩展名不符。")
        try:
            return _parse_xlsx(data, filename)
        except CoreError:
            raise
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise CoreError("INVALID_XLSX", "工作簿结构或单元格内容无效。") from exc
    raise CoreError("UNSUPPORTED_FORMAT", "仅支持 CSV 和 XLSX 文件。")
