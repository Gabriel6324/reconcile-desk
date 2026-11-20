"""Transaction-scoped durable queue primitives; no HTTP or compute side effects.

Call each operation in a fresh Session.begin() and commit its returned state.
An exception requires rollback. Never execute computation while the transaction
holds locks. A worker process must also acquire advisory lock 732914622 before
dispatching, to exclude the historical Runner. The database single-active index
and capacity row protect this API even if multiple dispatchers call claim().

authorize_inputs(db, workspace_id, actor_id, kind, snapshot), when supplied,
must raise Problem to reject referenced resources. Integrations MUST supply it
for jobs referring to business resources; queue-only snapshots have no such refs.
Commit ACK uncertainty is reconciled with the same admit key or finish token;
these functions never commit, retry external calls, or mark unknown commits failed.
"""
from datetime import timedelta
import hashlib
import json

from sqlalchemy import func, select

from api.common import Problem
from api.db import new_id
from .job_models import GlobalCapacity, Job, JobAttempt, QuotaLedger, WorkspaceQuota
from .models import AuditEvent, Workspace
from .security import authorize_workspace, workspace_permissions

PERMISSIONS = {'import': 'data.import', 'reconcile': 'runs.create', 'export': 'data.export'}
ACTIVE = frozenset({'running', 'cancel_requested'})
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'interrupted'})
MAX_SNAPSHOT_BYTES = 65536
MAX_RESULT_BYTES = 2 * 1024 * 1024
# Logical accounting, not PG/WAL/physical disk size. Bounds cover one job, one
# attempt, two ledger entries and up to six small audit events, including their
# bounded identifiers and indexes. The snapshot/result bytes are added exactly.
METADATA_BUDGET_BYTES = 16384


def _json(value, maximum):
    if not isinstance(value, dict):
        raise Problem('INVALID_JOB_PAYLOAD', '任务内容须为对象。')
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise Problem('INVALID_JOB_PAYLOAD', '任务内容包含无效值。') from None
    if len(encoded) > maximum:
        raise Problem('JOB_PAYLOAD_TOO_LARGE', '任务内容超过限制。', 413)
    return encoded, json.loads(encoded)


def _read(db, statement, *, lock=False):
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement.execution_options(populate_existing=True))


def _stamp(db):
    return db.scalar(select(func.clock_timestamp()))


def _authorize(db, workspace_id, actor_id, kind, snapshot, callback=None):
    if kind not in PERMISSIONS:
        raise Problem('UNSUPPORTED_JOB_KIND', '不支持该任务类型。')
    membership = authorize_workspace(db, actor_id, workspace_id, PERMISSIONS[kind], lock=True)
    if callback:
        callback(db, workspace_id, actor_id, kind, snapshot)
    return membership


def _authorized(db, job, callback):
    try:
        _authorize(db, job.workspace_id, job.actor_id, job.kind, job.request_snapshot, callback)
        return True
    except Problem as exc:
        if exc.status not in (401, 403, 404):
            raise
        return False


def _capacity(db, workspace_id):
    capacity = _read(db, select(GlobalCapacity).where(GlobalCapacity.id == 1), lock=True)
    if capacity is None:
        raise Problem('QUEUE_NOT_MIGRATED', '任务队列尚未初始化。', 503)
    quota = _read(db, select(WorkspaceQuota).where(WorkspaceQuota.workspace_id == workspace_id), lock=True)
    if quota is None:
        # Global capacity serializes quota creation, including different actors.
        quota = WorkspaceQuota(workspace_id=workspace_id)
        db.add(quota)
        db.flush()
    return capacity, quota


def _job(db, job_id, workspace_id=None, *, lock=False, skip_locked=False):
    query = select(Job).where(Job.id == job_id)
    if workspace_id is not None:
        query = query.where(Job.workspace_id == workspace_id)
    if lock:
        query = query.with_for_update(skip_locked=skip_locked)
    item = _read(db, query)
    if item is None and not skip_locked:
        raise Problem('NOT_FOUND', '任务不存在。', 404)
    return item


def _audit(db, job, action, *, actor_id=None, detail=None):
    workspace = db.get(Workspace, job.workspace_id)
    db.add(AuditEvent(id=new_id(), org_id=workspace.org_id, workspace_id=job.workspace_id,
                     actor_id=actor_id, action='job.' + action, resource_type='job',
                     resource_id=job.id, request_id=new_id(), safe_detail=detail or {}))


def _settle(db, job, capacity, quota, state, *, result=None, error_code=None, actor_id=None, external_retained_bytes=0):
    """Caller has authorization/capacity/quota/job locks, in that order."""
    if job.state in TERMINAL:
        return job
    result_bytes = 0 if result is None else len(_json(result, MAX_RESULT_BYTES)[0])
    if type(external_retained_bytes) is not int or not 0 <= external_retained_bytes <= 256 * 1024**2:
        raise Problem('INVALID_RETAINED_BYTES', '持久化容量无效。')
    retained = job.base_bytes + result_bytes + external_retained_bytes
    if retained > job.reserved_bytes:
        raise Problem('JOB_RESERVATION_EXCEEDED', '任务结果超过已预留容量。', 507)
    stamp, reserve, was_active = _stamp(db), job.reserved_bytes, job.state in ACTIVE
    capacity.pending_count -= 1
    capacity.pending_bytes -= reserve
    capacity.active_count -= int(was_active)
    quota.pending_count -= 1
    quota.reserved_bytes -= reserve
    quota.committed_bytes += retained
    db.add(QuotaLedger(job_id=job.id, workspace_id=job.workspace_id, event='settle',
                       reserved_delta=-reserve, committed_delta=retained))
    job.state, job.finished_at, job.error_code = state, stamp, error_code
    job.result, job.retained_bytes, job.reserved_bytes = result, retained, 0
    job.external_retained_bytes = external_retained_bytes
    if job.attempt:
        attempt = db.get(JobAttempt, (job.id, job.attempt))
        attempt.state, attempt.finished_at = state, stamp
    _audit(db, job, state, actor_id=actor_id, detail={'error_code': error_code} if error_code else {})
    db.flush()
    return job


def admit(db, workspace_id, actor_id, kind, key, request_snapshot, reserve_bytes, *, authorize_inputs=None):
    payload, snapshot = _json(request_snapshot, MAX_SNAPSHOT_BYTES)
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise Problem('IDEMPOTENCY_REQUIRED', '请提供有效请求标识。', 400)
    base_bytes = len(payload) + METADATA_BUDGET_BYTES
    if type(reserve_bytes) is not int or reserve_bytes < base_bytes or reserve_bytes > 1024**3:
        raise Problem('INVALID_RESERVATION', '预留容量不足或超出范围。')
    _authorize(db, workspace_id, actor_id, kind, snapshot, authorize_inputs)
    fingerprint = hashlib.sha256(payload + b'\0' + str(reserve_bytes).encode()).hexdigest()
    capacity, quota = _capacity(db, workspace_id)
    existing = _read(db, select(Job).where(Job.workspace_id == workspace_id, Job.actor_id == actor_id,
                                         Job.kind == kind, Job.idempotency_key == key))
    if existing:
        if existing.request_hash != fingerprint:
            raise Problem('IDEMPOTENCY_CONFLICT', '同一请求标识绑定了不同内容。', 409)
        return existing
    if capacity.recovery_hold:
        raise Problem('RECOVERY_HOLD', '恢复验证期间暂停接收任务。', 503)
    if (capacity.pending_count >= capacity.max_pending or quota.pending_count >= quota.max_pending
            or capacity.pending_bytes + reserve_bytes > capacity.max_pending_bytes):
        raise Problem('QUEUE_CAPACITY', '任务队列已满，请稍后重试。', 429)
    if quota.committed_bytes + quota.reserved_bytes + reserve_bytes > quota.limit_bytes:
        raise Problem('WORKSPACE_QUOTA', '工作区容量不足。', 507)
    job = Job(id=new_id(), workspace_id=workspace_id, actor_id=actor_id, kind=kind,
              idempotency_key=key, request_hash=fingerprint, request_snapshot=snapshot,
              reserved_bytes=reserve_bytes, base_bytes=base_bytes)
    db.add(job)
    db.flush()
    capacity.pending_count += 1
    capacity.pending_bytes += reserve_bytes
    quota.pending_count += 1
    quota.reserved_bytes += reserve_bytes
    db.add(QuotaLedger(job_id=job.id, workspace_id=workspace_id, event='reserve',
                       reserved_delta=reserve_bytes, committed_delta=0))
    _audit(db, job, 'admitted', actor_id=actor_id)
    db.flush()
    return job


def claim(db, worker_id, *, lease_seconds=60, authorize_inputs=None):
    if (not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128
            or any(ord(c) < 33 or ord(c) > 126 for c in worker_id)
            or type(lease_seconds) is not int or not 1 <= lease_seconds <= 300):
        raise Problem('INVALID_WORKER', '执行器参数无效。')
    # Discover without locks; never hold job before identity/capacity/quota.
    candidate = _read(db, select(Job).where(Job.state == 'queued').order_by(Job.created_at, Job.id).limit(1))
    if candidate is None:
        return None
    allowed = _authorized(db, candidate, authorize_inputs)
    capacity, quota = _capacity(db, candidate.workspace_id)
    job = _job(db, candidate.id, lock=True, skip_locked=True)
    if job is None or job.state != 'queued' or capacity.recovery_hold:
        return None
    if not allowed:
        _settle(db, job, capacity, quota, 'cancelled', error_code='AUTHORIZATION_REVOKED')
        return None
    if capacity.active_count:
        return None
    stamp = _stamp(db)
    job.state, job.worker_id = 'running', worker_id
    job.attempt += 1
    job.fence_token, job.generation = new_id(), capacity.generation
    job.lease_until = stamp + timedelta(seconds=lease_seconds)
    capacity.active_count += 1
    db.add(JobAttempt(job_id=job.id, attempt=job.attempt, fence_token=job.fence_token,
                      worker_id=worker_id, started_at=stamp, state='running'))
    _audit(db, job, 'claimed', detail={'attempt': job.attempt})
    db.flush()
    return job


def cancel(db, workspace_id, actor_id, job_id):
    membership = authorize_workspace(db, actor_id, workspace_id, 'jobs.read', lock=True)
    permissions = workspace_permissions(membership.roles)
    candidate = _job(db, job_id, workspace_id)
    if 'jobs.manage' not in permissions and not ('jobs.cancel_own' in permissions and candidate.actor_id == actor_id):
        raise Problem('FORBIDDEN', '没有取消此任务的权限。', 403)
    capacity, quota = _capacity(db, workspace_id)
    job = _job(db, job_id, workspace_id, lock=True)
    if job.state in TERMINAL or job.state == 'cancel_requested':
        return job
    if job.state == 'queued':
        return _settle(db, job, capacity, quota, 'cancelled', actor_id=actor_id)
    job.state = 'cancel_requested'
    _audit(db, job, 'cancel_requested', actor_id=actor_id)
    db.flush()
    return job


def _worker_locked(db, job_id, callback=None):
    candidate = _job(db, job_id)
    allowed = _authorized(db, candidate, callback)
    capacity, quota = _capacity(db, candidate.workspace_id)
    job = _job(db, job_id, lock=True)
    if not allowed and job.state in TERMINAL:
        raise Problem('FORBIDDEN', '任务权限已撤销。', 403)
    return job, capacity, quota, allowed


def _fence(db, job, capacity, token, *, live=True):
    if not token or token != job.fence_token:
        raise Problem('JOB_FENCED', '执行尝试已失效。', 409)
    if live and (job.generation != capacity.generation or capacity.recovery_hold
                 or job.lease_until is None or job.lease_until <= _stamp(db)):
        raise Problem('JOB_FENCED', '执行租约已失效。', 409)


def _revoke_running(db, job):
    if job.state == 'running':
        job.state = 'cancel_requested'
        job.error_code = 'AUTHORIZATION_REVOKED'
        _audit(db, job, 'authorization_revoked')
        db.flush()
    return job


def finish(db, job_id, fence_token, result, *, authorize_inputs=None, external_retained_bytes=0):
    _, clean_result = _json(result, MAX_RESULT_BYTES)
    job, capacity, quota, allowed = _worker_locked(db, job_id, authorize_inputs)
    _fence(db, job, capacity, fence_token, live=job.state not in TERMINAL)
    if not allowed:
        if job.state in TERMINAL:
            raise Problem('FORBIDDEN', '任务权限已撤销。', 403)
        return _revoke_running(db, job)
    if job.state == 'succeeded':
        if job.result != clean_result or job.external_retained_bytes != external_retained_bytes:
            raise Problem('JOB_RESULT_CONFLICT', '已提交结果与本次内容不同。', 409)
        return job
    if job.state in TERMINAL:
        return job
    if job.state == 'cancel_requested':
        # Compute has returned, so it is now safe to release the active slot.
        return _settle(db, job, capacity, quota, 'cancelled', error_code=job.error_code)
    if job.state != 'running':
        raise Problem('JOB_NOT_RUNNING', '任务尚未执行。', 409)
    return _settle(db, job, capacity, quota, 'succeeded', result=clean_result, external_retained_bytes=external_retained_bytes)


def fail(db, job_id, fence_token, error_code, *, authorize_inputs=None):
    if not isinstance(error_code, str) or not 1 <= len(error_code) <= 64 or not all(c.isascii() and (c.isupper() or c.isdigit() or c == '_') for c in error_code):
        raise Problem('INVALID_ERROR_CODE', '任务错误码无效。')
    job, capacity, quota, allowed = _worker_locked(db, job_id, authorize_inputs)
    _fence(db, job, capacity, fence_token, live=job.state not in TERMINAL)
    if job.state in TERMINAL:
        return job
    return _settle(db, job, capacity, quota, 'cancelled' if not allowed or job.state == 'cancel_requested' else 'failed',
                   error_code='AUTHORIZATION_REVOKED' if not allowed else error_code)


def acknowledge_cancel(db, job_id, fence_token):
    """Worker calls only after its compute child has actually stopped."""
    job, capacity, quota, allowed = _worker_locked(db, job_id)
    _fence(db, job, capacity, fence_token, live=job.state not in TERMINAL)
    if job.state in TERMINAL:
        return job
    if job.state != 'cancel_requested' and allowed:
        raise Problem('CANCEL_NOT_REQUESTED', '未请求取消该任务。', 409)
    return _settle(db, job, capacity, quota, 'cancelled', error_code=job.error_code)


def renew(db, job_id, fence_token, *, lease_seconds=60, authorize_inputs=None):
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise Problem('INVALID_LEASE', '租约时间无效。')
    job, capacity, _, allowed = _worker_locked(db, job_id, authorize_inputs)
    _fence(db, job, capacity, fence_token)
    if not allowed:
        return _revoke_running(db, job)
    if job.state == 'running':
        job.lease_until = _stamp(db) + timedelta(seconds=lease_seconds)
        db.flush()
    return job


def recover_expired(db, job_id, *, authorize_inputs=None):
    """Internal controller; safe to repeat. Does not retry any computation."""
    job, capacity, quota, allowed = _worker_locked(db, job_id, authorize_inputs)
    if job.state in TERMINAL:
        return job
    if job.state == 'queued':
        return job if allowed else _settle(db, job, capacity, quota, 'cancelled', error_code='AUTHORIZATION_REVOKED')
    if job.generation == capacity.generation and job.lease_until and job.lease_until > _stamp(db):
        return job if allowed else _revoke_running(db, job)
    return _settle(db, job, capacity, quota, 'cancelled' if job.state == 'cancel_requested' else 'interrupted',
                   error_code='LEASE_EXPIRED')


def job_view(job):
    """Safe status summary; request/result data need separate scoped permissions."""
    return {'id': job.id, 'workspace_id': job.workspace_id, 'kind': job.kind,
            'state': job.state, 'attempt': job.attempt, 'error_code': job.error_code,
            'created_at': job.created_at.isoformat(),
            'finished_at': job.finished_at.isoformat() if job.finished_at else None}
