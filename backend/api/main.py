"""Local single-user HTTP API. PostgreSQL is the only durable source of truth."""
import copy
import asyncio
import time
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile, File, Header, Query
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, func
from sqlalchemy.exc import DBAPIError

from core import CoreError, validate_mapping
from core.parsers import MAX_FILE_BYTES, PARSER_VERSION
from core.reconcile import ALGORITHM_VERSION, STATUSES
from .common import Problem, encoded, digest, needed, page, op_view
from .db import make_engine, Import, Mapping, Revision, MappingTemplate, Run, Operation, Export, Suggestion, now, new_id
from .executor import Runner
from . import model, templates


@asynccontextmanager
async def lifespan(app):
    engine = make_engine()
    runner = Runner(engine)
    app.state.runner = runner
    try:
        runner.startup()
        yield
    finally:
        if hasattr(runner, 'lease'):
            runner.close()
        engine.dispose()


app = FastAPI(title='对齐 · Reconcile Desk', version='0.1.0', lifespan=lifespan)


class Boundary:
    def __init__(self, app):
        self.app = app
        self.upload_lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['method'] == 'POST' and scope['path'] == '/api/imports':
            if self.upload_lock.locked():
                return await JSONResponse({'code': 'UPLOAD_CAPACITY', 'message': '另一份资料正在上传，请稍后再试。'}, status_code=429)(scope, receive, send)
            async with self.upload_lock:
                return await self.handle(scope, receive, send)
        return await self.handle(scope, receive, send)

    async def handle(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        async def reject(code, message, status):
            await JSONResponse({'code': code, 'message': message}, status_code=status)(scope, receive, send)
        if scope['method'] in ('POST', 'PUT', 'PATCH', 'DELETE'):
            allowed = os.getenv('ALLOWED_ORIGINS', 'http://127.0.0.1:8080,http://localhost:8080').split(',')
            origin = headers.get(b'origin', b'').decode('latin-1')
            if (origin and origin not in allowed) or headers.get(b'x-reconcile-client') != b'web':
                return await reject('ORIGIN_REJECTED', '请求来源不受支持。', 403)
            maximum = 11 * 1024**2 if scope['path'] == '/api/imports' else 65536
            try:
                if int(headers.get(b'content-length', b'0')) > maximum:
                    return await reject('REQUEST_TOO_LARGE', '请求内容超过限制。', 413)
            except ValueError:
                return await reject('INVALID_LENGTH', '请求长度无效。', 400)
            chunks, total = [], 0
            deadline = time.monotonic() + 30
            while True:
                try:
                    event = await asyncio.wait_for(receive(), timeout=max(.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return await reject('UPLOAD_TIMEOUT', '接收请求超时，请重新提交。', 408)
                if event['type'] == 'http.disconnect':
                    return
                total += len(event.get('body', b''))
                if total > maximum:
                    return await reject('REQUEST_TOO_LARGE', '请求内容超过限制。', 413)
                chunks.append(event.get('body', b''))
                if not event.get('more_body', False):
                    break
            body = b''.join(chunks)
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await receive()
            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


app.add_middleware(Boundary)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'api'])


@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return JSONResponse({'code': exc.code, 'message': exc.message, 'details': exc.details}, status_code=exc.status)


@app.exception_handler(CoreError)
async def core_handler(request, exc):
    return JSONResponse({'code': exc.code, 'message': exc.message, 'details': exc.details}, status_code=422)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return JSONResponse({'code': 'INVALID_REQUEST', 'message': '请求字段缺失或格式无效。'}, status_code=422)


@app.exception_handler(DBAPIError)
async def database_handler(request, exc):
    # Never disclose DSNs, SQL values, uploaded text, or passwords.
    return JSONResponse({'code': 'DATABASE_UNAVAILABLE', 'message': '数据库暂不可用，请稍后查看已有任务。'}, status_code=503)


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Pair(Body):
    left_import_id: str = Field(max_length=36)
    right_import_id: str = Field(max_length=36)
    definition: dict


class Expected(Body):
    expected_revision: int = Field(ge=1)


class Edit(Expected):
    definition: dict


class TemplateCreate(Expected):
    name: str
    mapping_id: str = Field(min_length=1, max_length=36)


class TemplatePreview(Body):
    left_import_id: str = Field(min_length=1, max_length=36)
    right_import_id: str = Field(min_length=1, max_length=36)


class RunBody(Expected):
    mapping_id: str = Field(max_length=36)
    retry_of: str | None = Field(default=None, max_length=36)


class SuggestBody(Expected):
    task: str = Field(default='', max_length=2000)
    retry_of: str | None = Field(default=None, max_length=36)


class ApplyBody(Expected):
    suggestion_id: str = Field(max_length=36)


def runner(request):
    return request.app.state.runner


def ready_import(s, ident):
    item = needed(s, Import, ident)
    if item.state != 'ready' or item.parsed is None:
        raise Problem('IMPORT_NOT_READY', '资料尚未成功导入。', 409)
    return item


def import_view(item, cursor=0, limit=10):
    data = item.parsed or {'headers': [], 'rows': []}
    rows = page(data['rows'], cursor, limit)
    return {'id': item.id, 'state': item.state, 'filename': item.filename, 'sha256': item.sha256,
            'columns': data['headers'], 'row_count': len(data['rows']), 'rows': rows['items'],
            'next_cursor': rows['next_cursor'], 'created_at': item.created_at.isoformat()}


def mapping_view(s, item):
    rev = needed(s, Revision, (item.id, item.current_revision))
    return {'mapping_id': item.id, 'current_revision': item.current_revision,
            'definition': rev.definition, 'confirmation': rev.confirmation,
            'left_import_id': item.left_id, 'right_import_id': item.right_id}


def run_view(item):
    return {'id': item.id, 'run_id': item.id, 'state': item.state, 'mapping_id': item.mapping_id,
            'revision': item.revision, 'left_import_id': item.left_id, 'right_import_id': item.right_id,
            'created_at': item.created_at.isoformat(), 'summary': item.result.get('summary') if item.result else None,
            'snapshot': item.snapshot, 'result_hash': item.result.get('result_hash') if item.result else None}


def revision(s, ident, expected):
    item = s.get(Mapping, ident, with_for_update=True)
    if item is None:
        raise Problem('NOT_FOUND', '映射不存在。', 404)
    if item.current_revision != expected:
        raise Problem('REVISION_CONFLICT', '字段映射已被修改，请刷新后检查。', 409)
    return item, needed(s, Revision, (ident, expected))


def draft_definition(definition):
    # Incomplete drafts are allowed; confirmation always applies strict core validation.
    if not isinstance(definition, dict) or set(definition) != {'key', 'comparisons'} or len(encoded(definition)) > 16000:
        raise Problem('INVALID_MAPPING', '字段草稿格式无效。')
    if not isinstance(definition['key'], dict) or not isinstance(definition['comparisons'], list) or len(definition['comparisons']) > 20:
        raise Problem('INVALID_MAPPING', '字段草稿必须包含主键和最多 20 对比较字段。')
    return copy.deepcopy(definition)


def save_quota(r, s, quota):
    r.recount(s, quota)
    r.check_quota(quota, 0)


@app.get('/api/health')
def health():
    return {'status': 'alive'}


@app.get('/api/ready')
def ready(request: Request):
    r = runner(request)
    if not r.probe():
        raise Problem('NOT_READY', '工作区执行器不可用。', 503)
    with r.Session() as s:
        s.execute(text('SELECT 1'))
    return {'status': 'ready'}


@app.get('/api/capabilities')
def capabilities():
    return {'model': {'configured': model.configured(), 'model': os.getenv('LLM_MODEL') if model.configured() else None},
            'limits': {'file_bytes': MAX_FILE_BYTES, 'rows': 10000, 'columns': 50, 'comparisons': 20},
            'parser_version': PARSER_VERSION, 'algorithm_version': ALGORITHM_VERSION}


@app.post('/api/imports')
async def upload(request: Request, file: UploadFile = File(...), idempotency_key: str | None = Header(default=None)):
    data = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    if len(data) > MAX_FILE_BYTES:
        raise Problem('FILE_TOO_LARGE', '文件超过 10 MiB。', 413)
    filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    if not filename or len(filename) > 240 or any(ord(c) < 32 for c in filename):
        raise Problem('INVALID_FILENAME', '文件名无效。')
    sha = hashlib.sha256(data).hexdigest()
    def prepare(s, op):
        item = Import(id=new_id(), operation_id=op.id, filename=filename, sha256=sha, original=data)
        s.add(item)
        op.resource_id, op.resource_kind = item.id, 'import'
    view, _ = runner(request).admit('import', idempotency_key, {'filename': filename, 'sha256': sha}, prepare, len(data) + 64 * 1024**2)
    return JSONResponse(view, status_code=202)


@app.get('/api/imports')
def imports(request: Request, cursor: int = 0, limit: int = 50):
    page([], cursor, limit)
    with runner(request).Session() as s:
        rows = s.execute(select(Import.id, Import.state, Import.filename, Import.sha256, Import.created_at,
                                Import.parsed['headers'].label('columns'),
                                func.jsonb_array_length(Import.parsed['rows']).label('row_count'))
                         .order_by(Import.created_at.desc(), Import.id.desc()).offset(cursor).limit(limit + 1)).mappings().all()
        items = [{**dict(row), 'columns': row['columns'] or [], 'row_count': row['row_count'] or 0,
                  'created_at': row['created_at'].isoformat(), 'rows': []} for row in rows]
        result = page(items, 0, limit)
        if result['next_cursor'] is not None:
            result['next_cursor'] = str(cursor + len(result['items']))
        result.pop('total_count', None)
        return result


@app.get('/api/imports/{ident}')
def get_import(ident: str, request: Request, cursor: int = 0, limit: int = 10):
    with runner(request).Session() as s:
        return import_view(needed(s, Import, ident), cursor, limit)


@app.get('/api/imports/{ident}/rows/{row_id}')
def source_row(ident: str, row_id: str, request: Request):
    with runner(request).Session() as s:
        item = ready_import(s, ident)
        for row in item.parsed['rows']:
            if row['id'] == row_id:
                return row
    raise Problem('NOT_FOUND', '来源行不存在。', 404)


@app.get('/api/imports/{ident}/source')
def source(ident: str, request: Request):
    with runner(request).Session() as s:
        item = needed(s, Import, ident)
        return Response(item.original, media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(item.filename), 'ETag': '"' + item.sha256 + '"'})


@app.post('/api/mappings')
def create_mapping(body: Pair, request: Request):
    r = runner(request)
    with r.Session.begin() as s:
        quota = r.fence(s)
        ready_import(s, body.left_import_id)
        ready_import(s, body.right_import_id)
        item = Mapping(id=new_id(), left_id=body.left_import_id, right_id=body.right_import_id, current_revision=1)
        s.add(item)
        s.flush()
        s.add(Revision(mapping_id=item.id, revision=1, definition=draft_definition(body.definition)))
        save_quota(r, s, quota)
        return mapping_view(s, item)


@app.get('/api/mappings')
def mappings(request: Request, cursor: int = 0, limit: int = 50):
    with runner(request).Session() as s:
        return page([mapping_view(s, i) for i in s.scalars(select(Mapping).order_by(Mapping.created_at.desc(), Mapping.id.desc()))], cursor, limit)


@app.get('/api/mappings/{ident}')
def get_mapping(ident: str, request: Request):
    with runner(request).Session() as s:
        return mapping_view(s, needed(s, Mapping, ident))


@app.get('/api/templates')
def list_templates(request: Request, cursor: int = 0, limit: int = 50):
    page([], cursor, limit)
    with runner(request).Session() as s:
        items = s.scalars(select(MappingTemplate).order_by(MappingTemplate.created_at.desc(), MappingTemplate.id.desc()))
        return page([templates.template_view(item) for item in items], cursor, limit)


@app.post('/api/templates')
def create_template(body: TemplateCreate, request: Request):
    name = templates.template_name(body.name)
    r = runner(request)
    with r.Session.begin() as s:
        quota = r.fence(s)
        item, rev = revision(s, body.mapping_id, body.expected_revision)
        if not rev.confirmation or rev.confirmation['definition_hash'] != digest(rev.definition):
            raise Problem('CONFIRMATION_STALE', '请先确认当前字段映射，再保存为模板。', 409)
        if s.scalar(select(MappingTemplate.id).where(MappingTemplate.name == name)):
            raise Problem('TEMPLATE_NAME_EXISTS', '同名模板已存在，请使用其他名称。', 409)
        if s.scalar(select(func.count()).select_from(MappingTemplate)) >= templates.MAX_TEMPLATES:
            raise Problem('TEMPLATE_LIMIT', '最多可保存 50 个模板，请先删除不再使用的模板。', 409)
        saved = MappingTemplate(id=new_id(), name=name,
            specification=templates.specification(ready_import(s, item.left_id).parsed,
                                                   ready_import(s, item.right_id).parsed, rev.definition))
        s.add(saved)
        save_quota(r, s, quota)
        return templates.template_view(saved)


@app.post('/api/templates/{ident}/preview')
def preview_template(ident: str, body: TemplatePreview, request: Request):
    with runner(request).Session() as s:
        item = needed(s, MappingTemplate, ident)
        definition = templates.preview(item.specification, ready_import(s, body.left_import_id).parsed,
                                       ready_import(s, body.right_import_id).parsed)
        return {'template_id': item.id, 'name': item.name, 'definition': definition}


@app.delete('/api/templates/{ident}')
def delete_template(ident: str, request: Request):
    r = runner(request)
    with r.Session.begin() as s:
        quota = r.fence(s)
        item = needed(s, MappingTemplate, ident)
        s.delete(item)
        save_quota(r, s, quota)
        return {'deleted': True, 'id': ident}


@app.put('/api/mappings/{ident}')
def edit_mapping(ident: str, body: Edit, request: Request):
    r = runner(request)
    with r.Session.begin() as s:
        quota = r.fence(s)
        item, _ = revision(s, ident, body.expected_revision)
        item.current_revision += 1
        s.add(Revision(mapping_id=item.id, revision=item.current_revision, definition=draft_definition(body.definition)))
        save_quota(r, s, quota)
        return mapping_view(s, item)


@app.post('/api/mappings/{ident}/confirm')
def confirm(ident: str, body: Expected, request: Request):
    r = runner(request)
    with r.Session.begin() as s:
        quota = r.fence(s)
        item, rev = revision(s, ident, body.expected_revision)
        left, right = ready_import(s, item.left_id), ready_import(s, item.right_id)
        definition = validate_mapping(left.parsed, right.parsed, rev.definition)
        # Bind the immutable revision, both immutable inputs and the exact semantics.
        if rev.confirmation is None:
            rev.confirmation = {'revision': rev.revision, 'definition_hash': digest(rev.definition),
                                'left_sha256': left.sha256, 'right_sha256': right.sha256,
                                'confirmed_at': now().isoformat(), 'parser_version': PARSER_VERSION,
                                'algorithm_version': ALGORITHM_VERSION}
        save_quota(r, s, quota)
        return mapping_view(s, item)


@app.post('/api/runs')
def create_run(body: RunBody, request: Request, idempotency_key: str | None = Header(default=None)):
    def prepare(s, op):
        item, rev = revision(s, body.mapping_id, body.expected_revision)
        if not rev.confirmation or rev.confirmation['definition_hash'] != digest(rev.definition):
            raise Problem('CONFIRMATION_STALE', '请先确认当前版本的主键与字段范围。', 409)
        left, right = ready_import(s, item.left_id), ready_import(s, item.right_id)
        definition = validate_mapping(left.parsed, right.parsed, rev.definition)
        run = Run(id=new_id(), operation_id=op.id, mapping_id=item.id, revision=rev.revision,
                  left_id=item.left_id, right_id=item.right_id, snapshot={
                      'definition': definition, 'confirmation': rev.confirmation,
                      'left_sha256': left.sha256, 'right_sha256': right.sha256,
                      'parser_version': PARSER_VERSION, 'algorithm_version': ALGORITHM_VERSION})
        s.add(run)
        op.resource_id, op.resource_kind = run.id, 'run'
    view, _ = runner(request).admit('reconcile', idempotency_key, body.model_dump(exclude={'retry_of'}), prepare, 64 * 1024**2, body.retry_of)
    return JSONResponse(view, status_code=202)


@app.get('/api/runs')
def runs(request: Request, cursor: int = 0, limit: int = 50):
    page([], cursor, limit)
    with runner(request).Session() as s:
        rows = s.execute(select(Run.id, Run.state, Run.mapping_id, Run.revision,
                                Run.left_id.label('left_import_id'), Run.right_id.label('right_import_id'),
                                Run.created_at, Run.result['summary'].label('summary'))
                         .order_by(Run.created_at.desc(), Run.id.desc()).offset(cursor).limit(limit + 1)).mappings().all()
        result = page([{**dict(row), 'created_at': row['created_at'].isoformat()} for row in rows], 0, limit)
        if result['next_cursor'] is not None:
            result['next_cursor'] = str(cursor + len(result['items']))
        result.pop('total_count', None)
        return result


@app.get('/api/runs/{ident}')
def get_run(ident: str, request: Request):
    with runner(request).Session() as s:
        return run_view(needed(s, Run, ident))


@app.get('/api/runs/{ident}/results')
def results(ident: str, request: Request, cursor: int = 0, limit: int = 50, status: str | None = None):
    if status and status not in STATUSES:
        raise Problem('INVALID_FILTER', '结果分类无效。')
    with runner(request).Session() as s:
        item = needed(s, Run, ident)
        if item.state != 'completed':
            raise Problem('RUN_NOT_READY', '核对尚未完成。', 409)
        groups = []
        for g in item.result['groups']:
            if status and g['status'] != status:
                continue
            issues = g.get('source_cell_issues', [])
            groups.append({**g, 'source_cell_issues': issues[:20],
                           'source_cell_issue_count': len(issues), 'issues_truncated': len(issues) > 20})
        return page(groups, cursor, limit)


@app.post('/api/runs/{ident}/exports')
def prepare_export(ident: str, request: Request, idempotency_key: str | None = Header(default=None)):
    r = runner(request)
    def reusable(s):
        # Admission holds the executor row lock while checking both key and artifact.
        run = needed(s, Run, ident)
        if run.state != 'completed':
            raise Problem('RUN_NOT_READY', '仅已完成核对可导出。', 409)
        existing = s.scalar(select(Export).where(Export.run_id == ident, Export.version == '1'))
        if existing:
            return needed(s, Operation, existing.operation_id)
        return s.scalar(select(Operation).where(
            Operation.kind == 'export', Operation.state.in_(('running', 'stopping')),
            Operation.request_snapshot['run_id'].astext == ident,
            Operation.request_snapshot['version'].astext == '1'))
    def prepare(s, op):
        run = needed(s, Run, ident)
        if run.state != 'completed':
            raise Problem('RUN_NOT_READY', '核对尚未完成。', 409)
    view, _ = r.admit('export', idempotency_key, {'run_id': ident, 'version': '1'},
                      prepare, 64 * 1024**2, reuse=reusable)
    return JSONResponse(view, status_code=200 if view['state'] == 'completed' else 202)


@app.get('/api/runs/{ident}/export')
def export(ident: str, request: Request):
    with runner(request).Session() as s:
        needed(s, Run, ident)
        item = s.scalar(select(Export).where(Export.run_id == ident, Export.version == '1'))
        if item is None:
            raise Problem('EXPORT_NOT_READY', '请先准备导出。', 409)
        return Response(item.content, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={
            'Content-Disposition': 'attachment; filename="reconcile-' + ident + '.xlsx"', 'ETag': '"' + item.sha256 + '"'})


@app.get('/api/operations')
def operations(request: Request, cursor: int = 0, limit: int = 50):
    with runner(request).Session() as s:
        return page([op_view(i) for i in s.scalars(select(Operation).order_by(Operation.created_at.desc(), Operation.id.desc()))], cursor, limit)


@app.get('/api/operations/{ident}')
def operation(ident: str, request: Request):
    with runner(request).Session() as s:
        return op_view(needed(s, Operation, ident))


@app.post('/api/mappings/{ident}/suggest')
def suggest(ident: str, body: SuggestBody, request: Request, idempotency_key: str | None = Header(default=None)):
    if not model.configured():
        raise Problem('MODEL_NOT_CONFIGURED', '尚未配置模型接口，可继续手动选择字段。', 503)
    def prepare(s, op):
        item, rev = revision(s, ident, body.expected_revision)
        call = Suggestion(id=new_id(), mapping_id=item.id, operation_id=op.id, base_revision=rev.revision)
        s.add(call)
        op.resource_id, op.resource_kind = call.id, 'suggestion'
    view, _ = runner(request).admit('model_suggestion', idempotency_key,
                                  {'mapping_id': ident, **body.model_dump(exclude={'retry_of'})}, prepare, 131072, body.retry_of)
    return JSONResponse(view, status_code=202)


@app.get('/api/suggestions/{ident}')
def suggestion(ident: str, request: Request):
    with runner(request).Session() as s:
        item = needed(s, Suggestion, ident)
        return {'suggestion_id': item.id, 'base_revision': item.base_revision, 'state': item.state,
                'proposal': item.proposal, 'billing_state': item.billing_state, 'metadata': item.metadata_json}


@app.post('/api/mappings/{ident}/apply-suggestion')
def apply_suggestion(ident: str, body: ApplyBody, request: Request):
    r = runner(request)
    with r.Session.begin() as s:
        quota = r.fence(s)
        item, rev = revision(s, ident, body.expected_revision)
        call = needed(s, Suggestion, body.suggestion_id)
        if call.mapping_id != item.id or call.base_revision != rev.revision or call.state != 'completed':
            raise Problem('STALE_SUGGESTION', '建议与当前字段版本不匹配。', 409)
        definition = validate_mapping(ready_import(s, item.left_id).parsed, ready_import(s, item.right_id).parsed,
                                      {k: call.proposal[k] for k in ('key', 'comparisons')})
        item.current_revision += 1
        s.add(Revision(mapping_id=item.id, revision=item.current_revision, definition=definition, origin='model_suggestion'))
        save_quota(r, s, quota)
        return mapping_view(s, item)


# Portable desktop bundle serves the built client on the same loopback origin.
web_dist = os.getenv('WEB_DIST')
if web_dist:
    app.mount('/', StaticFiles(directory=web_dist, html=True), name='portable-web')
