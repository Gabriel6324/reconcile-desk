"""Real database + spawned compute process ingestion and interruption tests."""
import asyncio
import io
import os
import threading
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine,select,text,func
from sqlalchemy.orm import sessionmaker
from api.common import Problem
from team.app import create_app
from team.cli import migrate,bootstrap
from team import datasets,jobs
from team.source_models import SourceAsset,Dataset,DatasetRow
from team.job_models import Job,WorkspaceQuota,GlobalCapacity
from team.models import Workspace,Membership,User
from team.worker import ImportWorker
from core.dataset_parser import parse_dataset

URL=os.getenv('TEST_DATABASE_URL','')
pytestmark=pytest.mark.skipif(not URL,reason='real dedicated PostgreSQL required')
ORG='00000000-0000-0000-0000-000000000001'
PASSWORD='Integration-password-2026'
RAW=b'id,amount\nA,9007199254740993.01\nB,2.00\n'


@pytest.fixture(scope='module')
def env():
    if URL.rsplit('/',1)[-1]!='reconcile_test':pytest.fail('Dedicated reconcile_test required')
    engine=create_engine(URL,pool_pre_ping=True)
    with engine.begin() as c:
        c.execute(text('DROP SCHEMA public CASCADE'));c.execute(text('CREATE SCHEMA public'))
    migrate(engine)
    actor=bootstrap(engine,'import.owner',PASSWORD)
    Session=sessionmaker(engine,expire_on_commit=False)
    with Session.begin() as s:
        s.add_all([Workspace(id='wa',org_id=ORG,name='Imports A'),Workspace(id='wb',org_id=ORG,name='Imports B')]);s.flush()
        s.add_all([Membership(workspace_id=w,user_id=actor,roles=['workspace_manager','operator']) for w in ['wa','wb']])
    app=create_app(URL,origins=['https://localhost'])
    with TestClient(app,base_url='https://localhost',headers={'Origin':'https://localhost','X-Reconcile-Client':'web'}) as c:
        login=c.post('/api/v1/session/login',json={'login':'import.owner','password':PASSWORD})
        assert login.status_code==200,login.text
        c.headers['X-CSRF-Token']=login.json()['csrf_token']
        yield engine,Session,actor,c,app
    engine.dispose()


def upload(c,data=RAW,name='orders.csv',key=None,worksheet=None):
    r=c.post('/api/v1/workspaces/wa/datasets',files={'file':(name,data)},
             headers={'Idempotency-Key':key or str(uuid.uuid4())},params={'worksheet':worksheet} if worksheet else {})
    assert r.status_code==202,r.text
    return r.json()


def run_worker(engine,**kwargs):
    worker=ImportWorker(engine,**kwargs)
    try:
        worker.start();return worker.tick()
    finally:worker.close()


def test_upload_unauthenticated_rejected_before_receiving_body(env):
    engine,S,actor,c,app=env
    received=[];sent=[]
    async def receive():
        received.append(True)
        raise AssertionError('An unauthenticated body was consumed')
    async def send(message):sent.append(message)
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
           'scheme':'https','path':'/api/v1/workspaces/wa/datasets','raw_path':b'/api/v1/workspaces/wa/datasets',
           'query_string':b'','headers':[(b'host',b'localhost')],'client':('127.0.0.1',1),'server':('localhost',443)}
    asyncio.run(app(scope,receive,send))
    assert not received
    assert next(x['status'] for x in sent if x['type']=='http.response.start')==401


def test_upload_worker_provenance_idempotency_and_scope(env):
    engine,S,actor,c,app=env
    key=str(uuid.uuid4())
    a=upload(c,key=key);b=upload(c,key=key)
    assert a==b
    with S() as s:
        assert s.scalar(select(func.count()).select_from(SourceAsset))==1
        before=s.get(WorkspaceQuota,'wa').committed_bytes
    assert c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}/rows").status_code==409
    assert run_worker(engine)
    info=c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}").json()
    assert info['state']=='ready',info
    assert info['source_completeness']=='not_attested'
    rows=c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}/rows",params={'limit':1}).json()
    assert rows['items'][0]['cells']['c2']['raw']=='9007199254740993.01'
    assert rows['items'][0]['source']['row']==2
    assert rows['next_cursor']==1
    assert c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}/source").content==RAW
    assert c.get(f"/api/v1/workspaces/wb/datasets/{a['dataset_id']}/source").status_code==404
    with S() as s:
        q=s.get(WorkspaceQuota,'wa')
        assert q.committed_bytes>before and q.reserved_bytes==0


def test_failed_parse_retains_source_and_releases_reservation(env):
    engine,S,actor,c,app=env
    bad=b'id,amount\nA,1,extra\n'
    a=upload(c,bad)
    assert run_worker(engine)
    info=c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}").json()
    assert info['state']=='failed' and info['error_code']=='RAGGED_CSV',info
    assert c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}/source").content==bad
    with S() as s:
        assert s.get(WorkspaceQuota,'wa').reserved_bytes==0
        assert s.scalar(select(func.count()).select_from(DatasetRow).where(DatasetRow.dataset_id==a['dataset_id']))==0


def test_multisheet_explicit_selection_and_error_metadata(env):
    engine,S,actor,c,app=env
    wb=Workbook();wb.active.title='Orders';wb.active.append(['id','amount']);wb.active.append(['A','1'])
    ws=wb.create_sheet('Payments');ws.append(['id','amount']);ws.append(['B','2'])
    buffer=io.BytesIO();wb.save(buffer);data=buffer.getvalue()
    missing=upload(c,data,'sheets.xlsx');run_worker(engine)
    info=c.get(f"/api/v1/workspaces/wa/datasets/{missing['dataset_id']}").json()
    assert info['error_code']=='WORKSHEET_REQUIRED'
    assert len(info['error_details']['worksheets'])==2
    chosen=upload(c,data,'sheets.xlsx',worksheet='Payments');run_worker(engine)
    rows=c.get(f"/api/v1/workspaces/wa/datasets/{chosen['dataset_id']}/rows").json()['items']
    assert rows[0]['source']['sheet']=='Payments' and rows[0]['cells']['c1']['raw']=='B'


def test_cancel_queued_keeps_asset_without_rows(env):
    engine,S,actor,c,app=env
    a=upload(c)
    assert c.post(f"/api/v1/workspaces/wa/jobs/{a['job_id']}/cancel").json()['state']=='cancelled'
    assert not run_worker(engine)
    info=c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}").json()
    assert info['state']=='cancelled' and info['row_count']==0
    assert c.get(f"/api/v1/workspaces/wa/datasets/{a['dataset_id']}/source").content==RAW


def slow_compute(payload,output):
    time.sleep(20)


def test_timeout_really_terminates_compute_child(env):
    engine,S,actor,c,app=env
    a=upload(c)
    worker=ImportWorker(engine,timeout=1,heartbeat=.2,lease_seconds=5,compute_target=slow_compute)
    try:
        worker.start();assert worker.tick();assert worker.child is None
    finally:worker.close()
    with S() as s:assert s.get(Job,a['job_id']).error_code=='COMPUTE_TIMEOUT'


def test_cancel_running_waits_for_child_then_releases(env):
    engine,S,actor,c,app=env
    a=upload(c)
    worker=ImportWorker(engine,timeout=10,heartbeat=.2,lease_seconds=5,compute_target=slow_compute)
    errors=[]
    worker.start()
    def run():
        try:worker.tick()
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with S() as s:state=s.get(Job,a['job_id']).state
            if state=='running':break
            time.sleep(.02)
        assert state=='running'
        assert c.post(f"/api/v1/workspaces/wa/jobs/{a['job_id']}/cancel").json()['state']=='cancel_requested'
        thread.join(8)
        assert not thread.is_alive() and not errors
        with S() as s:
            assert s.get(Job,a['job_id']).state=='cancelled'
            assert s.get(WorkspaceQuota,'wa').reserved_bytes==0
    finally:worker.close();thread.join(5)


def test_replay_publication_does_not_duplicate_rows_or_charges(env):
    engine,S,actor,c,app=env
    a=upload(c)
    with S.begin() as s:
        job=jobs.claim(s,'unit-publisher',authorize_inputs=datasets.authorize_inputs)
        token=job.fence_token
    parsed=parse_dataset(RAW,'orders.csv')
    with S.begin() as s:datasets.publish(s,a['job_id'],token,parsed)
    with S() as s:charge=s.get(WorkspaceQuota,'wa').committed_bytes
    with S.begin() as s:datasets.publish(s,a['job_id'],token,parsed)
    with S() as s:
        assert s.get(WorkspaceQuota,'wa').committed_bytes==charge
        assert s.scalar(select(func.count()).select_from(DatasetRow).where(DatasetRow.dataset_id==a['dataset_id']))==2
    changed=parse_dataset(RAW.replace(b'A,',b'Z,'),'orders.csv')
    with pytest.raises(Problem) as error:
        with S.begin() as s:datasets.publish(s,a['job_id'],token,changed)
    assert error.value.code=='JOB_RESULT_CONFLICT'


def test_second_worker_refused_and_lease_loss_discards_output(env):
    engine,S,actor,c,app=env
    a=upload(c)
    first=ImportWorker(engine,timeout=10,heartbeat=.2,lease_seconds=2,compute_target=slow_compute)
    second=ImportWorker(engine)
    errors=[]
    first.start()
    with pytest.raises(RuntimeError,match='Another executor'):second.start()
    def run():
        try:first.tick()
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with S() as s:state=s.get(Job,a['job_id']).state
            if state=='running':break
            time.sleep(.02)
        with engine.begin() as conn:conn.execute(text('SELECT pg_terminate_backend(:pid)'),{'pid':first.pid})
        thread.join(8)
        assert not thread.is_alive() and errors
        with S.begin() as s:
            s.get(Job,a['job_id']).lease_until=s.scalar(select(func.clock_timestamp()))-timedelta(seconds=1)
        with S.begin() as s:jobs.recover_expired(s,a['job_id'],authorize_inputs=datasets.authorize_inputs)
        with S() as s:
            assert s.get(Job,a['job_id']).state=='interrupted'
            assert s.get(Dataset,a['dataset_id']).metadata_json is None
    finally:first.close();thread.join(5)


def test_new_worker_fences_previous_generation_without_waiting_for_expiry(env):
    engine,S,actor,c,app=env
    a=upload(c)
    with S.begin() as s:
        claimed=jobs.claim(s,'crashed-worker',authorize_inputs=datasets.authorize_inputs)
        oldtoken=claimed.fence_token
    worker=ImportWorker(engine)
    try:
        worker.start()
        with S() as s:assert s.get(Job,a['job_id']).state=='interrupted'
        with S.begin() as s:
            assert datasets.publish(s,a['job_id'],oldtoken,parse_dataset(RAW,'orders.csv')).state=='interrupted'
            assert s.get(Dataset,a['dataset_id']).metadata_json is None
    finally:worker.close()


def test_revoke_before_publication_never_creates_ready_dataset(env):
    engine,S,actor,c,app=env
    a=upload(c)
    with S.begin() as s:
        job=jobs.claim(s,'publishing-worker',authorize_inputs=datasets.authorize_inputs);token=job.fence_token
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['viewer']
    try:
        with S.begin() as s:assert datasets.publish(s,a['job_id'],token,parse_dataset(RAW,'orders.csv')).state=='cancel_requested'
        with S.begin() as s:jobs.acknowledge_cancel(s,a['job_id'],token)
        with S() as s:
            assert s.get(Dataset,a['dataset_id']).metadata_json is None
            assert s.scalar(select(func.count()).select_from(DatasetRow).where(DatasetRow.dataset_id==a['dataset_id']))==0
            assert s.get(WorkspaceQuota,'wa').reserved_bytes==0
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator']


def test_full_queue_rejects_upload_without_orphan_asset_or_quota(env):
    engine,S,actor,c,app=env
    with S.begin() as s:
        capacity=s.get(GlobalCapacity,1)
        oldlimit=capacity.max_pending
        capacity.max_pending=1
    a=upload(c)
    with S() as s:
        count=s.scalar(select(func.count()).select_from(SourceAsset));charge=s.get(WorkspaceQuota,'wa').committed_bytes
    try:
        r=c.post('/api/v1/workspaces/wa/datasets',files={'file':('another.csv',RAW)},headers={'Idempotency-Key':str(uuid.uuid4())})
        assert r.status_code==429,r.text
        with S() as s:
            assert s.scalar(select(func.count()).select_from(SourceAsset))==count
            assert s.get(WorkspaceQuota,'wa').committed_bytes==charge
    finally:
        c.post(f"/api/v1/workspaces/wa/jobs/{a['job_id']}/cancel")
        with S.begin() as s:s.get(GlobalCapacity,1).max_pending=oldlimit


def test_published_source_rows_and_manifest_are_immutable(env):
    engine,S,actor,c,app=env
    a=upload(c);run_worker(engine)
    with pytest.raises(Exception,match='immutable'):
        with engine.begin() as conn:conn.execute(text('UPDATE team_source_assets SET filename=:name WHERE id=:id'),{'name':'changed.csv','id':a['asset_id']})
    with pytest.raises(Exception,match='immutable'):
        with engine.begin() as conn:conn.execute(text('UPDATE team_datasets SET row_count=0 WHERE id=:id'),{'id':a['dataset_id']})


def test_raw_upload_key_conflict_and_wrong_csrf(env):
    engine,S,actor,c,app=env
    key=str(uuid.uuid4());a=upload(c,key=key)
    r=c.post('/api/v1/workspaces/wa/datasets',files={'file':('orders.csv',b'id,amount\nA,99\n')},headers={'Idempotency-Key':key})
    assert r.status_code==409
    r=c.post('/api/v1/workspaces/wa/datasets',files={'file':('orders.csv',RAW)},headers={'X-CSRF-Token':'wrong','Idempotency-Key':str(uuid.uuid4())})
    assert r.status_code==403
    c.post(f"/api/v1/workspaces/wa/jobs/{a['job_id']}/cancel")


def test_commit_ack_lost_after_real_commit_reads_back_manifest(env):
    from contextlib import contextmanager
    from sqlalchemy.exc import DBAPIError
    engine,S,actor,c,app=env
    a=upload(c)
    worker=ImportWorker(engine)
    try:
        worker.start()
        with S.begin() as s:
            job=jobs.claim(s,'ack-loss',authorize_inputs=datasets.authorize_inputs);token=job.fence_token
        original=worker.Session
        class LoseFirstAck:
            used=False
            @contextmanager
            def begin(self):
                with original.begin() as s:yield s
                if not self.used:
                    self.used=True
                    raise DBAPIError('COMMIT',{},ConnectionError('simulated lost acknowledgement'),connection_invalidated=True)
        worker.Session=LoseFirstAck()
        result=worker.publish_with_readback(a['job_id'],token,parse_dataset(RAW,'orders.csv'))
        assert result.state=='succeeded'
        with S() as s:
            assert s.scalar(select(func.count()).select_from(DatasetRow).where(DatasetRow.dataset_id==a['dataset_id']))==2
            assert s.get(WorkspaceQuota,'wa').reserved_bytes==0
    finally:worker.close()
