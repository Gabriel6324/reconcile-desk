"""Literal-only XLSX evidence exports; no formulas or automatic hyperlinks."""

import datetime
import io
import json

import xlsxwriter

from .errors import CoreError
from .reconcile import validate_mapping

EXPORT_VERSION = "evidence-xlsx-1.0.0"
MAX_EXPORT_BYTES = 64 * 1024 * 1024


def build_export(left, right, mapping, result):
    mapping = validate_mapping(left, right, mapping.get("definition", mapping))
    output = io.BytesIO()
    book = xlsxwriter.Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False})
    book.set_properties({"title": "对齐 · 核对证据报告", "author": "Reconcile Desk", "created": datetime.datetime(2000, 1, 1)})
    head = book.add_format({"bold": True, "bg_color": "#163D39", "font_color": "#FFFFFF", "text_wrap": True})
    sheets = {}

    def add_sheet(name, headers):
        sheet = book.add_worksheet(name)
        sheet.freeze_panes(1, 0)
        sheet.set_column(0, len(headers) - 1, 22)
        for c, value in enumerate(headers):
            sheet.write_string(0, c, value, head)
        sheets[name] = (sheet, 1, len(headers))

    def add_row(name, values):
        sheet, row, cols = sheets[name]
        for col, value in enumerate(values):
            if value is not None:
                text = str(value)
                if len(text) > 32767:
                    raise CoreError("EXPORT_CELL_LIMIT", "导出单元格超过 XLSX 字符限制。")
                sheet.write_string(row, col, text)
        sheets[name] = (sheet, row + 1, cols)

    add_sheet("核对摘要", ["指标", "值"])
    for key, value in result["summary"].items():
        if isinstance(value, dict):
            for sub, count in value.items():
                add_row("核对摘要", [f"{key}.{sub}", count])
        else:
            add_row("核对摘要", [key, value])
    add_row("核对摘要", ["result_hash", result["result_hash"]])
    add_row("核对摘要", ["algorithm_version", result["algorithm_version"]])
    add_row("核对摘要", ["export_version", EXPORT_VERSION])
    add_sheet("全部核对结果", ["group_id", "分类", "主键", "左侧行数", "右侧行数", "左列序", "右列序", "比较类型", "规则", "字段状态", "left_row_id", "left_type", "left_raw", "left_normalized", "right_row_id", "right_type", "right_raw", "right_normalized", "右减左"])
    max_cols = max(len(left["headers"]), len(right["headers"]))
    add_sheet("来源行", ["group_id", "side", "import_row_id", "source_hash", "sheet", "record", "row", "line_start", "line_end", *[label for i in range(1, max_cols + 1) for label in (f"column_{i}_value", f"column_{i}_type")]])
    add_sheet("待处理记录", ["group_id", "分类", "side", "import_row_id", "column_ordinal", "问题", "原始类型", "原始值", "来源行"])
    add_sheet("字段映射", ["用途", "左列序", "左表头", "右列序", "右表头", "类型", "规范化"])
    add_sheet("来源信息", ["side", "信息类别", "column_ordinal", "名称", "值"])
    tables = {"left": left, "right": right}
    row_maps = {side: {r["id"]: r for r in table["rows"]} for side, table in tables.items()}
    column_maps = {side: {h["id"]: h for h in table["headers"]} for side, table in tables.items()}
    for side, table in tables.items():
        for key in ("filename", "sha256", "parser_version", "sheet", "row_count"):
            add_row("来源信息", [side, "file", None, key, table.get(key)])
        add_row("来源信息", [side, "file", None, "parse_options", json.dumps(table["parse_options"], ensure_ascii=False, sort_keys=True)])
        for h in table["headers"]:
            add_row("来源信息", [side, "column", h["ordinal"], h["name"], h["id"]])
    for role, pair in [("主键", mapping["key"]), *[("比较", c) for c in mapping["comparisons"]]]:
        lc, rc = column_maps["left"][pair["left_column_id"]], column_maps["right"][pair["right_column_id"]]
        add_row("字段映射", [role, lc["ordinal"], lc["name"], rc["ordinal"], rc["name"], pair.get("type", "text"), pair["normalization"]])
    covered = set()
    for group in result["groups"]:
        for comparison in group["comparisons"]:
            evidence = []
            for side in tables:
                cell = comparison[side]
                normalized = cell.get("normalized") if cell else None
                evidence.extend([cell.get("row_id") if cell else None, cell.get("tag") if cell else None, cell.get("raw") if cell else None, normalized.get("value") if normalized else None])
            add_row("全部核对结果", [group["id"], group["status"], group["key_text"], len(group["left_rows"]), len(group["right_rows"]), column_maps["left"][comparison["left_column_id"]]["ordinal"], column_maps["right"][comparison["right_column_id"]]["ordinal"], comparison["type"], comparison["normalization"], comparison["status"], *evidence, comparison["difference"]])
        for side, table in tables.items():
            for rid in group[f"{side}_rows"]:
                identity = (side, rid)
                if identity in covered or rid not in row_maps[side]:
                    raise CoreError("INVALID_RESULT", "核对结果存在重复或无效的来源行。")
                covered.add(identity)
                row = row_maps[side][rid]
                source = row["source"]
                values = []
                for h in sorted(table["headers"], key=lambda h: h["ordinal"]):
                    cell = row["cells"][h["id"]]
                    tag = "literal_empty" if cell["tag"] == "literal_text" and cell["raw"] == "" else cell["tag"]
                    values.extend([cell["raw"], tag])
                add_row("来源行", [group["id"], side, rid, table["sha256"], source.get("sheet"), source.get("record"), source.get("row"), source.get("line_start"), source.get("line_end"), *values])
                if group["status"] in ("duplicate_key", "blank_key"):
                    add_row("待处理记录", [group["id"], group["status"], side, rid, None, group["status"], None, None, source.get("row")])
        for issue in group["source_cell_issues"]:
            add_row("待处理记录", [group["id"], group["status"], issue["side"], issue["row_id"], column_maps[issue["side"]][issue["column_id"]]["ordinal"], issue["code"], issue["tag"], issue["raw"], issue["source"].get("row")])
    if len(covered) != len(left["rows"]) + len(right["rows"]):
        raise CoreError("INVALID_RESULT", "核对结果缺少来源行。")
    for sheet, row, cols in sheets.values():
        sheet.autofilter(0, 0, max(1, row - 1), cols - 1)
    book.close()
    data = output.getvalue()
    if len(data) > MAX_EXPORT_BYTES:
        raise CoreError("EXPORT_TOO_LARGE", "导出文件超过 64 MiB。")
    return data
