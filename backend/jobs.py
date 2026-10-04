"""Durable jobs, serialized across API processes and workers by Postgres."""

from datetime import datetime, timedelta, timezone
from sqlalchemy import select, text
from fastapi import HTTPException
from models import IngestJob, CasUpload
import auth

CONTROL_LOCK = 74102002
WORKER_LOCK = 74102003


def active_job(session):
    return session.scalar(
        select(IngestJob)
        .where(IngestJob.status.in_(["queued", "processing"]))
        .order_by(IngestJob.job_id.desc())
        .limit(1)
    )


def enqueue(session, kind, payload=None):
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CONTROL_LOCK})
    if active_job(session):
        raise HTTPException(
            409,
            "An import or refresh is running. Follow its progress before starting another.",
        )
    job = IngestJob(
        kind=kind,
        status="queued",
        payload=auth.seal(payload) if payload is not None else None,
        dataset_id=session.scalar(select(CasUpload.upload_id).limit(1)),
        debug_stage="queued",
    )
    session.add(job)
    session.commit()
    return job


def public(job):
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status,
        "stage": job.debug_stage,
        "ready": job.ready,
        "cancel_requested": job.cancel_requested,
        "dataset_id": job.dataset_id,
        "attempts": job.attempts,
        "message": job.error_detail,
        "heartbeat_at": job.heartbeat_at.isoformat() + "Z"
        if job.heartbeat_at
        else None,
        **(job.result_json or {}),
    }


def assert_owner(session, job_id, attempt=None):
    job = session.scalar(
        select(IngestJob)
        .where(IngestJob.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not job or job.cancel_requested or job.status not in {"queued", "processing"}:
        raise RuntimeError("Job was cancelled or has already finished")
    if attempt is not None and job.attempts != attempt:
        raise RuntimeError("A newer worker has resumed this job")
    return job


def set_stage(session, job_id, stage, *, attempt=None, **fields):
    job = assert_owner(session, job_id, attempt)
    job.debug_stage = stage
    job.heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for key, value in fields.items():
        setattr(job, key, value)
    session.commit()
