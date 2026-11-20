"""Historical reports stay addressable beyond the first 100 closed versions."""
import hashlib,uuid
import pytest
from team.reports import PeriodReport
from team.models import Membership
from test_cases import live
from test_periods import env,ready,profile,BASE
from test_ingest import URL

pytestmark=pytest.mark.skipif(not URL,reason='isolated PostgreSQL required')

def test_report_history_pagination_and_workspace_permission(env,ready,profile):
    engine,S,actor,c,app=env
    p,_=live(env,ready,profile)
    # Seed immutable report history directly; report generation is covered by
    # the case/close integration test. Isolate the >100-version query boundary.
    with S.begin() as db:
        for version in range(1,104):
            content=f'report-{version}'.encode()
            db.add(PeriodReport(id=str(uuid.uuid4()),workspace_id='wa',period_id=p['id'],period_version=version,
                run_id=p['active_run_id'],sha256=hashlib.sha256(content).hexdigest(),content=content,accounted_bytes=len(content)+8192))
    endpoint=f'{BASE}/periods/{p["id"]}/reports'
    first=c.get(endpoint);assert first.status_code==200
    page=first.json();assert len(page['items'])==100 and page['next_cursor']==4
    tail=c.get(endpoint,params={'after':page['next_cursor']}).json()
    assert [r['period_version'] for r in tail['items']]==[3,2,1] and tail['next_cursor'] is None
    one=c.get(endpoint,params={'limit':1}).json();assert one['next_cursor']==103
    assert c.get(endpoint,params={'after':1}).json()=={'items':[],'next_cursor':None}
    assert c.get(endpoint,params={'after':0}).status_code==422
    assert c.get(endpoint,params={'limit':101}).status_code==422
    assert c.get(endpoint.replace('/wa/','/wb/')).status_code in (403,404)
    with S.begin() as db:db.get(Membership,('wa',actor)).roles=['viewer']
    try:assert c.get(endpoint).status_code==403
    finally:
        with S.begin() as db:db.get(Membership,('wa',actor)).roles=['workspace_manager','operator','reviewer']
