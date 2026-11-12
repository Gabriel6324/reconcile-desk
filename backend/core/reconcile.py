"""Deterministic reconciliation. Inputs and returned evidence remain immutable."""

import copy
import hashlib
import json
import re
from decimal import Decimal, FloatOperation, Inexact, Rounded, localcontext

from .errors import CoreError
from .parsers import TRIM

ALGORITHM_VERSION = "exact-reconcile-1.0.0"
STATUSES = ("equal", "changed", "left_only", "right_only", "duplicate_key", "invalid_value", "blank_key")
TEXT_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")
NATIVE_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee]([+-]?[0-9]+))?\Z")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _invalid(message, **details):
    raise CoreError("INVALID_MAPPING", message, details)


def validate_mapping(left, right, definition):
    if not isinstance(definition, dict) or set(definition) != {"key", "comparisons"}:
        _invalid("映射必须明确包含主键和比较字段。")
    key = definition["key"]
    comparisons = definition["comparisons"]
    if not isinstance(key, dict) or not {"left_column_id", "right_column_id"} <= set(key) or set(key) - {"left_column_id", "right_column_id", "normalization"}:
        _invalid("主键定义无效。")
    if not isinstance(comparisons, list) or not 1 <= len(comparisons) <= 20:
        _invalid("请选择 1 至 20 对比较字段。")
    columns = {side: {h["id"]: h for h in table["headers"]} for side, table in (("left", left), ("right", right))}
    normalized = {"key": copy.deepcopy(key), "comparisons": []}
    used = {"left": set(), "right": set()}
    for i, item in enumerate([normalized["key"], *copy.deepcopy(comparisons)]):
        if not isinstance(item, dict):
            _invalid("字段映射必须是对象。")
        allowed = {"left_column_id", "right_column_id", "normalization"} | ({"type"} if i else set())
        required = allowed - {"normalization"}
        if not required <= set(item) or set(item) - allowed:
            _invalid("字段映射缺少必填属性或包含不支持的属性。")
        item.setdefault("normalization", "exact")
        if item["normalization"] not in ("exact", "trim"):
            _invalid("仅支持精确比较或显式去除首尾 ASCII 空白。")
        if i and item["type"] not in ("text", "decimal"):
            _invalid("比较字段类型必须为文本或精确数值。")
        for side, table in (("left", left), ("right", right)):
            col = item[f"{side}_column_id"]
            if not isinstance(col, str) or col not in columns[side]:
                _invalid("映射引用的字段不存在。", side=side, column_id=col)
            if i:
                if col in used[side]:
                    _invalid("比较字段必须一对一，不能重复使用。", side=side, column_id=col)
                used[side].add(col)
            else:
                for row in table["rows"]:
                    if row["cells"][col]["tag"] not in ("literal_text", "null"):
                        _invalid("主键列只能包含文本或空值，请先在来源表中将编号保存为文本。", side=side, column_id=col, row_id=row["id"], source=row["source"])
        if i:
            normalized["comparisons"].append(item)
    normalized["comparisons"].sort(key=lambda c: (columns["left"][c["left_column_id"]]["ordinal"], columns["right"][c["right_column_id"]]["ordinal"]))
    return normalized


def _plain(value):
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def convert_cell(cell, kind, normalization="exact"):
    """Return (normalized tagged value, issue code). Never coerce bool/date/float."""
    tag, raw = cell["tag"], cell["raw"]
    if kind == "text":
        if tag == "null":
            return {"tag": "null", "value": None}, None
        if tag != "literal_text":
            return None, "EXPECTED_TEXT"
        return {"tag": "literal_text", "value": raw.strip(TRIM) if normalization == "trim" else raw}, None
    if tag not in ("literal_text", "decimal_lexeme") or not isinstance(raw, str):
        return None, "EXPECTED_DECIMAL"
    lexeme = raw.strip(TRIM) if normalization == "trim" else raw
    pattern = TEXT_DECIMAL if tag == "literal_text" else NATIVE_DECIMAL
    match = pattern.fullmatch(lexeme)
    if not match:
        return None, "INVALID_DECIMAL_SYNTAX"
    if tag == "decimal_lexeme" and match[1] is not None and (len(match[1].lstrip("+-")) > 3 or abs(int(match[1])) > 100):
        return None, "DECIMAL_OUT_OF_RANGE"
    with localcontext() as ctx:
        ctx.prec = 64
        for trap in (Inexact, Rounded, FloatOperation):
            ctx.traps[trap] = True
        number = Decimal(lexeme)
        expanded = format(number, "f").lstrip("+-")
        integer, _, fraction = expanded.partition(".")
        digits = len(integer.lstrip("0")) + len(fraction)
        if max(1, digits) > 38 or len(fraction) > 12:
            return None, "DECIMAL_OUT_OF_RANGE"
        return {"tag": "decimal", "value": _plain(number)}, None


def _evidence(table, row, col, kind, normalization):
    cell = row["cells"][col]
    normalized, issue = convert_cell(cell, kind, normalization)
    evidence = {"row_id": row["id"], "column_id": col, "tag": cell["tag"], "raw": cell["raw"], "normalized": normalized, "source": copy.deepcopy(row["source"]), "source_hash": table["sha256"]}
    return evidence, issue


def reconcile(left, right, definition):
    mapping = validate_mapping(left, right, definition)
    tables = {"left": left, "right": right}
    groups = []
    buckets = {}
    for side, table in tables.items():
        col = mapping["key"][f"{side}_column_id"]
        for row in table["rows"]:
            cell = row["cells"][col]
            raw = cell["raw"]
            if cell["tag"] == "null" or not raw.strip(TRIM):
                groups.append({"status": "blank_key", "key_text": None, "left_rows": [row] if side == "left" else [], "right_rows": [row] if side == "right" else []})
                continue
            key = raw.strip(TRIM) if mapping["key"]["normalization"] == "trim" else raw
            bucket = buckets.setdefault(key, {"key_text": key, "left_rows": [], "right_rows": []})
            bucket[f"{side}_rows"].append(row)
    for bucket in buckets.values():
        ls, rs = bucket["left_rows"], bucket["right_rows"]
        bucket["status"] = "duplicate_key" if len(ls) > 1 or len(rs) > 1 else "right_only" if not ls else "left_only" if not rs else "equal"
        groups.append(bucket)
    groups.sort(key=lambda g: min((0 if side == "left" else 1, r["ordinal"]) for side in tables for r in g[f"{side}_rows"]))
    counts = {status: 0 for status in STATUSES}
    unresolved_rows = 0
    for ordinal, group in enumerate(groups, 1):
        group["id"] = f"g{ordinal}"
        group["comparisons"] = []
        group["source_cell_issues"] = []
        paired = group["status"] == "equal"
        for pair in mapping["comparisons"]:
            evidence = {"left": [], "right": []}
            issues = []
            for side, table in tables.items():
                col = pair[f"{side}_column_id"]
                for row in group[f"{side}_rows"]:
                    value, issue = _evidence(table, row, col, pair["type"], pair["normalization"])
                    evidence[side].append(value)
                    if issue:
                        item = {"side": side, "row_id": row["id"], "column_id": col, "code": issue, "raw": value["raw"], "tag": value["tag"], "source": value["source"]}
                        issues.append(item)
                        group["source_cell_issues"].append(item)
            comparison = {**pair, "status": "unpaired", "left": evidence["left"][0] if len(evidence["left"]) == 1 else None, "right": evidence["right"][0] if len(evidence["right"]) == 1 else None, "difference": None}
            if paired:
                if issues:
                    comparison["status"] = "invalid_value"
                else:
                    ln, rn = comparison["left"]["normalized"], comparison["right"]["normalized"]
                    comparison["status"] = "equal" if ln == rn else "changed"
                    if pair["type"] == "decimal":
                        with localcontext() as ctx:
                            ctx.prec = 64
                            for trap in (Inexact, Rounded, FloatOperation):
                                ctx.traps[trap] = True
                            comparison["difference"] = _plain(Decimal(rn["value"]) - Decimal(ln["value"]))
            group["comparisons"].append(comparison)
        if paired:
            statuses = {c["status"] for c in group["comparisons"]}
            group["status"] = "invalid_value" if "invalid_value" in statuses else "changed" if "changed" in statuses else "equal"
        if group["status"] in ("blank_key", "duplicate_key", "invalid_value") or group["source_cell_issues"]:
            unresolved_rows += len(group["left_rows"]) + len(group["right_rows"])
        counts[group["status"]] += 1
        for side in tables:
            group[f"{side}_rows"] = [r["id"] for r in group[f"{side}_rows"]]
    input_rows = len(left["rows"]) + len(right["rows"])
    decision = "no_data" if not input_rows else "needs_review" if unresolved_rows else "has_differences" if any(counts[x] for x in ("changed", "left_only", "right_only")) else "all_equal"
    summary = {"left_rows": len(left["rows"]), "right_rows": len(right["rows"]), "input_rows": input_rows, "total_groups": len(groups), "key_groups": len(buckets), "blank_key_rows": counts["blank_key"], "unresolved_rows": unresolved_rows, "counts": counts, "decision": decision, "equal_rate": None if not input_rows else counts["equal"] * 2 / input_rows, "completeness": "contains_unresolved_records" if unresolved_rows else "fully_reconciled"}
    result = {"algorithm_version": ALGORITHM_VERSION, "groups": groups, "summary": summary, "completeness": summary["completeness"]}
    result["result_hash"] = _result_hash(left, right, mapping, result)
    return result


def _result_hash(left, right, mapping, result):
    """Map database identities to source ordinals before calculating content identity."""
    columns = {side: {h["id"]: h["ordinal"] for h in table["headers"]} for side, table in (("left", left), ("right", right))}
    rows = {side: {r["id"]: r["ordinal"] for r in table["rows"]} for side, table in (("left", left), ("right", right))}
    semantic = copy.deepcopy(mapping)
    for item in [semantic["key"], *semantic["comparisons"]]:
        for side in columns:
            item[f"{side}_column_id"] = columns[side][item[f"{side}_column_id"]]
    content = copy.deepcopy(result)
    for group in content["groups"]:
        group.pop("id")
        for side in columns:
            group[f"{side}_rows"] = [rows[side][rid] for rid in group[f"{side}_rows"]]
        for comparison in group["comparisons"]:
            for side in columns:
                comparison[f"{side}_column_id"] = columns[side][comparison[f"{side}_column_id"]]
                value = comparison[side]
                if value is not None:
                    value["column_id"] = columns[side][value["column_id"]]
                    value["row_id"] = rows[side][value["row_id"]]
        for issue in group["source_cell_issues"]:
            side = issue["side"]
            issue["column_id"] = columns[side][issue["column_id"]]
            issue["row_id"] = rows[side][issue["row_id"]]
    sources = {side: {key: table[key] for key in ("sha256", "parser_version", "parse_options")} for side, table in (("left", left), ("right", right))}
    return hashlib.sha256(canonical_json({"sources": sources, "mapping": semantic, "result": content}).encode("utf-8")).hexdigest()
