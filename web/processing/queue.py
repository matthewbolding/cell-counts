"""
queue.py — the job queue: enqueueing, the GPU scheduling policy, claiming work,
and the read-side views of the queue (who's running, who's next, how long).

Scheduling policy for the GPU (DESIGN.md, "GPU scheduling"):

1. Nothing starts while an admin has the GPU paused.
2. Rescans first: they're small and someone is waiting at the screen.
3. Then users take turns: among users with queued, unpaused bulk jobs, the one
   the GPU served least recently goes next. Within a user, lowest `position`
   wins, which is what the Queue panel's ▲/▼ buttons rewrite.

The CPU worker is plain FIFO by priority then age.

Workers claim jobs with SELECT … FOR UPDATE SKIP LOCKED inside a transaction, so
running a second worker of the same kind can never double-claim a job (even
though production runs exactly one of each).
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from processing.models import (
    ACTIVE_STATUSES,
    KIND_RESOURCE,
    PRIORITY_BULK,
    PRIORITY_INTERACTIVE,
    Job,
    JobKind,
    JobStatus,
    QueueState,
    Resource,
    UserQueueState,
)

MAX_ATTEMPTS = 2
ETA_SAMPLE_SIZE = 50
DEFAULT_SECONDS_PER_MEGAPIXEL = 1.5  # until there's history to measure from


# --------------------------------------------------------------------------- #
# Enqueueing
# --------------------------------------------------------------------------- #
def enqueue(kind: str, user, *, project=None, image=None, sweep=None, export=None,
            params: dict | None = None) -> Job:
    priority = PRIORITY_INTERACTIVE if kind == JobKind.RESCAN else PRIORITY_BULK
    return Job.objects.create(
        kind=kind, resource=KIND_RESOURCE[kind], priority=priority, user=user,
        project=project or (image.project if image else None), image=image, sweep=sweep,
        export=export, params=params or {}, megapixels=image.megapixels if image else 0.0)


def enqueue_segmentation(image, user, params: dict | None = None) -> Job:
    from projects.models import ImageStatus

    image.status = ImageStatus.QUEUED
    image.error = ""
    image.save(update_fields=["status", "error", "updated_at"])
    return enqueue(JobKind.SEGMENT, user, image=image, params=params)


def cancel_jobs(jobs, reason: str = "Cancelled") -> int:
    """Cancel queued jobs only; a running job always finishes (Stop Segmenting
    promises that)."""
    return jobs.filter(status=JobStatus.QUEUED).update(
        status=JobStatus.CANCELLED, error=reason, finished_at=timezone.now())


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #
def _claim(job: Job) -> Job:
    job.status = JobStatus.RUNNING
    job.started_at = timezone.now()
    job.attempts += 1
    job.save(update_fields=["status", "started_at", "attempts"])
    return job


@transaction.atomic
def claim_next(resource: str) -> Job | None:
    queued = Job.objects.select_for_update(skip_locked=True).filter(resource=resource, status=JobStatus.QUEUED)

    if resource == Resource.CPU:
        job = queued.order_by("priority", "created_at").first()
        return _claim(job) if job else None

    if QueueState.get().gpu_paused:
        return None

    rescan = queued.filter(priority=PRIORITY_INTERACTIVE).order_by("created_at").first()
    if rescan:
        return _claim(rescan)

    paused_users = list(UserQueueState.objects.filter(segmenting_paused=True).values_list("user_id", flat=True))
    # Who's waiting is read without locks (Postgres can't combine DISTINCT with
    # FOR UPDATE); only the job actually claimed is locked.
    user_ids = list(Job.objects.filter(resource=resource, status=JobStatus.QUEUED, priority=PRIORITY_BULK)
                    .exclude(user_id__in=paused_users).values_list("user_id", flat=True).distinct())
    if not user_ids:
        return None
    user_id = _next_user_in_turn(user_ids)
    job = (queued.filter(priority=PRIORITY_BULK, user_id=user_id)
           .order_by("position", "created_at").first())
    if job is None:
        return None
    UserQueueState.objects.update_or_create(user_id=user_id, defaults={"last_served_at": timezone.now()})
    return _claim(job)


def _next_user_in_turn(user_ids: list[int]) -> int:
    served = dict(UserQueueState.objects.filter(user_id__in=user_ids).values_list("user_id", "last_served_at"))
    # Never-served users go first; ties broken by user id so the order is stable.
    return min(user_ids, key=lambda uid: (served.get(uid) is not None, served.get(uid) or 0, uid))


def finish(job: Job, result: dict | None = None) -> None:
    job.status = JobStatus.DONE
    job.result = result
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "result", "finished_at"])


def cancel_running(job: Job, reason: str) -> None:
    """A running job whose subject disappeared mid-run: not a failure, just moot."""
    job.status = JobStatus.CANCELLED
    job.error = reason
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "finished_at"])


def fail(job: Job, error: str) -> None:
    job.status = JobStatus.ERROR
    job.error = error[:4000]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "finished_at"])


def recover_interrupted(resource: str) -> tuple[int, int]:
    """Called when a worker starts: anything still marked running for its
    resource was interrupted by a restart. Retry it once, then give up on it.
    Returns (requeued, failed)."""
    stale = Job.objects.filter(resource=resource, status=JobStatus.RUNNING)
    requeued = stale.filter(attempts__lt=MAX_ATTEMPTS).update(status=JobStatus.QUEUED, started_at=None)
    failed = stale.update(
        status=JobStatus.ERROR, error="Interrupted by a server restart.", finished_at=timezone.now())
    return requeued, failed


# --------------------------------------------------------------------------- #
# User controls
# --------------------------------------------------------------------------- #
@transaction.atomic
def reorder(user, job_ids: list[str]) -> int:
    """Put the given queued jobs (all the caller's own) into the given order,
    reusing the positions they already hold between them — jobs not named keep
    their places. Unknown/foreign/non-queued ids are ignored (a best-effort
    reorder). Returns how many jobs moved."""
    jobs = {str(j.id): j for j in Job.objects.select_for_update().filter(
        id__in=job_ids, user=user, status=JobStatus.QUEUED, priority=PRIORITY_BULK)}
    ordered = [jobs[jid] for jid in dict.fromkeys(str(j) for j in job_ids) if jid in jobs]
    positions = sorted(j.position for j in ordered)
    for job, position in zip(ordered, positions, strict=True):
        job.position = position
    Job.objects.bulk_update(ordered, ["position"])
    return len(ordered)


def set_user_paused(user, paused: bool) -> None:
    UserQueueState.objects.update_or_create(user=user, defaults={"segmenting_paused": paused})


def set_gpu_paused(paused: bool, by) -> None:
    state = QueueState.get()
    state.gpu_paused = paused
    state.paused_by = by if paused else None
    state.save(update_fields=["gpu_paused", "paused_by", "updated_at"])


# --------------------------------------------------------------------------- #
# Read side: order preview and ETAs
# --------------------------------------------------------------------------- #
def seconds_per_megapixel() -> float:
    recent = (Job.objects.filter(kind=JobKind.SEGMENT, status=JobStatus.DONE, megapixels__gt=0,
                                 started_at__isnull=False, finished_at__isnull=False)
              .order_by("-finished_at")[:ETA_SAMPLE_SIZE])
    rates = [j.duration_seconds / j.megapixels for j in recent if j.duration_seconds]
    return statistics.median(rates) if rates else DEFAULT_SECONDS_PER_MEGAPIXEL


def _estimate(job: Job, rate: float) -> float:
    if job.kind == JobKind.RESCAN:
        return max(5.0, job.params.get("megapixels", 0.25) * rate)
    return max(5.0, job.megapixels * rate)


@dataclass
class QueueSlot:
    job: Job
    ahead: int | None          # GPU jobs that will run before this one; None while its owner is paused
    eta_seconds: float | None  # until this job *starts*


def gpu_order() -> tuple[Job | None, list[QueueSlot]]:
    """Simulate the scheduler over everything queued for the GPU, so each job can
    show how many jobs are ahead of it and roughly when it'll start. The
    simulation applies the same rules as claim_next(): interactive first, then
    users in turn (least recently served first), each user's jobs by position."""
    running = (Job.objects.filter(resource=Resource.GPU, status=JobStatus.RUNNING)
               .select_related("user", "image__sample", "project").first())
    queued = list(Job.objects.filter(resource=Resource.GPU, status=JobStatus.QUEUED)
                  .select_related("user", "image__sample", "project").order_by("position", "created_at"))
    rate = seconds_per_megapixel()
    paused_users = set(UserQueueState.objects.filter(segmenting_paused=True).values_list("user_id", flat=True))
    global_pause = QueueState.get().gpu_paused

    clock = 0.0
    if running and running.started_at:
        elapsed = (timezone.now() - running.started_at).total_seconds()
        clock = max(0.0, _estimate(running, rate) - elapsed)

    slots: list[QueueSlot] = []
    interactive = sorted((j for j in queued if j.priority == PRIORITY_INTERACTIVE), key=lambda j: j.created_at)
    per_user: dict[int, deque[Job]] = defaultdict(deque)
    for j in queued:
        if j.priority == PRIORITY_BULK and j.user_id not in paused_users:
            per_user[j.user_id].append(j)

    served = dict(UserQueueState.objects.filter(user_id__in=list(per_user)).values_list(
        "user_id", "last_served_at"))
    turn = sorted(per_user, key=lambda uid: (served.get(uid) is not None, served.get(uid) or 0, uid))

    order: list[Job] = list(interactive)
    while turn:
        for uid in list(turn):
            order.append(per_user[uid].popleft())
            if not per_user[uid]:
                turn.remove(uid)

    for ahead, job in enumerate(order):
        slots.append(QueueSlot(job=job, ahead=ahead, eta_seconds=None if global_pause else clock))
        clock += _estimate(job, rate)
    for job in queued:
        if job.priority == PRIORITY_BULK and job.user_id in paused_users:
            slots.append(QueueSlot(job=job, ahead=None, eta_seconds=None))
    return running, slots


def job_row(job: Job, viewer, slot: QueueSlot | None = None) -> dict:
    image = job.image
    mine = job.user_id == viewer.id
    return {
        "job_id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "mine": mine,
        "owner": job.user.label,
        "image_id": str(image.id) if image else None,
        "filename": image.filename if image else None,
        "prefix": image.sample.prefix if image else None,
        "channel": image.channel if image else None,
        "project_id": str(job.project_id) if job.project_id else None,
        # Other people's project names are theirs to share; admins see everything.
        "project": job.project.name if job.project_id and (mine or viewer.is_staff) else None,
        "position": job.position,
        "ahead": slot.ahead if slot else None,
        "eta_seconds": round(slot.eta_seconds) if slot and slot.eta_seconds is not None else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
    }


def project_queue_rows(project, viewer) -> list[dict]:
    """The Queue panel's server-side rows for one project, in processing order:
    preparing (CPU) first, then whatever's on the GPU now, then GPU-queued jobs
    in the order they'll run. Includes collaborators' jobs (flagged mine=False),
    since they share the project's samples."""
    running, slots = gpu_order()
    rows = []
    for job in (Job.objects.filter(project=project, kind=JobKind.PREPARE, status__in=ACTIVE_STATUSES)
                .select_related("user", "image__sample", "project").order_by("created_at")):
        rows.append(job_row(job, viewer))
    if running and running.project_id == project.id and running.kind == JobKind.SEGMENT:
        rows.append(job_row(running, viewer))
    for slot in slots:
        if slot.job.project_id == project.id and slot.job.kind == JobKind.SEGMENT:
            rows.append(job_row(slot.job, viewer, slot))
    return rows


def overview(viewer) -> dict:
    """Everything the GPU Queue page and the workspace's queue summary need."""
    running, slots = gpu_order()
    state = QueueState.get()
    users: dict[int, dict] = {}
    for slot in slots:
        u = slot.job.user
        entry = users.setdefault(u.id, {"user": u.label, "mine": u.id == viewer.id, "queued": 0,
                                        "paused": slot.ahead is None, "next_eta_seconds": None})
        entry["queued"] += 1
        if slot.eta_seconds is not None and entry["next_eta_seconds"] is None:
            entry["next_eta_seconds"] = round(slot.eta_seconds)
    my_state = UserQueueState.for_user(viewer)
    visible = [s for s in slots if viewer.is_staff or s.job.user_id == viewer.id]
    return {
        "gpu_paused": state.gpu_paused,
        "segmenting_paused": my_state.segmenting_paused,
        "seconds_per_megapixel": round(seconds_per_megapixel(), 3),
        "running": job_row(running, viewer) if running else None,
        "users": sorted(users.values(), key=lambda e: (not e["mine"], e["user"])),
        "jobs": [job_row(s.job, viewer, s) for s in visible],
        "total_queued": len(slots),
    }
