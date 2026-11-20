"""Explicit versioned job dispatch shared by admission checks and workers."""
from api.common import Problem
from . import datasets, reconciliations, jobs
from .job_models import Job


def handler(kind):
    if kind == 'import':
        return datasets
    if kind == 'reconcile':
        return reconciliations
    raise Problem('UNSUPPORTED_JOB_CONTRACT', '该执行器不支持此任务契约。', 403)


def authorize_inputs(db, workspace_id, actor_id, kind, snapshot):
    return handler(kind).authorize_inputs(db, workspace_id, actor_id, kind, snapshot)


def input_for_job(db, job):
    return handler(job.kind).input_for_job(db, job)


def publish(db, job_id, token, result):
    job = db.get(Job, job_id)
    if job is None:
        raise Problem('NOT_FOUND', '任务不存在。', 404)
    return handler(job.kind).publish(db, job_id, token, result)


def fail(db, job_id, token, code, details=None):
    job = db.get(Job, job_id)
    if job is None:
        raise Problem('NOT_FOUND', '任务不存在。', 404)
    if job.kind == 'import':
        return datasets.fail_import(db, job_id, token, code, details)
    return jobs.fail(db, job_id, token, code, authorize_inputs=authorize_inputs)
