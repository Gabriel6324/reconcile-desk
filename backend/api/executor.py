import hashlib
import multiprocessing
import os
import threading
import time
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DBAPIError
from .db import (Executor, Quota, Operation, Import, ImportRow, Mapping, Revision,
                 Run, Coverage, Export, Suggestion, MappingTemplate, now, new_id)
from .common import Problem, encoded, digest, needed, op_view
from .worker import compute
from . import model

ACTIVE = ('running', 'stopping')
LOCK = 732914622

class Runner:
    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(engine, expire_on_commit=False)
        self.healthy = False
        self.stop = threading.Event()
        self.child = None
        self.lease_mutex = threading.Lock()
        self.recovery_mutex = threading.Lock()
        self.recovering = threading.Event()
        self.instance = new_id()
        self.threads = []

    def startup(self):
        self.lease = self.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
        if not self.lease.scalar(text('SELECT pg_try_advisory_lock(:lock)'), {'lock': LOCK}):
            self.lease.close()
            raise RuntimeError('Another Reconcile Desk executor is active for this database')
        self.lease_pid = self.lease.scalar(text('SELECT pg_backend_pid()'))
        cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
        cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
        with self.engine.begin() as conn:
            cfg.attributes['connection'] = conn
            command.upgrade(cfg, 'head')
        with self.Session.begin() as s:
            executor = s.get(Executor, 1, with_for_update=True)
            if executor is None:
                executor = Executor(id=1, epoch=0, instance='')
                s.add(executor)
            executor.epoch += 1
            executor.instance = self.instance
            self.epoch = executor.epoch
            quota = s.get(Quota, 1, with_for_update=True)
            if quota is None:
                quota = Quota(id=1, committed=0, reserved=0,
                              limit_bytes=int(os.getenv('WORKSPACE_QUOTA_BYTES', str(1024**3))))
                s.add(quota)
            for op in s.scalars(select(Operation).where(Operation.state.in_(ACTIVE))):
                op.state, op.error_code = 'interrupted', 'PROCESS_RESTARTED'
                op.error_message, op.finished_at, op.reserved = '服务重启中断了操作；没有自动重试。', now(), 0
                self.mark_resource(s, op, 'interrupted')
            self.recount(s, quota)
        self.healthy = True
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        self.threads.append(thread)

    def heartbeat(self):
        while not self.stop.wait(1):
            if self.recovering.is_set():
                continue
            if not self.probe():
                # A recovery temporarily closes admission while retaining this monitor.
                if self.recovering.is_set() or self.healthy:
                    continue
                self.healthy = False
                if self.child and self.child.is_alive():
                    self.child.terminate()
                    self.child.join(5)
                return

    def probe(self):
        if not self.healthy:
            return False
        try:
            with self.lease_mutex:
                valid = self.lease_valid()
                if not valid:
                    self.healthy = False
                return valid
        except BaseException:
            self.healthy = False
            return False

    def lease_valid(self):
        row = self.lease.execute(text('''SELECT pg_backend_pid(), EXISTS (
            SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()
            AND granted AND classid=0 AND objid=:key AND objsubid=1)'''), {'key': LOCK}).one()
        return row[0] == self.lease_pid and row[1]

    def close(self):
        self.healthy = False
        self.stop.set()
        if self.child and self.child.is_alive():
            self.child.terminate()
            self.child.join(5)
        for thread in self.threads:
            thread.join(3)
        with self.lease_mutex:
            if not self.lease.closed:
                try:
                    if not self.lease.invalidated:
                        self.lease.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK})
                except DBAPIError:
                    self.lease.invalidate()
                finally:
                    self.lease.close()

    def fence(self, s):
        if not self.probe():
            raise Problem('EXECUTOR_UNAVAILABLE', '执行器暂不可用，请稍后查看任务状态。', 503)
        owner = s.get(Executor, 1, with_for_update=True)
        if owner.epoch != self.epoch or owner.instance != self.instance:
            self.healthy = False
            raise Problem('EXECUTOR_FENCED', '执行器已切换，请刷新后查看。', 503)
        return s.get(Quota, 1, with_for_update=True)

    def recount(self, s, quota):
        s.flush()
        total = 0
        for item in s.scalars(select(Import)):
            total += len(item.original) + (len(encoded(item.parsed)) if item.parsed else 0)
        for item in s.scalars(select(Run)):
            total += len(encoded(item.snapshot)) + (len(encoded(item.result)) if item.result else 0)
        for item in s.scalars(select(Export)):
            total += len(item.content)
        for item in s.scalars(select(Revision)):
            total += len(encoded(item.definition)) + len(encoded(item.confirmation))
        for item in s.scalars(select(Suggestion)):
            total += len(encoded(item.metadata_json)) + len(encoded(item.proposal))
        for item in s.scalars(select(MappingTemplate)):
            total += len(item.name.encode('utf-8')) + len(encoded(item.specification))
        for item in s.scalars(select(Operation)):
            total += len(encoded(item.request_snapshot)) + len((item.error_message or '').encode('utf-8'))
        quota.committed = total
        quota.reserved = sum(s.scalars(select(Operation.reserved).where(Operation.state.in_(ACTIVE))))

    def check_quota(self, quota, reserve):
        if quota.committed + quota.reserved + reserve > quota.limit_bytes:
            raise Problem('WORKSPACE_QUOTA', '工作区逻辑容量不足；历史数据未被自动删除。', 507)

    def mark_resource(self, s, op, state):
        cls = {'import': Import, 'reconcile': Run, 'model_suggestion': Suggestion}.get(op.kind)
        resource = s.get(cls, op.resource_id) if cls and op.resource_id else None
        if resource is not None:
            resource.state = state
            if op.kind == 'model_suggestion' and resource.billing_state != 'not_sent':
                resource.billing_state = 'billing_unknown'

    def admit(self, kind, key, snapshot, prepare, reserve, retry_of=None, reuse=None):
        if not key or len(key) > 128 or any(ord(x) < 33 or ord(x) > 126 for x in key):
            raise Problem('IDEMPOTENCY_REQUIRED', '请提供有效 Idempotency-Key 请求标识。', 400)
        fingerprint = digest({'kind': kind, 'snapshot': snapshot, 'retry_of': retry_of})
        accepted = None
        try:
            with self.Session.begin() as s:
                quota = self.fence(s)
                existing = s.scalar(select(Operation).where(Operation.idempotency_key == key))
                if existing:
                    if existing.request_hash != fingerprint:
                        raise Problem('IDEMPOTENCY_CONFLICT', '同一请求标识绑定了不同内容。', 409)
                    return op_view(existing), False
                if reuse is not None:
                    reusable = reuse(s)
                    if reusable is not None:
                        return op_view(reusable), False
                if s.scalar(select(Operation.id).where(Operation.state.in_(ACTIVE)).limit(1)):
                    raise Problem('OPERATION_CAPACITY', '工作区正在处理另一项操作，请稍后再试。', 429)
                if retry_of:
                    previous = needed(s, Operation, retry_of)
                    if previous.state in ACTIVE:
                        raise Problem('RETRY_STILL_ACTIVE', '原操作仍在进行。', 409)
                self.check_quota(quota, reserve)
                op = Operation(id=new_id(), kind=kind, idempotency_key=key, request_hash=fingerprint,
                               epoch=self.epoch, token=new_id(), request_snapshot=snapshot,
                               reserved=reserve, state='running', retry_of=retry_of)
                s.add(op)
                s.flush()
                prepare(s, op)
                s.flush()
                self.recount(s, quota)
                self.check_quota(quota, 0)
                accepted = op.id
                view = op_view(op)
        except DBAPIError:
            return self.recover_commit(key), False
        thread = threading.Thread(target=self.execute, args=(accepted,), daemon=True)
        self.threads.append(thread)
        thread.start()
        return view, True

    def recover_commit(self, key):
        # A missing COMMIT acknowledgement is resolved using a new connection, never guessed.
        with self.recovery_mutex:
            self.recovering.set()
            self.healthy = False
            try:
                with self.Session.begin() as s:
                    owner = s.get(Executor, 1, with_for_update=True)
                    quota = s.get(Quota, 1, with_for_update=True)
                    op = s.scalar(select(Operation).where(Operation.idempotency_key == key).with_for_update())
                    if owner.epoch != self.epoch or owner.instance != self.instance:
                        raise Problem('COMMIT_UNKNOWN', '提交状态待核实，请查看任务记录。', 503)
                    if op is not None and op.state in ACTIVE:
                        op.state, op.finished_at, op.reserved = 'interrupted', now(), 0
                        op.error_code, op.error_message = 'COMMIT_INTERRUPTED', '提交回执异常；已核实并保留记录，请使用新请求重试。'
                        self.mark_resource(s, op, 'interrupted')
                        self.recount(s, quota)
                    view = op_view(op) if op is not None else None
                with self.lease_mutex:
                    if self.stop.is_set() or self.lease.closed or not self.lease_valid():
                        raise Problem('EXECUTOR_FENCED', '执行器锁已丢失，请重启服务后查看记录。', 503)
                    self.healthy = True
                if view is None:
                    raise Problem('COMMIT_NOT_FOUND', '请求尚未持久化，可使用同一标识重试。', 503)
                return view
            except DBAPIError:
                raise Problem('COMMIT_UNKNOWN', '数据库连接异常，提交状态待核实。', 503) from None
            finally:
                self.recovering.clear()

    def payload(self, s, op):
        if op.kind == 'import':
            item = needed(s, Import, op.resource_id)
            return {'filename': item.filename, 'data': item.original}
        if op.kind == 'reconcile':
            run = needed(s, Run, op.resource_id)
            return {'left': needed(s, Import, run.left_id).parsed,
                    'right': needed(s, Import, run.right_id).parsed, 'definition': run.snapshot['definition']}
        if op.kind == 'export':
            run = needed(s, Run, op.request_snapshot['run_id'])
            return {'left': needed(s, Import, run.left_id).parsed,
                    'right': needed(s, Import, run.right_id).parsed,
                    'definition': run.snapshot['definition'], 'result': run.result}
        suggestion = needed(s, Suggestion, op.resource_id)
        mapping = needed(s, Mapping, suggestion.mapping_id)
        return {'left': needed(s, Import, mapping.left_id).parsed,
                'right': needed(s, Import, mapping.right_id).parsed, 'task': op.request_snapshot.get('task', '')}

    def execute(self, operation_id):
        key = None
        try:
            with self.Session() as s:
                op = needed(s, Operation, operation_id)
                token, key, kind = op.token, op.idempotency_key, op.kind
                payload = self.payload(s, op)
            if not self.probe():
                return
            if kind == 'model_suggestion':
                with self.Session.begin() as s:
                    self.fence(s)
                    call = needed(s, Suggestion, op.resource_id)
                    call.billing_state = 'billing_unknown'
                result = model.propose(**payload)
            else:
                ctx = multiprocessing.get_context('spawn')
                receiver, sender = ctx.Pipe(duplex=False)
                process = ctx.Process(target=compute, args=(sender, kind, payload), daemon=True)
                self.child = process
                process.start()
                sender.close()
                deadline = time.monotonic() + (60 if kind == 'export' else 30)
                response = None
                while time.monotonic() < deadline and self.healthy and not self.stop.is_set():
                    if receiver.poll(.1):
                        try:
                            response = receiver.recv()
                        except EOFError:
                            pass
                        break
                    if not process.is_alive():
                        break
                if response is None:
                    self.stop_operation(operation_id, token, process)
                    receiver.close()
                    return
                process.join(5)
                receiver.close()
                if process.is_alive():
                    self.stop_operation(operation_id, token, process)
                    return
                self.child = None
                if not response['ok']:
                    raise Problem(response['code'], response['message'])
                result = response['result']
            self.complete(operation_id, token, result)
        except DBAPIError:
            if key:
                try:
                    self.recover_commit(key)
                except BaseException:
                    self.healthy = False
        except BaseException as error:
            self.fail(operation_id, getattr(error, 'code', 'OPERATION_FAILED'),
                      getattr(error, 'message', '操作失败，输入与历史记录已保留。'))

    def stop_operation(self, ident, token, process):
        if self.healthy:
            with self.Session.begin() as s:
                self.fence(s)
                op = s.get(Operation, ident, with_for_update=True)
                if op.state == 'running' and op.token == token:
                    op.state, op.stop_requested_at, op.token = 'stopping', now(), new_id()
        if process.is_alive():
            process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        if process.is_alive():
            self.healthy = False
            return
        self.child = None
        self.fail(ident, 'COMPUTATION_TIMEOUT', '计算超时或子进程异常退出；任务已停止。')

    def complete(self, ident, token, result):
        with self.Session.begin() as s:
            quota = self.fence(s)
            op = s.get(Operation, ident, with_for_update=True)
            if op.state != 'running' or op.token != token or op.epoch != self.epoch or op.stop_requested_at:
                return
            size = len(result) if isinstance(result, bytes) else len(encoded(result))
            if size > 64 * 1024**2:
                raise Problem('RESULT_TOO_LARGE', '结果超过工作区单次处理限制。')
            if op.kind == 'import':
                resource = needed(s, Import, op.resource_id)
                resource.parsed, resource.state = result, 'ready'
                s.add_all([ImportRow(import_id=resource.id, row_id=row['id'], ordinal=row['ordinal']) for row in result['rows']])
            elif op.kind == 'reconcile':
                run = needed(s, Run, op.resource_id)
                run.result, run.state = result, 'completed'
                expected = set()
                actual = set()
                for side, source_id in [('left', run.left_id), ('right', run.right_id)]:
                    expected.update((side, r['id']) for r in needed(s, Import, source_id).parsed['rows'])
                for group in result['groups']:
                    for side, source_id in [('left', run.left_id), ('right', run.right_id)]:
                        for row_id in group[side + '_rows']:
                            if (side, row_id) in actual:
                                raise Problem('COVERAGE_INVARIANT', '源行被重复计入结果。', 500)
                            actual.add((side, row_id))
                            s.add(Coverage(run_id=run.id, side=side, row_id=row_id, import_id=source_id, group_id=group['id']))
                if actual != expected:
                    raise Problem('COVERAGE_INVARIANT', '结果没有完整覆盖源行。', 500)
            elif op.kind == 'export':
                resource = Export(id=new_id(), run_id=op.request_snapshot['run_id'], operation_id=op.id,
                                  content=result, sha256=hashlib.sha256(result).hexdigest())
                s.add(resource)
                op.resource_id, op.resource_kind = resource.id, 'export'
            else:
                resource = needed(s, Suggestion, op.resource_id)
                resource.proposal, resource.metadata_json = result
                resource.state, resource.billing_state = 'completed', 'response_received'
            op.state, op.finished_at, op.retained, op.reserved = 'completed', now(), size, 0
            self.recount(s, quota)
            self.check_quota(quota, 0)

    def fail(self, ident, code, message):
        if not self.healthy:
            return
        try:
            with self.Session.begin() as s:
                quota = self.fence(s)
                op = s.get(Operation, ident, with_for_update=True)
                if op.state not in ACTIVE:
                    return
                op.state, op.error_code, op.error_message = 'failed', code[:64], message[:500]
                op.finished_at, op.reserved = now(), 0
                self.mark_resource(s, op, 'failed')
                self.recount(s, quota)
        except DBAPIError:
            self.healthy = False
