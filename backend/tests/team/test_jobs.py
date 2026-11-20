"""Queue invariants on isolated PostgreSQL schemas; never rebuild public."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import threading
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlalchemy.orm import sessionmaker

from api.common import Problem
from team import jobs
from team.job_models import GlobalCapacity, Job, JobAttempt, QuotaLedger, WorkspaceQuota
from team.models import AuditEvent, Membership, User, Workspace

BACKEND = Path(__file__).resolve().parents[2]
ORG = '00000000-0000-0000-0000-000000000001'


@pytest.fixture
def dbs():
    url = os.environ.get('TEST_DATABASE_URL', '')
    if not url:
        pytest.skip('TEST_DATABASE_URL required: real PostgreSQL')
    parsed = make_url(url)
    if parsed.database != 'reconcile_test' or parsed.get_backend_name() != 'postgresql':
        pytest.fail('Queue tests require the dedicated PostgreSQL reconcile_test database')
    schema = 'queue_test_' + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = create_engine(url, connect_args={'options': '-csearch_path=' + schema}, pool_size=8)
    Session = sessionmaker(engine, expire_on_commit=False)
    try:
        with engine.begin() as connection:
            config = Config(str(BACKEND / 'alembic.ini'))
            config.set_main_option('script_location', str(BACKEND / 'alembic'))
            config.attributes['connection'] = connection
            command.upgrade(config, '0009')
        with Session.begin() as db:
            db.add_all([User(id='operator', login='queue.operator', password_hash='unused'),
                        User(id='other', login='queue.other', password_hash='unused'),
                        User(id='viewer', login='queue.viewer', password_hash='unused'),
                        User(id='manager', login='queue.manager', password_hash='unused')])
            db.add_all([Workspace(id='w1', org_id=ORG, name='Queue A'), Workspace(id='w2', org_id=ORG, name='Queue B')])
            db.flush()
            db.add_all([Membership(workspace_id=w, user_id=u, roles=[role])
                        for w in ['w1', 'w2']
                        for u, role in [('operator', 'operator'), ('other', 'operator'), ('viewer', 'viewer'), ('manager', 'workspace_manager')]])
        yield Session
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def submit(dbs, key='k1', workspace='w1', actor='operator', reserve=65536, snapshot=None):
    with dbs.begin() as db:
        return jobs.admit(db, workspace, actor, 'reconcile', key, snapshot or {'rule_version': 'r1'}, reserve)


def take(dbs):
    with dbs.begin() as db:
        return jobs.claim(db, 'worker-1')


def assert_problem(code, action):
    with pytest.raises(Problem) as caught:
        action()
    assert caught.value.code == code


def stats(dbs):
    with dbs() as db:
        capacity = db.get(GlobalCapacity, 1)
        return (capacity.pending_count, capacity.active_count, capacity.pending_bytes,
                db.scalar(select(func.count()).select_from(QuotaLedger)))


def test_admit_idempotency_scoped_conflicts_and_reauthorization(dbs):
    first = submit(dbs)
    replay = submit(dbs)
    assert first.id == replay.id
    assert stats(dbs) == (1, 0, 65536, 1)
    assert_problem('IDEMPOTENCY_CONFLICT', lambda: submit(dbs, snapshot={'rule_version': 'different'}))
    assert_problem('IDEMPOTENCY_CONFLICT', lambda: submit(dbs, reserve=65537))
    assert submit(dbs, workspace='w2').id != first.id
    assert submit(dbs, actor='other').id != first.id
    with dbs.begin() as db:
        db.get(Membership, ('w1','operator')).roles = ['viewer']
    assert_problem('FORBIDDEN', lambda: submit(dbs))


def test_complete_result_and_quota_are_atomic_and_replayable(dbs):
    first = submit(dbs)
    claimed = take(dbs)
    assert claimed.id == first.id and claimed.state == 'running' and claimed.attempt == 1
    result = {'difference': '0.01', 'rows': 2}
    with dbs.begin() as db:
        completed = jobs.finish(db, first.id, claimed.fence_token, result)
        assert completed.state == 'succeeded' and completed.result == result
    assert stats(dbs) == (0, 0, 0, 2)
    with dbs.begin() as db:
        replay = jobs.finish(db, first.id, claimed.fence_token, result)
        assert replay.state == 'succeeded'
        quota = db.get(WorkspaceQuota, 'w1')
        assert quota.reserved_bytes == 0 and quota.committed_bytes == replay.retained_bytes > replay.base_bytes
    assert stats(dbs) == (0, 0, 0, 2)
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.finish(db, first.id, claimed.fence_token, {'difference': '0.02'})
    assert caught.value.code == 'JOB_RESULT_CONFLICT'
    assert 'result' not in jobs.job_view(completed)
    assert 'request_snapshot' not in jobs.job_view(completed)


def test_concurrent_global_limit_across_workspaces(dbs):
    with dbs.begin() as db:
        db.get(GlobalCapacity, 1).max_pending = 1
    barrier = threading.Barrier(2)
    def action(index):
        barrier.wait(timeout=5)
        try:
            return submit(dbs, workspace='w' + str(index+1), actor=['operator','other'][index]).state
        except Problem as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(action, [0,1]))
    assert sorted(outcomes) == ['QUEUE_CAPACITY', 'queued']
    assert stats(dbs) == (1,0,65536,1)


def test_workspace_capacity_and_quota_rollback(dbs):
    first = submit(dbs)
    with dbs.begin() as db:
        quota = db.get(WorkspaceQuota, 'w1')
        quota.limit_bytes = 65536
    assert_problem('WORKSPACE_QUOTA', lambda: submit(dbs, key='k2'))
    assert stats(dbs) == (1,0,65536,1)
    with dbs.begin() as db:
        jobs.cancel(db, 'w1', 'operator', first.id)
        quota = db.get(WorkspaceQuota, 'w1')
        assert 0 < quota.committed_bytes < 65536 and quota.reserved_bytes == 0


def test_single_active_compute_even_with_two_dispatchers(dbs):
    submit(dbs, key='one')
    submit(dbs, key='two', workspace='w2', actor='other')
    barrier = threading.Barrier(2)
    def action(index):
        barrier.wait(timeout=5)
        with dbs.begin() as db:
            item = jobs.claim(db, 'worker-' + str(index))
            return item.id if item else None
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(action, [0,1]))
    assert len([x for x in outcomes if x]) == 1
    assert stats(dbs)[0:2] == (2,1)
    with pytest.raises(IntegrityError), dbs.begin() as db:
        db.execute(text("UPDATE team_jobs SET state='running' WHERE state='queued'"))


def test_cancel_before_completion_never_publishes_result(dbs):
    first = submit(dbs)
    active = take(dbs)
    with dbs.begin() as db:
        assert jobs.cancel(db, 'w1', 'operator', first.id).state == 'cancel_requested'
    assert stats(dbs)[0:2] == (1,1)  # Child has not stopped yet.
    with dbs.begin() as db:
        item = jobs.finish(db, first.id, active.fence_token, {'late': 'result'})
        assert item.state == 'cancelled' and item.result is None
    assert stats(dbs) == (0,0,0,2)
    with dbs.begin() as db:
        assert jobs.cancel(db, 'w1', 'operator', first.id).state == 'cancelled'
        assert jobs.acknowledge_cancel(db, first.id, active.fence_token).state == 'cancelled'
    assert stats(dbs) == (0,0,0,2)


def test_completion_before_cancel_stays_succeeded(dbs):
    first = submit(dbs)
    active = take(dbs)
    with dbs.begin() as db:
        jobs.finish(db, first.id, active.fence_token, {'ok': True})
    with dbs.begin() as db:
        assert jobs.cancel(db, 'w1', 'operator', first.id).state == 'succeeded'


def test_cancel_and_publish_concurrently_have_one_outcome(dbs):
    first = submit(dbs)
    active = take(dbs)
    barrier = threading.Barrier(2)
    def action(index):
        barrier.wait(timeout=5)
        with dbs.begin() as db:
            if index:
                return jobs.cancel(db, 'w1', 'manager', first.id).state
            return jobs.finish(db, first.id, active.fence_token, {'ok': True}).state
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(action, [0,1]))
    with dbs() as db:
        item = db.get(Job, first.id)
        assert item.state in ('succeeded', 'cancelled')
        assert (item.result is not None) == (item.state == 'succeeded')
    assert stats(dbs) == (0,0,0,2)


def test_claim_cancels_revoked_queued_and_releases_once(dbs):
    first = submit(dbs)
    with dbs.begin() as db:
        db.get(Membership, ('w1','operator')).roles = ['viewer']
    assert take(dbs) is None
    with dbs() as db:
        item = db.get(Job, first.id)
        assert item.state == 'cancelled' and item.error_code == 'AUTHORIZATION_REVOKED'
    assert stats(dbs) == (0,0,0,2)


def test_publish_rechecks_kind_permission_not_only_membership(dbs):
    first = submit(dbs)
    active = take(dbs)
    with dbs.begin() as db:
        db.get(Membership, ('w1','operator')).roles = ['reviewer']
    with dbs.begin() as db:
        item = jobs.finish(db, first.id, active.fence_token, {'must_not_publish': True})
        assert item.state == 'cancel_requested' and item.result is None
    with dbs.begin() as db:
        assert jobs.acknowledge_cancel(db, first.id, active.fence_token).state == 'cancelled'
    assert stats(dbs) == (0,0,0,2)


def test_fence_expired_lease_and_recovery_idempotent(dbs):
    first = submit(dbs)
    active = take(dbs)
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.finish(db, first.id, 'wrong-token', {})
    assert caught.value.code == 'JOB_FENCED'
    with dbs.begin() as db:
        db.execute(text("UPDATE team_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {'id':first.id})
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.finish(db, first.id, active.fence_token, {})
    assert caught.value.code == 'JOB_FENCED'
    for _ in range(2):
        with dbs.begin() as db:
            assert jobs.recover_expired(db, first.id).state == 'interrupted'
    assert stats(dbs) == (0,0,0,2)


def test_recovery_hold_blocks_dispatch_and_new_admission(dbs):
    first = submit(dbs)
    with dbs.begin() as db:
        db.get(GlobalCapacity,1).recovery_hold = True
    assert take(dbs) is None
    assert_problem('RECOVERY_HOLD', lambda: submit(dbs,key='new'))
    assert submit(dbs).id == first.id  # Authorized status replay remains read-only.
    with dbs.begin() as db:
        assert jobs.recover_expired(db, first.id).state == 'queued'
    assert stats(dbs) == (1,0,65536,1)


def test_generation_change_fences_old_attempt(dbs):
    first=submit(dbs)
    active=take(dbs)
    with dbs.begin() as db:
        db.get(GlobalCapacity,1).generation += 1
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.renew(db, first.id, active.fence_token)
    assert caught.value.code == 'JOB_FENCED'
    with dbs.begin() as db:
        assert jobs.recover_expired(db, first.id).state == 'interrupted'


def test_failed_job_and_late_failure_cannot_overwrite(dbs):
    first=submit(dbs)
    active=take(dbs)
    with dbs.begin() as db:
        assert jobs.fail(db, first.id, active.fence_token, 'COMPUTE_FAILED').state == 'failed'
    with dbs.begin() as db:
        assert jobs.fail(db, first.id, active.fence_token, 'OTHER_FAILURE').error_code == 'COMPUTE_FAILED'
    assert stats(dbs) == (0,0,0,2)
    with pytest.raises(DBAPIError), dbs.begin() as db:
        db.execute(text("UPDATE team_jobs SET state='queued' WHERE id=:id"), {'id':first.id})


def test_request_and_result_immutable_in_database(dbs):
    first=submit(dbs)
    with pytest.raises(DBAPIError), dbs.begin() as db:
        db.execute(text("UPDATE team_jobs SET request_snapshot='{}' WHERE id=:id"), {'id':first.id})
    active=take(dbs)
    with dbs.begin() as db:
        jobs.finish(db, first.id, active.fence_token, {'immutable': True})
    with pytest.raises(DBAPIError), dbs.begin() as db:
        db.execute(text("UPDATE team_jobs SET result='{}' WHERE id=:id"), {'id':first.id})
    with pytest.raises(DBAPIError), dbs.begin() as db:
        db.execute(text("DELETE FROM team_job_quota_ledger WHERE job_id=:id"), {'id':first.id})


def test_callback_at_admission_claim_and_publish(dbs):
    calls=[]
    def allow(db,w,a,k,s):
        calls.append((w,a,k,s))
    def deny(db,w,a,k,s):
        raise Problem('NOT_FOUND','missing input',404)
    with pytest.raises(Problem), dbs.begin() as db:
        jobs.admit(db,'w1','operator','reconcile','denied',{},65536,authorize_inputs=deny)
    with dbs.begin() as db:
        first=jobs.admit(db,'w1','operator','reconcile','accepted',{},65536,authorize_inputs=allow)
    with dbs.begin() as db:
        active=jobs.claim(db,'worker-1',authorize_inputs=allow)
    with dbs.begin() as db:
        assert jobs.finish(db,first.id,active.fence_token,{},authorize_inputs=deny).state=='cancel_requested'
    assert len(calls)==2


def test_audit_failure_rolls_back_admission_and_capacity(dbs,monkeypatch):
    def broken(*args,**kwargs):
        raise RuntimeError('audit write failed')
    monkeypatch.setattr(jobs,'_audit',broken)
    with pytest.raises(RuntimeError):
        submit(dbs)
    assert stats(dbs)==(0,0,0,0)


def test_result_beyond_reservation_not_published(dbs):
    first=submit(dbs,reserve=16500)
    active=take(dbs)
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.finish(db,first.id,active.fence_token,{'large':'x'*1000})
    assert caught.value.code=='JOB_RESERVATION_EXCEEDED'
    with dbs() as db:
        assert db.get(Job,first.id).state=='running'
    with dbs.begin() as db:
        jobs.fail(db,first.id,active.fence_token,'RESULT_TOO_LARGE')
    assert stats(dbs)==(0,0,0,2)


def test_cancellation_scoped_and_viewer_denied(dbs):
    first=submit(dbs)
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.cancel(db,'w2','operator',first.id)
    assert caught.value.status==404
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.cancel(db,'w1','other',first.id)
    assert caught.value.status==403
    assert_problem('FORBIDDEN',lambda:submit(dbs,actor='viewer'))
    with dbs.begin() as db:
        assert jobs.cancel(db,'w1','manager',first.id).state=='cancelled'


def test_renew_uses_database_clock_and_keeps_request_private(dbs):
    first=submit(dbs)
    active=take(dbs)
    with dbs.begin() as db:
        item=jobs.renew(db,first.id,active.fence_token,lease_seconds=120)
        stamp=db.scalar(select(func.clock_timestamp()))
        assert timedelta(seconds=115)<item.lease_until-stamp<=timedelta(seconds=120)
        assert len(db.scalars(select(JobAttempt)).all())==1


@pytest.mark.parametrize('operation',['finish','fail','acknowledge_cancel','renew'])
def test_revoked_terminal_replay_never_returns_result(dbs,operation):
    first=submit(dbs)
    active=take(dbs)
    with dbs.begin() as db:
        jobs.finish(db,first.id,active.fence_token,{'private':'result'})
    with dbs.begin() as db:
        db.get(Membership,('w1','operator')).roles=['viewer']
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        args=[db,first.id,active.fence_token]
        if operation=='finish':args.append({'private':'result'})
        if operation=='fail':args.append('LATE_FAILURE')
        getattr(jobs,operation)(*args)
    assert caught.value.status==403


@pytest.mark.parametrize('worker_id',['worker\nsecret','worker\x00','含义',''])
def test_worker_identifiers_reject_control_and_unbounded_metadata(dbs,worker_id):
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.claim(db,worker_id)
    assert caught.value.code=='INVALID_WORKER'


@pytest.mark.parametrize('kind',['model','arbitrary',''])
def test_unsupported_job_kind_never_queued(dbs,kind):
    with pytest.raises(Problem) as caught, dbs.begin() as db:
        jobs.admit(db,'w1','operator',kind,'key',{},65536)
    assert caught.value.code=='UNSUPPORTED_JOB_KIND'
    assert stats(dbs)==(0,0,0,0)
