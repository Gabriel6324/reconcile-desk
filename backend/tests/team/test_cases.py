from copy import deepcopy
import uuid
import pytest
from sqlalchemy import select,text,func
from sqlalchemy.exc import DBAPIError
from api.common import Problem
from team import cases,periods
from team.case_models import DifferenceCase,CaseEvent,CaseBatch
from team.models import User,Membership
from team.period_models import BusinessPeriod
from team.reconcile_models import RowCoverage,CollectionRun
from test_periods import env,ready,profile,input_definition,create,revise,confirm,run,activate,BASE
from test_ingest import run_worker,URL

pytestmark=pytest.mark.skipif(not URL,reason='isolated PostgreSQL required')

@pytest.fixture(scope='module')
def reviewers(env):
    engine,S,actor,c,app=env
    reviewer=str(uuid.uuid4())
    with S.begin() as s:
        s.add(User(id=reviewer,login='review.'+reviewer,password_hash=s.get(User,actor).password_hash));s.flush()
        s.add(Membership(workspace_id='wa',user_id=reviewer,roles=['reviewer']))
        s.get(Membership,('wa',actor)).roles=['workspace_manager','operator','reviewer']
    return reviewer

def live(env,ready,profile,status='complete'):
    engine,S,actor,c,app=env
    p=confirm(c,revise(c,create(c),input_definition(profile,ready,status))['period'])
    result=run(c,p);run_worker(engine);p=activate(c,result['period'],result['run']['run_id'])
    with S() as s:ids=list(s.scalars(select(DifferenceCase.id).where(DifferenceCase.run_id==p['active_run_id']).order_by(DifferenceCase.id)))
    return p,ids

def action(S,actor,ident,verb,payload=None,key=None):
    with S.begin() as s:
        item=s.get(DifferenceCase,ident);p=s.get(BusinessPeriod,item.period_id)
        return cases.mutate(s,'wa',actor,ident,item.version,p.version,verb,payload or {},key or str(uuid.uuid4()))

def resolution(S,ident,code='accepted_difference'):
    with S() as s:
        case=s.get(DifferenceCase,ident)
        ref=s.scalar(select(RowCoverage).where(RowCoverage.run_id==case.run_id,RowCoverage.group_id==case.group_id))
        return {'code':code,'text':'已经核实来源记录，保留原始计算差额并记录业务处理。','evidence':[{'dataset_id':ref.dataset_id,'row_id':ref.row_id}]}

def test_review_close_reopen_keeps_money_immutable(env,ready,profile,reviewers):
    engine,S,actor,c,app=env;p,ids=live(env,ready,profile)
    assert len(ids)==4 and p['unresolved_case_count']==4
    with S() as s:original=deepcopy(s.get(CollectionRun,p['active_run_id']).summary_json)
    with pytest.raises(Problem):
        with S.begin() as s:periods.close_period(s,'wa',reviewers,p['id'],p['version'],'全部处理',str(uuid.uuid4()))
    for ident in ids:
        action(S,actor,ident,'claim');action(S,actor,ident,'submit',resolution(S,ident))
        with pytest.raises(Problem) as error:action(S,actor,ident,'approve',{'reason':'自己审批'})
        assert error.value.status==403
        action(S,reviewers,ident,'approve',{'reason':'独立复核来源与处置说明，批准。'})
    with S.begin() as s:
        p=periods.period_view(s,s.get(BusinessPeriod,p['id']))
        closed=periods.close_period(s,'wa',reviewers,p['id'],p['version'],'本期已完成独立复核',str(uuid.uuid4()))
        assert closed['business_closed']
        assert s.get(CollectionRun,p['active_run_id']).summary_json==original
    from team import reports
    from openpyxl import load_workbook
    import io
    with S.begin() as s:
        report=reports.create(s,'wa',actor,p['id'],closed['version'],str(uuid.uuid4()))
        frozen=s.get(reports.PeriodReport,report['id']).content
        assert load_workbook(io.BytesIO(frozen)).sheetnames==['周期快照','核对分组','独立复核']
    reopened=action(S,reviewers,ids[0],'reopen',{'reason':'发现新证据，重新检查'})
    assert reopened['period_reopened'] and not reopened['period']['business_closed']
    with S() as s:assert s.get(reports.PeriodReport,report['id']).content==frozen
    with pytest.raises(DBAPIError,match='immutable'):
        with engine.begin() as conn:conn.execute(text('DELETE FROM team_case_events WHERE case_id=:id'),{'id':ids[0]})

def test_followup_rejection_withdraw_and_out_of_scope_evidence(env,ready,profile,reviewers):
    engine,S,actor,c,app=env;p,ids=live(env,ready,profile);ident=ids[0]
    action(S,actor,ident,'claim')
    bad=resolution(S,ident);bad['evidence'][0]['dataset_id']='foreign'
    with pytest.raises(Problem):action(S,actor,ident,'submit',bad)
    action(S,actor,ident,'submit',resolution(S,ident,'needs_external_followup'))
    with pytest.raises(Problem):action(S,reviewers,ident,'approve',{'reason':'仍需外部确认'})
    action(S,reviewers,ident,'reject',{'reason':'请完成外部跟进后重新提交'})
    action(S,actor,ident,'submit',resolution(S,ident))
    action(S,actor,ident,'withdraw',{'reason':'补充说明'})
    with S() as s:assert s.get(DifferenceCase,ident).state=='in_progress'

def test_history_and_incomplete_source_block_close(env,ready,profile,reviewers):
    engine,S,actor,c,app=env;p,ids=live(env,ready,profile,'incomplete')
    for ident in ids:
        action(S,actor,ident,'claim');action(S,actor,ident,'submit',resolution(S,ident));action(S,reviewers,ident,'approve',{'reason':'复核通过'})
    with S.begin() as s:
        current=s.get(BusinessPeriod,p['id'])
        with pytest.raises(Problem) as error:periods.close_period(s,'wa',reviewers,current.id,current.version,'关闭',str(uuid.uuid4()))
        assert error.value.code=='INCOMPLETE_SOURCE'
    p=c.get(f'{BASE}/periods/{p["id"]}').json()
    revision=revise(c,p,input_definition(profile,ready))
    with pytest.raises(Problem) as error:action(S,reviewers,ids[0],'reopen',{'reason':'历史案件'})
    assert error.value.code=='CASE_NOT_CURRENT'

def test_case_cas_receipt_and_viewer_detail(env,ready,profile,reviewers):
    engine,S,actor,c,app=env;p,ids=live(env,ready,profile);ident=ids[0];key=str(uuid.uuid4())
    body={'expected_case_version':1,'expected_period_version':p['version'],'payload':{}}
    a=c.post(f'{BASE}/cases/{ident}/claim',json=body,headers={'Idempotency-Key':key});assert a.status_code==200,a.text
    assert c.post(f'{BASE}/cases/{ident}/claim',json=body,headers={'Idempotency-Key':key}).json()==a.json()
    b=c.post(f'{BASE}/cases/{ident}/claim',json=body,headers={'Idempotency-Key':str(uuid.uuid4())});assert b.status_code==409
    with S.begin() as s:s.get(Membership,('wa',actor)).roles=['viewer']
    try:
        assert c.get(f'{BASE}/cases/{ident}').status_code==403
        assert c.get(f'{BASE}/periods/{p["id"]}/cases').status_code==200
        assert c.post(f'{BASE}/cases/{ident}/claim',json=body,headers={'Idempotency-Key':key}).status_code==403
    finally:
        with S.begin() as s:s.get(Membership,('wa',actor)).roles=['workspace_manager','operator','reviewer']
