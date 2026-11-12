"""Integration tests against a disposable, real PostgreSQL database only."""
import copy
import hashlib
import io
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, select
from api.main import app
from api.db import Operation, Import, Quota
from api.executor import Runner

URL = os.environ.get('TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not URL, reason='TEST_DATABASE_URL required: real PostgreSQL')
HEADERS = {'X-Reconcile-Client': 'web'}
DEFINITION = {'key': {'left_column_id': 'c1', 'right_column_id': 'c1', 'normalization': 'exact'},
              'comparisons': [{'left_column_id': 'c2', 'right_column_id': 'c2', 'normalization': 'exact', 'type': 'decimal'}]}

@pytest.fixture(scope='module')
def client():
    if URL.rsplit('/', 1)[-1] != 'reconcile_test':
        pytest.fail('Refusing destructive fixture outside reconcile_test database')
    os.environ['DATABASE_URL'] = URL
    engine = create_engine(URL)
    with engine.begin() as c:
        c.execute(text('DROP SCHEMA public CASCADE'))
        c.execute(text('CREATE SCHEMA public'))
    engine.dispose()
    with TestClient(app, headers=HEADERS, base_url='http://127.0.0.1') as c:
        yield c

def wait(c, response):
    assert response.status_code in (200, 202), response.text
    op = response.json()
    until = time.monotonic() + 35
    while op['state'] in ('running', 'stopping') and time.monotonic() < until:
        time.sleep(.05)
        op = c.get('/api/operations/' + op['operation_id']).json()
    return op

def upload(c, content, name='table.csv', key=None):
    response = c.post('/api/imports', files={'file': (name, content)},
                      headers={'Idempotency-Key': key or str(uuid.uuid4())})
    return wait(c, response)

@pytest.fixture(scope='module')
def pair(client):
    a = upload(client, b'id,amount\nA,1.00\nB,9007199254740993.01\nC,invalid\nD,1\nD,2\n,4\nL,5\n')
    b = upload(client, b'id,amount\nA,1\nB,9007199254740993.02\nC,0\nD,1\nR,5\n')
    assert a['state'] == b['state'] == 'completed'
    return a['resource_id'], b['resource_id']

def mapping(c, pair):
    r = c.post('/api/mappings', json={'left_import_id':pair[0], 'right_import_id':pair[1], 'definition':DEFINITION})
    assert r.status_code == 200, r.text
    return r.json()['mapping_id']

def confirmed(c, pair):
    ident = mapping(c, pair)
    r=c.post(f'/api/mappings/{ident}/confirm',json={'expected_revision':1})
    assert r.status_code == 200, r.text
    return ident

def run(c, ident, key=None, rev=1):
    return c.post('/api/runs',json={'mapping_id':ident,'expected_revision':rev},headers={'Idempotency-Key':key or str(uuid.uuid4())})

def test_ready_and_model_absent(client):
    assert client.get('/api/ready').status_code == 200
    assert client.get('/api/capabilities').json()['model']['configured'] is False

def test_import_original_and_provenance(client, pair):
    a=client.get('/api/imports/'+pair[0]).json()
    raw=client.get('/api/imports/'+pair[0]+'/source').content
    assert hashlib.sha256(raw).hexdigest()==a['sha256']
    row=client.get('/api/imports/'+pair[0]+'/rows/r2').json()
    assert row['source']['row']==3 and row['cells']['c2']['raw']=='9007199254740993.01'

def test_idempotent_import_and_conflict(client):
    key=str(uuid.uuid4()); a=upload(client,b'id,x\nA,1\n',key=key)
    b=upload(client,b'id,x\nA,1\n',key=key)
    assert a['operation_id']==b['operation_id']
    r=client.post('/api/imports',files={'file':('table.csv',b'id,x\nA,2\n')},headers={'Idempotency-Key':key})
    assert r.status_code==409 and r.json()['code']=='IDEMPOTENCY_CONFLICT'

def test_unconfirmed_and_cas(client,pair):
    ident=mapping(client,pair)
    assert run(client,ident).status_code==409
    assert client.post(f'/api/mappings/{ident}/confirm',json={'expected_revision':1}).status_code==200
    response=client.put(f'/api/mappings/{ident}',json={'expected_revision':1,'definition':DEFINITION})
    assert response.json()['confirmation'] is None
    assert client.put(f'/api/mappings/{ident}',json={'expected_revision':1,'definition':DEFINITION}).status_code==409
    assert run(client,ident,rev=2).status_code==409

def test_concurrent_mapping_edit_one_winner(client,pair):
    ident=mapping(client,pair)
    def edit(_):return client.put(f'/api/mappings/{ident}',json={'expected_revision':1,'definition':DEFINITION}).status_code
    with ThreadPoolExecutor(2) as pool: outcomes=list(pool.map(edit,range(2)))
    assert sorted(outcomes)==[200,409]

def test_complete_run_history_export_and_immutability(client,pair):
    ident=confirmed(client,pair); key=str(uuid.uuid4()); op=wait(client,run(client,ident,key))
    assert op['state']=='completed',op
    rid=op['resource_id']; saved=client.get('/api/runs/'+rid).json()
    counts=saved['summary']['counts']
    assert counts=={'equal':1,'changed':1,'left_only':1,'right_only':1,'invalid_value':1,'duplicate_key':1,'blank_key':1}
    groups=client.get(f'/api/runs/{rid}/results').json()['items']
    refs=[(side,row) for g in groups for side in ('left','right') for row in g[side+'_rows']]
    assert len(refs)==len(set(refs))==12
    changed=client.get(f'/api/runs/{rid}/results?status=changed').json()['items']
    assert changed[0]['comparisons'][0]['difference']=='0.01'
    assert client.get(f'/api/runs/{rid}/export').status_code==409
    ep=wait(client,client.post(f'/api/runs/{rid}/exports',headers={'Idempotency-Key':str(uuid.uuid4())}))
    assert ep['state']=='completed',ep
    data=client.get(f'/api/runs/{rid}/export').content
    assert data[:2]==b'PK'
    import openpyxl
    wb=openpyxl.load_workbook(io.BytesIO(data))
    assert len(wb.sheetnames)>=4
    assert all(c.data_type!='f' and c.hyperlink is None for s in wb for row in s for c in row)
    assert client.post(f'/api/runs/{rid}/exports',headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==200
    assert client.get(f'/api/runs/{rid}/export').content==data
    assert wait(client,run(client,ident,key))['operation_id']==op['operation_id']
    edited=copy.deepcopy(DEFINITION);edited['comparisons'][0]['type']='text'
    client.put(f'/api/mappings/{ident}',json={'expected_revision':1,'definition':edited})
    assert client.get('/api/runs/'+rid).json()==saved
    assert client.get(f'/api/runs/{rid}/export').content==data

def test_failed_import_retains_original_and_quota(client):
    raw=b'id,x\nA,"bad'
    op=upload(client,raw)
    assert op['state']=='failed'
    assert client.get('/api/imports/'+op['resource_id']+'/source').content==raw
    with app.state.runner.Session() as s:
        q=s.get(Quota,1)
        assert q.reserved==0 and q.committed>=len(raw)

def test_same_import_both_sides(client,pair):
    ident=confirmed(client,(pair[0],pair[0]))
    op=wait(client,run(client,ident))
    assert op['state']=='completed'
    summary=client.get('/api/runs/'+op['resource_id']).json()['summary']
    assert summary['input_rows']==14

def test_invalid_origin_and_unbounded_body_rejected(client):
    assert client.get('/api/health',headers={'Host':'untrusted.invalid'}).status_code==400
    assert client.post('/api/mappings',json={},headers={'Origin':'https://untrusted.invalid'}).status_code==403
    assert client.post('/api/mappings',content=b'x'*65537).status_code==413
    assert client.get('/api/runs?limit=101').status_code==422

def test_model_unavailable_is_explicit(client,pair):
    ident=mapping(client,pair)
    r=client.post(f'/api/mappings/{ident}/suggest',json={'expected_revision':1},headers={'Idempotency-Key':str(uuid.uuid4())})
    assert r.status_code==503 and r.json()['code']=='MODEL_NOT_CONFIGURED'

def test_second_executor_denied(client):
    other=Runner(create_engine(URL))
    with pytest.raises(RuntimeError,match='Another'):other.startup()
    other.engine.dispose()

def test_capacity_stopping_and_terminal_absorbing(client):
    r=app.state.runner
    with r.Session.begin() as s:
        ident=str(uuid.uuid4())
        s.add(Operation(id=ident,kind='import',idempotency_key=str(uuid.uuid4()),request_hash='0'*64,
                        epoch=r.epoch,token=str(uuid.uuid4()),state='stopping',request_snapshot={},reserved=12))
    response=client.post('/api/imports',files={'file':('x.csv',b'id,x\nA,1\n')},headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==429
    r.fail(ident,'TEST_STOP','stopped')
    r.complete(ident,'wrong-token',{})
    with r.Session() as s:
        assert s.get(Operation,ident).state=='failed'
        assert s.get(Quota,1).reserved==0

def test_unknown_commit_readback_preserves_completed(client):
    r=app.state.runner
    with r.Session() as s:
        item=s.scalar(select(Operation).where(Operation.state=='completed'))
        key,ident=item.idempotency_key,item.id
    answer=r.recover_commit(key)
    assert answer['state']=='completed' and answer['operation_id']==ident
    assert r.probe()

def test_quota_rejection_rolls_back(client):
    r=app.state.runner
    with r.Session.begin() as s:
        quota=s.get(Quota,1); original=quota.limit_bytes; quota.limit_bytes=quota.committed+1
    try:
        response=client.post('/api/imports',files={'file':('x.csv',b'id,x\nA,1\n')},headers={'Idempotency-Key':str(uuid.uuid4())})
        assert response.status_code==507
        with r.Session() as s:assert s.get(Quota,1).reserved==0
    finally:
        with r.Session.begin() as s:s.get(Quota,1).limit_bytes=original

def test_restart_recovers_active_and_keeps_history(client):
    first=app.state.runner
    with first.Session.begin() as s:
        op=Operation(id=str(uuid.uuid4()),kind='import',idempotency_key=str(uuid.uuid4()),request_hash='0'*64,
                     epoch=first.epoch,token=str(uuid.uuid4()),state='stopping',request_snapshot={},reserved=1234)
        s.add(op); ident=op.id
        before=s.scalar(select(Operation).where(Operation.state=='completed')).id
    first.close()
    replacement=Runner(first.engine); replacement.startup(); app.state.runner=replacement
    try:
        with replacement.Session() as s:
            assert s.get(Operation,ident).state=='interrupted'
            assert s.get(Operation,before).state=='completed'
            assert s.get(Quota,1).reserved==0
        assert replacement.epoch>first.epoch
    finally:replacement.close()
