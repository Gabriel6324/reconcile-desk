"""Transaction-scoped collection rules and durable, immutable reconciliation runs.

Each rule ID identifies one explicitly published version. There is no mutable
alias or automatic substitution of a newer rule, and source completeness is
never inferred from technical row coverage.
"""
import hashlib
import re
from collections import Counter

from sqlalchemy import select
from api.common import Problem, digest, encoded
from api.db import new_id
from core.business import ENGINE_VERSION, MAX_RECORDS, STATUSES, reconcile_groups
from core.mapping import MAPPING_VERSION, validate_mapping
from core.errors import CoreError
from .models import AuditEvent, MutationReceipt, Workspace
from .source_models import Dataset, DatasetRow
from .job_models import Job
from .reconcile_models import CollectionRule, CollectionRun, ResultGroup, RowCoverage
from .security import authorize_workspace
from . import jobs

CONTRACT_VERSION = 'collection-reconcile-1'
MAX_INPUT_BYTES = 64 * 1024**2
MAX_RESULT_BYTES = 64 * 1024**2
RESERVE_BYTES = MAX_RESULT_BYTES + 1024**2
RULE_FIELDS = {'name', 'left_dataset_id', 'right_dataset_id', 'left_mapping', 'right_mapping', 'rule'}


def _bad(code='INVALID_RECONCILIATION_RESULT', message='核对结果未通过完整性检查。', status=422):
    raise Problem(code, message, status)


def _receipt(db, workspace_id, actor_id, action, key, request_hash):
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(not 33 <= ord(c) <= 126 for c in key):
        _bad('IDEMPOTENCY_REQUIRED', '请提供有效请求标识。', 400)
    identity = {'scope_id': workspace_id, 'actor_id': actor_id, 'action': action, 'key': key}
    previous = db.get(MutationReceipt, identity)
    if previous and previous.request_hash != request_hash:
        _bad('IDEMPOTENCY_CONFLICT', '同一请求标识绑定了不同内容。', 409)
    return identity, previous


def _ready_dataset(db, workspace_id, dataset_id):
    if not isinstance(dataset_id, str) or not 1 <= len(dataset_id) <= 36:
        _bad('INVALID_DATASET_ID', '资料标识无效。')
    dataset = db.scalar(select(Dataset).where(Dataset.workspace_id == workspace_id, Dataset.id == dataset_id))
    if dataset is None:
        _bad('NOT_FOUND', '资料不存在。', 404)
    job = db.get(Job, dataset.job_id) if dataset.job_id else None
    if dataset.parsed_hash is None or not dataset.metadata_json or job is None or job.state != 'succeeded':
        _bad('DATASET_NOT_READY', '资料尚未解析完成。', 409)
    return dataset


def _input_binding(dataset):
    return {'dataset_id': dataset.id, 'parsed_hash': dataset.parsed_hash,
            'asset_id': dataset.asset_id, 'sha256': dataset.metadata_json['sha256'],
            'parser_version': dataset.metadata_json['parser_version'], 'row_count': dataset.row_count}


def create_rule(db, workspace_id, actor_id, definition, key):
    authorize_workspace(db, actor_id, workspace_id, 'rules.publish', lock=True)
    authorize_workspace(db, actor_id, workspace_id, 'data.read', lock=True)
    return _create_rule(db,workspace_id,actor_id,definition,key)


def _create_rule(db,workspace_id,actor_id,definition,key,*,action='collection_rule.publish',provenance=None):
    """Internal persistence; callers establish authority and immutable provenance."""
    _, definition = jobs._json(definition, 65536)
    if set(definition) != RULE_FIELDS:
        _bad('INVALID_COLLECTION_RULE', '规则字段缺失或包含未知属性。')
    name = definition['name']
    if (not isinstance(name, str) or not 1 <= len(name) <= 120 or not name.strip()
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
        _bad('INVALID_RULE_NAME', '规则名称须为 1 至 120 个有效字符。')
    left = _ready_dataset(db, workspace_id, definition['left_dataset_id'])
    right = _ready_dataset(db, workspace_id, definition['right_dataset_id'])
    if left.id == right.id:
        _bad('SAME_DATASET', '核对两侧必须选择不同资料。')
    if left.row_count + right.row_count > MAX_RECORDS:
        _bad('BUSINESS_INPUT_LIMIT', '两侧资料合计超过 100000 行。', 413)
    request_hash = digest(definition)
    identity, previous = _receipt(db, workspace_id, actor_id, action, key, request_hash)
    if previous:
        return previous.result
    try:
        compiled_left = validate_mapping(definition['left_mapping'], left.metadata_json['headers'])
        compiled_right = validate_mapping(definition['right_mapping'], right.metadata_json['headers'])
        business_rule = reconcile_groups([], [], definition['rule'])['rule']
    except CoreError as exc:
        raise Problem(exc.code, exc.message, details=exc.details) from None
    frozen = {'name': name, 'left_dataset_id': left.id, 'right_dataset_id': right.id,
              'left_mapping': compiled_left, 'right_mapping': compiled_right, 'rule': business_rule,
              'left_input': _input_binding(left), 'right_input': _input_binding(right),
              'requested_mappings': {'left': definition['left_mapping'], 'right': definition['right_mapping']},
              'engine_version': ENGINE_VERSION, 'mapping_version': MAPPING_VERSION}
    if provenance is not None:
        frozen['profile_binding']=provenance
    # The immutable rule and idempotent command response both retain its definition.
    charge = 2 * len(encoded(frozen)) + 8192
    _, quota = jobs._capacity(db, workspace_id)
    if quota.committed_bytes + quota.reserved_bytes + charge > quota.limit_bytes:
        _bad('WORKSPACE_QUOTA', '工作区容量不足，规则未发布。', 507)
    rule = CollectionRule(id=new_id(), workspace_id=workspace_id, name=name, published_by=actor_id,
                          left_dataset_id=left.id, right_dataset_id=right.id, definition_json=frozen,
                          rule_hash=digest(frozen), accounted_bytes=charge)
    db.add(rule)
    quota.committed_bytes += charge
    db.flush()
    result = rule_view(rule)
    db.add(MutationReceipt(**identity, request_hash=request_hash, result=result))
    workspace = db.get(Workspace, workspace_id)
    db.add(AuditEvent(id=new_id(), org_id=workspace.org_id, workspace_id=workspace_id, actor_id=actor_id,
                     action='collection_rule.published' if action=='collection_rule.publish' else action, resource_type='collection_rule', resource_id=rule.id,
                     request_id=new_id(), safe_detail={'rule_hash': rule.rule_hash}))
    db.flush()
    return result


def get_rule(db, workspace_id, actor_id, rule_id):
    authorize_workspace(db, actor_id, workspace_id, 'data.read')
    rule = db.scalar(select(CollectionRule).where(CollectionRule.workspace_id == workspace_id, CollectionRule.id == rule_id))
    if rule is None:
        _bad('NOT_FOUND', '规则不存在。', 404)
    return rule


def rule_view(rule):
    return {'id': rule.id, 'version_id': rule.id, 'name': rule.name, 'state': 'published',
            'definition': rule.definition_json, 'rule_hash': rule.rule_hash, 'published_by': rule.published_by,
            'created_at': rule.created_at.isoformat(), 'source_completeness': 'not_attested'}


def _binding(rule):
    return {'contract': CONTRACT_VERSION, 'rule_id': rule.id, 'rule_hash': rule.rule_hash,
            'left_input': rule.definition_json['left_input'], 'right_input': rule.definition_json['right_input'],
            'engine_version': rule.definition_json['engine_version'], 'mapping_version': rule.definition_json['mapping_version']}


def _validate_rule_inputs(db, workspace_id, rule):
    definition = rule.definition_json
    if (digest(definition) != rule.rule_hash or definition.get('engine_version') != ENGINE_VERSION
            or definition.get('mapping_version') != MAPPING_VERSION):
        _bad('UNSUPPORTED_RULE_VERSION', '规则版本不受当前执行器支持。', 403)
    for side in ('left', 'right'):
        dataset = _ready_dataset(db, workspace_id, getattr(rule, side + '_dataset_id'))
        if _input_binding(dataset) != definition[side + '_input']:
            _bad('INPUT_VERSION_MISMATCH', '资料版本与确认规则不一致。', 403)


def create_run(db, workspace_id, actor_id, rule_id, key):
    authorize_workspace(db, actor_id, workspace_id, 'runs.create', lock=True)
    authorize_workspace(db, actor_id, workspace_id, 'data.read', lock=True)
    rule = get_rule(db, workspace_id, actor_id, rule_id)
    _validate_rule_inputs(db, workspace_id, rule)
    request_hash = digest({'rule_id': rule.id, 'rule_hash': rule.rule_hash, 'contract': CONTRACT_VERSION})
    identity, previous = _receipt(db, workspace_id, actor_id, 'collection_run.create', key, request_hash)
    if previous:
        return previous.result
    _, quota = jobs._capacity(db, workspace_id)
    # A run shell and its admission receipt survive failed/cancelled computations.
    if quota.committed_bytes + quota.reserved_bytes + 8192 + RESERVE_BYTES > quota.limit_bytes:
        _bad('WORKSPACE_QUOTA', '工作区容量不足，核对任务未创建。', 507)
    binding = _binding(rule)
    run = CollectionRun(id=new_id(), workspace_id=workspace_id, rule_id=rule.id, input_hash=digest(binding))
    db.add(run)
    db.flush()
    snapshot = dict(binding, run_id=run.id, input_hash=run.input_hash)
    job = jobs.admit(db, workspace_id, actor_id, 'reconcile', key, snapshot, RESERVE_BYTES, authorize_inputs=authorize_inputs)
    run.job_id = job.id
    quota.committed_bytes += 8192
    result = {'run_id': run.id, 'job_id': job.id, 'rule_id': rule.id, 'state': 'queued', 'input_hash': run.input_hash}
    db.add(MutationReceipt(**identity, request_hash=request_hash, result=result))
    db.flush()
    return result


def authorize_inputs(db, workspace_id, actor_id, kind, snapshot):
    authorize_workspace(db, actor_id, workspace_id, 'data.read', lock=True)
    if kind != 'reconcile' or snapshot.get('contract') != CONTRACT_VERSION:
        _bad('UNSUPPORTED_JOB_CONTRACT', '该执行器不支持此任务契约。', 403)
    run = db.scalar(select(CollectionRun).where(CollectionRun.workspace_id == workspace_id, CollectionRun.id == snapshot.get('run_id')))
    rule = db.scalar(select(CollectionRule).where(CollectionRule.workspace_id == workspace_id, CollectionRule.id == snapshot.get('rule_id')))
    if run is None or rule is None or run.rule_id != rule.id:
        _bad('NOT_FOUND', '核对来源不存在。', 404)
    _validate_rule_inputs(db, workspace_id, rule)
    binding = _binding(rule)
    if snapshot != dict(binding, run_id=run.id, input_hash=run.input_hash) or run.input_hash != digest(binding):
        _bad('INPUT_VERSION_MISMATCH', '核对快照版本不一致。', 403)


def input_for_job(db, job):
    authorize_inputs(db, job.workspace_id, job.actor_id, job.kind, job.request_snapshot)
    rule = db.get(CollectionRule, job.request_snapshot['rule_id'])
    payload = {'contract': CONTRACT_VERSION, 'run_id': job.request_snapshot['run_id'],
               'rule_hash': rule.rule_hash, 'input_hash': job.request_snapshot['input_hash'],
               'rule': rule.definition_json['rule'], 'engine_version': ENGINE_VERSION, 'mapping_version': MAPPING_VERSION}
    size, row_count = len(encoded(payload)), 0
    for side in ('left', 'right'):
        dataset_id = getattr(rule, side + '_dataset_id')
        rows = []
        query = select(DatasetRow).where(DatasetRow.workspace_id == job.workspace_id, DatasetRow.dataset_id == dataset_id).order_by(DatasetRow.ordinal)
        for item in db.scalars(query.execution_options(yield_per=500)):
            row = {'id': item.row_id, 'ordinal': item.ordinal, 'cells': item.cells, 'source': item.source}
            size += len(encoded(row)) + 1
            row_count += 1
            if size > MAX_INPUT_BYTES or row_count > MAX_RECORDS:
                _bad('BUSINESS_INPUT_LIMIT', '规范化输入超过计算上限。', 413)
            rows.append(row)
        if len(rows) != rule.definition_json[side + '_input']['row_count']:
            _bad('INPUT_ROW_COUNT_MISMATCH', '来源行数校验失败。', 409)
        payload[side] = {'dataset_id': dataset_id, 'rows': rows, 'mapping': rule.definition_json[side + '_mapping']}
    if len(encoded(payload)) > MAX_INPUT_BYTES:
        _bad('BUSINESS_INPUT_LIMIT', '规范化输入超过计算上限。', 413)
    return payload


def _validated_coverage(db, workspace_id, rule, result):
    if (not isinstance(result, dict) or set(result) != {'engine_version', 'stage_id', 'rule', 'groups', 'coverage', 'summary'}
            or result.get('engine_version') != ENGINE_VERSION or result.get('stage_id') != 'collection'
            or result.get('rule') != rule.definition_json['rule']):
        _bad()
    groups, coverage, summary = result['groups'], result['coverage'], result['summary']
    if not isinstance(groups, list) or not isinstance(coverage, list) or not isinstance(summary, dict):
        _bad()
    expected = {}
    for side in ('left', 'right'):
        dataset_id = getattr(rule, side + '_dataset_id')
        row_ids = db.scalars(select(DatasetRow.row_id).where(DatasetRow.workspace_id == workspace_id, DatasetRow.dataset_id == dataset_id)).all()
        if len(row_ids) != rule.definition_json[side + '_input']['row_count']:
            _bad()
        expected.update({(side, dataset_id + ':' + row_id): (dataset_id, row_id) for row_id in row_ids})
    if len(coverage) != len(expected) or len(groups) > len(expected):
        _bad()
    membership, group_ids, counts = {}, set(), Counter()
    group_fields = {'id', 'stage_id', 'key', 'currency', 'status', 'left_rows', 'right_rows', 'tolerance',
                    'expected_amount', 'observed_amount', 'delta', 'absolute_delta', 'issues'}
    for group in groups:
        if (not isinstance(group, dict) or set(group) != group_fields or not isinstance(group.get('id'), str)
                or not re.fullmatch(r'g_[0-9a-f]{64}', group['id']) or group['id'] in group_ids
                or group.get('stage_id') != 'collection' or group.get('status') not in STATUSES
                or group.get('tolerance') != result['rule']['tolerance'] or not isinstance(group.get('issues'), list)):
            _bad()
        group_ids.add(group['id'])
        counts[group['status']] += 1
        group_members = 0
        for side in ('left', 'right'):
            row_ids = group[side + '_rows']
            if not isinstance(row_ids, list):
                _bad()
            for row_id in row_ids:
                if not isinstance(row_id, str) or (side, row_id) not in expected or (side, row_id) in membership:
                    _bad()
                membership[(side, row_id)] = (group['id'], group['status'])
                group_members += 1
        if not group_members:
            _bad()
    if set(membership) != set(expected):
        _bad()
    rows, seen = [], set()
    for item in coverage:
        if not isinstance(item, dict) or set(item) != {'side', 'row_id', 'group_id', 'disposition'}:
            _bad()
        if not isinstance(item['side'], str) or not isinstance(item['row_id'], str):
            _bad()
        key = (item['side'], item['row_id'])
        if key in seen or key not in expected or membership[key] != (item['group_id'], item['disposition']):
            _bad()
        seen.add(key)
        dataset_id, row_id = expected[key]
        rows.append({'side': item['side'], 'dataset_id': dataset_id, 'row_id': row_id,
                     'group_id': item['group_id'], 'disposition': item['disposition']})
    expected_summary = {'left_rows': rule.definition_json['left_input']['row_count'],
                        'right_rows': rule.definition_json['right_input']['row_count'],
                        'input_rows': len(expected), 'covered_rows': len(expected), 'total_groups': len(groups),
                        'counts': {status: counts[status] for status in STATUSES}, 'technical_coverage': 'complete',
                        'source_completeness': 'not_attested', 'requires_review': any(status != 'exact_equal' for status in counts)}
    if summary != expected_summary:
        _bad()
    return rows


def publish(db, job_id, fence_token, envelope):
    candidate = db.get(Job, job_id)
    if candidate is None:
        _bad('NOT_FOUND', '任务不存在。', 404)
    if not isinstance(envelope, dict) or set(envelope) != {'run_id', 'rule_hash', 'input_hash', 'result'}:
        _bad()
    snapshot = candidate.request_snapshot
    if any(envelope[key] != snapshot.get(key) for key in ('run_id', 'rule_hash', 'input_hash')):
        _bad('INPUT_VERSION_MISMATCH', '核对结果的来源版本不一致。', 409)
    # jobs.finish rechecks current authority and transitions a revoked active job
    # to cancel_requested. Do not raise before that transition can be persisted.
    run = db.get(CollectionRun, snapshot['run_id'])
    rule = db.get(CollectionRule, snapshot['rule_id'])
    serialized, envelope = jobs._json(envelope, MAX_RESULT_BYTES)
    result = envelope['result']
    if not isinstance(result, dict) or not isinstance(result.get('groups'), list) or not isinstance(result.get('coverage'), list):
        _bad()
    charge = len(serialized) + len(result['groups']) * 512 + len(result['coverage']) * 512
    from .period_models import PeriodRun
    from . import cases
    period_link=db.get(PeriodRun,run.id)
    if period_link is not None:charge+=cases.storage_charge(result['groups'])
    if charge > MAX_RESULT_BYTES:
        _bad('RECONCILIATION_RESULT_TOO_LARGE', '核对结果超过持久化上限。', 413)
    result_hash = hashlib.sha256(serialized).hexdigest()
    manifest = {'run_id': run.id, 'rule_id': rule.id, 'rule_hash': rule.rule_hash,
                'input_hash': run.input_hash, 'result_hash': result_hash,
                'engine_version': ENGINE_VERSION, 'mapping_version': MAPPING_VERSION, 'summary': result.get('summary')}
    job = jobs.finish(db, job_id, fence_token, manifest, authorize_inputs=authorize_inputs, external_retained_bytes=charge)
    if job.state != 'succeeded':
        return job
    if run.result_hash is not None:
        if run.result_hash != result_hash:
            _bad('PUBLISHED_RESULT_CONFLICT', '已保存的核对结果不同。', 409)
        return job
    coverage = _validated_coverage(db, run.workspace_id, rule, result)
    db.add_all([ResultGroup(workspace_id=run.workspace_id, run_id=run.id, group_id=group['id'], ordinal=index + 1, detail=group)
                for index, group in enumerate(result['groups'])])
    db.flush()
    db.add_all([RowCoverage(workspace_id=run.workspace_id, run_id=run.id, **item) for item in coverage])
    run.result_hash, run.summary_json = result_hash, result['summary']
    db.flush()
    if period_link is not None:cases.publish_cases(db,run,result['groups'])
    return job


def get_run(db, workspace_id, actor_id, run_id):
    authorize_workspace(db, actor_id, workspace_id, 'results.read')
    run = db.scalar(select(CollectionRun).where(CollectionRun.workspace_id == workspace_id, CollectionRun.id == run_id))
    if run is None:
        _bad('NOT_FOUND', '核对记录不存在。', 404)
    return run


def run_view(db, run):
    job = db.get(Job, run.job_id)
    rule = db.get(CollectionRule, run.rule_id)
    return {'id': run.id, 'rule_id': run.rule_id, 'rule_hash': rule.rule_hash, 'job_id': run.job_id,
            'state': job.state, 'error_code': job.error_code, 'input_hash': run.input_hash,
            'result_hash': run.result_hash, 'summary': run.summary_json,
            'engine_version': rule.definition_json['engine_version'], 'mapping_version': rule.definition_json['mapping_version'],
            'created_at': run.created_at.isoformat(), 'source_completeness': 'not_attested'}


def group_view(group):
    detail=group.detail
    return {'ordinal':group.ordinal,**{key:value for key,value in detail.items()
            if key not in ('left_rows','right_rows','issues')},
            'left_row_count':len(detail['left_rows']),'right_row_count':len(detail['right_rows']),
            'issue_count':len(detail['issues']),'issues_preview':detail['issues'][:5]}
