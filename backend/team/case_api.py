from fastapi import Request,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from api.common import Problem
from . import cases,periods
from .case_models import DifferenceCase,CaseEvent
from .security import authorize_workspace

class ActionBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_case_version:int=Field(strict=True,ge=1)
    expected_period_version:int=Field(strict=True,ge=1)
    payload:dict=Field(default_factory=dict)

def install(app,authenticated,page):
    base='/api/v1/workspaces/{workspace_id}'
    @app.get(base+'/periods/{ident}/cases')
    def list_cases(workspace_id:str,ident:str,request:Request,run_id:str|None=None,after:str='',limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            period=periods.get_period(db,workspace_id,user.id,ident)
            target=run_id or period.active_run_id
            rows=list(db.scalars(select(DifferenceCase).where(DifferenceCase.workspace_id==workspace_id,DifferenceCase.period_id==ident,DifferenceCase.run_id==target,DifferenceCase.id>after).order_by(DifferenceCase.id).limit(limit+1)))
            def summary(item):
                value=cases.case_view(item)
                value.pop('resolution',None);value.pop('resolution_json',None)
                value['historical']=item.run_id!=period.active_run_id or period.confirmed_revision_id!=period.current_revision_id
                return value
            return page(rows,limit,summary,lambda row:row.id)

    @app.get(base+'/cases/{ident}')
    def detail(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.read')
            item=db.scalar(select(DifferenceCase).where(DifferenceCase.workspace_id==workspace_id,DifferenceCase.id==ident))
            if item is None:raise Problem('NOT_FOUND','案件不存在。',404)
            events=list(db.scalars(select(CaseEvent).where(CaseEvent.case_id==ident).order_by(CaseEvent.sequence.desc()).limit(50)))
            return {'case':cases.case_view(item),'period':periods.period_view(db,periods._period(db,workspace_id,item.period_id)),
                'events':[{'id':e.id,'sequence':e.sequence,'action':e.action,'actor_id':e.actor_id,'detail':e.detail,'created_at':e.created_at.isoformat()} for e in reversed(events)]}

    @app.post(base+'/cases/{ident}/{action}')
    def change(workspace_id:str,ident:str,action:str,body:ActionBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return cases.mutate(db,workspace_id,user.id,ident,body.expected_case_version,body.expected_period_version,action,body.payload,request.headers.get('idempotency-key',''))

    @app.get(base+'/cases/{ident}/events')
    def events(workspace_id:str,ident:str,request:Request,after:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            cases.get_case(db,workspace_id,user.id,ident,details=True)
            rows=list(db.scalars(select(CaseEvent).where(CaseEvent.workspace_id==workspace_id,CaseEvent.case_id==ident,CaseEvent.sequence>after).order_by(CaseEvent.sequence).limit(limit+1)))
            return page(rows,limit,cases.event_view,lambda row:row.sequence)
