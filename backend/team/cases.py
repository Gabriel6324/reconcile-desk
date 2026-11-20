"""Independent case review on frozen result groups, never edits money results.

Caller owns the transaction. Publication includes case rows in the same result
transaction. User actions lock identity -> capacity/quota -> period -> case, and
advance both workflow versions; close/activate races therefore serialize.
"""
from sqlalchemy import select,func
from api.common import Problem,digest,encoded
from api.db import new_id
from core.case_policy import CASE_POLICY_VERSION,needs_case,validate_resolution,validate_reason,transition
from core.errors import CoreError
from .case_models import CaseBatch,DifferenceCase,CaseEvent
from .period_models import PeriodRun
from .reconcile_models import RowCoverage
from .security import authorize_workspace,workspace_permissions
from . import periods

INITIAL_CASE_BYTES=3072
BATCH_BYTES=4096
ACTION_PERMISSIONS={'claim':'cases.claim','submit':'cases.work','withdraw':'cases.work',
                    'approve':'cases.review','reject':'cases.review'}


def _bad(code,message,status=422):raise Problem(code,message,status)


def _core(fn,*args):
    try:return fn(*args)
    except CoreError as exc:
        status=409 if exc.code in ('CASE_STATE_CONFLICT','CASE_ALREADY_ASSIGNED','CASE_EXTERNAL_FOLLOWUP_REQUIRED') else 403 if exc.code in ('CASE_ASSIGNEE_REQUIRED','CASE_SUBMITTER_REQUIRED','CASE_INDEPENDENT_REVIEW_REQUIRED') else 422
        raise Problem(exc.code,exc.message,status,exc.details) from None


def storage_charge(groups):
    """Initial batch, case identities and created events; mutable actions charge separately."""
    if not isinstance(groups,list) or any(not isinstance(g,dict) for g in groups):
        _bad('INVALID_CASE_GROUPS','结果分组无效。')
    return BATCH_BYTES+INITIAL_CASE_BYTES*sum(_core(needs_case,g.get('status')) for g in groups)


def publish_cases(db,run,groups):
    """Called after groups exist, inside the atomic publication transaction.

    A standalone run has no period linkage and no case batch. Existing batches
    are read back without replacing IDs, dispositions or history. The caller
    includes storage_charge(groups) in its external retained-byte settlement.
    """
    link=db.scalar(select(PeriodRun).where(PeriodRun.workspace_id==run.workspace_id,PeriodRun.run_id==run.id))
    if link is None:return None
    charge=storage_charge(groups)
    wanted=[group for group in groups if _core(needs_case,group.get('status'))]
    prior=db.get(CaseBatch,run.id)
    if prior is not None:
        if (prior.workspace_id!=run.workspace_id or prior.period_id!=link.period_id
                or prior.policy_version!=CASE_POLICY_VERSION or prior.total_count!=len(wanted)):
            _bad('CASE_BATCH_CONFLICT','已保存的差异案件批次不同。',409)
        return prior
    batch=CaseBatch(run_id=run.id,workspace_id=run.workspace_id,period_id=link.period_id,
        policy_version=CASE_POLICY_VERSION,total_count=len(wanted),accounted_bytes=charge)
    db.add(batch);db.flush()
    cases=[DifferenceCase(id=new_id(),workspace_id=run.workspace_id,period_id=link.period_id,
        run_id=run.id,group_id=group['id'],kind=group['status']) for group in wanted]
    db.add_all(cases);db.flush()
    db.add_all([CaseEvent(id=new_id(),workspace_id=item.workspace_id,case_id=item.id,sequence=1,action='created',actor_id=None,
        detail={'policy_version':CASE_POLICY_VERSION,'group_id':item.group_id,'kind':item.kind,'state':'open'}) for item in cases])
    db.flush()
    return batch


def get_case(db,w,actor,case_id,*,details=False):
    authorize_workspace(db,actor,w,'data.read' if details else 'results.read')
    item=db.scalar(select(DifferenceCase).where(DifferenceCase.workspace_id==w,DifferenceCase.id==case_id))
    if item is None:_bad('NOT_FOUND','差异案件不存在。',404)
    return item


def case_view(item,*,include_resolution=True):
    result={'id':item.id,'period_id':item.period_id,'run_id':item.run_id,'group_id':item.group_id,'kind':item.kind,
        'state':item.state,'version':item.version,'assigned_to':item.assigned_to,'submitted_by':item.submitted_by,
        'reviewed_by':item.reviewed_by,'resolution_hash':item.resolution_hash,
        'created_at':item.created_at.isoformat(),'updated_at':item.updated_at.isoformat()}
    if include_resolution:result['resolution']=item.resolution_json
    return result


def event_view(event):
    return {'id':event.id,'case_id':event.case_id,'sequence':event.sequence,'action':event.action,
        'actor_id':event.actor_id,'detail':event.detail,'created_at':event.created_at.isoformat()}


def batch_status(db,w,run_id):
    batch=db.scalar(select(CaseBatch).where(CaseBatch.workspace_id==w,CaseBatch.run_id==run_id))
    if batch is None:return {'ready':False,'total_count':None,'unresolved_count':None,'policy_version':None}
    rows=db.execute(select(DifferenceCase.state,func.count()).where(DifferenceCase.workspace_id==w,DifferenceCase.run_id==run_id).group_by(DifferenceCase.state)).all()
    counts=dict(rows)
    total=sum(counts.values())
    return {'ready':total==batch.total_count,'total_count':total,'unresolved_count':total-counts.get('closed',0),
        'policy_version':batch.policy_version,'counts':counts}


def _evidence(db,item,resolution):
    for ref in resolution['evidence']:
        found=db.scalar(select(RowCoverage.row_id).where(RowCoverage.workspace_id==item.workspace_id,
            RowCoverage.run_id==item.run_id,RowCoverage.group_id==item.group_id,
            RowCoverage.dataset_id==ref['dataset_id'],RowCoverage.row_id==ref['row_id']))
        if found is None:_bad('CASE_EVIDENCE_OUT_OF_SCOPE','证据必须来自本案件对应的原始记录。',422)


def mutate(db,w,actor,case_id,expected_case_version,expected_period_version,action,payload,key):
    if not isinstance(action,str) or action not in set(ACTION_PERMISSIONS)|{'reopen'}:
        _bad('INVALID_CASE_ACTION','案件操作无效。')
    # Hold the complete identity and membership set before quota or workflow locks.
    membership=authorize_workspace(db,actor,w,'data.read',lock=True)
    granted=workspace_permissions(membership.roles)
    permission=ACTION_PERMISSIONS.get(action)
    if action=='reopen':
        permission='cases.review' if 'cases.review' in granted else 'cases.work'
    body={'case_id':case_id,'expected_case_version':expected_case_version,'expected_period_version':expected_period_version,
          'action':action,'payload':payload}
    identity,fingerprint,replay,quota=periods._begin(db,w,actor,permission,'case.'+action,key,body)
    if replay is not None:
        if replay.get('period_reopened') and 'period.reopen' not in granted:
            _bad('FORBIDDEN','当前账号没有重新打开周期的权限。',403)
        return replay
    if type(expected_case_version) is not int or expected_case_version<1:
        _bad('INVALID_VERSION','需要明确当前案件版本。')
    candidate=db.scalar(select(DifferenceCase).where(DifferenceCase.workspace_id==w,DifferenceCase.id==case_id))
    if candidate is None:_bad('NOT_FOUND','差异案件不存在。',404)
    period=periods._period(db,w,candidate.period_id,True)
    periods._cas(period,expected_period_version)
    item=db.scalar(select(DifferenceCase).where(DifferenceCase.workspace_id==w,DifferenceCase.id==case_id)
                   .with_for_update().execution_options(populate_existing=True))
    if item.version!=expected_case_version:_bad('VERSION_CONFLICT','案件已被修改，请刷新后重试。',409)
    link=db.get(PeriodRun,item.run_id)
    if (period.active_run_id!=item.run_id or period.current_revision_id!=period.confirmed_revision_id
            or period.confirmed_revision_id is None or link is None or link.revision_id!=period.current_revision_id):
        _bad('CASE_NOT_CURRENT','历史结果的案件只读，请处理当前启用结果。',409)
    period_reopened=period.state=='closed'
    if period_reopened:
        if action!='reopen':_bad('PERIOD_CLOSED','请先重新打开周期和案件。',409)
        if 'period.reopen' not in granted:_bad('FORBIDDEN','当前账号没有重新打开周期的权限。',403)
    elif period.state!='reviewing':_bad('PERIOD_NOT_REVIEWING','周期当前不在复核阶段。',409)
    if not isinstance(payload,dict):_bad('INVALID_CASE_PAYLOAD','案件操作内容无效。')
    resolution=None;reason=None
    if action=='submit':
        resolution=_core(validate_resolution,payload)
        _evidence(db,item,resolution)
    elif action=='claim':
        if payload:_bad('INVALID_CASE_PAYLOAD','认领案件无需附加字段。')
    else:
        if set(payload)!={'reason'}:_bad('INVALID_CASE_PAYLOAD','请提供本次操作的说明。')
        reason=_core(validate_reason,payload['reason'])
    existing=item.resolution_json
    if action in ('approve','reject','withdraw'):
        if existing is None or item.resolution_hash!=digest(existing):
            _bad('CASE_RESOLUTION_MISMATCH','待复核处置内容校验失败。',409)
        _evidence(db,item,existing)
    next_state=_core(transition,item.state,action,actor,item.assigned_to,item.submitted_by,
                     existing.get('code') if existing else None)
    # Calculate before dirtying the ORM row; later queries must not accidentally
    # flush a workflow change without its matching version and event.
    stamp=db.scalar(select(func.clock_timestamp()))
    periods._charge(quota,8192+3*len(encoded(payload))+len(encoded(case_view(item)))+
                    len(encoded(periods.period_view(db,period))))
    previous_state=item.state;previous_hash=item.resolution_hash
    if action=='claim':item.assigned_to=actor
    if action=='submit':
        item.resolution_json=resolution;item.resolution_hash=digest(resolution)
        item.submitted_by=actor;item.reviewed_by=None
    elif action=='approve':item.reviewed_by=actor
    elif action in ('reject','withdraw','reopen'):item.reviewed_by=None
    detail={'from_state':previous_state,'to_state':next_state,'previous_resolution_hash':previous_hash,
            'resolution_hash':item.resolution_hash,'reason':reason}
    if resolution is not None:detail['resolution']=resolution
    item.state=next_state;item.version+=1;item.updated_at=stamp
    if period_reopened:period.state='reviewing'
    period.version+=1
    db.add(CaseEvent(id=new_id(),workspace_id=w,case_id=item.id,sequence=item.version,action=action,actor_id=actor,detail=detail))
    db.flush()
    result={'case':case_view(item),'period':periods.period_view(db,period),'period_reopened':period_reopened}
    return periods._complete(db,w,actor,identity,fingerprint,result,'case',item.id,
        {'case_version':item.version,'period_id':period.id,'period_version':period.version,'resolution_hash':item.resolution_hash,
         'period_reopened':period_reopened})
