"""Immutable uploaded evidence and atomic parsed-data publication."""
import hashlib
import json
from pathlib import PurePath
from sqlalchemy import select
from api.common import Problem, digest, encoded
from api.db import new_id
from core.parsers import MAX_FILE_BYTES
from core.dataset_parser import PARSER_VERSION
from .models import MutationReceipt
from .source_models import SourceAsset, Dataset, DatasetRow
from .job_models import Job
from .security import authorize_workspace
from . import jobs

MAX_PARSED_BYTES=64*1024**2
RESERVE_BYTES=MAX_PARSED_BYTES+1024**2
CONTRACT_VERSION='dataset-import-1'


def create_dataset(db, workspace_id, actor_id, filename, content, worksheet, key):
    authorize_workspace(db,actor_id,workspace_id,'data.import',lock=True)
    if not isinstance(key,str) or not 1<=len(key)<=128 or any(not 33<=ord(x)<=126 for x in key):
        raise Problem('IDEMPOTENCY_REQUIRED','请提供有效请求标识。',400)
    if (not isinstance(filename,str) or not 1<=len(filename)<=240 or any(ord(x)<32 for x in filename)
        or '/' in filename or '\\' in filename or PurePath(filename).suffix.lower() not in ('.csv','.xlsx')):
        raise Problem('INVALID_FILENAME','需要 CSV 或 XLSX 文件名。')
    if not content or len(content)>MAX_FILE_BYTES:
        raise Problem('FILE_SIZE','文件不能为空且不得超过 10 MiB。',413)
    if worksheet is not None and (not isinstance(worksheet,str) or not 1<=len(worksheet)<=240):
        raise Problem('INVALID_WORKSHEET','工作表名称无效。')
    sha=hashlib.sha256(content).hexdigest()
    request_hash=digest({'filename':filename,'sha256':sha,'worksheet':worksheet,'contract':CONTRACT_VERSION})
    identity={'scope_id':workspace_id,'actor_id':actor_id,'action':'dataset.create','key':key}
    previous=db.get(MutationReceipt,identity)
    if previous:
        if previous.request_hash!=request_hash:
            raise Problem('IDEMPOTENCY_CONFLICT','同一请求标识绑定了不同资料。',409)
        return previous.result
    # Locks already include the initiating identity and its membership.
    capacity,quota=jobs._capacity(db,workspace_id)
    asset_cost=len(content)+8192
    if quota.committed_bytes+quota.reserved_bytes+asset_cost+RESERVE_BYTES>quota.limit_bytes:
        raise Problem('WORKSPACE_QUOTA','工作区容量不足，资料未保存。',507)
    asset=SourceAsset(id=new_id(),workspace_id=workspace_id,created_by=actor_id,filename=filename,
                      sha256=sha,content=content,accounted_bytes=asset_cost)
    db.add(asset);db.flush()
    dataset=Dataset(id=new_id(),workspace_id=workspace_id,asset_id=asset.id,worksheet=worksheet)
    db.add(dataset);db.flush()
    snapshot={'contract':CONTRACT_VERSION,'parser_version':PARSER_VERSION,'dataset_id':dataset.id,'asset_id':asset.id,'sha256':sha,'worksheet':worksheet}
    job=jobs.admit(db,workspace_id,actor_id,'import',key,snapshot,RESERVE_BYTES,authorize_inputs=authorize_inputs)
    dataset.job_id=job.id
    quota.committed_bytes+=asset_cost
    # Asset PK is the once-only storage charge identity; immutable accounted_bytes permits reconciliation.
    result={'dataset_id':dataset.id,'job_id':job.id,'asset_id':asset.id,'state':'queued','sha256':sha}
    db.add(MutationReceipt(**identity,request_hash=request_hash,result=result))
    db.flush()
    return result


def authorize_inputs(db,workspace_id,actor_id,kind,snapshot):
    if kind!='import' or snapshot.get('contract')!=CONTRACT_VERSION or snapshot.get('parser_version')!=PARSER_VERSION:
        raise Problem('UNSUPPORTED_JOB_CONTRACT','该执行器不支持此任务契约。',403)
    dataset=db.scalar(select(Dataset).where(Dataset.id==snapshot.get('dataset_id'),Dataset.workspace_id==workspace_id))
    asset=db.execute(select(SourceAsset.id,SourceAsset.sha256).where(SourceAsset.id==snapshot.get('asset_id'),SourceAsset.workspace_id==workspace_id)).first()
    if (dataset is None or asset is None or dataset.asset_id!=asset.id or snapshot.get('sha256')!=asset.sha256
        or snapshot.get('worksheet')!=dataset.worksheet):
        raise Problem('NOT_FOUND','任务来源不存在或版本不一致。',404)
    authorize_workspace(db,actor_id,workspace_id,'data.read',lock=True)


def input_for_job(db,job):
    authorize_inputs(db,job.workspace_id,job.actor_id,job.kind,job.request_snapshot)
    asset=db.get(SourceAsset,job.request_snapshot['asset_id'])
    if hashlib.sha256(asset.content).hexdigest()!=asset.sha256:
        raise Problem('SOURCE_HASH_MISMATCH','原始资料校验失败。',409)
    return {'data':asset.content,'filename':asset.filename,'worksheet':job.request_snapshot.get('worksheet')}


def publish(db,job_id,fence_token,parsed):
    if not isinstance(parsed,dict) or not isinstance(parsed.get('rows'),list):
        raise Problem('INVALID_PARSE_RESULT','解析结果无效。')
    serialized=encoded(parsed)
    # Charge exact canonical row/provenance and metadata bytes, plus a per-row index/record allowance.
    charge=len(serialized)+len(parsed['rows'])*512
    if charge>MAX_PARSED_BYTES:
        raise Problem('PARSED_DATA_TOO_LARGE','解析结果超过持久化上限。',413)
    snapshot_hash=hashlib.sha256(serialized).hexdigest()
    candidate=db.get(Job,job_id)
    if candidate is None:
        raise Problem('NOT_FOUND','任务不存在。',404)
    ident=candidate.request_snapshot['dataset_id']
    result={'dataset_id':ident,'row_count':len(parsed['rows']),'parsed_hash':snapshot_hash,
            'input_sha256':parsed.get('sha256'),'parser_version':parsed.get('parser_version'),'worksheet':parsed.get('sheet')}
    job=jobs.finish(db,job_id,fence_token,result,authorize_inputs=authorize_inputs,external_retained_bytes=charge)
    if job.state!='succeeded':
        return job
    dataset=db.get(Dataset,ident)
    if dataset.parsed_hash is not None:
        if dataset.parsed_hash!=snapshot_hash:
            raise Problem('PUBLISHED_RESULT_CONFLICT','已保存的解析结果不同。',409)
        return job
    if parsed.get('sha256')!=candidate.request_snapshot['sha256'] or parsed.get('parser_version')!=candidate.request_snapshot['parser_version']:
        raise Problem('SOURCE_HASH_MISMATCH','解析来源校验失败。',409)
    # The job success, metadata, rows, quota settlement and audit commit together.
    metadata={k:v for k,v in parsed.items() if k!='rows'}
    dataset.metadata_json=metadata
    dataset.row_count=len(parsed['rows'])
    dataset.parsed_hash=snapshot_hash
    db.add_all([DatasetRow(workspace_id=dataset.workspace_id,dataset_id=ident,row_id=row['id'],
                           ordinal=row['ordinal'],source=row['source'],cells=row['cells']) for row in parsed['rows']])
    db.flush()
    return job


def fail_import(db,job_id,token,code,details=None):
    job=jobs.fail(db,job_id,token,code,authorize_inputs=authorize_inputs)
    dataset=db.get(Dataset,job.request_snapshot['dataset_id'])
    if job.state=='failed' and dataset.error_details is None:
        safe=details if isinstance(details,dict) else {}
        dataset.error_details=safe if len(encoded(safe))<=4096 else {'details_truncated':True}
    return job


def get_dataset(db,workspace_id,actor_id,ident):
    authorize_workspace(db,actor_id,workspace_id,'data.read')
    dataset=db.scalar(select(Dataset).where(Dataset.id==ident,Dataset.workspace_id==workspace_id))
    if dataset is None:
        raise Problem('NOT_FOUND','资料不存在。',404)
    return dataset


def view(db,dataset):
    job=db.get(Job,dataset.job_id)
    asset=db.execute(select(SourceAsset.filename,SourceAsset.sha256).where(SourceAsset.id==dataset.asset_id)).one()
    return {'id':dataset.id,'asset_id':dataset.asset_id,'filename':asset.filename,'sha256':asset.sha256,
            'job_id':dataset.job_id,'state':'ready' if job.state=='succeeded' else job.state,
            'error_code':job.error_code,'error_details':dataset.error_details,'row_count':dataset.row_count,'metadata':dataset.metadata_json,
            'parsed_hash':dataset.parsed_hash,'source_completeness':'not_attested'}
