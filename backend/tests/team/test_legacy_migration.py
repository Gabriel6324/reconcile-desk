"""Frozen migration checks, independent of the current application ORM.

V01_DDL was captured from the actually shipped v0.1 source ZIP db.py:
SHA256 4b0359a47ad9b58ad3684dcf6e3498bc8e4dd85ff6c949a93f5cb5454e1ca87b.
V02_TEMPLATE_DDL was captured from released v0.2 db.py:
SHA256 b214f78a481764b458995e288bd256870971cc530471d7da7470898eb42f4981.
Neither fixture is generated at test runtime or imported from the live models.
Integration tests use disposable schemas inside reconcile_test only.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable, CreateIndex

BACKEND = Path(__file__).resolve().parents[2]


def load_file(path, name):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def normalized_ddl(statement):
    # Constraint ordering in SQLAlchemy metadata sets is not semantically relevant.
    lines = [line.strip().rstrip(',') for line in str(statement).strip().splitlines()]
    return tuple(sorted(line for line in lines if line))


def compiled_ddl(metadata):
    dialect = postgresql.dialect()
    statements = []
    for table in metadata.sorted_tables:
        statements.append(CreateTable(table).compile(dialect=dialect))
        statements.extend(CreateIndex(index).compile(dialect=dialect) for index in table.indexes)
    return sorted(normalized_ddl(statement) for statement in statements)


def test_frozen_schema_matches_actual_released_ddl():
    frozen = load_file(BACKEND / 'alembic/legacy_v02_schema.py', 'test_frozen_legacy_schema')
    expected = sorted(normalized_ddl(statement) for statement in (*V01_DDL, V02_TEMPLATE_DDL))
    assert compiled_ddl(frozen.metadata()) == expected


def test_0001_emits_only_frozen_tables_without_importing_live_metadata(monkeypatch):
    initial = load_file(BACKEND / 'alembic/versions/0001_initial.py', 'test_frozen_initial_migration')
    emitted = []
    engine = sa.create_mock_engine('postgresql://', lambda ddl, *a, **k: emitted.append(
        normalized_ddl(ddl.compile(dialect=postgresql.dialect()))))
    monkeypatch.setattr(initial.op, 'get_bind', lambda: engine)
    initial.upgrade()
    expected = sorted(normalized_ddl(statement) for statement in (*V01_DDL, V02_TEMPLATE_DDL))
    assert sorted(emitted) == expected
    assert 'from api.' not in (BACKEND / 'alembic/versions/0001_initial.py').read_text('utf-8')
    assert 'from api.' not in (BACKEND / 'alembic/legacy_v02_schema.py').read_text('utf-8')


@pytest.fixture
def migration_connection():
    url = os.environ.get('TEST_DATABASE_URL', '')
    if not url:
        pytest.skip('TEST_DATABASE_URL required: real PostgreSQL')
    parsed = make_url(url)
    if parsed.database != 'reconcile_test' or parsed.get_backend_name() != 'postgresql':
        pytest.fail('Migration fixture requires the dedicated PostgreSQL reconcile_test database')
    engine = sa.create_engine(url)
    # Isolate from the application's public schema and concurrent test modules.
    schema = 'legacy_migration_' + uuid.uuid4().hex
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(sa.text('CREATE SCHEMA "' + schema + '"'))
            connection.execute(sa.text('SET LOCAL search_path TO "' + schema + '"'))
            try:
                yield connection, schema
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def upgrade(connection, target='0002'):
    config = Config(str(BACKEND / 'alembic.ini'))
    config.set_main_option('script_location', str(BACKEND / 'alembic'))
    config.attributes['connection'] = connection
    command.upgrade(config, target)


def install_historical_schema(connection, version):
    # These are fixed statements from the shipped source, not current metadata.
    for statement in V01_DDL:
        connection.execute(sa.text(statement))
    if version == '0002':
        connection.execute(sa.text(V02_TEMPLATE_DDL))
    connection.execute(sa.text('CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)'))
    connection.execute(sa.text('INSERT INTO alembic_version VALUES (:version)'), {'version': version})


def insert_historical_records(connection, with_template):
    # SQL uses historical required fields explicitly, without ORM client defaults.
    for op_id, kind in [('op-left', 'import'), ('op-right', 'import'), ('op-run', 'reconcile'), ('op-export', 'export')]:
        connection.execute(sa.text('''INSERT INTO operations
            (id,kind,idempotency_key,request_hash,token,epoch,state,created_at,
             request_snapshot,reserved,retained)
            VALUES (:id,:kind,:id,:hash,:id,7,'completed','2026-11-03T01:02:03Z','{}',0,0)'''),
            {'id': op_id, 'kind': kind, 'hash': 'a' * 64})
    originals = {
        'left': b'id,amount\nlegacy,9007199254740993.01\n',
        'right': b'id,amount\nlegacy,9007199254740993.02\n',
    }
    for side, original in originals.items():
        connection.execute(sa.text('''INSERT INTO imports
            (id,operation_id,filename,sha256,original,parsed,state,created_at)
            VALUES (:id,:operation,:filename,:hash,:original,'{"rows":[]}',
                    'ready','2026-11-03T01:02:03Z')'''), {
            'id': 'import-' + side, 'operation': 'op-' + side, 'filename': side + '.csv',
            'hash': hashlib.sha256(original).hexdigest(), 'original': original})
        connection.execute(sa.text('INSERT INTO import_rows VALUES (:id,\'r1\',0)'),
                           {'id': 'import-' + side})
    connection.execute(sa.text('''INSERT INTO mappings
        VALUES ('mapping-old','import-left','import-right',1,'2026-11-03T01:02:03Z')'''))
    connection.execute(sa.text('''INSERT INTO mapping_revisions VALUES
        ('mapping-old',1,'{"kind":"legacy","amount_type":"decimal"}',
         'manual',CAST(:confirmation AS jsonb),'2026-11-03T01:02:03Z')'''),
                       {'confirmation': '{"confirmed":true}'})
    connection.execute(sa.text('''INSERT INTO runs VALUES
        ('run-old','op-run','mapping-old',1,'import-left','import-right',
         '{"algorithm_version":"exact-reconcile-1.0.0"}',
         '{"groups":[{"id":"g1","status":"changed","delta":"0.01"}]}',
         'completed','2026-11-03T01:02:03Z')'''))
    for side in originals:
        connection.execute(sa.text('INSERT INTO result_group_rows VALUES (\'run-old\',:side,\'r1\',:import_id,\'g1\')'),
                           {'side': side, 'import_id': 'import-' + side})
    content = b'PK\x03\x04immutable-legacy-export\x00\xff'
    connection.execute(sa.text('INSERT INTO exports VALUES (\'export-old\',\'run-old\',\'op-export\',\'1\',:content,:hash)'),
                       {'content': content, 'hash': hashlib.sha256(content).hexdigest()})
    connection.execute(sa.text('INSERT INTO executor_state VALUES (1,7,\'legacy-instance\')'))
    connection.execute(sa.text('INSERT INTO workspace_quota VALUES (1,512,0,1073741824)'))
    if with_template:
        connection.execute(sa.text('''INSERT INTO mapping_templates VALUES
            ('template-old','Legacy template','{"key":{"left":"id","right":"id"}}',
             '2026-11-03T01:02:03Z')'''))


def data_snapshot(connection, schema):
    # All old columns, including bytea and JSON content, must survive exactly.
    tables = sa.inspect(connection).get_table_names(schema=schema)
    old = ('executor_state', 'workspace_quota', 'operations', 'imports', 'import_rows',
           'mappings', 'mapping_revisions', 'runs', 'result_group_rows', 'exports', 'model_calls')
    captured = {}
    for name in old + (('mapping_templates',) if 'mapping_templates' in tables else ()):
        values = connection.execute(sa.text('SELECT row_to_json(t)::text FROM "' + schema + '"."' + name + '" AS t')).scalars()
        captured[name] = sorted(json.dumps(json.loads(value), sort_keys=True) for value in values)
    return captured


def schema_snapshot(connection, schema):
    inspector = sa.inspect(connection)
    captured = {}
    for name in inspector.get_table_names(schema=schema):
        if name == 'alembic_version':
            continue
        columns = inspector.get_columns(name, schema=schema)
        captured[name] = {
            'columns': [(c['name'], str(c['type']), c['nullable'], c['default'] is not None) for c in columns],
            'primary_key': inspector.get_pk_constraint(name, schema=schema)['constrained_columns'],
            'unique': sorted(tuple(c['column_names']) for c in inspector.get_unique_constraints(name, schema=schema)),
            'checks': sorted(c['sqltext'] for c in inspector.get_check_constraints(name, schema=schema)),
            'foreign_keys': sorted((tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']))
                                   for f in inspector.get_foreign_keys(name, schema=schema)),
            'indexes': sorted((i['name'], i['unique'], tuple(i.get('expressions', i['column_names'])),
                               i.get('dialect_options', {}).get('postgresql_where'))
                              for i in inspector.get_indexes(name, schema=schema)),
        }
    return captured


@pytest.mark.parametrize('historical_version', ['0001', '0002'])
def test_upgrade_historical_released_schema_preserves_records(migration_connection, historical_version):
    connection, schema = migration_connection
    install_historical_schema(connection, historical_version)
    assert ('mapping_templates' in sa.inspect(connection).get_table_names(schema=schema)) == (historical_version == '0002')
    insert_historical_records(connection, historical_version == '0002')
    before = data_snapshot(connection, schema)
    upgrade(connection)
    after = data_snapshot(connection, schema)
    assert {name: after[name] for name in before} == before
    if historical_version == '0001':
        assert after['mapping_templates'] == []
    assert connection.scalar(sa.text('SELECT version_num FROM alembic_version')) == '0002'
    upgrade(connection)
    assert data_snapshot(connection, schema) == after


def test_fresh_0002_schema_matches_released_0002(migration_connection):
    connection, schema = migration_connection
    upgrade(connection)
    fresh = schema_snapshot(connection, schema)
    # Replace tables only inside this test's disposable, rolled-back schema.
    for name in reversed(list(sa.inspect(connection).get_table_names(schema=schema))):
        connection.execute(sa.text('DROP TABLE "' + schema + '"."' + name + '" CASCADE'))
    install_historical_schema(connection, '0002')
    assert schema_snapshot(connection, schema) == fresh

V01_DDL = ('CREATE TABLE executor_state (\n'
 '\tid SERIAL NOT NULL, \n'
 '\tepoch BIGINT NOT NULL, \n'
 '\tinstance VARCHAR(36) NOT NULL, \n'
 '\tPRIMARY KEY (id)\n'
 ')',
 'CREATE TABLE operations (\n'
 '\tid VARCHAR(36) NOT NULL, \n'
 '\tkind VARCHAR(32) NOT NULL, \n'
 '\tidempotency_key VARCHAR(128) NOT NULL, \n'
 '\trequest_hash VARCHAR(64) NOT NULL, \n'
 '\ttoken VARCHAR(36) NOT NULL, \n'
 '\tepoch BIGINT NOT NULL, \n'
 '\tstate VARCHAR(16) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tfinished_at TIMESTAMP WITH TIME ZONE, \n'
 '\tstop_requested_at TIMESTAMP WITH TIME ZONE, \n'
 '\tresource_id VARCHAR(36), \n'
 '\tresource_kind VARCHAR(32), \n'
 '\terror_code VARCHAR(64), \n'
 '\terror_message VARCHAR(500), \n'
 '\tretry_of VARCHAR(36), \n'
 '\trequest_snapshot JSONB NOT NULL, \n'
 '\treserved BIGINT NOT NULL, \n'
 '\tretained BIGINT NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 "\tCHECK (state IN ('running','stopping','completed','failed','interrupted')), \n"
 '\tUNIQUE (idempotency_key), \n'
 '\tFOREIGN KEY(retry_of) REFERENCES operations (id)\n'
 ')',
 'CREATE UNIQUE INDEX uq_active_operation ON operations ((1)) WHERE state IN '
 "('running','stopping')",
 'CREATE TABLE workspace_quota (\n'
 '\tid SERIAL NOT NULL, \n'
 '\tcommitted BIGINT NOT NULL, \n'
 '\treserved BIGINT NOT NULL, \n'
 '\tlimit_bytes BIGINT NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tCHECK (committed >= 0 AND reserved >= 0)\n'
 ')',
 'CREATE TABLE imports (\n'
 '\tid VARCHAR(36) NOT NULL, \n'
 '\toperation_id VARCHAR(36) NOT NULL, \n'
 '\tfilename VARCHAR(240) NOT NULL, \n'
 '\tsha256 VARCHAR(64) NOT NULL, \n'
 '\toriginal BYTEA NOT NULL, \n'
 '\tparsed JSONB, \n'
 '\tstate VARCHAR(16) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (operation_id), \n'
 '\tFOREIGN KEY(operation_id) REFERENCES operations (id)\n'
 ')',
 'CREATE TABLE import_rows (\n'
 '\timport_id VARCHAR(36) NOT NULL, \n'
 '\trow_id VARCHAR(32) NOT NULL, \n'
 '\tordinal INTEGER NOT NULL, \n'
 '\tPRIMARY KEY (import_id, row_id), \n'
 '\tUNIQUE (import_id, ordinal), \n'
 '\tFOREIGN KEY(import_id) REFERENCES imports (id)\n'
 ')',
 'CREATE TABLE mappings (\n'
 '\tid VARCHAR(36) NOT NULL, \n'
 '\tleft_id VARCHAR(36) NOT NULL, \n'
 '\tright_id VARCHAR(36) NOT NULL, \n'
 '\tcurrent_revision INTEGER NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tFOREIGN KEY(left_id) REFERENCES imports (id), \n'
 '\tFOREIGN KEY(right_id) REFERENCES imports (id)\n'
 ')',
 'CREATE TABLE mapping_revisions (\n'
 '\tmapping_id VARCHAR(36) NOT NULL, \n'
 '\trevision INTEGER NOT NULL, \n'
 '\tdefinition JSONB NOT NULL, \n'
 '\torigin VARCHAR(32) NOT NULL, \n'
 '\tconfirmation JSONB, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tPRIMARY KEY (mapping_id, revision), \n'
 '\tFOREIGN KEY(mapping_id) REFERENCES mappings (id)\n'
 ')',
 'CREATE TABLE model_calls (\n'
 '\tid VARCHAR(36) NOT NULL, \n'
 '\tmapping_id VARCHAR(36) NOT NULL, \n'
 '\toperation_id VARCHAR(36) NOT NULL, \n'
 '\tbase_revision INTEGER NOT NULL, \n'
 '\tstate VARCHAR(32) NOT NULL, \n'
 '\tbilling_state VARCHAR(32) NOT NULL, \n'
 '\tmetadata_json JSONB NOT NULL, \n'
 '\tproposal JSONB, \n'
 '\tPRIMARY KEY (id), \n'
 '\tFOREIGN KEY(mapping_id) REFERENCES mappings (id), \n'
 '\tUNIQUE (operation_id), \n'
 '\tFOREIGN KEY(operation_id) REFERENCES operations (id)\n'
 ')',
 'CREATE TABLE runs (\n'
 '\tid VARCHAR(36) NOT NULL, \n'
 '\toperation_id VARCHAR(36) NOT NULL, \n'
 '\tmapping_id VARCHAR(36) NOT NULL, \n'
 '\trevision INTEGER NOT NULL, \n'
 '\tleft_id VARCHAR(36) NOT NULL, \n'
 '\tright_id VARCHAR(36) NOT NULL, \n'
 '\tsnapshot JSONB NOT NULL, \n'
 '\tresult JSONB, \n'
 '\tstate VARCHAR(16) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (operation_id), \n'
 '\tFOREIGN KEY(operation_id) REFERENCES operations (id), \n'
 '\tFOREIGN KEY(mapping_id) REFERENCES mappings (id), \n'
 '\tFOREIGN KEY(left_id) REFERENCES imports (id), \n'
 '\tFOREIGN KEY(right_id) REFERENCES imports (id)\n'
 ')',
 'CREATE TABLE exports (\n'
 '\tid VARCHAR(36) NOT NULL, \n'
 '\trun_id VARCHAR(36) NOT NULL, \n'
 '\toperation_id VARCHAR(36) NOT NULL, \n'
 '\tversion VARCHAR(16) NOT NULL, \n'
 '\tcontent BYTEA NOT NULL, \n'
 '\tsha256 VARCHAR(64) NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (run_id, version), \n'
 '\tFOREIGN KEY(run_id) REFERENCES runs (id), \n'
 '\tUNIQUE (operation_id), \n'
 '\tFOREIGN KEY(operation_id) REFERENCES operations (id)\n'
 ')',
 'CREATE TABLE result_group_rows (\n'
 '\trun_id VARCHAR(36) NOT NULL, \n'
 '\tside VARCHAR(5) NOT NULL, \n'
 '\trow_id VARCHAR(32) NOT NULL, \n'
 '\timport_id VARCHAR(36) NOT NULL, \n'
 '\tgroup_id VARCHAR(64) NOT NULL, \n'
 '\tPRIMARY KEY (run_id, side, row_id), \n'
 "\tCHECK (side IN ('left','right')), \n"
 '\tFOREIGN KEY(run_id) REFERENCES runs (id), \n'
 '\tFOREIGN KEY(import_id) REFERENCES imports (id)\n'
 ')')

V02_TEMPLATE_DDL = 'CREATE TABLE mapping_templates (\n\tid VARCHAR(36) NOT NULL, \n\tname VARCHAR(80) NOT NULL, \n\tspecification JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (char_length(name) BETWEEN 1 AND 80), \n\tUNIQUE (name)\n)'
