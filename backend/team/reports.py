"""Immutable XLSX exports of an explicitly closed business period version."""
import hashlib
import io
from datetime import datetime
from sqlalchemy import String,Integer,BigInteger,LargeBinary,DateTime,ForeignKeyConstraint,UniqueConstraint,select,text
from sqlalchemy.orm import Mapped,mapped_column
from fastapi import Request,Query
from fastapi.responses import Response
from pydantic import BaseModel,Field
import xlsxwriter
from api.common import Problem,encoded
from api.db import new_id
from .models import TeamBase
from . import periods,cases
from .case_models import DifferenceCase
from .reconcile_models import CollectionRun,ResultGroup
from .security import authorize_workspace

class PeriodReport(TeamBase):
    __tablename__='team_period_reports'
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id:Mapped[str]=mapped_column(String(36))
    period_id:Mapped[str]=mapped_column(String(36))
    period_version:Mapped[int]=mapped_column(Integer)
    run_id:Mapped[str]=mapped_column(String(36))
    sha256:Mapped[str]=mapped_column(String(64))
    content:Mapped[bytes]=mapped_column(LargeBinary)
    accounted_bytes:Mapped[int]=mapped_column(BigInteger)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('period_id','period_version'),
        ForeignKeyConstraint(['workspace_id','period_id'],['team_business_periods.workspace_id','team_business_periods.id']),
        ForeignKeyConstraint(['workspace_id','run_id'],['team_collection_runs.workspace_id','team_collection_runs.id']))

def view(item):
    return {'id':item.id,'period_id':item.period_id,'period_version':item.period_version,'run_id':item.run_id,'sha256':item.sha256,'created_at':item.created_at.isoformat()}

def create(db,w,actor,ident,version,key):
    body={'period_id':ident,'expected_version':version}
    identity,fingerprint,replay,quota=periods._begin(db,w,actor,'data.export','period.report',key,body)
    if replay is not None:return replay
    period=periods._period(db,w,ident,True);periods._cas(period,version)
    if period.state!='closed':raise Problem('PERIOD_NOT_CLOSED','关闭周期后才能生成正式报告。',409)
    existing=db.scalar(select(PeriodReport).where(PeriodReport.period_id==ident,PeriodReport.period_version==version))
    if existing:
        periods._charge(quota,8192)
        return periods._complete(db,w,actor,identity,fingerprint,view(existing),'report',existing.id)
    run=db.get(CollectionRun,period.active_run_id)
    buffer=io.BytesIO()
    book=xlsxwriter.Workbook(buffer,{'in_memory':True,'strings_to_formulas':False,'strings_to_urls':False})
    heading=book.add_format({'bold':True,'bg_color':'#E7EEF5'})
    def sheet(name,columns,rows):
        ws=book.add_worksheet(name);ws.freeze_panes(1,0);ws.set_column(0,len(columns)-1,25)
        ws.write_row(0,0,columns,heading)
        for i,row in enumerate(rows,1):
            # Every amount/identifier is text; spreadsheet floating-point never changes evidence.
            for j,value in enumerate(row):ws.write_string(i,j,'' if value is None else str(value))
        ws.autofilter(0,0,max(1,i if 'i' in locals() else 0),len(columns)-1)
    summary=periods.period_view(db,period)
    sheet('周期快照',['项目','内容'],[(k,encoded(v).decode() if isinstance(v,(dict,list)) else v) for k,v in summary.items()]+[('结果摘要哈希',run.result_hash),('输入哈希',run.input_hash),('完整性依据','人工声明；未自动校验导出日期范围')])
    groups=db.scalars(select(ResultGroup).where(ResultGroup.run_id==run.id).order_by(ResultGroup.ordinal))
    sheet('核对分组',['分组ID','业务键','币种','状态','应收','实收','差额'],[(g.group_id,' / '.join(g.detail['key'] or []),g.detail['currency'],g.detail['status'],g.detail['expected_amount'],g.detail['observed_amount'],g.detail['delta']) for g in groups])
    items=db.scalars(select(DifferenceCase).where(DifferenceCase.run_id==run.id).order_by(DifferenceCase.id))
    sheet('独立复核',['案件ID','分组ID','状态','处置','说明','提交人','复核人','证据','处置哈希'],[(x.id,x.group_id,x.state,(x.resolution_json or {}).get('code'),(x.resolution_json or {}).get('text'),x.submitted_by,x.reviewed_by,encoded((x.resolution_json or {}).get('evidence',[])).decode(),x.resolution_hash) for x in items])
    book.close();content=buffer.getvalue()
    if len(content)>32*1024**2:raise Problem('REPORT_TOO_LARGE','报告超过容量上限。',413)
    charge=len(content)+8192;periods._charge(quota,charge)
    item=PeriodReport(id=new_id(),workspace_id=w,period_id=ident,period_version=version,run_id=run.id,sha256=hashlib.sha256(content).hexdigest(),content=content,accounted_bytes=charge)
    db.add(item);db.flush()
    return periods._complete(db,w,actor,identity,fingerprint,view(item),'report',item.id,{'sha256':item.sha256,'period_version':version})

class CreateBody(BaseModel):
    expected_version:int=Field(strict=True,ge=1)

def install(app,authenticated):
    base='/api/v1/workspaces/{workspace_id}'
    @app.post(base+'/periods/{ident}/reports',status_code=201)
    def generate(workspace_id:str,ident:str,body:CreateBody,request:Request):
        with authenticated(request,True) as (db,user,session):return create(db,workspace_id,user.id,ident,body.expected_version,request.headers.get('idempotency-key',''))
    @app.get(base+'/periods/{ident}/reports')
    def listing(workspace_id:str,ident:str,request:Request,after:int|None=Query(default=None,ge=1),limit:int=Query(default=100,ge=1,le=100)):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.export');periods._period(db,workspace_id,ident)
            query=select(PeriodReport).where(PeriodReport.workspace_id==workspace_id,PeriodReport.period_id==ident)
            if after is not None:query=query.where(PeriodReport.period_version<after)
            rows=list(db.scalars(query.order_by(PeriodReport.period_version.desc()).limit(limit+1)))
            return {'items':[view(x) for x in rows[:limit]],'next_cursor':rows[limit-1].period_version if len(rows)>limit else None}
    @app.get(base+'/reports/{ident}/source')
    def download(workspace_id:str,ident:str,request:Request):
        with authenticated(request) as (db,user,session):
            authorize_workspace(db,user.id,workspace_id,'data.export')
            item=db.scalar(select(PeriodReport).where(PeriodReport.workspace_id==workspace_id,PeriodReport.id==ident))
            if item is None:raise Problem('NOT_FOUND','报告不存在。',404)
            return Response(item.content,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="reconciliation-report.xlsx"','X-Content-SHA256':item.sha256})
