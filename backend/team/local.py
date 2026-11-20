"""Loopback portable entry point with first-run provisioning and bundled UI."""
import os
from pathlib import Path
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from .app import create_app,UserCreate,ORG_ID
from .models import Organization,OrganizationRole,User,Workspace,Membership,AuditEvent
from .security import normalize_login,hash_password
from api.common import Problem
from api.db import new_id

port=int(os.getenv('RECONCILE_PORTABLE_WEB_PORT','8080'))
if not 1024<=port<=65535:raise RuntimeError('Invalid loopback port')
app=create_app(mode='personal',origins=[f'http://127.0.0.1:{port}',f'http://localhost:{port}'])


@app.middleware('http')
async def loopback_only(request,call_next):
    if not request.client or request.client.host not in ('127.0.0.1','::1','testclient'):
        from fastapi.responses import JSONResponse
        return JSONResponse({'code':'LOOPBACK_ONLY','message':'本机版本仅允许本机访问。'},status_code=403)
    return await call_next(request)


@app.get('/api/v1/setup/status')
def setup_status(request:Request):
    with request.app.state.db() as db:
        return {'required':db.scalar(select(OrganizationRole).limit(1)) is None}


@app.get('/api/local-instance')
def local_instance():
    # A per-launch identity, never a credential. Readiness must belong to the
    # child launched for this directory even when another install takes a port.
    return {'instance_id':os.getenv('RECONCILE_PORTABLE_INSTANCE_ID','')}


@app.post('/api/v1/setup',status_code=201)
def setup(body:UserCreate,request:Request):
    # The app boundary already requires exact loopback Origin/custom header.
    with request.app.state.db.begin() as db:
        db.get(Organization,ORG_ID,with_for_update=True)
        if db.scalar(select(OrganizationRole).limit(1)) is not None:
            raise Problem('SETUP_COMPLETE','管理员已创建，请登录。',409)
        user=User(id=new_id(),login=normalize_login(body.login),password_hash=hash_password(body.password))
        db.add(user);db.flush()
        db.add(OrganizationRole(org_id=ORG_ID,user_id=user.id,role='deployment_admin'))
        workspace=Workspace(id=new_id(),org_id=ORG_ID,name='我的工作区');db.add(workspace);db.flush()
        db.add(Membership(workspace_id=workspace.id,user_id=user.id,roles=['workspace_manager','operator','reviewer']))
        db.add(AuditEvent(id=new_id(),org_id=ORG_ID,workspace_id=workspace.id,actor_id=user.id,action='admin.local_setup',
            resource_type='user',resource_id=user.id,request_id=new_id(),safe_detail={'loopback':True}))
        return {'id':user.id,'login':user.login}


dist=Path(os.getenv('WEB_DIST',str(Path(__file__).resolve().parents[2]/'frontend/dist')))
if dist.is_dir():app.mount('/',StaticFiles(directory=dist,html=True),name='frontend')
