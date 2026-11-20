"""First-run setup is one use, loopback-origin bounded, and persists user choices."""
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,text
from team.cli import migrate
from team.local import app
URL=os.getenv('TEST_DATABASE_URL','')
pytestmark=pytest.mark.skipif(not URL,reason='dedicated PostgreSQL required')

def test_local_setup_and_membership_requirements(monkeypatch):
    assert URL.rsplit('/',1)[-1]=='reconcile_test'
    engine=create_engine(URL)
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA public CASCADE'));conn.execute(text('CREATE SCHEMA public'))
    migrate(engine)
    headers={'Origin':'http://127.0.0.1:8080','X-Reconcile-Client':'web'}
    body={'login':'local.owner','password':'Local-setup-test-2026'}
    with TestClient(app,base_url='http://127.0.0.1:8080',headers=headers) as client:
        monkeypatch.setenv('RECONCILE_PORTABLE_INSTANCE_ID','local-test-identity')
        assert client.get('/api/local-instance').json()=={'instance_id':'local-test-identity'}
        assert client.get('/api/v1/setup/status').json()=={'required':True}
        assert client.post('/api/v1/setup',json=body,headers={'Origin':'https://evil.example'}).status_code==403
        assert client.post('/api/v1/setup',json=body).status_code==201
        assert client.get('/api/v1/setup/status').json()=={'required':False}
        assert client.post('/api/v1/setup',json=body).status_code==409
        login=client.post('/api/v1/session/login',json=body)
        assert login.status_code==200
        client.headers['X-CSRF-Token']=login.json()['csrf_token']
        workspace=client.get('/api/v1/workspaces').json()['items'][0]
        members=client.get(f'/api/v1/workspaces/{workspace["id"]}/members').json()['items']
        assert members[0]['login']=='local.owner' and 'reviewer' in members[0]['roles']
        assert client.post('/api/v1/session/logout').status_code==200
        assert client.get('/api/v1/workspaces').status_code==401
    engine.dispose()
