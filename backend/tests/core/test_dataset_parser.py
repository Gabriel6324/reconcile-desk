import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET

import openpyxl
import pytest

from core.dataset_parser import parse_dataset, inspect_workbook, PARSER_VERSION
from core.errors import CoreError
from core import parsers


def workbook(sheets, states=None):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
        if states and name in states:
            sheet.sheet_state = states[name]
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def changed(data, additions=None, replace=None):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts.update(additions or {})
    if replace:
        parts = replace(parts)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


@pytest.fixture
def multiple():
    return workbook({'订单': [['id', 'amount'], ['A', '9007199254740993.01']],
                     'Payments': [['reference', 'paid'], ['B', '12.30']]})


def failure(data, filename, worksheet, code):
    with pytest.raises(CoreError) as exc:
        parse_dataset(data, filename, worksheet)
    assert exc.value.code == code
    return exc.value


def test_multisheet_requires_exact_explicit_selection(multiple):
    exc = failure(multiple, 'source.xlsx', None, 'WORKSHEET_REQUIRED')
    assert [s['name'] for s in exc.details['worksheets']] == ['订单', 'Payments']
    failure(multiple, 'source.xlsx', 'payments', 'WORKSHEET_NOT_FOUND')
    result = parse_dataset(multiple, 'source.xlsx', 'Payments')
    assert result['rows'][0]['cells']['c1']['raw'] == 'B'
    assert result['rows'][0]['cells']['c2']['raw'] == '12.30'
    assert result['headers'][0]['name'] == 'reference'
    assert result['sheet'] == 'Payments'
    assert result['rows'][0]['source']['sheet'] == 'Payments'
    assert result['rows'][0]['source']['row'] == 2
    assert result['sha256'] == hashlib.sha256(multiple).hexdigest()
    assert result['filename'] == 'source.xlsx'
    assert result['parser_version'] == PARSER_VERSION
    assert result['strict_parser_version'] == parsers.PARSER_VERSION
    assert result['parse_options']['worksheet_selection'] == 'explicit'
    assert parse_dataset(multiple, 'source.xlsx', '订单')['rows'][0]['cells']['c2']['raw'] == '9007199254740993.01'


def test_single_visible_sheet_auto_selection_and_csv():
    data = workbook({'Only': [['id'], ['001']], 'EmptyHidden': []}, {'EmptyHidden': 'hidden'})
    parsed = parse_dataset(data, 'book.xlsx')
    assert parsed['sheet'] == 'Only'
    assert parsed['parse_options']['worksheet_selection'] == 'single_visible'
    failure(data, 'book.xlsx', 'EmptyHidden', 'HIDDEN_WORKSHEET')
    csv = b'id,note\n001,"two\nlines"\n'
    result = parse_dataset(csv, 'source.csv')
    assert result['rows'][0]['source']['line_end'] == 3
    assert result['parse_options']['worksheet_selection'] == 'not_applicable'
    assert inspect_workbook(csv, 'source.csv')['worksheets'] == []
    failure(csv, 'source.csv', 'Only', 'WORKSHEET_NOT_APPLICABLE')


def test_empty_sheet_is_not_silently_ignored():
    data = workbook({'Full': [['id'], ['A']], 'Empty': []})
    failure(data, 'source.xlsx', None, 'WORKSHEET_REQUIRED')
    failure(data, 'source.xlsx', 'Empty', 'EMPTY_WORKSHEET')
    assert parse_dataset(data, 'source.xlsx', 'Full')['row_count'] == 1


def test_metadata_preserves_original_hash_and_names(multiple):
    metadata = inspect_workbook(multiple, '资料.xlsx')
    assert metadata['sha256'] == hashlib.sha256(multiple).hexdigest()
    assert metadata['filename'] == '资料.xlsx'
    assert metadata['worksheets'] == [{'name': '订单', 'state': 'visible', 'selectable': True},
                                      {'name': 'Payments', 'state': 'visible', 'selectable': True}]


@pytest.mark.parametrize('worksheet', [True, 2, '', [], {}, 'x' * 32])
def test_invalid_selection_type_rejected(multiple, worksheet):
    failure(multiple, 'source.xlsx', worksheet, 'INVALID_WORKSHEET')


def test_formula_in_unselected_sheet_still_rejected():
    data = workbook({'Safe': [['id'], ['A']], 'Unsafe': [['id'], ['=1+1']]})
    failure(data, 'source.xlsx', 'Safe', 'FORMULA_NOT_SUPPORTED')
    with pytest.raises(CoreError) as exc:
        inspect_workbook(data, 'source.xlsx')
    assert exc.value.code == 'FORMULA_NOT_SUPPORTED'


def test_hidden_data_in_unselected_sheet_rejected():
    data = workbook({'Safe': [['id'], ['A']], 'Hidden': [['id'], ['B']]}, {'Hidden': 'hidden'})
    failure(data, 'source.xlsx', 'Safe', 'HIDDEN_WORKSHEET')


def test_unselected_merged_cells_rejected():
    book = openpyxl.Workbook()
    book.active.title = 'Safe'
    book.active.append(['id'])
    second = book.create_sheet('Merged')
    second['A1'] = 'id'
    second.merge_cells('A1:B1')
    output = io.BytesIO()
    book.save(output)
    failure(output.getvalue(), 'source.xlsx', 'Safe', 'MERGED_CELLS')


@pytest.mark.parametrize('addition,code', [
    ({'xl/externalLinks/externalLink1.xml': b'<root/>'}, 'UNSUPPORTED_XLSX'),
    ({'xl/vbaProject.bin': b'macro'}, 'UNSUPPORTED_XLSX'),
    ({'evil.xml': b'<!DOCTYPE x [<!ENTITY a "injected">]><x>&a;</x>'}, 'UNSAFE_XML'),
    ({'deep.xml': ('<x>' * 65 + '</x>' * 65).encode()}, 'XML_LIMIT'),
])
def test_unselected_parts_security_checks_not_bypassed(multiple, addition, code):
    failure(changed(multiple, addition), 'source.xlsx', 'Payments', code)


def test_external_relationship_type_rejected(multiple):
    relation = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="external" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" Target="elsewhere.xlsx" TargetMode="External"/></Relationships>'
    failure(changed(multiple, {'xl/worksheets/_rels/sheet1.xml.rels': relation}), 'source.xlsx', 'Payments', 'EXTERNAL_LINK')


def test_original_sparse_row_and_exact_numeric_lexeme(multiple):
    def replace(parts):
        name = 'xl/worksheets/sheet2.xml'
        root = ET.fromstring(parts[name])
        rows = [e for e in root.iter() if e.tag.endswith('}row')]
        rows[1].set('r', '9000')
        for c in rows[1]:
            c.set('r', c.attrib['r'][0] + '9000')
        amount = rows[1][1]
        amount.set('t', 'n')
        amount.remove(amount[0])
        ET.SubElement(amount, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v').text = '9007199254740993.01'
        parts[name] = ET.tostring(root)
        return parts
    raw = changed(multiple, replace=replace)
    parsed = parse_dataset(raw, 'original.xlsx', 'Payments')
    assert parsed['rows'][0]['source']['row'] == 9000
    assert parsed['rows'][0]['cells']['c2'] == {'tag': 'decimal_lexeme', 'raw': '9007199254740993.01'}
    assert parsed['sha256'] == hashlib.sha256(raw).hexdigest()


def test_selected_row_column_and_file_limits():
    data = workbook({'Safe': [['id']], 'Many': [['id']] + [[str(i)] for i in range(10001)]})
    failure(data, 'source.xlsx', 'Many', 'TOO_MANY_ROWS')
    data = workbook({'Safe': [['id']], 'Wide': [[f'c{i}' for i in range(51)]]})
    failure(data, 'source.xlsx', 'Wide', 'INVALID_HEADER')
    failure(b'x' * (parsers.MAX_FILE_BYTES + 1), 'source.xlsx', None, 'FILE_TOO_LARGE')


def test_too_many_sheets_rejected():
    data = workbook({f'S{i}': [['id']] for i in range(21)})
    failure(data, 'source.xlsx', 'S0', 'WORKSHEET_LIMIT')


def test_invalid_filename_and_format_rejected():
    for data, filename, code in [(b'id\nA\n', 'book.xls', 'UNSUPPORTED_FORMAT'),
                                 (b'not a zip', 'book.xlsx', 'INVALID_XLSX'),
                                 (b'id\nA\n', '', 'INVALID_FILENAME')]:
        failure(data, filename, None, code)


def test_duplicate_sheet_names_or_relationships_not_selectable(multiple):
    for attribute in ['name', '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']:
        def replace(parts):
            root = ET.fromstring(parts['xl/workbook.xml'])
            sheets = [e for e in root.iter() if e.tag.endswith('}sheet')]
            sheets[1].set(attribute, sheets[0].attrib[attribute])
            parts['xl/workbook.xml'] = ET.tostring(root)
            return parts
        failure(changed(multiple, replace=replace), 'source.xlsx', '订单', 'INVALID_XLSX')


def test_single_sheet_semantics_match_frozen_parser():
    raw = workbook({'Data': [['id', 'amount', 'missing'], ['001', '1.00', None]]})
    old = parsers.parse_file(raw, 'single.xlsx')
    new = parse_dataset(raw, 'single.xlsx')
    for key in ['filename', 'format', 'sha256', 'sheet', 'headers', 'columns', 'rows', 'row_count', 'profiles', 'skipped_blank_records']:
        assert new[key] == old[key]


def test_worksheet_name_is_not_trimmed_or_case_folded():
    raw = workbook({' Data ': [['id'], ['A']], 'Data': [['id'], ['B']]})
    assert parse_dataset(raw, 'book.xlsx', ' Data ')['rows'][0]['cells']['c1']['raw'] == 'A'
    assert parse_dataset(raw, 'book.xlsx', 'Data')['rows'][0]['cells']['c1']['raw'] == 'B'


def test_zip_entry_and_expanded_byte_limits_apply_to_whole_file(multiple, monkeypatch):
    oversized_entry_count = changed(multiple, {f'parts/{i}.bin': b'x' for i in range(parsers.MAX_ZIP_ENTRIES)})
    failure(oversized_entry_count, 'source.xlsx', 'Payments', 'XLSX_LIMIT')
    with zipfile.ZipFile(io.BytesIO(multiple)) as archive:
        old_expanded = sum(info.file_size for info in archive.infolist())
    monkeypatch.setattr(parsers, 'MAX_ZIP_BYTES', old_expanded + 100)
    expanded = changed(multiple, {'unselected.bin': b'0' * 1024})
    failure(expanded, 'source.xlsx', 'Payments', 'XLSX_LIMIT')


def test_duplicate_zip_part_and_parent_path_rejected(multiple):
    parts = changed(multiple, {'../evil.xml': b'<x/>'})
    failure(parts, 'source.xlsx', 'Payments', 'INVALID_XLSX')
    stream = io.BytesIO(multiple)
    with pytest.warns(UserWarning, match='Duplicate name'):
        with zipfile.ZipFile(stream, 'a', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('xl/workbook.xml', b'<workbook/>')
    failure(stream.getvalue(), 'source.xlsx', 'Payments', 'INVALID_XLSX')
