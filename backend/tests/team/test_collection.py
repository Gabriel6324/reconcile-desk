"""Published mapping -> durable worker -> immutable result and source evidence."""
from copy import deepcopy
from contextlib import contextmanager
import uuid

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError

from api.common import Problem
from core.business import reconcile_groups
from core.mapping import map_rows
from team import jobs, dispatch, reconciliations
from team.job_models import Job, WorkspaceQuota, QuotaLedger, GlobalCapacity
from team.models import Membership
from team.reconcile_models import CollectionRule, CollectionRun, ResultGroup, RowCoverage
from team.worker import ImportWorker
from test_ingest import env, run_worker, upload, URL

pytestmark=pytest.mark.skipif(not URL,reason='real dedicated PostgreSQL required')
BASE='/api/v1/workspaces/wa'
LEFT=b'order,event,amount\nA,OA,10\nB,OB,20\nC,OC,\nD,DUP,1\nD,DUP,1\nE,OE,9007199254740993.01\n'
RIGHT=b'order,event,amount\nA,PA1,6\nA,PA2,4\nB,PB,19\nC,PC,0\nD,PD,2\nE,PE,9007199254740993.01\nF,PF,5\n'


def definition(left,right,tolerance='0'):
    mapping={'key_columns':['order'],'event_id_column':'event','amount_column':'amount',
             'currency':{'constant':'CNY'},'source_namespace':'orders'}
    return {'name':'订单与收款确认规则','left_dataset_id':left,'right_dataset_id':right,
            'left_mapping':mapping,'right_mapping':{**mapping,'source_namespace':'payments'},
            'rule':{'stage_id':'collection','aggregation':'1:N','tolerance':tolerance}}


@pytest.fixture(scope='module')
def ready(env):
    engine,S,actor,c,app=env
    left=upload(c,LEFT,'orders.csv');right=upload(c,RIGHT,'payments.csv')
    run_worker(engine);run_worker(engine)
    return definition(left['dataset_id'],right['dataset_id'])


def publish(c,body,key=None):
    r=c.post(BASE+'/collection-rules',json={'definition':body},headers={'Idempotency-Key':key or str(uuid.uuid4())})
    assert r.status_code==201,r.text
    return r.json()


def submit(c,rule_id,key=None):
    r=c.post(BASE+'/collection-runs',json={'rule_id':rule_id},headers={'Idempotency-Key':key or str(uuid.uuid4())})
    assert r.status_code==202,r.text
    return r.json()


def computed(payload):
    left,right=payload['left'],payload['right']
    result=reconcile_groups(map_rows(left['dataset_id'],left['rows'],left['mapping']),
                            map_rows(right['dataset_id'],right['rows'],right['mapping']),payload['rule'])
    return {'run_id':payload['run_id'],'rule_hash':payload['rule_hash'],'input_hash':payload['input_hash'],'result':result}


def claim(S):
    with S.begin() as s:
        job=jobs.claim(s,'collection-test',authorize_inputs=dispatch.authorize_inputs)
        return job.id,job.fence_token,dispatch.input_for_job(s,job)


def test_end_to_end_exact_money_coverage_and_source_pages(env,ready):
    engine,S,actor,c,app=env
    rule=publish(c,ready);run=submit(c,rule['id']);rid=run['run_id']
    assert c.get(f'{BASE}/collection-runs/{rid}/groups').status_code==409
    assert run_worker(engine)
    info=c.get(f'{BASE}/collection-runs/{rid}').json()
    assert info['state']=='succeeded',info
    page=c.get(f'{BASE}/collection-runs/{rid}/groups',params={'limit':2}).json()
    assert len(page['items'])==2 and page['next_cursor'] is not None
    groups=[]
    after=0
    while True:
        page=c.get(f'{BASE}/collection-runs/{rid}/groups',params={'limit':2,'after':after}).json()
        groups.extend(page['items'])
        if page['next_cursor'] is None:break
        after=page['next_cursor']
    bykey={g['key'][0]:g for g in groups}
    assert {k:g['status'] for k,g in bykey.items()}=={'A':'exact_equal','B':'amount_difference','C':'invalid_value','D':'duplicate_event','E':'exact_equal','F':'missing_counterpart'}
    assert bykey['E']['observed_amount']=='9007199254740993.01'
    assert bykey['C']['expected_amount'] is None
    gid=bykey['A']['id']
    rows=c.get(f'{BASE}/collection-runs/{rid}/groups/{gid}/rows',params={'side':'right','limit':1}).json()
    assert rows['items'][0]['source']['row']==2 and rows['next_cursor']==1
    nextrows=c.get(f'{BASE}/collection-runs/{rid}/groups/{gid}/rows',params={'side':'right','after':rows['next_cursor']}).json()
    assert nextrows['items'][0]['source']['row']==3 and nextrows['next_cursor'] is None
    assert c.get(f'/api/v1/workspaces/wb/collection-runs/{rid}').status_code==404
    assert c.get(f'/api/v1/workspaces/wb/collection-rules/{rule["id"]}').status_code==404
    with S() as s:
        result=s.get(CollectionRun,rid)
        assert result.summary_json['covered_rows']==13 and result.summary_json['source_completeness']=='not_attested'
        assert s.scalar(select(func.count()).select_from(RowCoverage).where(RowCoverage.run_id==rid))==13
        assert s.get(WorkspaceQuota,'wa').reserved_bytes==0


def test_idempotency_conflicts_and_repeat_run_keeps_prior_snapshot(env,ready):
    engine,S,actor,c,app=env
    key=str(uuid.uuid4());rule=publish(c,ready,key)
    assert publish(c,ready,key)==rule
    changed=deepcopy(ready);changed['rule']['tolerance']='1'
    assert c.post(BASE+'/collection-rules',json={'definition':changed},headers={'Idempotency-Key':key}).status_code==409
    runkey=str(uuid.uuid4());a=submit(c,rule['id'],runkey)
    assert submit(c,rule['id'],runkey)==a
    run_worker(engine)
    original=c.get(f'{BASE}/collection-runs/{a["run_id"]}/groups').json()
    newrule=publish(c,changed);b=submit(c,newrule['id']);run_worker(engine)
    newgroups=c.get(f'{BASE}/collection-runs/{b["run_id"]}/groups').json()['items']
    assert next(g for g in newgroups if g['key']==['B'])['status']=='within_tolerance'
    assert c.get(f'{BASE}/collection-runs/{a["run_id"]}/groups').json()==original
    assert c.post(BASE+'/collection-runs',json={'rule_id':newrule['id']},headers={'Idempotency-Key':runkey}).status_code==409
    repeated=submit(c,rule['id']);run_worker(engine)
    assert repeated['run_id']!=a['run_id']
    assert c.get(f'{BASE}/collection-runs/{repeated["run_id"]}/groups').json()==original


def test_unready_cross_workspace_and_self_comparison_rejected(env,ready):
    engine,S,actor,c,app=env
    pending=upload(c,LEFT,'pending.csv');body=deepcopy(ready);body['left_dataset_id']=pending['dataset_id']
    assert c.post(BASE+'/collection-rules',json={'definition':body},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==409
    c.post(f'{BASE}/jobs/{pending["job_id"]}/cancel')
    assert c.post('/api/v1/workspaces/wb/collection-rules',json={'definition':ready},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==404
    body=deepcopy(ready);body['right_dataset_id']=body['left_dataset_id']
    assert c.post(BASE+'/collection-rules',json={'definition':body},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==422


@pytest.mark.parametrize('change',[{'left_mapping':{'unknown':'field'}},{'rule':{'stage_id':'refund','aggregation':'1:N','tolerance':'0'}},{'rule':{'stage_id':'collection','aggregation':'N:N','tolerance':'0'}}])
def test_bad_mapping_and_unsupported_stage_rejected_before_job(env,ready,change):
    engine,S,actor,c,app=env
    with S() as s:count=s.scalar(select(func.count()).select_from(CollectionRule))
    r=c.post(BASE+'/collection-rules',json={'definition':{**ready,**change}},headers={'Idempotency-Key':str(uuid.uuid4())})
    assert r.status_code==422,r.text
    with S() as s:assert s.scalar(select(func.count()).select_from(CollectionRule))==count


def test_permission_rechecked_before_idempotent_replay_and_publish(env,ready):
    engine,S,actor,c,app=env
    key=str(uuid.uuid4());rule=publish(c,ready,key);run=submit(c,rule['id']);jid,token,payload=claim(S)
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['viewer']
    try:
        assert c.post(BASE+'/collection-rules',json={'definition':ready},headers={'Idempotency-Key':key}).status_code==403
        assert c.post(BASE+'/collection-runs',json={'rule_id':rule['id']},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==403
        with S.begin() as s:assert reconciliations.publish(s,jid,token,computed(payload)).state=='cancel_requested'
        with S.begin() as s:jobs.acknowledge_cancel(s,jid,token)
        with S() as s:
            assert s.get(CollectionRun,run['run_id']).result_hash is None
            assert s.scalar(select(func.count()).select_from(ResultGroup).where(ResultGroup.run_id==run['run_id']))==0
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator']


def test_viewer_cannot_read_original_rows_or_confirm_rule(env,ready):
    engine,S,actor,c,app=env
    rule=publish(c,ready);run=submit(c,rule['id']);run_worker(engine)
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['viewer']
    try:
        groups=c.get(f'{BASE}/collection-runs/{run["run_id"]}/groups')
        assert groups.status_code==200
        gid=groups.json()['items'][0]['id']
        assert c.get(f'{BASE}/collection-runs/{run["run_id"]}/groups/{gid}/rows',params={'side':'left'}).status_code==403
        assert c.get(f'{BASE}/collection-rules/{rule["id"]}').status_code==403
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator']


def test_commit_ack_loss_readback_settles_once_and_evidence_is_immutable(env,ready):
    engine,S,actor,c,app=env
    rule=publish(c,ready);run=submit(c,rule['id']);worker=ImportWorker(engine)
    try:
        worker.start();jid,token,payload=claim(S);result=computed(payload);original=worker.Session
        class LoseFirstAck:
            used=False
            @contextmanager
            def begin(self):
                with original.begin() as s:yield s
                if not self.used:
                    self.used=True
                    raise DBAPIError('COMMIT',{},ConnectionError('lost ack'),connection_invalidated=True)
        worker.Session=LoseFirstAck()
        assert worker.publish_with_readback(jid,token,result).state=='succeeded'
        with S() as s:
            assert s.scalar(select(func.count()).select_from(RowCoverage).where(RowCoverage.run_id==run['run_id']))==13
            assert s.scalar(select(func.count()).select_from(QuotaLedger).where(QuotaLedger.job_id==jid,QuotaLedger.event=='settle'))==1
        for table,column,value,ident in [('team_collection_rules','name','changed',rule['id']),('team_collection_runs','result_hash','bad',run['run_id'])]:
            with pytest.raises(DBAPIError,match='immutable'):
                with engine.begin() as conn:conn.execute(text(f'UPDATE {table} SET {column}=:value WHERE id=:id'),{'value':value,'id':ident})
        with pytest.raises(DBAPIError,match='immutable'):
            with engine.begin() as conn:conn.execute(text('DELETE FROM team_collection_groups WHERE run_id=:id'),{'id':run['run_id']})
    finally:worker.close()


def test_corrupt_provenance_cannot_publish_partial_results(env,ready):
    engine,S,actor,c,app=env
    rule=publish(c,ready);run=submit(c,rule['id']);jid,token,payload=claim(S);result=computed(payload)
    result['result']['coverage'][0]['row_id']='foreign:r1'
    with pytest.raises(Problem):
        with S.begin() as s:reconciliations.publish(s,jid,token,result)
    with S() as s:
        assert s.get(Job,jid).state=='running'
        assert s.scalar(select(func.count()).select_from(ResultGroup).where(ResultGroup.run_id==run['run_id']))==0
    with S.begin() as s:jobs.fail(s,jid,token,'INVALID_RESULT',authorize_inputs=dispatch.authorize_inputs)


def test_full_queue_rolls_back_run_without_orphan(env,ready):
    engine,S,actor,c,app=env
    rule=publish(c,ready)
    with S.begin() as s:
        capacity=s.get(GlobalCapacity,1);old=capacity.max_pending;capacity.max_pending=1
    queued=submit(c,rule['id'])
    with S() as s:count=s.scalar(select(func.count()).select_from(CollectionRun))
    try:
        r=c.post(BASE+'/collection-runs',json={'rule_id':rule['id']},headers={'Idempotency-Key':str(uuid.uuid4())})
        assert r.status_code==429,r.text
        with S() as s:assert s.scalar(select(func.count()).select_from(CollectionRun))==count
    finally:
        c.post(f'{BASE}/jobs/{queued["job_id"]}/cancel')
        with S.begin() as s:s.get(GlobalCapacity,1).max_pending=old


def test_input_failure_settled_instead_of_blocking_queue(env,ready,monkeypatch):
    engine,S,actor,c,app=env
    rule=publish(c,ready);run=submit(c,rule['id'])
    def bad_input(db,job):raise Problem('BUSINESS_INPUT_LIMIT','bounded input')
    monkeypatch.setattr(reconciliations,'input_for_job',bad_input)
    assert run_worker(engine)
    with S() as s:
        assert s.get(Job,run['job_id']).state=='failed'
        assert s.get(Job,run['job_id']).error_code=='BUSINESS_INPUT_LIMIT'
        assert s.get(WorkspaceQuota,'wa').reserved_bytes==0


def test_large_anomaly_group_remains_browsable(env):
    engine,S,actor,c,app=env
    # Whole group exceeds the API response ceiling before summary projection.
    raw=b'order,event,amount\n'+b'A,,invalid\n'*10000
    left=upload(c,raw,'many-orders.csv');right=upload(c,raw,'many-payments.csv')
    run_worker(engine);run_worker(engine)
    rule=publish(c,definition(left['dataset_id'],right['dataset_id']))
    run=submit(c,rule['id']);run_worker(engine)
    rid=run['run_id'];response=c.get(f'{BASE}/collection-runs/{rid}/groups')
    assert response.status_code==200,response.text
    group=response.json()['items'][0]
    assert group['left_row_count']==group['right_row_count']==10000
    assert group['issue_count']==20000 and len(group['issues_preview'])==5
    assert 'left_rows' not in group and 'issues' not in group and len(response.content)<10000
    gid=group['id']
    issues=c.get(f'{BASE}/collection-runs/{rid}/groups/{gid}/issues',params={'after':19990}).json()
    assert len(issues['items'])==10 and issues['next_cursor'] is None and issues['total_count']==20000
    rows=c.get(f'{BASE}/collection-runs/{rid}/groups/{gid}/rows',params={'side':'left','after':9998}).json()
    assert len(rows['items'])==2 and rows['items'][-1]['source']['row']==10001


def test_worker_spawn_failure_settles_reservation(env,ready,monkeypatch):
    import team.worker as module
    engine,S,actor,c,app=env
    rule=publish(c,ready);run=submit(c,rule['id'])
    class BrokenProcess:
        pid=None
        def start(self):raise OSError('resource unavailable')
    class Context:
        def Process(self,**kwargs):return BrokenProcess()
    monkeypatch.setattr(module.multiprocessing,'get_context',lambda method:Context())
    assert run_worker(engine)
    with S() as s:
        assert s.get(Job,run['job_id']).error_code=='COMPUTE_START_FAILED'
        assert s.get(WorkspaceQuota,'wa').reserved_bytes==0


def test_rule_quota_failure_atomic_and_queued_revocation(env,ready):
    engine,S,actor,c,app=env
    with S.begin() as s:
        q=s.get(WorkspaceQuota,'wa');oldlimit=q.limit_bytes;q.limit_bytes=q.committed_bytes+q.reserved_bytes
        count=s.scalar(select(func.count()).select_from(CollectionRule))
    try:
        r=c.post(BASE+'/collection-rules',json={'definition':ready},headers={'Idempotency-Key':str(uuid.uuid4())})
        assert r.status_code==507,r.text
        with S() as s:assert s.scalar(select(func.count()).select_from(CollectionRule))==count
    finally:
        with S.begin() as s:s.get(WorkspaceQuota,'wa').limit_bytes=oldlimit
    rule=publish(c,ready);run=submit(c,rule['id'])
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['viewer']
    try:
        run_worker(engine)
        with S() as s:
            assert s.get(Job,run['job_id']).state=='cancelled'
            assert s.get(CollectionRun,run['run_id']).result_hash is None
            assert s.get(WorkspaceQuota,'wa').reserved_bytes==0
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator']

