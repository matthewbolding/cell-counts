"""
notifications.py — opt-in processing emails (DESIGN.md, "Notifications").

All three are off by default and controlled per user in Settings:

- failures: one email per sweep listing the images of yours that failed in a
  project, so a folder where everything fails produces one email, not hundreds;
- batch finished: once your last queued image in a project is done, a summary
  of what finished and what failed since the previous one;
- daily summary: once a day (after DAILY_SUMMARY_HOUR, local time), if anything
  of yours ran since the last one.

The CPU worker calls send_pending() about once a minute and
send_daily_summaries() from housekeeping. Jobs are marked as covered whether or
not an email went out, so turning a preference on later doesn't unleash a
backlog of old news.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from allauth.account.adapter import get_adapter
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import NotificationPreferences
from processing.models import ACTIVE_STATUSES, Job, JobKind, JobStatus

log = logging.getLogger(__name__)

IMAGE_KINDS = (JobKind.PREPARE, JobKind.SEGMENT)
DAILY_SUMMARY_HOUR = 8
MAX_LISTED = 50  # longer lists are truncated with "…and N more"


def _prefs(user_id) -> NotificationPreferences | None:
    return NotificationPreferences.objects.filter(user_id=user_id, user__is_active=True).first()


def _project_url(project) -> str:
    return settings.SITE_URL.rstrip("/") + reverse("projects:workspace", kwargs={"project_id": project.id})


def _settings_url() -> str:
    return settings.SITE_URL.rstrip("/") + reverse("accounts:settings")


def _send(template: str, user, **context) -> None:
    get_adapter().send_mail(f"processing/email/{template}", user.email,
                            {"user": user, "settings_url": _settings_url(), **context})


def _failure_line(job: Job) -> dict:
    image = job.image
    return {
        "filename": image.filename if image else "(deleted image)",
        "message": (image.error if image and image.error else "Processing failed."),
    }


def _summarize(jobs) -> dict:
    segmented = [j for j in jobs if j.kind == JobKind.SEGMENT and j.status == JobStatus.DONE]
    imported = [j for j in jobs if j.kind == JobKind.PREPARE and j.status == JobStatus.DONE
                and j.params.get("imported")]
    failed = [j for j in jobs if j.status == JobStatus.ERROR]
    return {
        "n_segmented": len(segmented),
        "n_imported": len(imported),
        "n_failed": len(failed),
        "failures": [_failure_line(j) for j in failed[:MAX_LISTED]],
        "more_failures": max(0, len(failed) - MAX_LISTED),
    }


# --------------------------------------------------------------------------- #
def send_failure_emails() -> int:
    pending = (Job.objects.filter(kind__in=IMAGE_KINDS, status=JobStatus.ERROR, failure_notified=False)
               .select_related("user", "project", "image").order_by("finished_at"))
    groups: dict[tuple, list[Job]] = defaultdict(list)
    for job in pending:
        groups[(job.user_id, job.project_id)].append(job)

    sent = 0
    for (user_id, _project_id), jobs in groups.items():
        prefs = _prefs(user_id)
        project = jobs[0].project
        if prefs and prefs.job_failures and project is not None:
            _send("failures", jobs[0].user, project=project, project_url=_project_url(project),
                  failures=[_failure_line(j) for j in jobs[:MAX_LISTED]],
                  more_failures=max(0, len(jobs) - MAX_LISTED), n_failed=len(jobs))
            sent += 1
        Job.objects.filter(pk__in=[j.pk for j in jobs]).update(failure_notified=True)
    return sent


def send_batch_emails() -> int:
    finished = (Job.objects.filter(kind__in=IMAGE_KINDS, notified=False)
                .exclude(status__in=ACTIVE_STATUSES)
                .select_related("user", "project", "image"))
    groups: dict[tuple, list[Job]] = defaultdict(list)
    for job in finished:
        groups[(job.user_id, job.project_id)].append(job)

    sent = 0
    for (user_id, project_id), jobs in groups.items():
        if project_id is not None and Job.objects.filter(
                user_id=user_id, project_id=project_id, kind__in=IMAGE_KINDS,
                status__in=ACTIVE_STATUSES).exists():
            continue  # this batch is still running
        summary = _summarize(jobs)
        prefs = _prefs(user_id)
        project = jobs[0].project
        if (prefs and prefs.batch_finished and project is not None
                and (summary["n_segmented"] or summary["n_imported"] or summary["n_failed"])):
            _send("batch_finished", jobs[0].user, project=project, project_url=_project_url(project), **summary)
            sent += 1
        Job.objects.filter(pk__in=[j.pk for j in jobs]).update(notified=True)
    return sent


def send_pending() -> None:
    send_failure_emails()
    send_batch_emails()


def send_daily_summaries(now=None) -> int:
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    if local_now.hour < DAILY_SUMMARY_HOUR:
        return 0
    sent = 0
    for prefs in NotificationPreferences.objects.filter(daily_summary=True, user__is_active=True).select_related(
            "user"):
        last = prefs.last_daily_summary_at
        if last is not None and timezone.localtime(last).date() >= local_now.date():
            continue
        since = last or now - timedelta(hours=24)
        jobs = list(Job.objects.filter(user=prefs.user, kind__in=IMAGE_KINDS,
                                       status__in=(JobStatus.DONE, JobStatus.ERROR),
                                       finished_at__gt=since, finished_at__lte=now)
                    .select_related("project", "image"))
        by_project: dict = defaultdict(list)
        for job in jobs:
            if job.project is not None:
                by_project[job.project].append(job)
        projects = [{"project": p, "url": _project_url(p), **_summarize(js)}
                    for p, js in sorted(by_project.items(), key=lambda kv: kv[0].name)]
        projects = [p for p in projects if p["n_segmented"] or p["n_imported"] or p["n_failed"]]
        if not projects:
            continue
        _send("daily_summary", prefs.user, projects=projects, since=since)
        prefs.last_daily_summary_at = now
        prefs.save(update_fields=["last_daily_summary_at"])
        sent += 1
    return sent
