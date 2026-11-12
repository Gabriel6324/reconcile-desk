import copy
import csv
import io
import random
import zipfile
from collections import Counter
from decimal import getcontext
from xml.sax.saxutils import escape

import openpyxl
import pytest

from core import CoreError, build_export, parse_file, reconcile, validate_mapping


MAPPING = {"key": {"left_column_id": "c1", "right_column_id": "c1", "normalization": "exact"}, "comparisons": [{"left_column_id": "c2", "right_column_id": "c2", "type": "decimal", "normalization": "exact"}]}


def table(records, headers=("id", "amount")):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(records)
    return parse_file(stream.getvalue().encode("utf-8"), "sample.csv")


def xlsx_xml(sheet, extra=None, sheet_attrs="", workbook_extra=""):
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        "xl/workbook.xml": f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1" {sheet_attrs}/>{workbook_extra}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{sheet}</sheetData></worksheet>',
        **(extra or {}),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return out.getvalue()


HEADER = '<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c><c r="B1" t="inlineStr"><is><t>amount</t></is></c></row>'


def native(value, kind="n", style=""):
    cell = f'<c r="B2" t="{kind}" {style}><v>{escape(value)}</v></c>'
    return parse_file(xlsx_xml(HEADER + '<row r="2"><c r="A2" t="inlineStr"><is><t>A</t></is></c>' + cell + '</row>'), "native.xlsx")


def test_csv_raw_lexemes_bom_and_multiline_provenance():
    parsed = parse_file('\ufeffid,amount,note\r\n001,1.00,"first\r\nsecond"\r\n\r\n002,,\r\n'.encode(), "记录.csv")
    assert parsed["rows"][0]["cells"]["c1"]["raw"] == "001"
    assert parsed["rows"][0]["source"] == {"row": 2, "record": 2, "sheet": None, "line_start": 2, "line_end": 3}
    assert parsed["rows"][1]["source"]["line_start"] == 5
    assert parsed["rows"][1]["cells"]["c2"] == {"tag": "literal_text", "raw": ""}
    assert parsed["skipped_blank_records"] == 1


@pytest.mark.parametrize("data,code", [(b"id,id\na,b\n", "INVALID_HEADER"), (b"id, id \na,b\n", "INVALID_HEADER"), (b"id,\na,b\n", "INVALID_HEADER"), (b"id,n\na\n", "RAGGED_CSV"), (b'id,n\na,"unclosed', "INVALID_CSV"), (b"id,n\na,\x00\n", "INVALID_CHARACTER"), (b"id,n\na,\xff\n", "UNSUPPORTED_ENCODING"), (b"PK\x03\x04foo", "INVALID_CSV"), (b"", "INVALID_HEADER")])
def test_csv_rejections(data, code):
    with pytest.raises(CoreError) as err:
        parse_file(data, "input.csv")
    assert err.value.code == code


def test_file_cell_row_and_column_limits():
    for data, expected in [(b"x" * (10 * 1024 * 1024 + 1), "FILE_TOO_LARGE"), (b"id,n\na," + b"x" * 4001, "CELL_TOO_LONG"), ((",".join(f"c{i}" for i in range(51)) + "\n").encode(), "INVALID_HEADER"), (b"id,n\n" + b"a,1\n" * 10001, "TOO_MANY_ROWS")]:
        with pytest.raises(CoreError) as err:
            parse_file(data, "bounded.csv")
        assert err.value.code == expected


def test_xlsx_precision_is_original_xml_not_float():
    raw = "12345678901234567890123456789012345678"
    parsed = native(raw)
    assert parsed["rows"][0]["cells"]["c2"] == {"tag": "decimal_lexeme", "raw": raw}
    assert reconcile(table([["A", raw]]), parsed, MAPPING)["summary"]["decision"] == "all_equal"


def test_xlsx_missing_empty_and_sparse_rows():
    xml = HEADER + '<row r="2"><c r="A2" t="inlineStr"><is><t>A</t></is></c></row><row r="70000"><c r="A70000" t="inlineStr"><is><t>B</t></is></c><c r="B70000" t="inlineStr"/></row><row r="1048576"><c r="Z1048576" s="1"/></row>'
    parsed = parse_file(xlsx_xml(xml), "sparse.xlsx")
    assert len(parsed["rows"]) == 2
    assert parsed["rows"][0]["cells"]["c2"] == {"tag": "null", "raw": None}
    assert parsed["rows"][1]["cells"]["c2"] == {"tag": "literal_text", "raw": ""}
    assert parsed["rows"][1]["source"]["row"] == 70000


@pytest.mark.parametrize("cell,code", [('<c r="Z2"><v>9</v></c>', "VALUE_OUTSIDE_TABLE"), ('<c r="Z2"><f>1+1</f><v>2</v></c>', "FORMULA_NOT_SUPPORTED"), ('<c r="A2"><v>1E101</v></c>', "INVALID_XLSX_NUMBER"), ('<c r="A2" t="e"><v>#REF!</v></c>', "CELL_ERROR"), ('<c r="XFE2"><v>1</v></c>', "INVALID_XLSX"), ('<c r="A1048577"><v>1</v></c>', "INVALID_XLSX"), ('<c r="A2" t="s"><v>-1</v></c>', "INVALID_XLSX")])
def test_xlsx_rejects_unsafe_or_outside_values(cell, code):
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER + '<row r="2">' + cell + '</row>'), "bad.xlsx")
    assert err.value.code == code


def test_xlsx_rejects_macro_external_links_hidden_and_merges():
    cases = [dict(extra={"xl/vbaProject.bin": b"fake"}), dict(extra={"xl/externalLinks/externalLink1.xml": "<root/>"}), dict(sheet_attrs='state="hidden"')]
    for kwargs in cases:
        with pytest.raises(CoreError):
            parse_file(xlsx_xml(HEADER, **kwargs), "bad.xlsx")
    with pytest.raises(CoreError, match="合并"):
        parse_file(xlsx_xml(HEADER + '<mergeCell ref="A1:B1"/>'), "merged.xlsx")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le"])
def test_xlsx_xml_entities_rejected_across_encodings(encoding):
    attack = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE foo [<!ENTITY x "injected">]><foo>&x;</foo>'
    if encoding == "utf-8":
        attack = attack.replace("UTF-16", "UTF-8")
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER, extra={"evil.xml": attack.encode(encoding)}), "unsafe.xlsx")
    assert err.value.code == "UNSAFE_XML"


def test_xml_depth_and_zip_entry_limit():
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER, extra={"deep.xml": "<a>" * 65 + "</a>" * 65}), "deep.xlsx")
    assert err.value.code == "XML_LIMIT"
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER, extra={f"parts/{i}": "x" for i in range(200)}), "many.xlsx")
    assert err.value.code == "XLSX_LIMIT"


def test_ooxml_escaped_controls_and_literals():
    def parse_text(text):
        return parse_file(xlsx_xml(HEADER + f'<row r="2"><c r="A2" t="inlineStr"><is><t>{text}</t></is></c></row>'), "text.xlsx")
    assert parse_text("_x005F_x0000_")["rows"][0]["cells"]["c1"]["raw"] == "_x0000_"
    assert parse_text("_xD83D__xDE80_")["rows"][0]["cells"]["c1"]["raw"] == "🚀"
    with pytest.raises(CoreError) as err:
        parse_text("_x0000_")
    assert err.value.code == "INVALID_CHARACTER"


@pytest.mark.parametrize("value", ["1e3", "NaN", "Infinity", "1,000", "$1", "", " ", ".1", "1.", "1_000", "1.0000000000000", "9" * 39])
def test_invalid_decimal_is_unresolved_not_zero(value):
    result = reconcile(table([["A", value]]), table([["A", "0"]]), MAPPING)
    assert result["groups"][0]["status"] == "invalid_value"
    assert result["summary"]["decision"] == "needs_review"
    assert result["groups"][0]["comparisons"][0]["difference"] is None


@pytest.mark.parametrize("left,right,difference", [("1.0", "1.00", "0"), ("-0.000", "+0", "0"), ("9" * 38, "0.000000000001", "-" + "9" * 37 + "8.999999999999"), ("-99999999999999999999999999999999999999", "1", "100000000000000000000000000000000000000"), ("12345678901234567890123456789012345678", "12345678901234567890123456789012345679", "1")])
def test_exact_decimal_arithmetic_independent_of_global_context(left, right, difference):
    before = getcontext().prec
    getcontext().prec = 2
    try:
        result = reconcile(table([["A", left]]), table([["A", right]]), MAPPING)
    finally:
        getcontext().prec = before
    assert result["groups"][0]["comparisons"][0]["difference"] == difference
    assert result["groups"][0]["status"] == ("equal" if difference == "0" else "changed")


def test_native_exponent_and_type_matrix():
    assert reconcile(table([["A", "0.000000000001"]]), native("1E-12"), MAPPING)["summary"]["decision"] == "all_equal"
    for parsed in (native("1E-13"), native("1", "b"), native("2026-11-04", "d")):
        assert reconcile(table([["A", "1"]]), parsed, MAPPING)["groups"][0]["status"] == "invalid_value"
    xml = HEADER + '<row r="2"><c r="A2"><v>001</v></c><c r="B2"><v>1</v></c></row>'
    with pytest.raises(CoreError, match="主键列"):
        validate_mapping(parse_file(xlsx_xml(xml), "key.xlsx"), table([["001", "1"]]), MAPPING)


def test_date_style_cannot_silently_become_decimal():
    styles = '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    data = xlsx_xml(HEADER + '<row r="2"><c r="A2" t="inlineStr"><is><t>A</t></is></c><c r="B2" s="1"><v>45000</v></c></row>', extra={"xl/styles.xml": styles})
    parsed = parse_file(data, "dates.xlsx")
    assert parsed["rows"][0]["cells"]["c2"]["tag"] == "date_formatted_value"
    assert reconcile(table([["A", "45000"]]), parsed, MAPPING)["summary"]["decision"] == "needs_review"


def test_quarantine_coverage_status_precedence_and_known_differences():
    left = table([["A", "1", "x"], ["D", "3", "a"], ["D", "4", "b"], ["", "1", "x"], ["BAD", "oops", "old"], ["L", "bad", "x"]], ("id", "amount", "note"))
    right = table([["A", "1.00", "x"], ["D", "3", "c"], [" ", "2", "x"], ["BAD", "1", "new"], ["R", "1", "x"]], ("id", "amount", "note"))
    mapping = copy.deepcopy(MAPPING)
    mapping["comparisons"].append({"left_column_id": "c3", "right_column_id": "c3", "type": "text"})
    result = reconcile(left, right, mapping)
    assert result["summary"]["counts"] == {"equal": 1, "changed": 0, "left_only": 1, "right_only": 1, "duplicate_key": 1, "invalid_value": 1, "blank_key": 2}
    bad = next(g for g in result["groups"] if g["key_text"] == "BAD")
    assert [c["status"] for c in bad["comparisons"]] == ["invalid_value", "changed"]
    lone = next(g for g in result["groups"] if g["key_text"] == "L")
    assert lone["status"] == "left_only" and lone["source_cell_issues"]
    for side, source in (("left", left), ("right", right)):
        assert Counter(rid for g in result["groups"] for rid in g[f"{side}_rows"]) == Counter(r["id"] for r in source["rows"])
    assert result["summary"]["input_rows"] == 11
    assert result["summary"]["unresolved_rows"] == 8


def test_empty_tables_and_self_reconciliation():
    empty = table([])
    result = reconcile(empty, empty, MAPPING)
    assert result["summary"]["decision"] == "no_data" and result["summary"]["equal_rate"] is None
    one = table([["A", "2"]])
    result = reconcile(one, one, MAPPING)
    assert result["summary"]["input_rows"] == 2 and result["summary"]["decision"] == "all_equal"
    assert result["groups"][0]["left_rows"] == result["groups"][0]["right_rows"] == ["r1"]


def test_null_empty_space_zero_and_ascii_only_trim():
    mapping = copy.deepcopy(MAPPING)
    mapping["comparisons"][0]["type"] = "text"
    left, right = table([["A", ""], ["B", " "], ["C", "0"]]), table([["A", ""], ["B", ""], ["C", "0"]])
    left["rows"][0]["cells"]["c2"] = {"tag": "null", "raw": None}
    assert [g["status"] for g in reconcile(left, right, mapping)["groups"]] == ["changed", "changed", "equal"]
    mapping["key"]["normalization"] = "trim"
    assert reconcile(table([[" \tA\r\n", "x"]]), table([["A", "x"]]), mapping)["summary"]["decision"] == "all_equal"
    for non_ascii in ("\u00a0", "\u3000"):
        result = reconcile(table([[non_ascii + "A", "x"]]), table([["A", "x"]]), mapping)
        assert result["summary"]["counts"]["left_only"] == 1
        assert result["summary"]["counts"]["right_only"] == 1


def test_mapping_rejects_missing_duplicate_and_unsupported_fields():
    sample = table([["A", "1"]])
    definitions = [{}, {"key": MAPPING["key"], "comparisons": []}, {**MAPPING, "row_filter": {}}, {**MAPPING, "comparisons": MAPPING["comparisons"] * 2}, {**MAPPING, "key": {**MAPPING["key"], "left_column_id": "missing"}}, {**MAPPING, "comparisons": [{**MAPPING["comparisons"][0], "type": "float"}]}]
    for mapping in definitions:
        with pytest.raises(CoreError):
            validate_mapping(sample, sample, mapping)


def test_hash_ignores_random_ids_and_comparison_display_order():
    source = table([["A", "1", "x"]], ("id", "amount", "note"))
    mapping = copy.deepcopy(MAPPING)
    mapping["comparisons"].append({"left_column_id": "c3", "right_column_id": "c3", "type": "text"})
    before = copy.deepcopy(source)
    expected = reconcile(source, source, mapping)["result_hash"]
    assert source == before
    changed = copy.deepcopy(source)
    for h in changed["headers"]:
        h["id"] = "random-" + h["id"]
    for row in changed["rows"]:
        row["id"] = "random-" + row["id"]
        row["cells"] = {"random-" + k: v for k, v in row["cells"].items()}
    for pair in [mapping["key"], *mapping["comparisons"]]:
        for side in ("left", "right"):
            pair[f"{side}_column_id"] = "random-" + pair[f"{side}_column_id"]
    mapping["comparisons"].reverse()
    assert reconcile(changed, changed, mapping)["result_hash"] == expected


def test_randomized_participation_coverage_and_repeatability():
    rng = random.Random(2601104)
    for _ in range(30):
        sources = [table([[rng.choice(["", " ", "A", "B", "C", "D"]), rng.choice(["0", "1.0", "-2", "bad"])] for _ in range(rng.randrange(30))]) for side in range(2)]
        report = reconcile(*sources, MAPPING)
        assert report == reconcile(*sources, MAPPING)
        for side, source in zip(("left", "right"), sources):
            assert Counter(rid for g in report["groups"] for rid in g[f"{side}_rows"]) == Counter(r["id"] for r in source["rows"])


def test_export_all_groups_literal_cells_precision_and_provenance():
    left = table([["A", "12345678901234567890123456789012345678", "=1+1"], ["D", "1", "https://evil.invalid"], ["D", "2", "+cmd"], ["", "bad", "@sum"], ["N", "", "\tformula"]], ("id", "amount", "note"))
    right = table([["=1+1", "A", "12345678901234567890123456789012345678"], ["-cmd", "D", "2"], ["", "N", "0"]], ("note", "id", "amount"))
    mapping = {"key": {"left_column_id": "c1", "right_column_id": "c2"}, "comparisons": [{"left_column_id": "c2", "right_column_id": "c3", "type": "decimal"}, {"left_column_id": "c3", "right_column_id": "c1", "type": "text"}]}
    result = reconcile(left, right, mapping)
    data = build_export(left, right, mapping, result)
    assert data == build_export(left, right, mapping, result)
    book = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    assert book.sheetnames == ["核对摘要", "全部核对结果", "来源行", "待处理记录", "字段映射", "来源信息"]
    assert book["来源行"].max_row == 1 + len(left["rows"]) + len(right["rows"])
    source_rows = list(book["来源行"].values)
    assert any(row[1] == "right" and row[9] == "=1+1" and row[11] == "A" for row in source_rows[1:])
    all_values = [cell for sheet in book for row in sheet for cell in row]
    assert any(c.value == "12345678901234567890123456789012345678" for c in all_values)
    assert any(c.value == result["result_hash"] for c in all_values)
    assert any(c.value == "literal_empty" for c in all_values)
    assert all(c.data_type != "f" and c.hyperlink is None for c in all_values)
    assert book["全部核对结果"].max_row == 1 + len(result["groups"]) * 2
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert not any(b"<f>" in z.read(name) or b"<hyperlink " in z.read(name) for name in z.namelist() if name.endswith(".xml"))


def test_export_refuses_incomplete_or_duplicate_coverage():
    source = table([["A", "1"]])
    result = reconcile(source, source, MAPPING)
    result["groups"][0]["right_rows"] = []
    with pytest.raises(CoreError) as err:
        build_export(source, source, MAPPING, result)
    assert err.value.code == "INVALID_RESULT"


@pytest.mark.parametrize("raw", [b'id,n\na,b"c\n', b'id,n\na, "b"\n', b'id,n\na,"b" trailing\n'])
def test_csv_does_not_accept_stray_or_unescaped_quotes(raw):
    with pytest.raises(CoreError) as err:
        parse_file(raw, "quotes.csv")
    assert err.value.code == "INVALID_CSV"


def test_csv_escaped_quotes_preserve_exact_content():
    p = parse_file(b'id,n\n"a""b","line1\nline2"\n', "quotes.csv")
    assert p["rows"][0]["cells"]["c1"]["raw"] == 'a"b'
    assert p["rows"][0]["cells"]["c2"]["raw"] == "line1\nline2"


@pytest.mark.parametrize("cell", [
    '<c r="B2" t="s"/>', '<c r="B2" t="b"/>', '<c r="B2" t="e"/>',
    '<c r="B2" t="inlineStr"><v>1</v></c>',
    '<c r="B2" t="n"><is><t>1</t></is></c>',
    '<c r="B2"><v>1</v><v>2</v></c>',
    '<c r="B2" s="999"><v>45000</v></c>',
    '<c r="B2" s="-1"><v>45000</v></c>',
])
def test_malformed_typed_cells_cannot_silently_become_null_or_decimal(cell):
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER + '<row r="2">' + cell + '</row>'), "malformed.xlsx")
    assert err.value.code == "INVALID_XLSX"


def test_duplicate_groups_on_right_and_both_sides_are_never_arbitrarily_paired():
    for left_rows, right_rows in [([["A", "1"]], [["A", "1"], ["A", "2"]]),
                                  ([["A", "1"], ["A", "2"]], [["A", "1"], ["A", "2"]])]:
        result = reconcile(table(left_rows), table(right_rows), MAPPING)
        group = result["groups"][0]
        assert group["status"] == "duplicate_key"
        assert len(group["left_rows"]) == len(left_rows)
        assert len(group["right_rows"]) == len(right_rows)
        assert all(c["status"] == "unpaired" for c in group["comparisons"])
        assert result["summary"]["unresolved_rows"] == len(left_rows) + len(right_rows)


def test_null_blank_keys_each_remain_one_group():
    left = table([["", "1"], [" \t\r\n", "2"]])
    right = copy.deepcopy(left)
    right["rows"][0]["cells"]["c1"] = {"tag": "null", "raw": None}
    result = reconcile(left, right, MAPPING)
    assert len(result["groups"]) == 4
    assert all(g["status"] == "blank_key" and len(g["left_rows"]) + len(g["right_rows"]) == 1 for g in result["groups"])


def test_key_leading_zeros_and_explicit_trim_change_uniqueness():
    sample = table([["00123", "1"], ["123", "2"], [" 123", "3"]])
    result = reconcile(sample, sample, MAPPING)
    assert result["summary"]["counts"]["equal"] == 3
    mapping = copy.deepcopy(MAPPING)
    mapping["key"]["normalization"] = "trim"
    trimmed = reconcile(sample, sample, mapping)
    assert trimmed["summary"]["counts"]["equal"] == 1
    assert trimmed["summary"]["counts"]["duplicate_key"] == 1
    assert result["result_hash"] != trimmed["result_hash"]


def test_large_decimal_cent_difference():
    result = reconcile(table([["A", "9007199254740993.01"]]), table([["A", "9007199254740993.02"]]), MAPPING)
    assert result["groups"][0]["status"] == "changed"
    assert result["groups"][0]["comparisons"][0]["difference"] == "0.01"


def test_zip_expanded_bytes_limit_is_real():
    data = xlsx_xml(HEADER, extra={"padding.bin": b"0" * (50 * 1024 * 1024)})
    assert len(data) < 10 * 1024 * 1024
    with pytest.raises(CoreError) as err:
        parse_file(data, "expands.xlsx")
    assert err.value.code == "XLSX_LIMIT"


def test_repeated_shared_string_respects_parsed_text_budget():
    shared = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>' + ('字' * 4000) + '</t></si></sst>'
    body = ''.join(f'<row r="{i}"><c r="A{i}" t="s"><v>0</v></c><c r="B{i}" t="s"><v>0</v></c></row>' for i in range(2, 1002))
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER + body, extra={"xl/sharedStrings.xml": shared}), "repeated.xlsx")
    assert err.value.code == "PARSED_DATA_TOO_LARGE"


def test_export_every_status_and_source_cell_matches_input():
    left = table([["A", "1"], ["B", "2"], ["L", "3"], ["D", "4"], ["D", "5"], ["I", "bad"], ["", "6"]])
    right = table([["A", "1.0"], ["B", "20"], ["R", "7"], ["D", "4"], ["I", "0"]])
    result = reconcile(left, right, MAPPING)
    book = openpyxl.load_workbook(io.BytesIO(build_export(left, right, MAPPING, result)), data_only=False)
    result_rows = list(book["全部核对结果"].values)[1:]
    assert {row[1] for row in result_rows} == {"equal", "changed", "left_only", "right_only", "duplicate_key", "invalid_value", "blank_key"}
    sources = {side: {row["id"]: row for row in parsed["rows"]} for side, parsed in (("left", left), ("right", right))}
    for row in list(book["来源行"].values)[1:]:
        original = sources[row[1]][row[2]]
        assert row[6] == str(original["source"]["row"])
        assert row[9] == original["cells"]["c1"]["raw"]
        assert row[11] == original["cells"]["c2"]["raw"]


def test_external_workbook_relationship_is_rejected_even_without_external_part():
    relationship = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="foreign" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" Target="https://invalid.example/book.xlsx" TargetMode="External"/></Relationships>'
    with pytest.raises(CoreError) as err:
        parse_file(xlsx_xml(HEADER, extra={"xl/_rels/foreign.xml.rels": relationship}), "external.xlsx")
    assert err.value.code == "EXTERNAL_LINK"


def test_header_only_xlsx_is_no_data():
    source = parse_file(xlsx_xml(HEADER), "empty.xlsx")
    result = reconcile(source, source, MAPPING)
    assert source["row_count"] == 0
    assert result["summary"]["decision"] == "no_data"
    assert result["summary"]["equal_rate"] is None


def test_valid_unicode_whitespace_survives_export_roundtrip():
    original = "字\tline\nnext\r🚀"
    source = table([["A", original]])
    mapping = copy.deepcopy(MAPPING)
    mapping["comparisons"][0]["type"] = "text"
    result = reconcile(source, source, mapping)
    book = openpyxl.load_workbook(io.BytesIO(build_export(source, source, mapping, result)), data_only=False)
    # OOXML encodes CR as an escaped sequence. Our import decoder restores it.
    value = book["来源行"].cell(2, 12).value
    assert value.replace("_x000D_", "\r") == original
