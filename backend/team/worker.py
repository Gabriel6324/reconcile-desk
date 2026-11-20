"""Standalone bounded import and collection worker. Run: python -m team.worker."""
import argparse
import ctypes
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import threading
import time

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DBAPIError
from api.common import Problem
from api.db import make_engine, new_id
from core.errors import CoreError
from . import jobs, datasets, dispatch
from .job_models import Job, GlobalCapacity

LOCK=732914622
MEMORY_BYTES=768*1024**2


def memory_guard():
    """Enforce a child-process address/memory ceiling; keep Windows job handle alive."""
    if os.name!='nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(MEMORY_BYTES,MEMORY_BYTES))
        return None
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),
                  ('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),('active',w.DWORD),
                  ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class Io(ctypes.Structure):
        _fields_=[(name,ctypes.c_ulonglong) for name in ['read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes']]
    class Extended(ctypes.Structure):
        _fields_=[('basic',Basic),('io',Io),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                  ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];api.CreateJobObjectW.restype=w.HANDLE
    api.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD];api.SetInformationJobObject.restype=w.BOOL
    api.GetCurrentProcess.restype=w.HANDLE
    api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];api.AssignProcessToJobObject.restype=w.BOOL
    handle=api.CreateJobObjectW(None,None)
    limits=Extended();limits.basic.flags=0x100;limits.process_memory=MEMORY_BYTES
    if not handle or not api.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not api.AssignProcessToJobObject(handle,api.GetCurrentProcess()):
        raise RuntimeError('RESOURCE_GUARD_UNAVAILABLE')
    return handle


def compute_child(payload,output):
    # No credentials or database work in the compute child.
    for key in ['DATABASE_URL','TEST_DATABASE_URL','PGPASSWORD','LLM_API_KEY']:
        os.environ.pop(key,None)
    try:
        handle=memory_guard()
        from core.dataset_parser import parse_dataset
        if payload.get('contract') == 'collection-reconcile-1':
            from core.mapping import map_rows
            from core.business import reconcile_groups
            left, right = payload['left'], payload['right']
            computed = reconcile_groups(map_rows(left['dataset_id'], left['rows'], left['mapping']),
                                        map_rows(right['dataset_id'], right['rows'], right['mapping']), payload['rule'])
            parsed = {'run_id': payload['run_id'], 'rule_hash': payload['rule_hash'],
                      'input_hash': payload['input_hash'], 'result': computed}
        else:
            parsed = parse_dataset(payload['data'],payload['filename'],payload.get('worksheet'))
        result={'ok':True,'parsed':parsed}
    except CoreError as exc:
        result={'ok':False,'code':exc.code,'details':exc.details}
    except MemoryError:
        result={'ok':False,'code':'MEMORY_LIMIT'}
    except Exception:
        result={'ok':False,'code':'COMPUTE_FAILED'}
    try:
        encoded=json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(encoded)>datasets.MAX_PARSED_BYTES:
            encoded=b'{"ok":false,"code":"PARSED_DATA_TOO_LARGE"}'
        with open(output,'xb') as file:
            file.write(encoded)
    except BaseException:
        # Parent recognizes missing/incomplete output as failure, never partial success.
        raise SystemExit(2)


class ImportWorker:
    def __init__(self,engine,*,timeout=30,heartbeat=5,lease_seconds=30,compute_target=compute_child):
        if not 0.1<=heartbeat<lease_seconds/2 or not 1<=timeout<=300:
            raise ValueError('Invalid worker timing bounds')
        self.engine=engine
        self.Session=sessionmaker(engine,expire_on_commit=False)
        self.timeout,self.heartbeat,self.lease_seconds=timeout,heartbeat,lease_seconds
        self.compute_target=compute_target
        self.worker_id=new_id()
        self.stop=threading.Event()
        self.lease=None
        self.child=None

    def start(self):
        self.lease=self.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
        if self.lease.scalar(text('SELECT version_num FROM alembic_version'))!='0009':
            self.lease.close();self.lease=None
            raise RuntimeError('Explicit migration to 0009 required')
        if not self.lease.scalar(text('SELECT pg_try_advisory_lock(:lock)'),{'lock':LOCK}):
            self.lease.close();self.lease=None
            raise RuntimeError('Another executor owns this database')
        self.pid=self.lease.scalar(text('SELECT pg_backend_pid()'))
        # A new exclusive owner invalidates all previous task generations before dispatch.
        # Existing quota/job locks serialize this with any old worker's final publication.
        with self.Session.begin() as db:
            capacity=db.get(GlobalCapacity,1,with_for_update=True)
            capacity.generation+=1
        self.recover()

    def owns_lease(self):
        if self.lease is None or self.lease.closed or self.lease.invalidated:
            return False
        try:
            row=self.lease.execute(text("SELECT pg_backend_pid(), EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted AND classid=0 AND objid=:key AND objsubid=1)"),{'key':LOCK}).one()
            return row[0]==self.pid and row[1]
        except DBAPIError:
            return False

    def recover(self):
        with self.Session() as db:
            ids=list(db.scalars(select(Job.id).where(Job.state.in_(['running','cancel_requested']))))
        for ident in ids:
            with self.Session.begin() as db:
                jobs.recover_expired(db,ident,authorize_inputs=dispatch.authorize_inputs)

    def publish_with_readback(self,job_id,token,parsed):
        # Repeating a publication reads the immutable manifest before inserting rows.
        # This also handles COMMIT succeeded but its network acknowledgement was lost.
        for attempt in range(2):
            if not self.owns_lease():raise RuntimeError('Worker ownership lost before publication')
            try:
                with self.Session.begin() as db:
                    return dispatch.publish(db,job_id,token,parsed)
            except DBAPIError:
                if attempt:raise
        raise RuntimeError('COMMIT_UNKNOWN')

    def stop_child(self):
        if self.child is not None:
            if self.child.pid is None:return
            if self.child.is_alive():self.child.terminate()
            self.child.join(5)
            if self.child.is_alive():
                self.child.kill();self.child.join(5)
            if self.child.is_alive():
                raise RuntimeError('Child termination not confirmed; active slot must remain reserved')

    def record_failure(self,job_id,token,code,details=None):
        # Call only before spawning or after the child has stopped. A concurrent
        # revocation requests cancellation, which can now be acknowledged safely.
        if not self.owns_lease():raise RuntimeError('Worker ownership lost before failure settlement')
        with self.Session.begin() as db:
            failed=dispatch.fail(db,job_id,token,code,details)
            if failed.state=='cancel_requested':jobs.acknowledge_cancel(db,job_id,token)

    def close(self):
        self.stop.set()
        self.stop_child()
        if self.lease is not None:
            try:
                if self.owns_lease():self.lease.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LOCK})
            finally:
                self.lease.close();self.lease=None

    def tick(self):
        if not self.owns_lease():
            raise RuntimeError('Worker lease lost')
        self.recover()
        with self.Session.begin() as db:
            claimed=jobs.claim(db,self.worker_id,lease_seconds=self.lease_seconds,authorize_inputs=dispatch.authorize_inputs)
            if claimed is None:return False
            job_id,token=claimed.id,claimed.fence_token
        # Input materialization failures must settle the claimed task, rather than
        # rolling it back into the front of the queue forever.
        try:
            with self.Session.begin() as db:
                payload=dispatch.input_for_job(db,db.get(Job,job_id))
        except Problem as exc:
            self.record_failure(job_id,token,exc.code)
            return True
        # A successful admission/claim has committed before expensive work starts.
        with tempfile.TemporaryDirectory(prefix='reconcile-import-') as temporary:
            output=str(Path(temporary)/'result.json')
            ctx=multiprocessing.get_context('spawn')
            self.child=ctx.Process(target=self.compute_target,args=(payload,output),daemon=True)
            try:
                self.child.start()
            except (OSError,RuntimeError):
                self.stop_child();self.child=None
                self.record_failure(job_id,token,'COMPUTE_START_FAILED')
                return True
            started=last_heartbeat=time.monotonic()
            try:
                while self.child.is_alive():
                    self.child.join(.1)
                    if self.stop.is_set():
                        self.stop_child()
                        return True  # lease expiry/recovery records interrupted, without pretending completion.
                    if time.monotonic()-started>self.timeout:
                        self.stop_child()
                        self.record_failure(job_id,token,'COMPUTE_TIMEOUT')
                        return True
                    if time.monotonic()-last_heartbeat>=self.heartbeat:
                        if not self.owns_lease():
                            self.stop_child();raise RuntimeError('Worker ownership lost during compute')
                        with self.Session.begin() as db:
                            current=jobs.renew(db,job_id,token,lease_seconds=self.lease_seconds,authorize_inputs=dispatch.authorize_inputs)
                        if current.state=='cancel_requested':
                            self.stop_child()
                            with self.Session.begin() as db:jobs.acknowledge_cancel(db,job_id,token)
                            return True
                        last_heartbeat=time.monotonic()
                if not self.owns_lease():raise RuntimeError('Worker ownership lost before publish')
                file=Path(output)
                if self.child.exitcode!=0 or not file.is_file() or file.stat().st_size>datasets.MAX_PARSED_BYTES:
                    result={'ok':False,'code':'COMPUTE_FAILED'}
                else:
                    try:result=json.loads(file.read_text('utf-8'))
                    except (ValueError,OSError):result={'ok':False,'code':'COMPUTE_FAILED'}
                if not isinstance(result,dict) or (result.get('ok') is True and 'parsed' not in result):
                    result={'ok':False,'code':'COMPUTE_FAILED'}
                if not result.get('ok'):
                    self.record_failure(job_id,token,result.get('code','COMPUTE_FAILED'),result.get('details'))
                else:
                    try:
                        publication=self.publish_with_readback(job_id,token,result['parsed'])
                        if publication.state=='cancel_requested':
                            with self.Session.begin() as db:jobs.acknowledge_cancel(db,job_id,token)
                    except Problem as exc:
                        if exc.code not in ('JOB_FENCED','FORBIDDEN'):
                            self.record_failure(job_id,token,exc.code)
                return True
            finally:
                self.stop_child()
                self.child=None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    engine=make_engine();worker=ImportWorker(engine)
    for sig in [signal.SIGINT,signal.SIGTERM]:signal.signal(sig,lambda *args:worker.stop.set())
    try:
        worker.start()
        while not worker.stop.is_set():
            worker.tick()
            if args.once:break
            worker.stop.wait(.5)
    except (DBAPIError,Problem,RuntimeError):
        # SQLAlchemy may include data parameters in exceptions; never log raw payloads/DSNs.
        print('Worker stopped safely; inspect persisted task status before restarting.',flush=True)
        raise SystemExit(1) from None
    finally:
        worker.close();engine.dispose()


if __name__=='__main__':main()
