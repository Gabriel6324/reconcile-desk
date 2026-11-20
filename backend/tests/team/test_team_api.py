"""Real PostgreSQL verification of the authenticated foundation and legacy boundary."""
import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session as DBSession

from api.db import Import, Operation, Mapping, Revision, Run, Export, now
from api.executor import Runner
from team.app import create_app, ORG_ID, LEGACY_WORKSPACE_ID
from team.cli import migrate, bootstrap, change_account
from team.models import User, Session, Workspace, Membership, AuditEvent, LegacyScope
from team.security import hash_password

URL=os.getenv('TEST_DATABASE_URL','')
pytestmark=pytest.mark.skipif(not URL,reason='dedicated real PostgreSQL required')
PASSWORD='A-test-password-2026'
HEADERS={'Origin':'https://localhost','X-Reconcile-Client':'web'}


@pytest.fixture(scope='module')
def environment():
    if URL.rsplit('/',1)[-1]!='reconcile_test':
        pytest.fail('Refusing destructive fixture outside reconcile_test')
    engine=create_engine(URL)
    with engine.begin() as c:
        c.execute(text('DROP SCHEMA public CASCADE'))
        c.execute(text('CREATE SCHEMA public'))
    migrate(engine)
    owner_id=bootstrap(engine,'owner',PASSWORD,claim_legacy=True)
    app=create_app(URL,origins=['https://localhost'])
    with TestClient(app,base_url='https://localhost',headers=HEADERS) as client:
        yield app,engine,client,owner_id
    engine.dispose()


def login(client,login_name='owner',password=PASSWORD):
    r=client.post('/api/v1/session/login',json={'login':login_name,'password':password})
    assert r.status_code==200,r.text
    client.headers['X-CSRF-Token']=r.json()['csrf_token']
    return r


def command(client,method,path,payload,key=None):
    return client.request(method,path,json=payload,headers={'Idempotency-Key':key or str(uuid.uuid4())})


def workspace(client,name):
    r=command(client,'POST','/api/v1/workspaces',{'name':name})
    assert r.status_code==200,r.text
    return r.json()['id']


def add_user(client,name):
    r=command(client,'POST','/api/v1/users',{'login':name,'password':PASSWORD})
    assert r.status_code==200,r.text
    return r.json()['id']


def grant(client,w,user,roles,version=0,active=True):
    return command(client,'PUT',f'/api/v1/workspaces/{w}/members/{user}',{'expected_version':version,'roles':roles,'active':active})


def test_api_lifecycle_does_not_start_worker(environment):
    app,engine,c,owner=environment
    assert c.get('/api/ready').json()['worker']=='offline'
    assert not hasattr(app.state,'runner')
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT version_num FROM alembic_version'))=='0009'


def test_auth_required_cookie_csrf_and_old_route_closed(environment):
    app,engine,c,owner=environment
    assert c.get('/api/v1/workspaces').status_code==401
    r=login(c)
    cookie=r.headers['set-cookie'].lower()
    assert all(x in cookie for x in ('httponly','secure','samesite=lax'))
    assert c.get('/api/v1/session/me').json()['login']=='owner'
    assert c.get('/api/v1/session/csrf').json()['csrf_token']==r.json()['csrf_token']
    assert c.get('/api/v1/session/csrf').json()['csrf_token']==r.json()['csrf_token']
    assert c.get('/api/v1/session/csrf',headers={'Origin':'https://evil.test'}).status_code==403
    assert c.post('/api/v1/workspaces',json={'name':'x'},headers={'X-CSRF-Token':'bad'}).status_code==403
    assert c.post('/api/v1/workspaces',json={'name':'x'},headers={'Origin':'https://evil.test'}).status_code==403
    assert c.get('/api/imports').status_code==404
    assert c.post('/api/imports').status_code==404
    assert c.get('/api/operations').status_code==404


def test_password_not_stored_and_bootstrap_single_use(environment):
    app,engine,c,owner=environment
    with DBSession(engine) as s:
        u=s.get(User,owner)
        assert u.password_hash.startswith('$argon2id$') and PASSWORD not in u.password_hash
        stored=s.scalar(select(Session))
        assert len(stored.token_hash)==64 and stored.token_hash!=c.cookies.get('rd_session')
    with pytest.raises(RuntimeError,match='already exists'):
        bootstrap(engine,'another-admin',PASSWORD)


def test_workspace_idempotency_and_replay_conflict(environment):
    app,engine,c,owner=environment
    login(c)
    key=str(uuid.uuid4())
    a=command(c,'POST','/api/v1/workspaces',{'name':'idempotent'},key)
    b=command(c,'POST','/api/v1/workspaces',{'name':'idempotent'},key)
    assert a.status_code==b.status_code==200 and a.json()==b.json()
    changed=command(c,'POST','/api/v1/workspaces',{'name':'different'},key)
    assert changed.status_code==409
    with DBSession(engine) as s:
        assert len(list(s.scalars(select(Workspace).where(Workspace.name=='idempotent'))))==1


def test_role_isolation_and_revocation(environment):
    app,engine,c,owner=environment
    login(c)
    a,b=workspace(c,'alpha'),workspace(c,'beta')
    uid=add_user(c,'viewer.one')
    assert grant(c,a,uid,['viewer']).status_code==200
    with TestClient(app,base_url='https://localhost',headers=HEADERS) as other:
        login(other,'viewer.one')
        assert [x['id'] for x in other.get('/api/v1/workspaces').json()['items']]==[a]
        assert other.get(f'/api/v1/workspaces/{a}/legacy/runs').status_code==200
        assert other.get(f'/api/v1/workspaces/{a}/legacy/imports').status_code==403
        assert other.get(f'/api/v1/workspaces/{b}/legacy/runs').status_code==404
        assert command(other,'POST','/api/v1/workspaces',{'name':'escape'}).status_code==403
        assert grant(c,a,uid,['viewer'],version=1,active=False).status_code==200
        assert other.get(f'/api/v1/workspaces/{a}/legacy/runs').status_code==404


def test_member_cas_and_global_role_injection(environment):
    app,engine,c,owner=environment
    login(c)
    w=workspace(c,'concurrent')
    uid=add_user(c,'operator.one')
    assert grant(c,w,uid,['operator']).status_code==200
    def update(role):
        # Each request authenticates against the same persisted session; expected_version is the conflict guard.
        return grant(c,w,uid,[role],version=1).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(update,['viewer','reviewer']))
    assert sorted(statuses)==[200,409]
    assert grant(c,w,uid,['deployment_admin'],version=2).status_code==422
    assert grant(c,w,uid,[],version=2).status_code==422


def test_unknown_login_and_persistent_throttle(environment):
    app,engine,c,owner=environment
    for _ in range(5):
        assert c.post('/api/v1/session/login',json={'login':'missing.account','password':PASSWORD}).status_code==401
    assert c.post('/api/v1/session/login',json={'login':'missing.account','password':PASSWORD}).status_code==429


def test_user_create_retry_secret_conflict(environment):
    app,engine,c,owner=environment
    login(c)
    key=str(uuid.uuid4())
    body={'login':'idempotent.user','password':PASSWORD}
    a=command(c,'POST','/api/v1/users',body,key)
    b=command(c,'POST','/api/v1/users',body,key)
    assert a.status_code==b.status_code==200 and a.json()==b.json()
    different=command(c,'POST','/api/v1/users',dict(body,password='A-different-password-2026'),key)
    assert different.status_code==409


def test_legacy_source_export_and_audit_are_scoped(environment):
    app,engine,c,owner=environment
    login(c)
    w=LEGACY_WORKSPACE_ID
    raw=b'id,amount\nA,9007199254740993.01\n'
    export=b'immutable previously exported bytes'
    ids=[str(uuid.uuid4()) for _ in range(7)]
    op1,op2,op3,import_id,map_id,run_id,export_id=ids
    with DBSession(engine) as s,s.begin():
        for oid in [op1,op2,op3]:
            s.add(Operation(id=oid,kind='import',idempotency_key=oid,request_hash='a'*64,epoch=1,state='completed',request_snapshot={},reserved=0))
        s.flush()
        s.add(Import(id=import_id,operation_id=op1,filename='legacy.csv',sha256=hashlib.sha256(raw).hexdigest(),original=raw,state='ready'))
        s.flush()
        s.add(Mapping(id=map_id,left_id=import_id,right_id=import_id,current_revision=1))
        s.flush()
        s.add(Revision(mapping_id=map_id,revision=1,definition={},origin='manual'))
        s.add(Run(id=run_id,operation_id=op2,mapping_id=map_id,revision=1,left_id=import_id,right_id=import_id,snapshot={},result={'summary':{'n':1}},state='completed'))
        s.flush()
        s.add(Export(id=export_id,run_id=run_id,operation_id=op3,version='1',content=export,sha256=hashlib.sha256(export).hexdigest()))
        s.add_all([LegacyScope(resource_type='import',legacy_id=import_id,workspace_id=w),LegacyScope(resource_type='run',legacy_id=run_id,workspace_id=w)])
    assert c.get(f'/api/v1/workspaces/{w}/legacy/imports/{import_id}/source').content==raw
    assert c.get(f'/api/v1/workspaces/{w}/legacy/runs/{run_id}/export').content==export
    other=workspace(c,'other-legacy-scope')
    assert c.get(f'/api/v1/workspaces/{other}/legacy/imports/{import_id}/source').status_code==404
    assert c.get(f'/api/v1/workspaces/{other}/legacy/runs/{run_id}/export').status_code==404
    assert c.get(f'/api/v1/workspaces/{other}/audit').status_code==200
    with engine.begin() as conn:
        with pytest.raises(Exception,match='append only'):
            conn.execute(text("UPDATE team_audit_events SET action='tampered'"))


def test_disabled_and_version_changed_sessions_rejected(environment):
    app,engine,c,owner=environment
    login(c)
    uid=add_user(c,'disabled.user')
    with TestClient(app,base_url='https://localhost',headers=HEADERS) as other:
        login(other,'disabled.user')
        with DBSession(engine) as s,s.begin():
            s.get(User,uid).session_version+=1
        assert other.get('/api/v1/session/me').status_code==401
        login(other,'disabled.user')
        change_account(engine,'disabled.user',active=False)
        assert other.get('/api/v1/session/me').status_code==401


def test_recovery_console_protects_last_admin_and_revokes_sessions(environment):
    app,engine,c,owner=environment
    with pytest.raises(RuntimeError,match='last active'):
        change_account(engine,'owner',active=False)
    change_account(engine,'disabled.user',active=True)
    with TestClient(app,base_url='https://localhost',headers=HEADERS) as other:
        login(other,'disabled.user')
        change_account(engine,'disabled.user',password='A-reset-password-2026')
        assert other.get('/api/v1/session/me').status_code==401
        login(other,'disabled.user','A-reset-password-2026')


def test_legacy_executor_refuses_team_database(environment):
    app,engine,c,owner=environment
    with pytest.raises(RuntimeError,match='team API'):
        Runner(engine).startup()


def test_job_api_hides_payload_and_checks_scope_before_cancel(environment):
    from team import jobs
    app,engine,c,owner=environment
    login(c)
    w=workspace(c,'job-api')
    other=workspace(c,'job-api-other')
    with DBSession(engine) as s,s.begin():
        job=jobs.admit(s,w,owner,'reconcile','api-job',{'sensitive':'not an API status field'},65536)
        job_id=job.id
    r=c.get(f'/api/v1/workspaces/{w}/jobs/{job_id}')
    assert r.status_code==200 and r.json()['state']=='queued'
    assert 'request_snapshot' not in r.json() and 'sensitive' not in r.text
    assert c.get(f'/api/v1/workspaces/{other}/jobs/{job_id}').status_code==404
    assert c.post(f'/api/v1/workspaces/{other}/jobs/{job_id}/cancel').status_code==404
    assert c.post(f'/api/v1/workspaces/{w}/jobs/{job_id}/cancel').json()['state']=='cancelled'
    assert c.post(f'/api/v1/workspaces/{w}/jobs/{job_id}/cancel').json()['state']=='cancelled'


def test_concurrent_cross_membership_grants_do_not_deadlock(environment):
    app,engine,c,owner=environment
    login(c)
    wa,wb=workspace(c,'cross-admin-a'),workspace(c,'cross-admin-b')
    ua,ub=add_user(c,'cross.admin.a'),add_user(c,'cross.admin.b')
    assert grant(c,wa,ua,['workspace_manager']).status_code==200
    assert grant(c,wb,ub,['workspace_manager']).status_code==200
    with TestClient(app,base_url='https://localhost',headers=HEADERS) as a, TestClient(app,base_url='https://localhost',headers=HEADERS) as b:
        login(a,'cross.admin.a');login(b,'cross.admin.b')
        import threading
        barrier=threading.Barrier(2)
        def update(args):
            client,w,user=args
            barrier.wait(timeout=5)
            return grant(client,w,user,['operator']).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(update,x) for x in [(a,wa,ub),(b,wb,ua)]]
            assert [f.result(timeout=10) for f in futures]==[200,200]


def test_logout_revokes_session(environment):
    app,engine,c,owner=environment
    login(c)
    cookie=c.cookies.get('rd_session')
    assert c.post('/api/v1/session/logout').status_code==200
    assert c.get('/api/v1/session/me',headers={'Cookie':'rd_session='+cookie}).status_code==401
