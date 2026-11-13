"""Durable templates require explicit confirmation and match exact header names."""
import copy
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from api.common import encoded
from api.db import Mapping, MappingTemplate, Quota, Revision
from api.main import app
from test_postgres import client, pair, confirmed, mapping, run, wait, upload, URL, DEFINITION

pytestmark = pytest.mark.skipif(not URL, reason='TEST_DATABASE_URL required: real PostgreSQL')


def save(client, mapping_id, name=None, revision=1):
    return client.post('/api/templates', json={'name': name or '模板-' + str(uuid.uuid4()),
        'mapping_id': mapping_id, 'expected_revision': revision})


def preview(client, template_id, pair):
    return client.post(f'/api/templates/{template_id}/preview',
                       json={'left_import_id': pair[0], 'right_import_id': pair[1]})


def test_template_requires_current_confirmed_revision(client, pair):
    ident = mapping(client, pair)
    assert save(client, ident).json()['code'] == 'CONFIRMATION_STALE'
    client.post(f'/api/mappings/{ident}/confirm', json={'expected_revision': 1})
    assert save(client, ident).status_code == 200
    client.put(f'/api/mappings/{ident}', json={'expected_revision': 1, 'definition': DEFINITION})
    assert save(client, ident).json()['code'] == 'REVISION_CONFLICT'
    assert save(client, ident, revision=2).json()['code'] == 'CONFIRMATION_STALE'


def test_saved_template_contains_headers_and_semantics_without_rows(client, pair):
    name = '  每月订单核对  '
    saved = save(client, confirmed(client, pair), name).json()
    assert saved['name'] == name.strip() and saved['comparison_count'] == 1
    assert set(saved) == {'id', 'name', 'created_at', 'comparison_count'}
    with app.state.runner.Session() as s:
        spec = s.get(MappingTemplate, saved['id']).specification
        assert spec == {'schema_version': 1,
            'key': {'left_header': 'id', 'right_header': 'id', 'normalization': 'exact'},
            'comparisons': [{'left_header': 'amount', 'right_header': 'amount', 'normalization': 'exact', 'type': 'decimal'}]}
        assert b'9007199254740993' not in encoded(spec)
    assert save(client, confirmed(client, pair)).status_code == 200
    first = client.get('/api/templates?limit=1').json()
    second = client.get('/api/templates?limit=1&cursor=' + first['next_cursor']).json()
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert client.get('/api/templates?limit=101').status_code == 422


def test_preview_rebinds_reordered_columns_and_does_not_confirm(client, pair):
    saved = save(client, confirmed(client, pair)).json()
    a = upload(client, b'amount,extra,id\n12.00,left,A\n')
    b = upload(client, b'extra,id,amount\nright,A,12.01\n')
    current = (a['resource_id'], b['resource_id'])
    with app.state.runner.Session() as s:
        before = s.scalar(select(func.count()).select_from(Mapping))
    response = preview(client, saved['id'], current)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['template_id'] == saved['id'] and value['name'] == saved['name']
    assert value['definition']['key']['left_column_id'] == 'c3'
    assert value['definition']['key']['right_column_id'] == 'c2'
    assert value['definition']['comparisons'][0]['left_column_id'] == 'c1'
    assert value['definition']['comparisons'][0]['right_column_id'] == 'c3'
    with app.state.runner.Session() as s:
        assert s.scalar(select(func.count()).select_from(Mapping)) == before
    created = client.post('/api/mappings', json={'left_import_id': current[0], 'right_import_id': current[1],
                                               'definition': value['definition']}).json()
    assert created['confirmation'] is None
    assert run(client, created['mapping_id']).json()['code'] == 'CONFIRMATION_STALE'
    client.post(f"/api/mappings/{created['mapping_id']}/confirm", json={'expected_revision': 1})
    operation = wait(client, run(client, created['mapping_id']))
    assert operation['state'] == 'completed'
    groups = client.get(f"/api/runs/{operation['resource_id']}/results").json()['items']
    assert groups[0]['comparisons'][0]['difference'] == '0.01'


@pytest.mark.parametrize('header', ['Amount', ' amount ', 'total'])
def test_preview_reports_missing_exact_headers_without_partial_mapping(client, pair, header):
    saved = save(client, confirmed(client, pair)).json()
    a = upload(client, f'id,{header}\nA,12\n'.encode())
    original = client.get('/api/mappings').json()
    response = preview(client, saved['id'], (a['resource_id'], pair[1]))
    assert response.status_code == 422
    assert response.json()['code'] == 'TEMPLATE_COLUMNS_MISSING'
    assert response.json()['details']['missing_headers'] == {'left': ['amount'], 'right': []}
    assert '左表' in response.json()['message'] and 'amount' in response.json()['message']
    assert 'definition' not in response.json()
    assert client.get('/api/mappings').json() == original


def test_duplicate_template_names_have_one_concurrent_winner(client, pair):
    ident = confirmed(client, pair)
    name = '重复-' + str(uuid.uuid4())
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda n: save(client, ident, n), [name, ' ' + name + ' ']))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()['code'] == 'TEMPLATE_NAME_EXISTS'


@pytest.mark.parametrize('name', ['', '   ', '长' * 81, '名称\x00', '名称\n中间'])
def test_invalid_template_name_is_rejected(client, pair, name):
    response = client.post('/api/templates', json={'name': name, 'mapping_id': confirmed(client, pair), 'expected_revision': 1})
    assert response.status_code == 422 and response.json()['code'] == 'INVALID_TEMPLATE_NAME'


def test_template_quota_failure_rolls_back_and_delete_reclaims_only_template_bytes(client, pair):
    runner = app.state.runner
    ident = confirmed(client, pair)
    with runner.Session.begin() as s:
        quota = s.get(Quota, 1)
        original_limit, original_committed = quota.limit_bytes, quota.committed
        quota.limit_bytes = original_committed
        before = s.scalar(select(func.count()).select_from(MappingTemplate))
    try:
        response = save(client, ident)
        assert response.status_code == 507 and response.json()['code'] == 'WORKSPACE_QUOTA'
        with runner.Session() as s:
            assert s.scalar(select(func.count()).select_from(MappingTemplate)) == before
            assert s.get(Quota, 1).committed == original_committed
    finally:
        with runner.Session.begin() as s:
            s.get(Quota, 1).limit_bytes = original_limit
    saved = save(client, ident).json()
    with runner.Session() as s:
        template = s.get(MappingTemplate, saved['id'])
        expected_bytes = len(template.name.encode()) + len(encoded(template.specification))
        assert s.get(Quota, 1).committed == original_committed + expected_bytes
    response = client.delete('/api/templates/' + saved['id'])
    assert response.json() == {'deleted': True, 'id': saved['id']}
    with runner.Session() as s:
        assert s.get(Quota, 1).committed == original_committed
    assert client.delete('/api/templates/' + saved['id']).status_code == 404


def test_template_limit_and_delete_preserve_completed_result(client, pair):
    runner = app.state.runner
    ident = confirmed(client, pair)
    saved = save(client, ident).json()
    operation = wait(client, run(client, ident))
    rid = operation['resource_id']
    snapshot = client.get('/api/runs/' + rid).json()
    exported = wait(client, client.post(f'/api/runs/{rid}/exports', headers={'Idempotency-Key': str(uuid.uuid4())}))
    assert exported['state'] == 'completed'
    content = client.get(f'/api/runs/{rid}/export').content
    created = []
    with runner.Session.begin() as s:
        spec = copy.deepcopy(s.get(MappingTemplate, saved['id']).specification)
        count = s.scalar(select(func.count()).select_from(MappingTemplate))
        for i in range(50 - count):
            template = MappingTemplate(id=str(uuid.uuid4()), name='限额-' + str(uuid.uuid4()), specification=spec)
            s.add(template)
            created.append(template.id)
        runner.recount(s, s.get(Quota, 1))
    try:
        response = save(client, ident)
        assert response.status_code == 409 and response.json()['code'] == 'TEMPLATE_LIMIT'
    finally:
        for template_id in created:
            assert client.delete('/api/templates/' + template_id).status_code == 200
    assert client.delete('/api/templates/' + saved['id']).status_code == 200
    assert client.get('/api/runs/' + rid).json() == snapshot
    assert client.get(f'/api/runs/{rid}/export').content == content
    assert preview(client, saved['id'], pair).status_code == 404
