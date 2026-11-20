"""Period routes use the team's existing session, CSRF and transaction boundary."""
from fastapi import Request,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from api.common import Problem
from . import periods
from .period_models import CollectionProfile,BusinessPeriod,PeriodRevision,PeriodRun
from .security import authorize_workspace


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')


class ProfileBody(Body):
    source_rule_id:str=Field(min_length=1,max_length=36)
    name:str=Field(min_length=1,max_length=120)


class DefinitionBody(Body):
    definition:dict


class VersionBody(Body):
    expected_version:int=Field(strict=True,ge=1)


class InputsBody(VersionBody):
    definition:dict


class ConfirmBody(VersionBody):
    config_hash:str=Field(pattern='^[0-9a-f]{64}$')


class ActivateBody(VersionBody):
    run_id:str=Field(min_length=1,max_length=36)
    acknowledged_open_cases:int=Field(default=0,strict=True,ge=0)


class ReasonBody(VersionBody):
    reason:str=Field(min_length=1,max_length=2000)


def install(app,authenticated,page):
    base='/api/v1/workspaces/{workspace_id}'

    @app.post(base+'/collection-profiles',status_code=201)
    def create_profile(workspace_id:str,body:ProfileBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.create_profile(db,workspace_id,user.id,body.source_rule_id,body.name,request.headers.get('idempotency-key',''))

    @app.get(base+'/collection-profiles')
    def list_profiles(workspace_id:str,request:Request,after:str='',limit:int=Query(default=20,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.read')
            rows=list(db.scalars(select(CollectionProfile).where(CollectionProfile.workspace_id==workspace_id,CollectionProfile.id>after).order_by(CollectionProfile.id).limit(limit+1)))
            return page(rows,limit,periods.profile_view,lambda row:row.id)

    @app.get(base+'/collection-profiles/{ident}')
    def get_profile(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.read')
            return periods.profile_view(periods._profile(db,workspace_id,ident))

    @app.post(base+'/periods',status_code=201)
    def create_period(workspace_id:str,body:DefinitionBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.create_period(db,workspace_id,user.id,body.definition,request.headers.get('idempotency-key',''))

    @app.get(base+'/periods')
    def list_periods(workspace_id:str,request:Request,after:str='',limit:int=Query(default=20,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'results.read')
            rows=list(db.scalars(select(BusinessPeriod).where(BusinessPeriod.workspace_id==workspace_id,BusinessPeriod.id>after).order_by(BusinessPeriod.id).limit(limit+1)))
            return page(rows,limit,lambda row:periods.period_view(db,row),lambda row:row.id)

    @app.get(base+'/periods/{ident}')
    def get_period(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            return periods.period_view(db,periods.get_period(db,workspace_id,user.id,ident))

    @app.post(base+'/periods/{ident}/revisions',status_code=201)
    def revise(workspace_id:str,ident:str,body:InputsBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.revise_inputs(db,workspace_id,user.id,ident,body.expected_version,body.definition,request.headers.get('idempotency-key',''))

    @app.get(base+'/periods/{ident}/revisions')
    def revisions(workspace_id:str,ident:str,request:Request,after:int=Query(default=0,ge=0),limit:int=Query(default=10,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.read')
            periods._period(db,workspace_id,ident)
            rows=list(db.scalars(select(PeriodRevision).where(PeriodRevision.workspace_id==workspace_id,PeriodRevision.period_id==ident,PeriodRevision.number>after).order_by(PeriodRevision.number).limit(limit+1)))
            return page(rows,limit,periods.revision_view,lambda row:row.number)

    @app.post(base+'/periods/{ident}/confirm')
    def confirm(workspace_id:str,ident:str,body:ConfirmBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.confirm(db,workspace_id,user.id,ident,body.expected_version,body.config_hash,request.headers.get('idempotency-key',''))

    @app.post(base+'/periods/{ident}/runs',status_code=202)
    def submit(workspace_id:str,ident:str,body:VersionBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.submit_run(db,workspace_id,user.id,ident,body.expected_version,request.headers.get('idempotency-key',''))

    @app.get(base+'/periods/{ident}/runs')
    def runs(workspace_id:str,ident:str,request:Request,after:str='',limit:int=Query(default=20,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            period=periods.get_period(db,workspace_id,user.id,ident)
            rows=list(db.scalars(select(PeriodRun).where(PeriodRun.workspace_id==workspace_id,PeriodRun.period_id==ident,PeriodRun.run_id>after).order_by(PeriodRun.run_id).limit(limit+1)))
            return page(rows,limit,lambda row:periods.run_link_view(db,period,row),lambda row:row.run_id)

    @app.post(base+'/periods/{ident}/activate')
    def activate(workspace_id:str,ident:str,body:ActivateBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.activate(db,workspace_id,user.id,ident,body.expected_version,body.run_id,request.headers.get('idempotency-key',''),body.acknowledged_open_cases)

    @app.post(base+'/periods/{ident}/close')
    def close(workspace_id:str,ident:str,body:ReasonBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.close_period(db,workspace_id,user.id,ident,body.expected_version,body.reason,request.headers.get('idempotency-key',''))

    @app.post(base+'/periods/{ident}/reopen')
    def reopen(workspace_id:str,ident:str,body:ReasonBody,request:Request):
        with authenticated(request,True) as (db,user,session):
            return periods.reopen_period(db,workspace_id,user.id,ident,body.expected_version,body.reason,request.headers.get('idempotency-key',''))
