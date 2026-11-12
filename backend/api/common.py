import hashlib
import json
from fastapi import HTTPException

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

class Problem(Exception):
    def __init__(self, code, message, status=422, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details or {}

def needed(session, cls, ident):
    obj = session.get(cls, ident)
    if obj is None:
        raise Problem('NOT_FOUND', '记录不存在。', 404)
    return obj

def page(items, cursor=0, limit=50):
    if cursor < 0 or limit < 1 or limit > 100:
        raise Problem('INVALID_PAGE', '分页参数超出范围。')
    found, size = [], 0
    for item in items[cursor:cursor + limit]:
        cost = len(encoded(item))
        if size + cost > 1900000:
            if not found:
                raise Problem('RESPONSE_TOO_LARGE', '单条结果超过响应限制。', 413)
            break
        found.append(item)
        size += cost
    end = cursor + len(found)
    return {'items': found, 'next_cursor': str(end) if end < len(items) else None, 'total_count': len(items)}

def op_view(op):
    resource = {'kind': op.resource_kind, 'id': op.resource_id} if op.resource_id else None
    return {'operation_id': op.id, 'kind': op.kind, 'state': op.state,
            'error_code': op.error_code, 'error_message': op.error_message,
            'result_resource': resource, 'resource_id': op.resource_id,
            'poll_url': '/api/operations/' + op.id, 'retry_of': op.retry_of,
            'created_at': op.created_at.isoformat() if op.created_at else None}
