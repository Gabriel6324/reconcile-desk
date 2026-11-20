"""Real PostgreSQL period configuration, concurrency and immutable history."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.exc import DBAPIError
from api.common import Problem
from team import periods
from team.period_models import CollectionProfile,BusinessPeriod,PeriodRevision,PeriodRun
from team.reconcile_models import CollectionRule,CollectionRun
from team.models import Membership,User,AuditEvent
from team.job_models import Job,GlobalCapacity,WorkspaceQuota
from test_collection import env,ready,publish,submit,BASE
from test_ingest import upload,run_worker,URL

pytestmark=pytest.mark.skipif(not URL,reason='real dedicated PostgreSQL required')
CALENDAR={'name':'2026年10月收款核对','start_date':'2026-10-01','end_date':'2026-10-31',
          'timezone':'Asia/Shanghai','cutoff_at':'2026-11-01T00:00:00+08:00'}


def post(c,path,body,status=200,key=None):
    response=c.post(BASE+path,json=body,headers={'Idempotency-Key':key or str(uuid.uuid4())})
    assert response.status_code==status,response.text
    return response.json()


@pytest.fixture(scope='module')
def profile(env,ready):
    engine,S,actor,c,app=env
    rule=publish(c,ready)
    return post(c,'/collection-profiles',{'source_rule_id':rule['id'],'name':'收款规则 v1'},201)


def input_definition(profile,ready,status='complete'):
    return {'profile_id':profile['id'],'orders_dataset_id':ready['left_dataset_id'],'payments_dataset_id':ready['right_dataset_id'],
        'attestations':{role:{'status':status,'note':'已确认本周期及截止时刻的导出范围。' if status!='not_attested' else ''} for role in ['orders','payments']}}


def create(c):return post(c,'/periods',{'definition':CALENDAR},201)


def revise(c,p,definition):
    return post(c,f'/periods/{p["id"]}/revisions',{'expected_version':p['version'],'definition':definition},201)


def confirm(c,p):
    return post(c,f'/periods/{p["id"]}/confirm',{'expected_version':p['version'],'config_hash':p['config_hash']})


def run(c,p):return post(c,f'/periods/{p["id"]}/runs',{'expected_version':p['version']},202)


def activate(c,p,run_id):
    return post(c,f'/periods/{p["id"]}/activate',{'expected_version':p['version'],'run_id':run_id,'acknowledged_open_cases':p.get('unresolved_case_count',0)})


def test_period_full_flow_keeps_attestation_separate_from_calculation(env,ready,profile):
    engine,S,actor,c,app=env
    p=create(c)
    assert p['source_completeness']=='not_attested' and p['state']=='draft'
    revision=revise(c,p,input_definition(profile,ready));p=revision['period']
    assert p['source_completeness']=='attested_complete'
    assert revision['revision']['config']['attested_by']==actor
    assert revision['revision']['config']['rule_definition']['profile_binding']['profile_id']==profile['id']
    p=confirm(c,p);submitted=run(c,p);p=submitted['period'];rid=submitted['run']['run_id']
    post(c,f'/periods/{p["id"]}/activate',{'expected_version':p['version'],'run_id':rid},409)
    run_worker(engine)
    assert c.get(f'{BASE}/periods/{p["id"]}').json()['active_run_id'] is None
    p=activate(c,p,rid)
    assert p['state']=='reviewing' and not p['active_run_stale'] and not p['business_closed']
    result=c.get(f'{BASE}/collection-runs/{rid}').json()
    assert result['summary']['source_completeness']=='not_attested'
    assert result['summary']['covered_rows']==13
    with S() as s:
        confirmation=s.scalar(select(AuditEvent).where(AuditEvent.resource_id==p['id'],AuditEvent.action=='period.confirm'))
        assert confirmation.actor_id==actor and confirmation.safe_detail['config_hash']==p['config_hash']
    assert c.post(f'{BASE}/periods/{p["id"]}/close',json={}).status_code==422


def test_profile_rebinds_names_to_new_column_order(env,ready,profile):
    engine,S,actor,c,app=env
    orders=upload(c,b'amount,event,order\n10,OA,A\n','new-orders.csv')
    payments=upload(c,b'event,order,amount\nPA,A,10\n','new-payments.csv')
    run_worker(engine);run_worker(engine)
    body=input_definition(profile,ready)
    body.update(orders_dataset_id=orders['dataset_id'],payments_dataset_id=payments['dataset_id'])
    revision=revise(c,create(c),body)
    config=revision['revision']['config']['rule_definition']
    assert config['left_mapping']['amount_column']=='c1' and config['right_mapping']['key_columns']==['c2']
    submitted=run(c,confirm(c,revision['period']));run_worker(engine)
    info=c.get(f'{BASE}/collection-runs/{submitted["run"]["run_id"]}').json()
    assert info['summary']['counts']['exact_equal']==1
    stored=c.get(f'{BASE}/collection-profiles/{profile["id"]}').json()
    assert stored==profile and 'dataset_id' not in str(stored['definition'])


def test_revision_invalidates_confirmation_and_marks_active_history(env,ready,profile):
    engine,S,actor,c,app=env
    p=confirm(c,revise(c,create(c),input_definition(profile,ready))['period'])
    first=run(c,p);run_worker(engine);p=activate(c,first['period'],first['run']['run_id'])
    snapshot=c.get(f'{BASE}/collection-runs/{first["run"]["run_id"]}').json()
    change=input_definition(profile,ready,'incomplete');r=revise(c,p,change);p=r['period']
    assert p['state']=='draft' and p['active_run_stale'] and p['active_run_id']==first['run']['run_id']
    post(c,f'/periods/{p["id"]}/runs',{'expected_version':p['version']},409)
    p=confirm(c,p)
    post(c,f'/periods/{p["id"]}/activate',{'expected_version':p['version'],'run_id':first['run']['run_id']},409)
    second=run(c,p);run_worker(engine);p=activate(c,second['period'],second['run']['run_id'])
    assert p['source_completeness']=='incomplete' and not p['active_run_stale']
    assert c.get(f'{BASE}/collection-runs/{first["run"]["run_id"]}').json()==snapshot
    links=c.get(f'{BASE}/periods/{p["id"]}/runs').json()['items']
    assert next(x for x in links if x['id']==first['run']['run_id'])['stale']
    assert len(c.get(f'{BASE}/periods/{p["id"]}/revisions').json()['items'])==2


def test_late_old_job_finishes_without_replacing_current_revision(env,ready,profile):
    engine,S,actor,c,app=env
    p=confirm(c,revise(c,create(c),input_definition(profile,ready))['period'])
    old=run(c,p)
    changed=input_definition(profile,ready,'not_attested')
    p=confirm(c,revise(c,old['period'],changed)['period'])
    newer=run(c,p)
    run_worker(engine);run_worker(engine)
    p=activate(c,newer['period'],newer['run']['run_id'])
    post(c,f'/periods/{p["id"]}/activate',{'expected_version':p['version'],'run_id':old['run']['run_id']},409)
    assert c.get(f'{BASE}/collection-runs/{old["run"]["run_id"]}').json()['state']=='succeeded'
    assert c.get(f'{BASE}/periods/{p["id"]}').json()['active_run_id']==newer['run']['run_id']


def test_cas_wrong_hash_and_receipt_replay_after_edits(env,ready,profile):
    engine,S,actor,c,app=env
    key=str(uuid.uuid4());p=post(c,'/periods',{'definition':CALENDAR},201,key)
    assert post(c,'/periods',{'definition':CALENDAR},201,key)==p
    r=revise(c,p,input_definition(profile,ready));p=r['period'];path=f'/periods/{p["id"]}/confirm'
    post(c,path,{'expected_version':p['version']-1,'config_hash':p['config_hash']},409)
    post(c,path,{'expected_version':p['version'],'config_hash':'f'*64},409)
    key=str(uuid.uuid4());body={'expected_version':p['version'],'config_hash':p['config_hash']}
    confirmed=post(c,path,body,key=key)
    updated=revise(c,confirmed,input_definition(profile,ready,'incomplete'))['period']
    assert post(c,path,body,key=key)==confirmed
    assert c.get(f'{BASE}/periods/{p["id"]}').json()==updated
    post(c,path,{**body,'config_hash':updated['config_hash']},409,key)


def test_standalone_and_cross_period_runs_cannot_be_activated(env,ready,profile):
    engine,S,actor,c,app=env
    revision=revise(c,create(c),input_definition(profile,ready));p=confirm(c,revision['period'])
    standalone=submit(c,revision['revision']['rule_id']);run_worker(engine)
    post(c,f'/periods/{p["id"]}/activate',{'expected_version':p['version'],'run_id':standalone['run_id']},404)
    submitted=run(c,p);run_worker(engine)
    other=confirm(c,revise(c,create(c),input_definition(profile,ready))['period'])
    post(c,f'/periods/{other["id"]}/activate',{'expected_version':other['version'],'run_id':submitted['run']['run_id']},404)
    assert c.get(f'/api/v1/workspaces/wb/periods/{p["id"]}').status_code==404


def test_operator_reuses_policy_but_cannot_publish_or_change_mapping(env,ready,profile):
    engine,S,actor,c,app=env
    p=create(c)
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['operator']
    try:
        post(c,'/collection-profiles',{'source_rule_id':profile['source_rule_id'],'name':'forbidden'},403)
        bad=input_definition(profile,ready);bad['left_mapping']={'amount_column':'order'}
        post(c,f'/periods/{p["id"]}/revisions',{'expected_version':p['version'],'definition':bad},422)
        revision=revise(c,p,input_definition(profile,ready))
        assert revision['revision']['config']['rule_definition']['rule']==profile['definition']['rule']
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator']


def test_revoked_permission_denies_replay_and_viewer_omits_notes(env,ready,profile):
    engine,S,actor,c,app=env
    key=str(uuid.uuid4());body={'definition':CALENDAR};p=post(c,'/periods',body,201,key)
    p=revise(c,p,input_definition(profile,ready))['period']
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['viewer']
    try:
        post(c,'/periods',body,403,key)
        summary=c.get(f'{BASE}/periods/{p["id"]}')
        assert summary.status_code==200 and 'attestations' not in summary.text and '已确认本周期' not in summary.text
        assert c.get(f'{BASE}/periods/{p["id"]}/revisions').status_code==403
        assert c.get(f'{BASE}/collection-profiles/{profile["id"]}').status_code==403
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator']


def test_queue_full_rolls_back_period_submit_and_link(env,ready,profile):
    engine,S,actor,c,app=env
    p=confirm(c,revise(c,create(c),input_definition(profile,ready))['period'])
    with S.begin() as s:cap=s.get(GlobalCapacity,1);old=cap.max_pending;cap.max_pending=1
    queued=run(c,p);p=queued['period']
    with S() as s:
        count=s.scalar(select(func.count()).select_from(CollectionRun));quota=s.get(WorkspaceQuota,'wa').committed_bytes
    try:
        post(c,f'/periods/{p["id"]}/runs',{'expected_version':p['version']},429)
        assert c.get(f'{BASE}/periods/{p["id"]}').json()==p
        with S() as s:
            assert s.scalar(select(func.count()).select_from(CollectionRun))==count
            assert s.scalar(select(func.count()).select_from(PeriodRun).where(PeriodRun.period_id==p['id']))==1
            assert s.get(WorkspaceQuota,'wa').committed_bytes==quota
    finally:
        c.post(f'{BASE}/jobs/{queued["run"]["job_id"]}/cancel')
        with S.begin() as s:s.get(GlobalCapacity,1).max_pending=old


def test_revision_quota_failure_rolls_back_derived_rule(env,ready,profile):
    engine,S,actor,c,app=env
    p=create(c)
    with S.begin() as s:
        q=s.get(WorkspaceQuota,'wa');old=q.limit_bytes;q.limit_bytes=q.committed_bytes+q.reserved_bytes+20000
        count=s.scalar(select(func.count()).select_from(CollectionRule))
    try:
        post(c,f'/periods/{p["id"]}/revisions',{'expected_version':p['version'],'definition':input_definition(profile,ready)},507)
        with S() as s:
            assert s.scalar(select(func.count()).select_from(CollectionRule))==count
            assert s.get(BusinessPeriod,p['id']).version==1
    finally:
        with S.begin() as s:s.get(WorkspaceQuota,'wa').limit_bytes=old


def test_two_operators_cannot_overwrite_each_others_revision(env,ready,profile):
    engine,S,actor,c,app=env
    p=create(c);second=str(uuid.uuid4())
    with S.begin() as s:
        s.add(User(id=second,login='parallel.'+second,password_hash=s.get(User,actor).password_hash));s.flush()
        s.add(Membership(workspace_id='wa',user_id=second,roles=['operator']))
    barrier=threading.Barrier(2)
    def edit(who):
        barrier.wait(timeout=5)
        try:
            with S.begin() as s:
                periods.revise_inputs(s,'wa',who,p['id'],p['version'],input_definition(profile,ready),str(uuid.uuid4()))
            return 'saved'
        except Problem as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(edit,[actor,second]))
    assert sorted(results)==['VERSION_CONFLICT','saved']
    with S() as s:
        assert s.get(BusinessPeriod,p['id']).version==2
        assert s.scalar(select(func.count()).select_from(PeriodRevision).where(PeriodRevision.period_id==p['id']))==1


def test_database_prevents_mutation_and_null_confirmation_state(env,ready,profile):
    engine,S,actor,c,app=env
    p=create(c)
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:conn.execute(text("UPDATE team_business_periods SET state='ready',version=version+1 WHERE id=:id"),{'id':p['id']})
    revision=revise(c,p,input_definition(profile,ready))
    for table,ident in [('team_collection_profiles',profile['id']),('team_period_revisions',revision['revision']['id'])]:
        with pytest.raises(DBAPIError,match='immutable'):
            with engine.begin() as conn:conn.execute(text(f'DELETE FROM {table} WHERE id=:id'),{'id':ident})
    with pytest.raises(DBAPIError,match='immutable'):
        with engine.begin() as conn:conn.execute(text("UPDATE team_business_periods SET definition_hash='changed',version=version+1 WHERE id=:id"),{'id':p['id']})


def test_missing_role_and_cross_workspace_inputs_leave_draft(env,ready,profile):
    engine,S,actor,c,app=env
    p=create(c);body=input_definition(profile,ready);body.pop('payments_dataset_id')
    post(c,f'/periods/{p["id"]}/revisions',{'expected_version':p['version'],'definition':body},422)
    response=c.post('/api/v1/workspaces/wb/periods',json={'definition':CALENDAR},headers={'Idempotency-Key':str(uuid.uuid4())})
    other=response.json()
    response=c.post(f'/api/v1/workspaces/wb/periods/{other["id"]}/revisions',json={'expected_version':1,'definition':input_definition(profile,ready)},headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==404
    assert c.get(f'{BASE}/periods/{p["id"]}').json()['current_revision_id'] is None


@pytest.mark.parametrize('value',[None,{},[],0,True,'','x'*37])
def test_invalid_profile_ids_are_user_errors_not_database_outages(env,ready,profile,value):
    engine,S,actor,c,app=env
    p=create(c);body=input_definition(profile,ready);body['profile_id']=value
    result=post(c,f'/periods/{p["id"]}/revisions',{'expected_version':p['version'],'definition':body},422)
    assert result['code']=='INVALID_PROFILE_ID'
