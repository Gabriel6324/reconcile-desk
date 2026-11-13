"""Reusable field semantics keyed by exact header names, never source cell values."""
from core import validate_mapping
from .common import Problem

MAX_TEMPLATES = 50


def template_name(value):
    value = value.strip()
    if not 1 <= len(value) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Problem('INVALID_TEMPLATE_NAME', '模板名称应为 1 至 80 个字符，且不能包含控制字符。')
    return value


def specification(left, right, definition):
    checked = validate_mapping(left, right, definition)
    names = {side: {h['id']: h['name'] for h in table['headers']}
             for side, table in (('left', left), ('right', right))}

    def fields(item):
        return {**{k: v for k, v in item.items() if k not in ('left_column_id', 'right_column_id')},
                'left_header': names['left'][item['left_column_id']],
                'right_header': names['right'][item['right_column_id']]}

    return {'schema_version': 1, 'key': fields(checked['key']),
            'comparisons': [fields(item) for item in checked['comparisons']]}


def preview(saved, left, right):
    columns = {side: {h['name']: h['id'] for h in table['headers']}
               for side, table in (('left', left), ('right', right))}
    missing = {'left': [], 'right': []}
    for item in [saved['key'], *saved['comparisons']]:
        for side in ('left', 'right'):
            name = item[side + '_header']
            if name not in columns[side] and name not in missing[side]:
                missing[side].append(name)
    if any(missing.values()):
        labels = [('左表', missing['left']), ('右表', missing['right'])]
        # Bound the displayed message; details retain the exact required headers.
        hints = ['{}缺少：{}'.format(side, '、'.join('“' + n[:80] + ('…' if len(n) > 80 else '') + '”' for n in names))
                 for side, names in labels if names]
        raise Problem('TEMPLATE_COLUMNS_MISSING', '；'.join(hints) + '。请检查表头或手动选择字段。',
                      422, {'missing_headers': missing})

    def fields(item):
        return {**{k: v for k, v in item.items() if k not in ('left_header', 'right_header')},
                'left_column_id': columns['left'][item['left_header']],
                'right_column_id': columns['right'][item['right_header']]}

    definition = {'key': fields(saved['key']), 'comparisons': [fields(item) for item in saved['comparisons']]}
    # Even the same headers may now contain numeric keys or incompatible inputs.
    return validate_mapping(left, right, definition)


def template_view(item):
    return {'id': item.id, 'name': item.name, 'created_at': item.created_at.isoformat(),
            'comparison_count': len(item.specification['comparisons'])}
