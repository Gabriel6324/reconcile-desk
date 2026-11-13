"""Upgrade a genuine schema without templates while preserving legacy records."""
import hashlib
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from api.db import Base, Import, MappingTemplate, Operation
from api.main import app

URL = os.environ.get('TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not URL, reason='TEST_DATABASE_URL required: real PostgreSQL')


def test_upgrade_from_legacy_schema_preserves_import_and_new_template_table():
    if URL.rsplit('/', 1)[-1] != 'reconcile_test':
        pytest.fail('Refusing destructive fixture outside reconcile_test database')
    engine = create_engine(URL)
    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA public CASCADE'))
        connection.execute(text('CREATE SCHEMA public'))
        Base.metadata.create_all(connection, tables=[table for table in Base.metadata.sorted_tables
                                                    if table.name != 'mapping_templates'])
        connection.execute(text('CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)'))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0001')"))
    assert 'mapping_templates' not in inspect(engine).get_table_names()
    operation_id, import_id = str(uuid.uuid4()), str(uuid.uuid4())
    original = b'id,amount\nlegacy-order,9007199254740993.01\n'
    with Session(engine) as session, session.begin():
        session.add(Operation(id=operation_id, kind='import', idempotency_key=str(uuid.uuid4()),
            request_hash='a' * 64, epoch=1, state='failed', request_snapshot={}, reserved=0))
        session.flush()
        session.add(Import(id=import_id, operation_id=operation_id, filename='legacy.csv', original=original,
                           sha256=hashlib.sha256(original).hexdigest(), state='failed'))
    os.environ['DATABASE_URL'] = URL
    try:
        with TestClient(app, base_url='http://127.0.0.1', headers={'X-Reconcile-Client': 'web'}) as client:
            assert client.get('/api/ready').status_code == 200
            assert client.get('/api/templates').json()['items'] == []
            assert client.get(f'/api/imports/{import_id}/source').content == original
            with app.state.runner.Session() as session:
                assert session.scalar(text('SELECT version_num FROM alembic_version')) == '0002'
                assert session.get(Operation, operation_id).state == 'failed'
                assert session.scalar(select(MappingTemplate)) is None
    finally:
        engine.dispose()
