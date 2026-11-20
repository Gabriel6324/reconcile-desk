"""Explicit worksheet selection layered over the frozen strict file parser.

The entire original workbook is checked before selecting a worksheet. Selection
must not conceal formulas, malformed XML, external workbook links, hidden data,
merged cells, duplicate coordinates or ZIP expansion attacks in another sheet.
The projected workbook exists only in memory; provenance always names the
original file, original worksheet and original physical cell/row coordinates.
"""
import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET

from .errors import CoreError
from . import parsers


PARSER_VERSION = 'explicit-dataset-tables-1.0.0'
MAX_WORKSHEETS = 20


def _selection(value):
    if value is not None:
        if not isinstance(value, str) or not value or len(value) > 31:
            raise CoreError('INVALID_WORKSHEET', '工作表名称必须为 1 至 31 个字符。')
        parsers.validate_text(value)
    return value


def _inspect_checked(data, filename):
    """Reuse every existing security check; only multiple sheets become supported."""
    try:
        original_result = parsers.parse_file(data, filename)
    except CoreError as exc:
        if exc.code != 'WORKSHEET_COUNT':
            raise
        original_result = None
    if filename.rsplit('.', 1)[-1].lower() != 'xlsx':
        return original_result, None

    parts = parsers._zip_parts(data)
    book = parsers._xml(parts['xl/workbook.xml'], 'xl/workbook.xml')
    containers = [element for element in book if parsers._local(element.tag) == 'sheets']
    if len(containers) != 1:
        raise CoreError('INVALID_XLSX', '工作簿必须包含一个工作表目录。')
    container = containers[0]
    sheets = [element for element in container if parsers._local(element.tag) == 'sheet']
    if not sheets or len(sheets) > MAX_WORKSHEETS:
        raise CoreError('WORKSHEET_LIMIT', '工作簿须包含 1 至 20 个工作表。')
    # The strict parser uses local tag names; reject extra sheet declarations
    # outside the real directory rather than hiding them during projection.
    if len(sheets) != sum(parsers._local(element.tag) == 'sheet' for element in book.iter()):
        raise CoreError('INVALID_XLSX', '工作表目录结构无效。')
    seen_names, seen_relations = set(), set()
    description = []
    for sheet in sheets:
        name = sheet.attrib.get('name')
        _selection(name)
        if name is None or name.casefold() in seen_names:
            raise CoreError('INVALID_XLSX', '工作表名称缺失或重复。')
        relation = next((value for key, value in sheet.attrib.items() if parsers._local(key) == 'id'), None)
        if not relation or relation in seen_relations:
            raise CoreError('INVALID_XLSX', '工作表关系缺失或重复。')
        state = sheet.attrib.get('state', 'visible')
        if state not in ('visible', 'hidden', 'veryHidden'):
            raise CoreError('INVALID_XLSX', '工作表可见性状态无效。')
        seen_names.add(name.casefold())
        seen_relations.add(relation)
        description.append({'name': name, 'state': state, 'selectable': state == 'visible'})

    return original_result, (parts, book, container, sheets, description)


def inspect_workbook(data: bytes, filename: str) -> dict:
    """Return validated metadata; this is bounded parsing, not a cheap ZIP peek."""
    result, workbook = _inspect_checked(data, filename)
    return {
        'filename': filename,
        'format': 'xlsx' if workbook else 'csv',
        'sha256': hashlib.sha256(data).hexdigest(),
        'worksheets': workbook[4] if workbook else [],
        'parser_version': PARSER_VERSION,
    }


def _project(parts, book, container, sheets, selected):
    for sheet in sheets:
        if sheet.attrib['name'] != selected:
            container.remove(sheet)
    projected = io.BytesIO()
    # Preserve every original part. The strict parser therefore checks all XML
    # again, and styles/shared-string references retain their original indices.
    with zipfile.ZipFile(projected, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, ET.tostring(book, encoding='utf-8', xml_declaration=True)
                             if name == 'xl/workbook.xml' else content)
    return projected.getvalue()


def parse_dataset(data: bytes, filename: str, worksheet: str | None = None) -> dict:
    """Parse a CSV or one explicitly selected visible XLSX sheet.

    Selecting a worksheet changes neither bytes kept by the caller nor source
    hashes. A multi-visible-sheet workbook always requires a selection, even
    when only one sheet happens to contain values. Empty selected sheets fail.
    """
    worksheet = _selection(worksheet)
    result, workbook = _inspect_checked(data, filename)
    if workbook is None:
        if worksheet is not None:
            raise CoreError('WORKSHEET_NOT_APPLICABLE', 'CSV 文件不能指定工作表。')
        selected = None
        mode = 'not_applicable'
    else:
        parts, book, container, sheets, description = workbook
        visible = [sheet['name'] for sheet in description if sheet['selectable']]
        if worksheet is None:
            if len(visible) != 1:
                raise CoreError('WORKSHEET_REQUIRED', '请选择要导入的可见工作表。', {'worksheets': description})
            selected, mode = visible[0], 'single_visible'
        else:
            if worksheet not in [sheet['name'] for sheet in description]:
                raise CoreError('WORKSHEET_NOT_FOUND', '指定的工作表不存在，名称必须完全匹配。', {'worksheets': description})
            if worksheet not in visible:
                raise CoreError('HIDDEN_WORKSHEET', '不能选择隐藏工作表。')
            selected, mode = worksheet, 'explicit'
        if result is None or result['sheet'] != selected:
            projected = _project(parts, book, container, sheets, selected)
            try:
                # Original bytes already passed parse_file's 10 MiB bound.
                # Internal XML serialization must not redefine the upload size.
                result = parsers._parse_xlsx(projected, filename)
            except CoreError as exc:
                if exc.code == 'WORKSHEET_COUNT':
                    raise CoreError('EMPTY_WORKSHEET', '选定工作表没有可导入的数据或表头。', {'worksheet': selected}) from exc
                raise
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                raise CoreError('INVALID_XLSX', '工作簿结构或单元格内容无效。') from exc
        result['sha256'] = hashlib.sha256(data).hexdigest()
    result['parser_version'] = PARSER_VERSION
    result['strict_parser_version'] = parsers.PARSER_VERSION
    result['parse_options'] = dict(result['parse_options'], worksheet=selected, worksheet_selection=mode)
    return result
