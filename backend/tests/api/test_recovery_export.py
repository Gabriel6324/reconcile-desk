"""Regressions for export admission and unknown-COMMIT recovery on real PostgreSQL."""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text

from api.common import Problem
from api.db import Export, Operation
from api.main import app
from test_postgres import client, pair, confirmed, run, wait, upload, URL

pytestmark = pytest.mark.skipif(not URL, reason='TEST_DATABASE_URL required: real PostgreSQL')


def completed_run(client, pair):
    mapping_id = confirmed(client, pair)
    operation = wait(client, run(client, mapping_id))
    assert operation['state'] == 'completed', operation
    return operation['resource_id']


def test_concurrent_export_requests_reuse_one_operation(client, pair):
    run_id = completed_run(client, pair)
    barrier = threading.Barrier(2)

    def request_export(_):
        barrier.wait(timeout=5)
        return client.post(f'/api/runs/{run_id}/exports',
                           headers={'Idempotency-Key': str(uuid.uuid4())})

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(request_export, range(2)))
    operations = [wait(client, response) for response in responses]
    assert all(item['state'] == 'completed' for item in operations), operations
    assert operations[0]['operation_id'] == operations[1]['operation_id']
    with app.state.runner.Session() as session:
        assert session.scalar(select(func.count()).select_from(Export).where(Export.run_id == run_id)) == 1
        assert session.scalar(select(func.count()).select_from(Operation).where(
            Operation.kind == 'export', Operation.request_snapshot['run_id'].astext == run_id)) == 1


def test_existing_export_still_checks_key_conflicts_and_missing_key(client, pair):
    run_id = completed_run(client, pair)
    operation = wait(client, client.post(f'/api/runs/{run_id}/exports',
                                       headers={'Idempotency-Key': str(uuid.uuid4())}))
    assert operation['state'] == 'completed'
    conflicting_key = str(uuid.uuid4())
    imported = upload(client, b'id,x\nconflict,1\n', key=conflicting_key)
    assert imported['state'] == 'completed'
    response = client.post(f'/api/runs/{run_id}/exports', headers={'Idempotency-Key': conflicting_key})
    assert response.status_code == 409
    assert response.json()['code'] == 'IDEMPOTENCY_CONFLICT'
    assert client.post(f'/api/runs/{run_id}/exports').status_code == 400


def test_rolled_back_admission_recovers_health_and_accepts_same_key(client):
    runner = app.state.runner
    key = str(uuid.uuid4())
    with pytest.raises(Problem) as raised:
        runner.recover_commit(key)
    assert raised.value.code == 'COMMIT_NOT_FOUND'
    assert runner.probe()
    operation = upload(client, b'id,x\nretry,1\n', key=key)
    assert operation['state'] == 'completed'


def test_heartbeat_survives_slow_commit_recovery(client):
    runner = app.state.runner
    key = str(uuid.uuid4())
    outcome = []

    def recover():
        try:
            runner.recover_commit(key)
        except Problem as error:
            outcome.append(error.code)

    # Block readback on the real executor row for longer than the heartbeat interval.
    with runner.engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text('SELECT id FROM executor_state WHERE id=1 FOR UPDATE'))
        thread = threading.Thread(target=recover)
        thread.start()
        assert runner.recovering.wait(2)
        time.sleep(1.3)
        assert runner.threads[0].is_alive()
        transaction.commit()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome == ['COMMIT_NOT_FOUND']
    assert runner.probe()
    time.sleep(1.1)
    assert runner.threads[0].is_alive()
