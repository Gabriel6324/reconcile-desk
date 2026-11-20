"""Authenticated team API. No schema migration or compute worker in API lifecycle."""
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
import hashlib
import os
import secrets

from fastapi import FastAPI, Request, Query, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.common import Problem, digest
from api.db import now, new_id, Import, Run, Export, MappingTemplate, Revision, Mapping
from .models import User, Organization, OrganizationRole, Workspace, Membership, Session, AuditEvent, LegacyScope, LoginThrottle, MutationReceipt
from .security import (ROLE_PERMISSIONS, normalize_login, hash_password, verify_password,
                       token_hash, issue_session, authenticate_session, authorize_workspace, require_org_admin)
from . import jobs
from .job_models import Job
from . import datasets, reconciliations
from .source_models import SourceAsset, Dataset, DatasetRow
from .reconcile_models import CollectionRule, CollectionRun, ResultGroup, RowCoverage

SCHEMA_REVISION = '0009'
ORG_ID = '00000000-0000-0000-0000-000000000001'
LEGACY_WORKSPACE_ID = '00000000-0000-0000-0000-000000000002'
COOKIE = 'rd_session'


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LoginBody(Body):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class UserCreate(Body):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=128)


class WorkspaceCreate(Body):
    name: str = Field(min_length=1, max_length=80)


class MemberEdit(Body):
    expected_version: int = Field(ge=0)
    roles: list[str] = Field(max_length=4)
    active: bool = True


class UserStatus(Body):
    active: bool


class RulePublication(Body):
    definition: dict


class CollectionSubmission(Body):
    rule_id: str = Field(min_length=1, max_length=36)


def audit(db, actor, action, resource_type, resource_id, request, workspace_id=None, detail=None):
    db.add(AuditEvent(id=new_id(), org_id=ORG_ID, workspace_id=workspace_id,
                     actor_id=actor.id if actor else None, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     request_id=request.state.request_id, safe_detail=detail or {}))


def workspace_view(w):
    return {'id': w.id, 'name': w.name, 'version': w.version}


def create_app(database_url=None, *, origins=None, mode='team', hosts=None):
    if mode not in ('team', 'personal'):
        raise ValueError('Invalid deployment mode')
    allowed_origins = origins or [x for x in os.getenv('TEAM_ALLOWED_ORIGINS', 'https://localhost').split(',') if x]
    if mode == 'team' and any(not x.startswith('https://') for x in allowed_origins):
        raise ValueError('Team origins require HTTPS')
    if mode == 'personal':
        from urllib.parse import urlsplit
        if any(urlsplit(x).scheme!='http' or urlsplit(x).hostname not in ('localhost','127.0.0.1')
               or urlsplit(x).path or urlsplit(x).query or urlsplit(x).fragment or urlsplit(x).username for x in allowed_origins):
            raise ValueError('Personal mode is loopback only')
    @asynccontextmanager
    async def lifespan(app):
        url = database_url or os.environ.get('DATABASE_URL', '')
        if not url.startswith(('postgresql://', 'postgresql+psycopg://')):
            raise RuntimeError('PostgreSQL DATABASE_URL required')
        if url.startswith('postgresql://'):
            url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
        app.state.engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=3)
        app.state.db = sessionmaker(app.state.engine, expire_on_commit=False)
        import asyncio
        app.state.upload_lock=asyncio.Lock()
        try:
            with app.state.engine.connect() as c:
                if c.scalar(text('SELECT version_num FROM alembic_version')) != SCHEMA_REVISION:
                    raise RuntimeError('Explicit database migration required before team API start')
            yield
        finally:
            app.state.engine.dispose()

    app = FastAPI(title='Reconcile Desk', version='0.3.0', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts or ['localhost', '127.0.0.1'])
    # A real Argon2 hash balances unknown-user verification work without storing a password.
    dummy_hash = hash_password(secrets.token_urlsafe(32))

    @app.middleware('http')
    async def boundary(request,call_next):
        parts=request.url.path.split('/')
        is_upload=request.method=='POST' and len(parts)==6 and parts[1:4]==['api','v1','workspaces'] and parts[5]=='datasets'
        request.state.is_upload=is_upload
        if is_upload:
            if request.app.state.upload_lock.locked():
                return JSONResponse({'code':'UPLOAD_CAPACITY','message':'另一份资料正在上传，请稍后重试。'},status_code=429)
            async with request.app.state.upload_lock:
                # Authenticate before Starlette consumes multipart bytes.
                try:
                    from starlette.concurrency import run_in_threadpool
                    def preauthorize():
                        with request.app.state.db.begin() as db:
                            user,_=authenticate_session(db,request.cookies.get(COOKIE,''),csrf=request.headers.get('x-csrf-token'),mutation=True)
                            authorize_workspace(db,user.id,parts[4],'data.import',lock=True)
                    await run_in_threadpool(preauthorize)
                except Problem as exc:
                    return JSONResponse({'code':exc.code,'message':exc.message},status_code=exc.status)
                return await checked_boundary(request,call_next)
        return await checked_boundary(request,call_next)

    async def checked_boundary(request, call_next):
        request.state.request_id = new_id()
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if request.headers.get('origin') not in allowed_origins or request.headers.get('x-reconcile-client') != 'web':
                return JSONResponse({'code': 'ORIGIN_REJECTED', 'message': '请求来源无效。'}, status_code=403)
            try:
                maximum=11*1024**2 if request.state.is_upload else 65536
                if int(request.headers.get('content-length', '0')) > maximum:
                    raise ValueError()
            except ValueError:
                return JSONResponse({'code': 'REQUEST_TOO_LARGE', 'message': '请求超过限制。'}, status_code=413)
            # Stream with a hard ceiling, including chunked requests with no Content-Length.
            chunks, size = [], 0
            import asyncio
            try:
                async with asyncio.timeout(15):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > maximum:
                            return JSONResponse({'code': 'REQUEST_TOO_LARGE', 'message': '请求超过限制。'}, status_code=413)
                        chunks.append(chunk)
            except TimeoutError:
                return JSONResponse({'code': 'REQUEST_TIMEOUT', 'message': '接收请求超时。'}, status_code=408)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Request-ID'] = request.state.request_id
        return response

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return JSONResponse({'code': exc.code, 'message': exc.message, 'details': exc.details,
                             'request_id': request.state.request_id}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({'code': 'INVALID_REQUEST', 'message': '请求字段无效。', 'request_id': request.state.request_id}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({'code': 'CONFLICT', 'message': '记录已存在或引用冲突。', 'request_id': request.state.request_id}, status_code=409)

    @app.exception_handler(DBAPIError)
    async def unavailable(request, exc):
        return JSONResponse({'code': 'DATABASE_UNAVAILABLE', 'message': '数据库暂不可用，请使用相同请求标识查询或重试。', 'request_id': request.state.request_id}, status_code=503)

    @contextmanager
    def authenticated(request, mutation=False):
        with request.app.state.db.begin() as db:
            user, session = authenticate_session(db, request.cookies.get(COOKIE, ''),
                                                 csrf=request.headers.get('x-csrf-token'), mutation=mutation)
            yield db, user, session

    def receipt(db, request, user, scope_id, action, payload, execute, replay_check=None):
        key = request.headers.get('idempotency-key', '')
        if not key or len(key) > 128 or not all(33 <= ord(c) <= 126 for c in key):
            raise Problem('IDEMPOTENCY_REQUIRED', '请提供有效请求标识。', 400)
        # Authenticated user row is locked by authenticate_session; same actor commands serialize.
        hash_value = digest({'scope': scope_id, 'action': action, 'body': payload})
        identity = {'scope_id': scope_id, 'actor_id': user.id, 'action': action, 'key': key}
        old = db.get(MutationReceipt, identity)
        if old:
            if old.request_hash != hash_value:
                raise Problem('IDEMPOTENCY_CONFLICT', '同一请求标识绑定了不同内容。', 409)
            if replay_check is not None and not replay_check(old.result):
                raise Problem('IDEMPOTENCY_CONFLICT', '同一请求标识绑定了不同内容。', 409)
            return old.result
        result = execute()
        db.add(MutationReceipt(**identity, request_hash=hash_value, result=result))
        return result

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'scope': 'collection_workflow', 'release_ready': False}

    @app.get('/api/ready')
    def ready(request: Request):
        with request.app.state.engine.connect() as c:
            if c.scalar(text('SELECT version_num FROM alembic_version')) != SCHEMA_REVISION:
                raise Problem('SCHEMA_MISMATCH', '数据库版本不兼容。', 503)
            held=c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted AND classid=0 AND objid=732914622 AND objsubid=1)"))
        return {'api': 'ready', 'worker': 'lease_present' if held else 'offline', 'schema': SCHEMA_REVISION}

    @app.post('/api/v1/session/login')
    def login(body: LoginBody, request: Request):
        login_name = normalize_login(body.login)
        # Per-account throttling persists across API restarts and cannot be bypassed by spoofed proxy IPs.
        throttle_key = hashlib.sha256(('login:' + login_name).encode('utf-8')).hexdigest()
        failed = False
        with request.app.state.db.begin() as db:
            from sqlalchemy.dialects.postgresql import insert
            # Bound expensive password verification and unknown-name writes across processes.
            # Use the actual peer; untrusted X-Forwarded-For never selects a rate-limit bucket.
            peer = request.client.host if request.client else 'unknown'
            for bucket, maximum in [('global', 120), ('peer:' + peer, 30)]:
                bucket_key = hashlib.sha256(('login-rate:' + bucket).encode()).hexdigest()
                db.execute(insert(LoginThrottle).values(key_hash=bucket_key, failures=0, window_start=now()).on_conflict_do_nothing())
                rate = db.get(LoginThrottle, bucket_key, with_for_update=True)
                if now() - rate.window_start >= timedelta(minutes=1):
                    rate.failures, rate.window_start = 0, now()
                if rate.failures >= maximum:
                    raise Problem('LOGIN_THROTTLED', '尝试过于频繁，请稍后再试。', 429)
                rate.failures += 1
            db.execute(insert(LoginThrottle).values(key_hash=throttle_key, failures=0, window_start=now()).on_conflict_do_nothing())
            throttle = db.get(LoginThrottle, throttle_key, with_for_update=True)
            time_now = now()
            if throttle.blocked_until and throttle.blocked_until > time_now:
                raise Problem('LOGIN_THROTTLED', '尝试过于频繁，请稍后再试。', 429)
            if time_now - throttle.window_start > timedelta(minutes=15):
                throttle.failures, throttle.window_start = 0, time_now
            user = db.scalar(select(User).where(User.login == login_name).with_for_update())
            good = verify_password(user.password_hash if user else dummy_hash, body.password)
            if not user or not user.active or not good:
                throttle.failures += 1
                if throttle.failures >= 5:
                    throttle.blocked_until = time_now + timedelta(minutes=15)
                failed = True
            else:
                throttle.failures, throttle.blocked_until = 0, None
                stored, raw, csrf = issue_session(db, user)
                audit(db, user, 'session.login', 'user', user.id, request)
                result = {'user': {'id': user.id, 'login': user.login}, 'csrf_token': csrf}
        if failed:
            raise Problem('INVALID_CREDENTIALS', '账号或密码无效。', 401)
        response = JSONResponse(result)
        response.set_cookie(COOKIE, raw, httponly=True, secure=mode == 'team', samesite='lax', max_age=43200, path='/')
        return response

    @app.get('/api/v1/session/me')
    def me(request: Request):
        with authenticated(request) as (db, user, session):
            grants = list(db.scalars(select(OrganizationRole.role).where(OrganizationRole.user_id == user.id)))
            return {'id': user.id, 'login': user.login, 'organization_roles': grants, 'mode': mode}

    @app.get('/api/v1/session/csrf')
    def refresh_csrf(request: Request):
        # A deterministic token can be recovered without invalidating another browser tab.
        # A custom header requires a CORS preflight for a cross-origin fetch; no CORS is enabled.
        if (request.headers.get('origin') and request.headers['origin'] not in allowed_origins) or request.headers.get('x-reconcile-client') != 'web':
            raise Problem('ORIGIN_REJECTED', '请求来源无效。', 403)
        from .security import csrf_for_session
        import hmac
        with authenticated(request) as (db, user, session):
            csrf=csrf_for_session(request.cookies.get(COOKIE,''))
            if not hmac.compare_digest(session.csrf_hash,token_hash(csrf)):
                raise Problem('UNAUTHENTICATED','请重新登录。',401)
            return {'csrf_token':csrf}

    @app.post('/api/v1/session/logout')
    def logout(request: Request):
        with authenticated(request, True) as (db, user, session):
            db.delete(session)
            audit(db, user, 'session.logout', 'user', user.id, request)
        response = JSONResponse({'logged_out': True})
        response.delete_cookie(COOKIE, path='/')
        return response

    @app.post('/api/v1/users')
    def create_user(body: UserCreate, request: Request):
        with authenticated(request, True) as (db, actor, session):
            require_org_admin(db, actor.id, ORG_ID, lock=True)
            def execute():
                encoded_password = hash_password(body.password)
                user = User(id=new_id(), login=normalize_login(body.login), password_hash=encoded_password, active=True, session_version=1)
                db.add(user)
                db.flush()
                audit(db, actor, 'user.create', 'user', user.id, request)
                return {'id': user.id, 'login': user.login, 'active': True}
            # Never persist a fast SHA-256 of password-bearing request bodies.
            # Existing Argon2 credentials establish secret equality on a retry.
            def same_secret(result):
                existing = db.get(User, result['id'])
                return existing is not None and verify_password(existing.password_hash, body.password)
            return receipt(db, request, actor, ORG_ID, 'user.create',
                           {'login': normalize_login(body.login)}, execute, same_secret)

    @app.get('/api/v1/users')
    def users(request: Request):
        with authenticated(request) as (db, actor, session):
            require_org_admin(db, actor.id, ORG_ID)
            rows = list(db.scalars(select(User).order_by(User.login).limit(100)))
            return {'items': [{'id': u.id, 'login': u.login, 'active': u.active} for u in rows]}

    @app.post('/api/v1/workspaces')
    def create_workspace(body: WorkspaceCreate, request: Request):
        name = body.name.strip()
        if not name:
            raise Problem('INVALID_NAME', '名称不能为空。')
        with authenticated(request, True) as (db, actor, session):
            require_org_admin(db, actor.id, ORG_ID, lock=True)
            def execute():
                w = Workspace(id=new_id(), org_id=ORG_ID, name=name, version=1)
                db.add(w)
                db.flush()
                db.add(Membership(workspace_id=w.id, user_id=actor.id,
                                  roles=['workspace_manager', 'operator', 'reviewer'], active=True, version=1))
                audit(db, actor, 'workspace.create', 'workspace', w.id, request, w.id, {'initial_roles': ['workspace_manager', 'operator', 'reviewer']})
                return workspace_view(w)
            return receipt(db, request, actor, ORG_ID, 'workspace.create', {'name': name}, execute)

    @app.get('/api/v1/workspaces')
    def workspaces(request: Request):
        with authenticated(request) as (db, user, session):
            rows = db.execute(select(Workspace, Membership.roles).join(Membership, Membership.workspace_id == Workspace.id)
                              .where(Membership.user_id == user.id, Membership.active.is_(True)).order_by(Workspace.id).limit(100))
            return {'items': [dict(workspace_view(w), roles=roles) for w, roles in rows]}

    @app.get('/api/v1/workspaces/{workspace_id}/members')
    def list_members(workspace_id:str,request:Request):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'members.manage')
            rows=db.execute(select(Membership,User.login).join(User,User.id==Membership.user_id).where(Membership.workspace_id==workspace_id).order_by(User.login).limit(100))
            return {'items':[{'user_id':m.user_id,'login':login,'roles':m.roles,'active':m.active,'version':m.version} for m,login in rows]}

    @app.put('/api/v1/workspaces/{workspace_id}/members/{user_id}')
    def member(workspace_id: str, user_id: str, body: MemberEdit, request: Request):
        if not body.roles or any(r not in ROLE_PERMISSIONS or r == 'deployment_admin' for r in body.roles):
            raise Problem('INVALID_ROLE', '工作区角色无效。')
        roles = sorted(set(body.roles))
        with authenticated(request, True) as (db, actor, session):
            authorize_workspace(db, actor.id, workspace_id, 'members.manage')
            # Lock every affected membership in the same order, including the caller's grant.
            list(db.scalars(select(Membership).where(Membership.workspace_id == workspace_id,
                 Membership.user_id.in_([actor.id, user_id])).order_by(Membership.user_id).with_for_update()))
            authorize_workspace(db, actor.id, workspace_id, 'members.manage')
            target = db.get(User, user_id)
            if not target or not target.active:
                raise Problem('NOT_FOUND', '用户不存在或已停用。', 404)
            # Serialize membership CAS through the workspace; no target-user lock after actor locks.
            db.get(Workspace, workspace_id, with_for_update=True)
            def execute():
                row = db.get(Membership, (workspace_id, user_id), with_for_update=True)
                current = row.version if row else 0
                if current != body.expected_version:
                    raise Problem('VERSION_CONFLICT', '成员权限已被修改，请刷新后重试。', 409, {'current_version': current})
                if actor.id == user_id and ('workspace_manager' not in roles or not body.active):
                    raise Problem('SELF_ADMIN_REMOVAL', '不能通过此入口移除自己的工作区管理权限。', 409)
                if row:
                    row.roles, row.active, row.version = roles, body.active, row.version + 1
                else:
                    row = Membership(workspace_id=workspace_id, user_id=user_id, roles=roles, active=body.active, version=1)
                    db.add(row)
                audit(db, actor, 'member.change', 'membership', user_id, request, workspace_id, {'roles': roles, 'active': body.active, 'version': row.version})
                return {'user_id': user_id, 'roles': roles, 'active': row.active, 'version': row.version}
            return receipt(db, request, actor, workspace_id, 'member.change:' + user_id, body.model_dump(), execute)

    def scoped_legacy(db, user, w, kind, ident, permission):
        authorize_workspace(db, user.id, w, permission)
        scope = db.get(LegacyScope, (kind, ident))
        if scope is None or scope.workspace_id != w:
            raise Problem('NOT_FOUND', '记录不存在。', 404)

    @app.get('/api/v1/workspaces/{workspace_id}/legacy/imports')
    def legacy_imports(workspace_id: str, request: Request):
        with authenticated(request) as (db, user, session):
            authorize_workspace(db, user.id, workspace_id, 'data.read')
            rows = db.scalars(select(Import).join(LegacyScope, (LegacyScope.legacy_id == Import.id) & (LegacyScope.resource_type == 'import'))
                              .where(LegacyScope.workspace_id == workspace_id).order_by(Import.created_at, Import.id).limit(100))
            return {'items': [{'id': x.id, 'filename': x.filename, 'sha256': x.sha256, 'state': x.state, 'actor': 'unknown_legacy'} for x in rows]}

    @app.get('/api/v1/workspaces/{workspace_id}/legacy/imports/{ident}/source')
    def legacy_source(workspace_id: str, ident: str, request: Request):
        with authenticated(request) as (db, user, session):
            scoped_legacy(db, user, workspace_id, 'import', ident, 'data.read')
            item = db.get(Import, ident)
            if item is None:
                raise Problem('NOT_FOUND', '记录不存在。', 404)
            return Response(item.original, media_type='application/octet-stream', headers={'Content-Disposition': 'attachment; filename="source.bin"'})

    @app.get('/api/v1/workspaces/{workspace_id}/legacy/runs')
    def legacy_runs(workspace_id: str, request: Request):
        with authenticated(request) as (db, user, session):
            authorize_workspace(db, user.id, workspace_id, 'results.read')
            rows = db.scalars(select(Run).join(LegacyScope, (LegacyScope.legacy_id == Run.id) & (LegacyScope.resource_type == 'run'))
                              .where(LegacyScope.workspace_id == workspace_id).order_by(Run.created_at, Run.id).limit(100))
            return {'items': [{'id': x.id, 'state': x.state, 'revision': x.revision,
                               'summary': x.result.get('summary') if x.result else None, 'actor': 'unknown_legacy'} for x in rows]}

    @app.get('/api/v1/workspaces/{workspace_id}/legacy/runs/{ident}/export')
    def legacy_export(workspace_id: str, ident: str, request: Request):
        with authenticated(request) as (db, user, session):
            scoped_legacy(db, user, workspace_id, 'run', ident, 'data.export')
            item = db.scalar(select(Export).where(Export.run_id == ident).order_by(Export.version.desc()))
            if item is None:
                raise Problem('NOT_FOUND', '未生成导出文件。', 404)
            return Response(item.content, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            headers={'Content-Disposition': 'attachment; filename="reconciliation.xlsx"'})

    @app.get('/api/v1/workspaces/{workspace_id}/audit')
    def audit_list(workspace_id: str, request: Request):
        with authenticated(request) as (db, user, session):
            authorize_workspace(db, user.id, workspace_id, 'audit.read')
            events = db.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id).order_by(AuditEvent.created_at, AuditEvent.id).limit(100))
            return {'items': [{'id': e.id, 'actor_id': e.actor_id, 'action': e.action, 'resource_id': e.resource_id,
                               'detail': e.safe_detail, 'created_at': e.created_at.isoformat()} for e in events]}

    @app.get('/api/v1/workspaces/{workspace_id}/jobs')
    def job_list(workspace_id: str, request: Request, after: str = '', limit: int = Query(default=50, ge=1, le=100)):
        with authenticated(request) as (db, user, session):
            authorize_workspace(db, user.id, workspace_id, 'jobs.read')
            rows=list(db.scalars(select(Job).where(Job.workspace_id==workspace_id, Job.id>after).order_by(Job.id).limit(limit+1)))
            return {'items':[jobs.job_view(j) for j in rows[:limit]],
                    'next_cursor':rows[limit-1].id if len(rows)>limit else None}

    @app.get('/api/v1/workspaces/{workspace_id}/jobs/{job_id}')
    def get_job(workspace_id: str, job_id: str, request: Request):
        with authenticated(request) as (db, user, session):
            authorize_workspace(db, user.id, workspace_id, 'jobs.read')
            job=db.scalar(select(Job).where(Job.id==job_id, Job.workspace_id==workspace_id))
            if job is None:
                raise Problem('NOT_FOUND','任务不存在。',404)
            return jobs.job_view(job)

    @app.post('/api/v1/workspaces/{workspace_id}/datasets',status_code=202)
    async def upload_dataset(workspace_id:str,request:Request,file:UploadFile=File(...),worksheet:str|None=Query(default=None,max_length=31)):
        content=await file.read(10*1024**2+1)
        from starlette.concurrency import run_in_threadpool
        def commit_upload():
            with authenticated(request,True) as (db,user,session):
                return datasets.create_dataset(db,workspace_id,user.id,file.filename or '',content,worksheet,request.headers.get('idempotency-key',''))
        return await run_in_threadpool(commit_upload)

    @app.get('/api/v1/workspaces/{workspace_id}/datasets')
    def list_datasets(workspace_id:str,request:Request,after:str='',limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.read')
            rows=list(db.scalars(select(Dataset).where(Dataset.workspace_id==workspace_id,Dataset.id>after).order_by(Dataset.id).limit(limit+1)))
            return {'items':[datasets.view(db,x) for x in rows[:limit]],'next_cursor':rows[limit-1].id if len(rows)>limit else None}

    @app.get('/api/v1/workspaces/{workspace_id}/datasets/{ident}')
    def get_dataset(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            return datasets.view(db,datasets.get_dataset(db,workspace_id,user.id,ident))

    @app.get('/api/v1/workspaces/{workspace_id}/datasets/{ident}/rows')
    def dataset_rows(workspace_id:str,ident:str,request:Request,after:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            dataset=datasets.get_dataset(db,workspace_id,user.id,ident)
            if db.get(Job,dataset.job_id).state!='succeeded':
                raise Problem('DATASET_NOT_READY','资料尚未解析完成。',409)
            rows=list(db.scalars(select(DatasetRow).where(DatasetRow.workspace_id==workspace_id,DatasetRow.dataset_id==ident,DatasetRow.ordinal>after).order_by(DatasetRow.ordinal).limit(limit+1)))
            from api.common import encoded
            items=[];size=0
            for x in rows[:limit]:
                item={'id':x.row_id,'ordinal':x.ordinal,'source':x.source,'cells':x.cells}
                cost=len(encoded(item))
                if size+cost>1900000:break
                size+=cost;items.append(item)
            if rows and not items:
                raise Problem('RESPONSE_TOO_LARGE','单行结果超过响应限制。',413)
            return {'items':items,'next_cursor':items[-1]['ordinal'] if len(rows)>len(items) else None,'total_count':dataset.row_count}

    @app.get('/api/v1/workspaces/{workspace_id}/datasets/{ident}/source')
    def dataset_source(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            dataset=datasets.get_dataset(db,workspace_id,user.id,ident)
            asset=db.get(SourceAsset,dataset.asset_id)
            return Response(asset.content,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="source.bin"'})

    @app.post('/api/v1/workspaces/{workspace_id}/jobs/{job_id}/cancel')
    def cancel_job(workspace_id: str, job_id: str, request: Request):
        with authenticated(request,True) as (db,user,session):
            job=jobs.cancel(db,workspace_id,user.id,job_id)
            return jobs.job_view(job)

    @app.post('/api/v1/workspaces/{workspace_id}/collection-rules',status_code=201)
    def publish_collection_rule(workspace_id:str,body:RulePublication,request:Request):
        with authenticated(request,True) as (db,user,session):
            return reconciliations.create_rule(db,workspace_id,user.id,body.definition,request.headers.get('idempotency-key',''))

    @app.get('/api/v1/workspaces/{workspace_id}/collection-rules')
    def list_collection_rules(workspace_id:str,request:Request,after:str='',limit:int=Query(default=20,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.read')
            rows=list(db.scalars(select(CollectionRule).where(CollectionRule.workspace_id==workspace_id,CollectionRule.id>after).order_by(CollectionRule.id).limit(limit+1)))
            return bounded_page(rows,limit,reconciliations.rule_view,lambda row:row.id)

    @app.get('/api/v1/workspaces/{workspace_id}/collection-rules/{ident}')
    def get_collection_rule(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            return reconciliations.rule_view(reconciliations.get_rule(db,workspace_id,user.id,ident))

    @app.post('/api/v1/workspaces/{workspace_id}/collection-runs',status_code=202)
    def submit_collection_run(workspace_id:str,body:CollectionSubmission,request:Request):
        with authenticated(request,True) as (db,user,session):
            return reconciliations.create_run(db,workspace_id,user.id,body.rule_id,request.headers.get('idempotency-key',''))

    @app.get('/api/v1/workspaces/{workspace_id}/collection-runs')
    def list_collection_runs(workspace_id:str,request:Request,after:str='',limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'results.read')
            rows=list(db.scalars(select(CollectionRun).where(CollectionRun.workspace_id==workspace_id,CollectionRun.id>after).order_by(CollectionRun.id).limit(limit+1)))
            return bounded_page(rows,limit,lambda row:reconciliations.run_view(db,row),lambda row:row.id)

    @app.get('/api/v1/workspaces/{workspace_id}/collection-runs/{ident}')
    def get_collection_run(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            return reconciliations.run_view(db,reconciliations.get_run(db,workspace_id,user.id,ident))

    def ready_run(db,workspace_id,actor_id,ident):
        run=reconciliations.get_run(db,workspace_id,actor_id,ident)
        if db.get(Job,run.job_id).state!='succeeded':
            raise Problem('RUN_NOT_READY','核对结果尚未完成。',409)
        return run

    def bounded_page(rows,limit,convert,cursor):
        from api.common import encoded
        items=[];size=0
        for row in rows[:limit]:
            item=convert(row);cost=len(encoded(item))
            if size+cost>1900000:break
            size+=cost;items.append(item)
        if rows and not items:
            raise Problem('RESPONSE_TOO_LARGE','单项结果超过响应限制。',413)
        return {'items':items,'next_cursor':cursor(rows[len(items)-1]) if len(rows)>len(items) else None}

    @app.get('/api/v1/workspaces/{workspace_id}/collection-runs/{ident}/groups')
    def collection_groups(workspace_id:str,ident:str,request:Request,after:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            ready_run(db,workspace_id,user.id,ident)
            rows=list(db.scalars(select(ResultGroup).where(ResultGroup.workspace_id==workspace_id,ResultGroup.run_id==ident,ResultGroup.ordinal>after).order_by(ResultGroup.ordinal).limit(limit+1)))
            return bounded_page(rows,limit,reconciliations.group_view,lambda row:row.ordinal)

    @app.get('/api/v1/workspaces/{workspace_id}/collection-runs/{ident}/groups/{group_id}/issues')
    def collection_group_issues(workspace_id:str,ident:str,group_id:str,request:Request,after:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            ready_run(db,workspace_id,user.id,ident)
            group=db.scalar(select(ResultGroup).where(ResultGroup.workspace_id==workspace_id,ResultGroup.run_id==ident,ResultGroup.group_id==group_id))
            if group is None:raise Problem('NOT_FOUND','结果分组不存在。',404)
            issues=group.detail['issues'];items=issues[after:after+limit]
            return {'items':items,'next_cursor':after+len(items) if after+len(items)<len(issues) else None,'total_count':len(issues)}

    @app.get('/api/v1/workspaces/{workspace_id}/collection-runs/{ident}/groups/{group_id}/rows')
    def collection_group_sources(workspace_id:str,ident:str,group_id:str,request:Request,side:str=Query(pattern='^(left|right)$'),after:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            ready_run(db,workspace_id,user.id,ident)
            authorize_workspace(db,user.id,workspace_id,'data.read')
            group=db.scalar(select(ResultGroup).where(ResultGroup.workspace_id==workspace_id,ResultGroup.run_id==ident,ResultGroup.group_id==group_id))
            if group is None:raise Problem('NOT_FOUND','结果分组不存在。',404)
            query=select(DatasetRow).join(RowCoverage,(RowCoverage.workspace_id==DatasetRow.workspace_id)&(RowCoverage.dataset_id==DatasetRow.dataset_id)&(RowCoverage.row_id==DatasetRow.row_id)).where(RowCoverage.workspace_id==workspace_id,RowCoverage.run_id==ident,RowCoverage.group_id==group_id,RowCoverage.side==side,DatasetRow.ordinal>after).order_by(DatasetRow.ordinal).limit(limit+1)
            rows=list(db.scalars(query))
            return bounded_page(rows,limit,lambda row:{'id':f'{row.dataset_id}:{row.row_id}','dataset_id':row.dataset_id,'row_id':row.row_id,'ordinal':row.ordinal,'source':row.source,'cells':row.cells},lambda row:row.ordinal)

    from .period_api import install as install_period_routes
    install_period_routes(app,authenticated,bounded_page)
    from .case_api import install as install_case_routes
    install_case_routes(app,authenticated,bounded_page)
    from .reports import install as install_reports
    install_reports(app,authenticated)
    return app


app = create_app()
