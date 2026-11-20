"""Upgrade actual historical schema fixtures into a scoped team database."""
import importlib.util
from pathlib import Path
import pytest
import sqlalchemy as sa
from team.models import TeamBase

spec=importlib.util.spec_from_file_location('legacy_fixture',Path(__file__).with_name('test_legacy_migration.py'))
legacy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
migration_connection=legacy.migration_connection


@pytest.mark.parametrize('version',['0001','0002'])
def test_team_upgrade_preserves_historical_records_and_assigns_scope(migration_connection,version):
    conn,schema=migration_connection
    legacy.install_historical_schema(conn,version)
    legacy.insert_historical_records(conn,version=='0002')
    before=legacy.data_snapshot(conn,schema)
    if version=='0001':
        before['mapping_templates']=[]  # The known additive 0002 migration introduces an empty table.
    legacy.upgrade(conn,'0003')
    assert legacy.data_snapshot(conn,schema)==before
    scope=conn.execute(sa.text("SELECT workspace_id FROM team_legacy_scopes WHERE resource_type='run' AND legacy_id='run-old'")).scalar_one()
    assert scope=='00000000-0000-0000-0000-000000000002'
    assert conn.scalar(sa.text('SELECT count(*) FROM team_users'))==0
    assert conn.scalar(sa.text('SELECT count(*) FROM team_audit_events'))==0
    assert conn.scalar(sa.text('SELECT count(*) FROM team_legacy_scopes'))>=8
    legacy.upgrade(conn,'0003')
    assert legacy.data_snapshot(conn,schema)==before


def test_team_fresh_schema_matches_identity_models(migration_connection):
    conn,schema=migration_connection
    legacy.upgrade(conn,'0003')
    inspector=sa.inspect(conn)
    actual=set(inspector.get_table_names(schema=schema))
    for table in TeamBase.metadata.sorted_tables:
        if not table.name.startswith('team_') or table.name.startswith('team_job'):
            continue
        # Later independent migrations may add tables to shared metadata.
        if table.name not in actual:
            continue
        columns={c['name']:c['nullable'] for c in inspector.get_columns(table.name,schema=schema)}
        assert columns=={c.name:c.nullable for c in table.columns}
    assert {'team_users','team_sessions','team_memberships','team_mutation_receipts'}<=actual


@pytest.mark.parametrize('target',['0006','0007'])
def test_collection_upgrade_preserves_existing_sources_and_rows(migration_connection,target):
    import hashlib
    from core.dataset_parser import parse_dataset
    from sqlalchemy.orm import Session
    from team.models import User
    from team.job_models import Job  # Register the referenced queue table in ORM metadata.
    from team.source_models import SourceAsset,Dataset,DatasetRow
    from team.reconcile_models import CollectionRule,CollectionRun,ResultGroup,RowCoverage
    from team.period_models import CollectionProfile,BusinessPeriod,PeriodRevision,PeriodRun
    conn,schema=migration_connection
    legacy.upgrade(conn,'0005')
    workspace='00000000-0000-0000-0000-000000000002'
    data=b'order,amount\nA,9007199254740993.01\n'
    parsed=parse_dataset(data,'preserved.csv')
    with Session(bind=conn,join_transaction_mode='create_savepoint') as db:
        db.add(User(id='preserved-user',login='preserved.user',password_hash='unusable-test-fixture'))
        db.flush()
        db.add(SourceAsset(id='preserved-source',workspace_id=workspace,created_by='preserved-user',filename='preserved.csv',sha256=hashlib.sha256(data).hexdigest(),content=data,accounted_bytes=len(data)+8192))
        db.flush()
        db.add(Dataset(id='preserved-dataset',workspace_id=workspace,asset_id='preserved-source',row_count=1,metadata_json={k:v for k,v in parsed.items() if k!='rows'},parsed_hash='a'*64))
        db.flush()
        row=parsed['rows'][0]
        db.add(DatasetRow(workspace_id=workspace,dataset_id='preserved-dataset',row_id=row['id'],ordinal=row['ordinal'],source=row['source'],cells=row['cells']))
        db.flush()
        def snapshot():
            return {name:conn.execute(sa.text(f'SELECT * FROM {name} ORDER BY 1')).all() for name in ['team_users','team_source_assets','team_datasets','team_dataset_rows']}
        before=snapshot()
        legacy.upgrade(conn,target);legacy.upgrade(conn,target)
        assert snapshot()==before
        inspector=sa.inspect(conn)
        models=[CollectionRule,CollectionRun,ResultGroup,RowCoverage]
        if target=='0007':models += [CollectionProfile,BusinessPeriod,PeriodRevision,PeriodRun]
        for model in models:
            columns={c['name']:c['nullable'] for c in inspector.get_columns(model.__tablename__,schema=schema)}
            assert columns=={c.name:c.nullable for c in model.__table__.columns}


def test_period_upgrade_preserves_immutable_collection_results(migration_connection):
    conn,schema=migration_connection
    legacy.upgrade(conn,'0006')
    w='00000000-0000-0000-0000-000000000002'
    conn.execute(sa.text("INSERT INTO team_users(id,login,password_hash) VALUES ('u','test.user','disabled-fixture')"))
    conn.execute(sa.text("INSERT INTO team_source_assets(id,workspace_id,created_by,filename,sha256,content,accounted_bytes) VALUES ('a',:w,'u','source.csv',:hash,:data,100)"),{'w':w,'hash':'a'*64,'data':b'id\nA\n'})
    for ident in ['left','right']:
        conn.execute(sa.text("INSERT INTO team_datasets(id,workspace_id,asset_id,row_count) VALUES (:id,:w,'a',1)"),{'id':ident,'w':w})
    conn.execute(sa.text("INSERT INTO team_dataset_rows(workspace_id,dataset_id,row_id,ordinal,source,cells) VALUES (:w,'left','r1',1,'{}','{}')"),{'w':w})
    conn.execute(sa.text("INSERT INTO team_collection_rules(id,workspace_id,name,published_by,left_dataset_id,right_dataset_id,definition_json,rule_hash,accounted_bytes) VALUES ('r',:w,'frozen policy','u','left','right','{}',:hash,100)"),{'w':w,'hash':'b'*64})
    conn.execute(sa.text("INSERT INTO team_collection_runs(id,workspace_id,rule_id,input_hash,result_hash,summary_json) VALUES ('run',:w,'r',:hash,:hash,'{\"source_completeness\":\"not_attested\"}')"),{'w':w,'hash':'c'*64})
    conn.execute(sa.text("INSERT INTO team_collection_groups(workspace_id,run_id,group_id,ordinal,detail) VALUES (:w,'run','group',1,'{\"status\":\"exact_equal\"}')"),{'w':w})
    conn.execute(sa.text("INSERT INTO team_collection_coverage(workspace_id,run_id,side,dataset_id,row_id,group_id,disposition) VALUES (:w,'run','left','left','r1','group','exact_equal')"),{'w':w})
    tables=['team_collection_rules','team_collection_runs','team_collection_groups','team_collection_coverage']
    before={name:conn.execute(sa.text(f'SELECT * FROM {name}')).all() for name in tables}
    legacy.upgrade(conn,'0007')
    assert {name:conn.execute(sa.text(f'SELECT * FROM {name}')).all() for name in tables}==before
