"""Explicit period confirmation and activation; computation never changes pointers.

All mutation callers own one transaction. Lock order is authorization, capacity,
quota, period, then queue. Old admitted jobs may finish after period revision.
Closing requires complete human declarations and independently reviewed cases.
"""
from sqlalchemy import select,func
from api.common import Problem,digest,encoded
from api.db import new_id
from core.errors import CoreError
from core.business import ENGINE_VERSION
from core.mapping import MAPPING_VERSION
from core.period_policy import validate_period_definition,validate_attestations,assess_completeness,PERIOD_POLICY_VERSION
from .models import MutationReceipt,AuditEvent,Workspace
from .period_models import CollectionProfile,BusinessPeriod,PeriodRevision,PeriodRun
from .reconcile_models import CollectionRule,CollectionRun
from .job_models import Job
from .security import authorize_workspace
from . import jobs,reconciliations


def _bad(code,message,status=422):
    raise Problem(code,message,status)


def _core(fn,*args):
    try:return fn(*args)
    except CoreError as exc:raise Problem(exc.code,exc.message,details=exc.details) from None


def _begin(db,w,actor,permission,action,key,body):
    authorize_workspace(db,actor,w,permission,lock=True)
    authorize_workspace(db,actor,w,'data.read',lock=True)
    _,body=jobs._json(body,65536)
    fingerprint=digest(body)
    identity,previous=reconciliations._receipt(db,w,actor,action,key,fingerprint)
    if previous:return identity,fingerprint,previous.result,None
    _,quota=jobs._capacity(db,w)
    return identity,fingerprint,None,quota


def _charge(quota,amount):
    if quota.committed_bytes+quota.reserved_bytes+amount>quota.limit_bytes:
        _bad('WORKSPACE_QUOTA','工作区容量不足，本次修改未保存。',507)
    quota.committed_bytes+=amount


def _complete(db,w,actor,identity,fingerprint,result,resource,ident,detail=None):
    db.add(MutationReceipt(**identity,request_hash=fingerprint,result=result))
    db.add(AuditEvent(id=new_id(),org_id=db.get(Workspace,w).org_id,workspace_id=w,actor_id=actor,
        action=identity['action'],resource_type=resource,resource_id=ident,request_id=new_id(),safe_detail=detail or {}))
    db.flush()
    return result


def _period(db,w,ident,lock=False):
    query=select(BusinessPeriod).where(BusinessPeriod.workspace_id==w,BusinessPeriod.id==ident)
    if lock:query=query.with_for_update()
    item=db.scalar(query.execution_options(populate_existing=True))
    if item is None:_bad('NOT_FOUND','业务周期不存在。',404)
    return item


def _cas(period,expected):
    if type(expected) is not int or expected<1:_bad('INVALID_VERSION','需要明确当前版本。')
    if period.version!=expected:_bad('VERSION_CONFLICT','周期已被修改，请刷新后重试。',409)


def _profile(db,w,ident):
    if not isinstance(ident,str) or not 1<=len(ident)<=36:
        _bad('INVALID_PROFILE_ID','规则配置标识无效。')
    value=db.scalar(select(CollectionProfile).where(CollectionProfile.workspace_id==w,CollectionProfile.id==ident))
    if value is None:_bad('NOT_FOUND','规则配置不存在。',404)
    return value


def profile_view(profile):
    return {'id':profile.id,'version_id':profile.id,'name':profile.name,'source_rule_id':profile.source_rule_id,
        'definition':profile.definition_json,'profile_hash':profile.profile_hash,'created_by':profile.created_by,'created_at':profile.created_at.isoformat()}


def create_profile(db,w,actor,source_rule_id,name,key):
    body={'source_rule_id':source_rule_id,'name':name}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'rules.publish','collection_profile.publish',key,body)
    if replay is not None:return replay
    if not isinstance(name,str) or not 1<=len(name)<=120 or not name.strip() or any(ord(c)<32 or 127<=ord(c)<=159 for c in name):
        _bad('INVALID_PROFILE_NAME','规则配置名称无效。')
    source=reconciliations.get_rule(db,w,actor,source_rule_id)
    original=source.definition_json
    if original['engine_version']!=ENGINE_VERSION or original['mapping_version']!=MAPPING_VERSION:
        _bad('UNSUPPORTED_RULE_VERSION','原规则版本不受支持。',409)
    frozen={'name':name,'left_mapping':original['requested_mappings']['left'],'right_mapping':original['requested_mappings']['right'],
        'rule':original['rule'],'engine_version':original['engine_version'],'mapping_version':original['mapping_version']}
    charge=2*len(encoded(frozen))+8192
    _charge(quota,charge)
    item=CollectionProfile(id=new_id(),workspace_id=w,source_rule_id=source.id,name=name,definition_json=frozen,
        profile_hash=digest(frozen),created_by=actor,accounted_bytes=charge)
    db.add(item);db.flush()
    return _complete(db,w,actor,identity,fingerprint,profile_view(item),'collection_profile',item.id,{'profile_hash':item.profile_hash})


def create_period(db,w,actor,definition,key):
    normalized=_core(validate_period_definition,definition)
    identity,fingerprint,replay,quota=_begin(db,w,actor,'period.create','period.create',key,{'definition':definition})
    if replay is not None:return replay
    frozen={'policy_version':PERIOD_POLICY_VERSION,**normalized}
    _charge(quota,2*len(encoded(frozen))+8192)
    item=BusinessPeriod(id=new_id(),workspace_id=w,definition_json=frozen,definition_hash=digest(frozen),created_by=actor)
    db.add(item);db.flush()
    return _complete(db,w,actor,identity,fingerprint,period_view(db,item),'period',item.id)


def _bind_profile(db,w,actor,profile,orders_id,payments_id,revision_id,period_id):
    """Operator can only instantiate the selected immutable manager policy."""
    definition=profile.definition_json
    if (digest(definition)!=profile.profile_hash or definition.get('engine_version')!=ENGINE_VERSION
        or definition.get('mapping_version')!=MAPPING_VERSION):
        _bad('UNSUPPORTED_PROFILE_VERSION','规则配置版本不受支持。',409)
    # Copy named mappings, resolving them against NEW headers, never old cN IDs.
    body={k:definition[k] for k in ('name','left_mapping','right_mapping','rule')}
    body.update(left_dataset_id=orders_id,right_dataset_id=payments_id)
    provenance={'profile_id':profile.id,'profile_hash':profile.profile_hash,'period_id':period_id,'revision_id':revision_id}
    bound=reconciliations._create_rule(db,w,actor,body,'period-binding:'+revision_id,action='period.rule_bound',provenance=provenance)
    return db.get(CollectionRule,bound['id'])


def revise_inputs(db,w,actor,period_id,expected_version,definition,key):
    body={'period_id':period_id,'expected_version':expected_version,'definition':definition}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'period.inputs','period.inputs',key,body)
    if replay is not None:return replay
    if not isinstance(definition,dict) or set(definition)!={'profile_id','orders_dataset_id','payments_dataset_id','attestations'}:
        _bad('INVALID_PERIOD_INPUTS','必须明确两侧资料、规则配置与完整性声明。')
    attestations=_core(validate_attestations,definition['attestations'])
    profile=_profile(db,w,definition['profile_id'])
    period=_period(db,w,period_id,True);_cas(period,expected_version)
    if period.state=='closed':_bad('PERIOD_CLOSED','请先重新打开周期。',409)
    revision_id=new_id()
    rule=_bind_profile(db,w,actor,profile,definition['orders_dataset_id'],definition['payments_dataset_id'],revision_id,period.id)
    stamp=db.scalar(select(func.clock_timestamp())).isoformat()
    config={'policy_version':PERIOD_POLICY_VERSION,'period_id':period.id,'period_definition':period.definition_json,
        'profile_id':profile.id,'profile_hash':profile.profile_hash,'rule_id':rule.id,'rule_hash':rule.rule_hash,
        'rule_definition':rule.definition_json,'attestations':attestations,'attested_by':actor,'attested_at':stamp,
        'scope_basis':'human_attestation_no_automatic_date_filter'}
    charge=2*len(encoded(config))+8192
    _charge(quota,charge)
    revision=PeriodRevision(id=revision_id,workspace_id=w,period_id=period.id,number=period.revision_number+1,
        profile_id=profile.id,rule_id=rule.id,config_json=config,config_hash=digest(config),created_by=actor,accounted_bytes=charge)
    db.add(revision);db.flush()
    period.current_revision_id=revision.id;period.confirmed_revision_id=None
    period.revision_number=revision.number;period.version+=1;period.state='draft'
    db.flush()
    result={'period':period_view(db,period),'revision':revision_view(revision)}
    return _complete(db,w,actor,identity,fingerprint,result,'period',period.id,{'version':period.version,'config_hash':revision.config_hash})


def confirm(db,w,actor,period_id,expected_version,config_hash,key):
    body={'period_id':period_id,'expected_version':expected_version,'config_hash':config_hash}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'period.inputs','period.confirm',key,body)
    if replay is not None:return replay
    period=_period(db,w,period_id,True);_cas(period,expected_version)
    revision=db.get(PeriodRevision,period.current_revision_id) if period.current_revision_id else None
    if revision is None or revision.config_hash!=config_hash:
        _bad('CONFIRMATION_MISMATCH','请确认当前完整配置及其摘要。',409)
    if period.state!='draft':_bad('PERIOD_ALREADY_CONFIRMED','该周期配置已确认。',409)
    _charge(quota,8192)
    period.confirmed_revision_id=revision.id;period.state='ready';period.version+=1;db.flush()
    return _complete(db,w,actor,identity,fingerprint,period_view(db,period),'period',period.id,
        {'version':period.version,'revision_id':revision.id,'config_hash':config_hash})


def _confirmed(db,period):
    if period.state not in ('ready','reviewing') or period.current_revision_id!=period.confirmed_revision_id or not period.confirmed_revision_id:
        _bad('PERIOD_NOT_CONFIRMED','请先确认当前周期资料和规则。',409)
    return db.get(PeriodRevision,period.confirmed_revision_id)


def submit_run(db,w,actor,period_id,expected_version,key):
    body={'period_id':period_id,'expected_version':expected_version}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'runs.create','period.run_submit',key,body)
    if replay is not None:return replay
    period=_period(db,w,period_id,True);_cas(period,expected_version)
    revision=_confirmed(db,period)
    _charge(quota,8192)
    # A private random key separates this admission from standalone user keys.
    run=reconciliations.create_run(db,w,actor,revision.rule_id,'period-run:'+new_id())
    link=PeriodRun(run_id=run['run_id'],workspace_id=w,period_id=period.id,revision_id=revision.id,
        config_hash=revision.config_hash,submitted_by=actor)
    db.add(link);period.version+=1;db.flush()
    result={'period':period_view(db,period),'run':run,'revision_id':revision.id,'config_hash':revision.config_hash}
    return _complete(db,w,actor,identity,fingerprint,result,'period',period.id,{'run_id':link.run_id,'revision_id':revision.id})


def activate(db,w,actor,period_id,expected_version,run_id,key,acknowledged_open_cases=0):
    body={'period_id':period_id,'expected_version':expected_version,'run_id':run_id,'acknowledged_open_cases':acknowledged_open_cases}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'period.activate','period.activate',key,body)
    if replay is not None:return replay
    period=_period(db,w,period_id,True);_cas(period,expected_version)
    revision=_confirmed(db,period)
    link=db.scalar(select(PeriodRun).where(PeriodRun.workspace_id==w,PeriodRun.period_id==period.id,PeriodRun.run_id==run_id))
    if link is None:_bad('NOT_FOUND','该结果不属于此业务周期。',404)
    if link.revision_id!=revision.id or link.config_hash!=revision.config_hash:
        _bad('STALE_PERIOD_RUN','该结果使用历史资料或规则，不能设为当前结果。',409)
    run=db.get(CollectionRun,run_id)
    if run.rule_id!=revision.rule_id:_bad('INPUT_VERSION_MISMATCH','结果规则与当前配置不一致。',409)
    if db.get(Job,run.job_id).state!='succeeded' or run.result_hash is None:
        _bad('RUN_NOT_READY','核对任务尚未成功完成。',409)
    from . import cases
    from .case_models import CaseBatch
    from .reconcile_models import ResultGroup
    if db.get(CaseBatch,run.id) is None:
        groups=[g.detail for g in db.scalars(select(ResultGroup).where(ResultGroup.workspace_id==w,ResultGroup.run_id==run.id).order_by(ResultGroup.ordinal))]
        _charge(quota,cases.storage_charge(groups))
        cases.publish_cases(db,run,groups)
    outstanding=unresolved_count(db,period.active_run_id)
    if type(acknowledged_open_cases) is not int or acknowledged_open_cases!=outstanding:
        _bad('OPEN_CASES_ACK_REQUIRED','请确认将保留为历史记录的未结案件数量。',409)
    _charge(quota,8192)
    previous=period.active_run_id
    period.active_run_id=run.id;period.state='reviewing';period.version+=1;db.flush()
    return _complete(db,w,actor,identity,fingerprint,period_view(db,period),'period',period.id,
        {'previous_run_id':previous,'run_id':run.id,'config_hash':revision.config_hash,'version':period.version})


def get_period(db,w,actor,ident):
    authorize_workspace(db,actor,w,'results.read')
    return _period(db,w,ident)


def revision_view(item):
    return {'id':item.id,'period_id':item.period_id,'number':item.number,'profile_id':item.profile_id,'rule_id':item.rule_id,
        'config':item.config_json,'config_hash':item.config_hash,'created_by':item.created_by,'created_at':item.created_at.isoformat()}


def period_view(db,period):
    revision=db.get(PeriodRevision,period.current_revision_id) if period.current_revision_id else None
    active=db.get(PeriodRun,period.active_run_id) if period.active_run_id else None
    completeness=_core(assess_completeness,revision.config_json['attestations']) if revision else {'status':'not_attested','roles':{}}
    # Viewer summary omits attestation notes and raw source configuration.
    status=completeness['status']
    stale=bool(active and (active.revision_id!=period.current_revision_id or period.confirmed_revision_id!=period.current_revision_id))
    return {'id':period.id,'definition':period.definition_json,'version':period.version,'state':period.state,
        'current_revision_id':period.current_revision_id,'confirmed_revision_id':period.confirmed_revision_id,
        'config_hash':revision.config_hash if revision else None,'active_run_id':period.active_run_id,
        'active_run_stale':stale,'source_completeness':status,'source_completeness_basis':'human_attestation',
        'unresolved_case_count':unresolved_count(db,period.active_run_id),'business_closed':period.state=='closed','created_at':period.created_at.isoformat()}


def run_link_view(db,period,link):
    run=db.get(CollectionRun,link.run_id)
    current=link.revision_id==period.current_revision_id and link.revision_id==period.confirmed_revision_id
    return {**reconciliations.run_view(db,run),'period_id':period.id,'revision_id':link.revision_id,'config_hash':link.config_hash,
        'active':period.active_run_id==run.id,'stale':not current}


def unresolved_count(db,run_id):
    if not run_id:return 0
    from .case_models import DifferenceCase
    return db.scalar(select(func.count()).select_from(DifferenceCase).where(DifferenceCase.run_id==run_id,DifferenceCase.state!='closed'))


def close_period(db,w,actor,period_id,expected_version,reason,key):
    from core.case_policy import validate_reason
    from .case_models import CaseBatch
    reason=_core(validate_reason,reason)
    body={'period_id':period_id,'expected_version':expected_version,'reason':reason}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'period.close','period.close',key,body)
    if replay is not None:return replay
    period=_period(db,w,period_id,True);_cas(period,expected_version)
    revision=_confirmed(db,period)
    if period.state!='reviewing' or not period.active_run_id:_bad('PERIOD_NOT_REVIEWING','请先启用本周期核对结果。',409)
    link=db.get(PeriodRun,period.active_run_id)
    if link.revision_id!=revision.id:_bad('STALE_PERIOD_RUN','当前结果已过期。',409)
    if _core(assess_completeness,revision.config_json['attestations'])['status']!='attested_complete':
        _bad('INCOMPLETE_SOURCE','两侧来源尚未声明完整，无法关闭周期。',409)
    from .cases import batch_status
    if not batch_status(db,w,period.active_run_id)['ready']:_bad('CASE_BATCH_REQUIRED','差异案件尚未完整生成。',409)
    if unresolved_count(db,period.active_run_id):_bad('OPEN_CASES','当前结果仍有未复核关闭的案件。',409)
    _charge(quota,8192+2*len(encoded(body)))
    period.state='closed';period.version+=1;db.flush()
    return _complete(db,w,actor,identity,fingerprint,period_view(db,period),'period',period.id,{'reason':reason,'run_id':period.active_run_id,'version':period.version})


def reopen_period(db,w,actor,period_id,expected_version,reason,key):
    from core.case_policy import validate_reason
    reason=_core(validate_reason,reason)
    body={'period_id':period_id,'expected_version':expected_version,'reason':reason}
    identity,fingerprint,replay,quota=_begin(db,w,actor,'period.reopen','period.reopen',key,body)
    if replay is not None:return replay
    period=_period(db,w,period_id,True);_cas(period,expected_version)
    if period.state!='closed':_bad('PERIOD_NOT_CLOSED','周期尚未关闭。',409)
    _charge(quota,8192+2*len(encoded(body)))
    period.state='reviewing';period.version+=1;db.flush()
    return _complete(db,w,actor,identity,fingerprint,period_view(db,period),'period',period.id,{'reason':reason,'version':period.version})
